"""Generating and deriving blueprints. Three jobs, none of them an agent.

`generate` turns a prompt into a plan. `describe` says what the parts of an
existing object are. `suggest_features` groups an app's objects into
capabilities. Each is one model call with a schema and no tools.

**Why none of these runs through the AppBuilder agent.** That agent spends
roughly 44K tokens of fixed prefix before the first word of the request, against
a 112K limit, because it carries sixty tool schemas and a component catalogue.
Planning needs none of that. Routing it through the agent would make the
cheapest step in the pipeline the most expensive one, and would put a tool-use
loop around a call that has nothing to call.

**Derivation is explicit, never automatic.** The board renders its titles from
definitions for free; a description costs tokens and the customer is metered, so
nothing here runs on open. It runs when somebody presses "Explain this" on one
card, or "Explain the site" for a sweep. That keeps the first open instant and
means no surprise charge. `AI_TOKENS_PER_MILLION` is 0 on SiteZump's billing
config today, and that will not stay true.

**Describe is per object, never "read the whole app".** One call covering forty
pages gets a dozen of them subtly wrong in a way nobody reads closely enough to
catch, and the wrong ones look exactly like the right ones.

The hardening below is not defensive padding: every piece of it is a bug that
`lore/curator.py` shipped with and that cost three weeks of producing nothing
while reporting success.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.config import settings
from app.services.blueprint import prompts
from app.services.blueprint.validate import (
    UID_PATTERN,
    ValidationIssue,
    coerce_lists,
    mint_uid,
    validate_blueprint,
)
from app.services.billing import OUT_OF_TOKENS
from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)

#: Hard ceiling on one blueprint model call. The provider clients set no timeout
#: of their own, and a request that hangs would otherwise hold a web worker
#: open until the client gives up.
DEFAULT_TIMEOUT_SECONDS = 180

DEFAULT_TIER = "balanced"
DEFAULT_MAX_TOKENS = 16000

#: How much of one object's definition is shown to `describe`. A page can carry
#: nine hundred components; the shape is what answers "what is this section".
DESCRIBE_MAX_SECTIONS = 60


class BlueprintGenerationError(Exception):
    """The model produced nothing usable. Says which of the ways it failed."""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


# ── The model call ───────────────────────────────────────────────────────


def _tier() -> str:
    return getattr(settings, "BLUEPRINT_TIER", DEFAULT_TIER) or DEFAULT_TIER


def _max_tokens() -> int:
    return int(getattr(settings, "BLUEPRINT_MAX_TOKENS", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS)


def _timeout() -> float:
    return float(
        getattr(settings, "BLUEPRINT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        or DEFAULT_TIMEOUT_SECONDS
    )


def parse_json_object(raw: str) -> tuple[dict[str, Any] | None, str]:
    """(object, reason). Reason is "" on success.

    The reason is the whole point. A bare `None` covers four different
    situations — nothing came back, prose came back, malformed JSON came back,
    and a valid empty object came back — and only the last is a normal outcome.
    Collapsing them is what made the lore curator's silence unreadable for
    weeks: every failure logged as "the model had nothing to say".
    """
    if not raw or not raw.strip():
        return None, "empty-response"

    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()

    try:
        parsed = json.loads(text)
        return (parsed, "") if isinstance(parsed, dict) else (None, "not-an-object")
    except ValueError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None, "no-json"
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        return None, "json-error"
    return (parsed, "") if isinstance(parsed, dict) else (None, "not-an-object")


async def _complete_json(
    *, system_prompt: str, user_message: str, label: str, meter: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One JSON-returning model call, with the retry that actually matters.

    Returns (object, diagnostics). Raises BlueprintGenerationError when two
    attempts produced nothing parseable.

    The `stop_reason == "length"` with empty content branch is the failure this
    exists for. A reasoning model can spend its entire output budget thinking
    and emit no content at all, and every symptom of that looks like "the model
    decided there was nothing to say". Doubling the budget fixes it; nothing
    else does, and without the branch nobody ever finds out.

    `meter` is a `billing.CallMeter` and every real caller passes one. This
    talks to a provider directly rather than through the agent loop, so nothing
    else in the path meters it: without a meter a sweep runs forty model calls
    against a suspended wallet, is billed for none of them, and disagrees with
    the chat on the same screen about whether the customer has any money.

    BOTH ATTEMPTS ARE CHARGED. A retry is a second real call to a real provider
    and the tokens are spent whether or not the answer parsed. Charging only
    the successful one would make the malformed-JSON path free, which is exactly
    the path a cheap model takes most often.
    """
    provider = get_llm_provider()
    max_tokens = _max_tokens()
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    diagnostics: dict[str, Any] = {"label": label, "attempts": 0}

    parsed: dict[str, Any] | None = None
    reason = ""

    for attempt in (1, 2):
        diagnostics["attempts"] = attempt
        # Before the call, and before the RETRY as well: a wallet that emptied
        # on the first attempt must not fund the second.
        if meter is not None and not await meter.allowed():
            raise BlueprintGenerationError(OUT_OF_TOKENS, reason="out-of-tokens")
        try:
            response = await asyncio.wait_for(
                provider.create_completion(
                    system_prompt=system_prompt,
                    messages=messages,
                    model_tier=_tier(),
                    max_tokens=max_tokens,
                ),
                timeout=_timeout(),
            )
        except asyncio.TimeoutError as exc:
            logger.warning("blueprint %s: timed out after %.0fs", label, _timeout())
            raise BlueprintGenerationError(
                f"The model did not answer within {_timeout():.0f} seconds.",
                reason="timeout",
            ) from exc

        # Charged as soon as it lands, before anything can decide the answer was
        # unusable. A call that produced garbage still consumed the tokens.
        if meter is not None:
            await meter.charge(response)

        raw = (response or {}).get("content") or ""
        reasoning = (response or {}).get("reasoning_content") or ""
        stop = str((response or {}).get("stop_reason") or "")
        diagnostics.update({
            "response_chars": len(raw),
            "reasoning_chars": len(reasoning),
            "stop_reason": stop[:32],
            "model": str((response or {}).get("model") or "")[:64],
        })

        parsed, reason = parse_json_object(raw)
        if parsed is not None or attempt == 2:
            break

        if stop == "length" and not raw:
            logger.warning(
                "blueprint %s: model emitted no content — %d reasoning chars, "
                "stop=length. Retrying with double the budget.",
                label, len(reasoning),
            )
            max_tokens = min(max_tokens * 2, 32000)
        else:
            logger.warning("blueprint %s: unusable response (%s). Retrying once.", label, reason)
            messages = messages + [
                {"role": "assistant", "content": raw[:4000]},
                {"role": "user", "content":
                    "That was not valid JSON. Return only the JSON object, with no "
                    "prose and no code fence."},
            ]

    if parsed is None:
        raise BlueprintGenerationError(
            f"The model produced nothing usable ({reason}).", reason=reason,
        )
    return parsed, diagnostics


# ── generate ─────────────────────────────────────────────────────────────


async def generate(
    *,
    prompt: str,
    kind: str = "application",
    app_code: str = "",
    context: dict[str, Any] | None = None,
    existing: dict[str, Any] | None = None,
    meter: Any = None,
) -> dict[str, Any]:
    """Turn a prompt into a plan for one object.

    `existing` is the current plan when there is one. Passing it makes this a
    REFINEMENT: the model is told to keep what still holds rather than start
    again, which is what a person means when they ask for a change to something
    they can already see.

    The result is coerced and then validated. Coercion is for arrays, which a
    model produces however firmly it is told not to and which convert
    mechanically. Validation is the gate, and a plan that still fails it is
    returned WITH its issues rather than written: a caller that cannot see why
    something was refused will retry the same thing.
    """
    if not (prompt or "").strip():
        raise BlueprintGenerationError("A prompt is required.", reason="no-prompt")

    user_message = _generate_user_message(prompt, kind, app_code, context, existing)
    parsed, diagnostics = await _complete_json(
        system_prompt=prompts.generate_system_prompt(kind),
        user_message=user_message,
        label=f"generate:{kind}",
        meter=meter,
    )

    blueprint = coerce_lists(parsed)
    blueprint = _stamp_origin(blueprint, prompt)
    issues = validate_blueprint(blueprint)

    return {
        "blueprint": blueprint,
        "kind": kind,
        "app_code": app_code,
        "valid": not issues,
        "issues": [{"path": i.path, "message": i.message} for i in issues],
        "diagnostics": diagnostics,
    }


def _generate_user_message(
    prompt: str,
    kind: str,
    app_code: str,
    context: dict[str, Any] | None,
    existing: dict[str, Any] | None,
) -> str:
    parts = [f"Object kind: {kind}"]
    if app_code:
        parts.append(f"Application: {app_code}")
    if context:
        parts.append(
            "What already exists in this application (do not re-invent any of it):\n"
            + json.dumps(context, indent=2)[:12000]
        )
    if existing:
        parts.append(
            "The CURRENT plan. This is a refinement, not a fresh start: keep every "
            "entry that still holds, keep its uid so history survives, and change "
            "only what the request asks for.\n"
            + json.dumps(existing, indent=2)[:20000]
        )
    else:
        parts.append("There is no plan yet. Create one.")
    parts.append("Request:\n" + prompt.strip())
    return "\n\n".join(parts)


def _stamp_origin(blueprint: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Record what produced this plan, without overwriting a stated origin.

    Cheap, and it is the difference between a plan somebody can audit and one
    that simply appeared.
    """
    from datetime import datetime, timezone

    origin = blueprint.get("origin")
    if not isinstance(origin, dict):
        origin = {}
    origin.setdefault("prompt", prompt.strip()[:2000])
    origin.setdefault("generatedBy", "blueprint-service")
    origin.setdefault("generatedAt", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    blueprint["origin"] = origin
    blueprint.setdefault("schemaVersion", 1)
    return blueprint


# ── describe ─────────────────────────────────────────────────────────────


def summarise_definition(document: dict[str, Any], kind: str) -> dict[str, str]:
    """{key: "what the definition says it is"} for the parts worth describing.

    For a page that is the root component's direct children, which is exactly
    what the board draws as cards. Sending the whole component map instead would
    put nine hundred components into a prompt that needs about twelve, for a
    result no better.
    """
    reader = _PART_READERS.get(kind)
    return reader(document) if reader else _whole_object(document, kind)


def _storage_fields(document: dict[str, Any]) -> dict[str, str]:
    schema = document.get("schema") or {}
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    return {
        name: str((spec or {}).get("type") or "field")
        for name, spec in list((properties or {}).items())[:DESCRIBE_MAX_SECTIONS]
        if isinstance(name, str)
    }


def _look(document: dict[str, Any], kind: str) -> dict[str, str]:
    """A theme's brand variables: the typeface, the palette, and nothing else.

    A theme carries a couple of hundred variables and all but a few dozen are
    per-component tokens — `textBoxBorderRadiusDefaultTertiary` and two hundred
    relatives — which say how one widget is drawn, not what the site looks like.
    Describing all of them is two hundred cards each restating its own name.

    Describing NONE of them was the other mistake, and the one that was actually
    shipped: a theme came back as one card reading "the theme called Crumb",
    which answers nothing. Somebody looking at a plan wants to know what the
    site is set in.
    """
    from app.services.blueprint.compose import brand_variables

    found = brand_variables(document)
    if found:
        return found
    name = str(document.get("name") or "").strip()
    title = str(document.get("title") or "").strip()
    return {name or kind: f"the {kind}" + (f" called {title}" if title else "")}


def _whole_object(document: dict[str, Any], kind: str) -> dict[str, str]:
    """One part, which is the object itself.

    The fallback for a kind with nothing worth picking apart. Keyed by the
    object's OWN name rather than a placeholder, so the card it produces is
    titled "Crumb" on the board instead of "object".
    """
    name = str(document.get("name") or "").strip()
    title = str(document.get("title") or "").strip()
    return {name or kind: f"the {kind}" + (f" called {title}" if title else "")}


#: How each kind's parts are read off its definition. A dispatch table rather
#: than a chain of ifs, because the list grows with every kind the board learns
#: to draw and a chain grows a branch each time.
_PART_READERS: dict[str, Any] = {
    # A lambda rather than the function itself, because the dict is built at
    # import and `_page_sections` is defined below it.
    "page": lambda d: _page_sections(d),
    "storage": _storage_fields,
    "function": lambda d: _function_steps(d.get("definition")),
    "uifunction": lambda d: _function_steps(d.get("definition")),
    "uripath": lambda d: _uripath_methods(d),
    "template": lambda d: _named_parts(d.get("templateParts"), "template part"),
    "notification": lambda d: _notification_channels(d),
    "theme": lambda d: _look(d, "theme"),
    "style": lambda d: _look(d, "style"),
}


def _function_steps(definition: Any) -> dict[str, str]:
    """{statementName: "what it calls, and on what"} for one KIRun definition.

    Keyed by statement name because that is what a plan entry points back at and
    what survives a step being moved, where a position or an index does not.

    The literal arguments are part of the line, and they are most of its value.
    `calls CoreServices.Storage.ReadPage` is true of a hundred steps in an app
    and distinguishes none of them; `calls CoreServices.Storage.ReadPage
    (storageName=Task)` says what the step is for. Only VALUE parameters are
    shown — an EXPRESSION is computed at run time and printing its text puts a
    guess in front of the model as though it were a fact.
    """
    if not isinstance(definition, dict):
        return {}
    steps = definition.get("steps")
    if not isinstance(steps, dict):
        return {}
    out: dict[str, str] = {}
    for key, step in list(steps.items())[:DESCRIBE_MAX_SECTIONS]:
        if not isinstance(step, dict):
            continue
        called = f"{step.get('namespace') or ''}.{step.get('name') or ''}".strip(".")
        arguments = _step_arguments(step.get("parameterMap"))
        line = f"calls {called}" if called else "a step"
        out[str(key)] = f"{line} ({arguments})" if arguments else line
    return out


#: Parameters worth putting in a description. Every KIRun primitive takes a
#: handful and most are plumbing — a `value`, an `eventName`, a position. These
#: are the ones that say what the step operates ON.
_TELLING_PARAMS = (
    "storagename", "url", "path", "name", "linkpath", "pagename", "filter",
    "eventname", "templatename", "to", "subject", "count", "size", "message",
)

#: How much of one argument is shown. A `filter` can be a page of JSON and the
#: shape of it, not the whole, is what identifies the step.
ARGUMENT_CHARS = 60


def _step_arguments(parameter_map: Any) -> str:
    """`storageName=Task, size=200` for one step's literal arguments."""
    if not isinstance(parameter_map, dict):
        return ""
    shown: list[str] = []
    for name, values in parameter_map.items():
        if not isinstance(name, str) or name.lower() not in _TELLING_PARAMS:
            continue
        if not isinstance(values, dict):
            continue
        for entry in values.values():
            if not isinstance(entry, dict) or entry.get("type") == "EXPRESSION":
                continue
            literal = entry.get("value")
            if literal in (None, "", {}, []):
                continue
            shown.append(f"{name}={str(literal)[:ARGUMENT_CHARS]}")
            break
        if len(shown) >= 4:
            break
    return ", ".join(shown)


def _uripath_methods(document: dict[str, Any]) -> dict[str, str]:
    """{METHOD: "what answers on it"} for one URI path.

    A URI path has no steps of its own. `pathDefinitions` — plural — maps an
    HTTP method to a handler whose `kiRunFxDefinition` NAMES the function that
    runs. So the parts of a URI path are its methods, and the useful thing to
    say about each is which function answers it.

    Reading the singular `pathDefinition`, which is not a field, is why every
    URI path on the board was one card carrying only its own path.
    """
    out: dict[str, str] = {}
    for method, handler in (document.get("pathDefinitions") or {}).items():
        if not isinstance(handler, dict):
            continue
        fx = handler.get("kiRunFxDefinition") or {}
        called = f"{fx.get('namespace') or ''}.{fx.get('name') or ''}".strip(".")
        public = str(handler.get("uriType") or "")
        line = f"answered by {called}" if called else "answered by nothing yet"
        out[str(method)] = f"{line} ({public})" if public else line
    return out


def _notification_channels(document: dict[str, Any]) -> dict[str, str]:
    """{channel: "the wording it sends"} for one notification.

    The field is `channelTemplates`, not `channelDetails`. Each channel — inapp,
    email, sms — usually carries its wording INLINE under `templateParts`, keyed
    by language, so the title is right there and is by far the most useful thing
    to show. Reading the wrong key gave every notification one card saying its
    own name back to it.
    """
    out: dict[str, str] = {}
    for channel, detail in (document.get("channelTemplates") or {}).items():
        if not isinstance(detail, dict):
            continue
        parts = detail.get("templateParts") or {}
        first = next((p for p in parts.values() if isinstance(p, dict)), {})
        title = str(first.get("title") or "").strip()
        body = str(first.get("description") or "").strip()
        summary = title or body[:80] or "no wording set"
        out[str(channel)] = f"{channel}: {summary[:120]}"
    return out


def _named_parts(parts: Any, what: str) -> dict[str, str]:
    """{key: "<what>"} for a map the platform keys by a human-readable name."""
    if not isinstance(parts, dict):
        return {}
    return {str(key): what for key in list(parts)[:DESCRIBE_MAX_SECTIONS]}


#: How deep into a section's subtree the words are gathered from. Two levels
#: below the section reaches a heading inside a card inside a row, which is
#: where the words on a real page actually are. Deeper adds icons and spacers.
SECTION_DEPTH = 3

#: Words shown per section. Enough to tell a hero from a pricing table, not
#: enough for one long section to crowd out the other eleven.
SECTION_TEXT_CHARS = 220


def _page_sections(document: dict[str, Any]) -> dict[str, str]:
    """{componentKey: "what this section is"} for a page's top-level sections.

    This used to say `Grid named 'heroGrid' with 4 direct children`, which is
    the shape of a section and tells you nothing about it. Every section on
    every page reads the same, so the describer had no way to tell a hero from a
    footer and wrote interchangeable sentences about both — and that, not the
    prompt, is why the board read as shallow.

    Three things go in now, and none of them costs a call:

      the WORDS in it     — a section that says "Order a box of pastries" is a
                            section anything can describe correctly.
      the WIDGETS in it   — a TextBox and a Button is a form, wherever it sits.
      what it RUNS        — an `onClick` naming an event function is the whole
                            difference between a heading and a working control.

    Still bounded: the words are gathered to a fixed depth and trimmed, because
    a page can carry nine hundred components and the section is the unit the
    board draws.
    """
    definition = document.get("componentDefinition") or {}
    root_key = document.get("rootComponent")
    root = definition.get(root_key) if root_key else None
    children = (root or {}).get("children") or {}

    ordered = []
    for key, on in children.items():
        if not on:
            continue
        component = definition.get(key) or {}
        ordered.append((component.get("displayOrder", 0), key, component))
    ordered.sort(key=lambda row: (row[0], row[1]))

    out: dict[str, str] = {}
    for _, key, component in ordered[:DESCRIBE_MAX_SECTIONS]:
        name = component.get("name") or key
        kind = component.get("type") or "component"
        words, widgets, runs = _section_contents(definition, key)
        line = f"{kind} '{name}'"
        if widgets:
            line += " containing " + ", ".join(widgets)
        if words:
            line += ' — reads: "' + words[:SECTION_TEXT_CHARS] + '"'
        if runs:
            line += " — runs " + ", ".join(sorted(runs)[:4])
        out[key] = line
    return out


#: Component types that are structure rather than content. Listing them as the
#: contents of a section says "a section contains a section", which is true of
#: everything and distinguishes nothing.
_STRUCTURAL = {"Grid", "SubPage", "TableGrid", "TableColumns", "ArrayRepeater"}


def _section_contents(
    definition: dict[str, Any], key: str,
) -> tuple[str, list[str], set[str]]:
    """(the words, the widget types, the event functions it runs) for a subtree."""
    words: list[str] = []
    widgets: dict[str, int] = {}
    runs: set[str] = set()

    def walk(node_key: str, depth: int) -> None:
        component = definition.get(node_key)
        if not isinstance(component, dict) or depth > SECTION_DEPTH:
            return
        kind = str(component.get("type") or "")
        properties = component.get("properties") or {}

        for field in ("text", "label", "placeholder", "title"):
            value = properties.get(field)
            literal = value.get("value") if isinstance(value, dict) else value
            if isinstance(literal, str) and literal.strip():
                words.append(literal.strip())

        for handler in ("onClick", "onChange", "onSubmit"):
            value = properties.get(handler)
            literal = value.get("value") if isinstance(value, dict) else value
            if isinstance(literal, str) and literal.strip():
                runs.add(literal.strip())

        if depth and kind and kind not in _STRUCTURAL:
            widgets[kind] = widgets.get(kind, 0) + 1

        for child_key, on in (component.get("children") or {}).items():
            if on:
                walk(child_key, depth + 1)

    walk(key, 0)
    listed = [
        f"{count} {kind}" if count > 1 else kind
        for kind, count in sorted(widgets.items(), key=lambda kv: -kv[1])[:5]
    ]
    return " / ".join(words)[:SECTION_TEXT_CHARS * 2], listed, runs


async def describe(
    *, document: dict[str, Any], kind: str, app_code: str = "",
    connections: str = "", meter: Any = None,
) -> dict[str, Any]:
    """One line per part of one object. Returns {"describes": {key: line}}.

    `describes` is derived and freely recomputed. It is never written over
    `purpose`, which is what a PERSON said the thing is for: a derivation that
    can overwrite a statement will eventually erase the only record of intent
    anybody wrote down.

    `connections` is what this object reaches and what reaches it, already
    derived from the definitions by `relations`. It is the single highest-value
    line in the prompt and it costs nothing: without it the describer sees a
    component tree and writes "a form with four fields" about a form whose whole
    purpose is the storage it fills, because from the tree alone that storage is
    invisible.
    """
    parts = summarise_definition(document, kind)
    if not parts:
        return {
            "describes": {}, "names": {}, "summary": "",
            "diagnostics": {"skipped": "nothing-to-describe"},
        }

    user_message = (
        f"Object kind: {kind}\n"
        f"Application: {app_code or '(unnamed)'}\n"
        f"Name: {document.get('name') or ''}\n"
        f"Title: {document.get('title') or ''}\n"
        + (f"\nHow it connects to the rest of the app: {connections}\n" if connections else "")
        + "\nIts parts, by key:\n"
        + json.dumps(parts, indent=2)[:12000]
        + "\n\nDescribe each one."
    )

    parsed, diagnostics = await _complete_json(
        system_prompt=prompts.DESCRIBE_SYSTEM_PROMPT,
        user_message=user_message,
        label=f"describe:{kind}",
        meter=meter,
    )

    described = parsed.get("describes")
    if not isinstance(described, dict):
        described = parsed if all(isinstance(v, str) for v in parsed.values()) else {}

    # Only keys we asked about. A model that invents a key would otherwise put a
    # description against a section that does not exist, which renders as a card
    # nobody can click.
    clean = {
        key: str(value).strip()
        for key, value in described.items()
        if key in parts and isinstance(value, str) and value.strip()
    }
    # The object seen from outside, which is what the app-level index carries so
    # a board of forty objects draws forty second lines without opening forty
    # documents. Same call, one extra line of output.
    summary = parsed.get("summary")
    # What a person would CALL each part. Only for keys we asked about, and
    # trimmed hard: this is a board title, and a title that wraps to three lines
    # is a description wearing a title's clothes.
    offered = parsed.get("names")
    names = {
        key: str(value).strip()[:60]
        for key, value in (offered or {}).items()
        if key in parts and isinstance(value, str) and value.strip()
    } if isinstance(offered, dict) else {}
    return {
        "describes": clean,
        "names": names,
        "summary": str(summary).strip()[:400] if isinstance(summary, str) else "",
        "diagnostics": {**diagnostics, "asked": len(parts)},
    }


# ── suggest_features ─────────────────────────────────────────────────────


async def suggest_features(
    *, objects: list[dict[str, Any]], app_code: str = "", meter: Any = None,
) -> dict[str, Any]:
    """Group an app's objects into named capabilities.

    Seventy per cent right beats one undifferentiated pile of forty rows, which
    is what the board shows without this. Every suggestion is a proposal: it is
    shown before it is saved, because a wrong grouping is easy to see and
    trivial to fix, and impossible to notice once it has been written silently.

    Returns `features` (flat, keyed) and `assignments` (object name -> feature
    uid). Membership is a POINTER and never nesting: `plan.objects.<uid>.feature`
    is the one place it is recorded, so moving an object between features is one
    changed value rather than a delete plus an add, which under the differ would
    orphan any tenant override of that object.
    """
    listed = [
        {"name": o.get("name"), "kind": o.get("kind"), "purpose": o.get("purpose")
         or o.get("description") or o.get("title") or ""}
        for o in objects if isinstance(o, dict) and o.get("name")
    ]
    if len(listed) < 2:
        return {"features": {}, "diagnostics": {"skipped": "too-few-objects"}}

    user_message = (
        f"Application: {app_code or '(unnamed)'}\n\n"
        "Its objects:\n" + json.dumps(listed, indent=2)[:16000]
        + "\n\nGroup the ones that belong together."
    )

    parsed, diagnostics = await _complete_json(
        system_prompt=prompts.SUGGEST_FEATURES_SYSTEM_PROMPT,
        user_message=user_message,
        label="suggest_features",
        meter=meter,
    )

    features = coerce_lists(parsed.get("features") or {})
    assignments = parsed.get("assignments") or {}
    if not isinstance(features, dict):
        features = {}
    if not isinstance(assignments, dict):
        assignments = {}

    known = {o["name"] for o in listed}
    # Only assignments naming a real object and a feature the model defined.
    placed = {
        str(name): str(uid)
        for name, uid in assignments.items()
        if name in known and isinstance(uid, str) and uid in features
    }
    members = _count_members(placed)

    cleaned: dict[str, Any] = {}
    renamed: dict[str, str] = {}
    order = 0
    for uid, feature in features.items():
        # A feature of one is a name for an object, which the object already has.
        if not isinstance(feature, dict) or members.get(uid, 0) < 2:
            continue
        order += 1000
        key = uid if _is_usable_uid(uid) else mint_uid()
        renamed[uid] = key
        cleaned[key] = {
            "order": order,
            "name": str(feature.get("name") or "").strip()[:80] or "Feature",
            "intent": str(feature.get("intent") or "").strip()[:500],
            "status": feature.get("status")
            if feature.get("status") in ("planned", "built", "retired") else "built",
        }

    final_assignments = {
        name: renamed[uid] for name, uid in placed.items() if uid in renamed
    }

    issues: list[ValidationIssue] = validate_blueprint({"plan": {"features": cleaned}})
    return {
        "features": cleaned,
        "assignments": final_assignments,
        "valid": not issues,
        "issues": [{"path": i.path, "message": i.message} for i in issues],
        "diagnostics": {**diagnostics, "considered": len(listed)},
    }


def _count_members(placed: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for uid in placed.values():
        counts[uid] = counts.get(uid, 0) + 1
    return counts


def _is_usable_uid(uid: Any) -> bool:
    """Letter-first ASCII alphanumeric, per `validate.py` rule 2.

    `str.isalnum()` is not the same test: it is true for "café", which would
    then fail the gate on write having passed here.
    """
    return isinstance(uid, str) and bool(UID_PATTERN.match(uid))
