"""The curator: turns raw observations into durable entries.

This is the only component that writes `lore_entry` from observations, and
the only place an LLM is involved in lore. The split is deliberate:

    the model PROPOSES,  this module DISPOSES.

The model returns a list of operations. Every operation is validated against
the taxonomy, checked against the entries that actually exist, and applied by
code that enforces the invariants (pinned entries are untouchable, a supersede
must name a real entry in the same app, confidence is clamped, a body that
already exists becomes a confirmation rather than a duplicate).

A malformed or hallucinated operation is dropped, not applied. A pass that
produces nothing is a normal outcome, not a failure.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

from app.config import settings
from app.services.lore import store
from app.services.lore.models import (
    ENTRY_KIND_HELP,
    ENTRY_KINDS,
    Entry,
    Observation,
    normalise_subject,
)
from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)

# How many observations one pass looks at. Large enough that related facts land
# in the same window (so the model can merge them), small enough to stay inside
# a cheap model's context.
BATCH_SIZE = 60

# Existing entries shown to the model as context, so it can confirm/revise
# rather than re-creating what is already known.
CONTEXT_ENTRIES = 120

# Cheapest tier that can follow the schema. Curation runs in the background and
# is called often; it should never be the expensive path.
MODEL_TIER = "fast"


SYSTEM_PROMPT = f"""You curate a knowledge base about ONE software application.

You are given raw observations (things that happened or were said while people
built the app) and the knowledge already recorded. You decide what durable
knowledge those observations justify.

You are writing for a person who joins this project in six months and has to be
useful on day one. Everything you write should still be true and worth reading
then. If an observation is only true today, it is a `status` entry or it is
nothing.

ENTRY KINDS — pick exactly one per entry:
{chr(10).join(f"  {k}: {v}" for k, v in ENTRY_KIND_HELP.items())}

OPERATIONS you may return:
  add      a fact that is not already recorded. Needs kind, subject, title, body.
  confirm  an existing entry is re-evidenced by these observations. Needs id.
  revise   an existing entry is now partly wrong or incomplete. Needs id + body.
  retire   an existing entry is no longer true and nothing replaced it. Needs id.
  supersede an existing entry was replaced by a new fact. Needs id (the OLD entry)
           plus kind/subject/title/body for the NEW one.
  contradict two existing entries disagree and you cannot tell which is right.
           Needs id + other_id. Both are marked contested and a person settles
           it. Use this instead of guessing.

RULES
1. Prefer confirm over add. Adding a near-duplicate is the main failure mode.
2. A `decision` entry must say what was chosen AND why. Without the why, it is
   not a decision, it is a convention.
3. Never record secrets, tokens, passwords, API keys, personal contact details,
   or the contents of customer data rows. If an observation contains one, either
   skip it or write the fact without the secret.
4. Do not record what the platform does in general. Only what is true about THIS
   application. "Modlix pages have 16 breakpoints" is not an entry. "This app
   only styles three of them, by convention" is.
5. Do not restate the observation. Write the durable claim behind it.
6. `body` is markdown, 1 to 6 sentences. `title` is one line, readable alone,
   no trailing full stop.
7. confidence 0-100: how sure you are the claim is true and will stay true.
   Something stated once in passing is 40. Something demonstrated by an edit or
   said twice is 70. Something explicitly agreed is 90.
8. `sources` lists the observation ids that justify the operation. Always fill it.
9. Return NOTHING for observations that carry no durable knowledge. An empty
   list is a correct answer and is expected most of the time.
9b. Nothing here expires on its own. An entry stays trusted until you supersede
   it, retire it, or mark it contradicted, so an old entry you can see no
   evidence against is not thereby suspect — leave it alone. Equally, do not
   let a wrong entry stand because it is old: say so with retire or contradict.
10. An entry marked READ-ONLY belongs to the client that owns the application.
    You may NOT confirm, revise, retire or supersede it. If an observation
    contradicts one, add your own entry stating what is true here and say in the
    body that it differs from the owner's; a person will reconcile them.

Return ONLY a JSON object, no prose, no code fence:
{{"operations": [
   {{"op":"add","kind":"convention","subject":"page:jobsToday",
     "title":"Job list filters live in Page.filters","body":"...",
     "tags":["naming"],"confidence":70,"sources":[12,13]}},
   {{"op":"confirm","id":41,"sources":[14]}},
   {{"op":"supersede","id":37,"kind":"decision","subject":"storage:job",
     "title":"...","body":"...","confidence":80,"sources":[15]}}
]}}"""


async def _read_chain(client_code: str, app_code: str) -> tuple[str, ...]:
    """The client inheritance chain for context, or just our own client.

    Curation runs unattended and must not fail because security is briefly
    unreachable: without the chain it simply sees less, and it can still only
    write its own rows.
    """
    try:
        from app.services.lore import access as _access

        class _A:
            client_code = ""

        a = _A()
        a.client_code = client_code
        scope = await _access.resolve_scope(a, app_code)
        return scope.read_chain
    except Exception:  # noqa: BLE001
        logger.debug("lore curator: falling back to own-client context only", exc_info=True)
        return (client_code,)


# ── Prompt assembly ──────────────────────────────────────────────────────


_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(bearer\s+)?eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),  # JWT
    re.compile(r"(?i)\b(sk|pk|rzp|EAA)[-_a-zA-Z0-9]{16,}"),                                          # api keys
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S+"),
]


def redact(text: str) -> str:
    """Strip obvious credentials before anything reaches the model or the store.

    This is a safety net, not a guarantee. The prompt also tells the model not
    to record secrets, and callers should not be sending them in the first
    place. Belt and braces because lore is long-lived by design: a token that
    leaks into an entry is still there in a year.
    """
    out = text or ""
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[redacted]", out)
    return out


def _render_observations(observations: Sequence[Observation], budget: int = 24000) -> str:
    lines: list[str] = []
    used = 0
    for obs in observations:
        body = redact(obs.body).strip()
        if len(body) > 1200:
            body = body[:1200] + " …[truncated]"
        block = (
            f"[{obs.id}] kind={obs.kind} subject={obs.subject} "
            f"source={obs.source} seen={obs.seen_count}x\n{body}"
        )
        if used + len(block) > budget:
            lines.append(f"…[{len(observations) - len(lines)} more observations not shown this pass]")
            break
        lines.append(block)
        used += len(block)
    return "\n\n".join(lines)


def _render_existing(entries: Sequence[Entry], budget: int = 12000) -> str:
    if not entries:
        return "(nothing recorded yet — this is a new app)"
    lines: list[str] = []
    used = 0
    for e in entries:
        body = e.body.strip().replace("\n", " ")
        if len(body) > 220:
            body = body[:220] + "…"
        flags = ""
        if e.pinned:
            flags += " PINNED"
        if e.inherited:
            # The model is told plainly rather than left to infer it from ids.
            flags += f" READ-ONLY (owned by {e.client_code})"
        block = (
            f"#{e.id} [{e.kind}] {e.subject} conf={e.effective_confidence}{flags}\n"
            f"  {e.title}\n  {body}"
        )
        if used + len(block) > budget:
            lines.append(f"…[{len(entries) - len(lines)} more entries not shown]")
            break
        lines.append(block)
        used += len(block)
    return "\n".join(lines)


def build_user_prompt(
    app_code: str,
    observations: Sequence[Observation],
    existing: Sequence[Entry],
) -> str:
    return (
        f"Application: {app_code}\n\n"
        f"=== KNOWLEDGE ALREADY RECORDED ===\n{_render_existing(existing)}\n\n"
        f"=== NEW OBSERVATIONS ===\n{_render_observations(observations)}\n\n"
        "Return the JSON object of operations."
    )


# ── Response parsing ─────────────────────────────────────────────────────


def parse_operations(raw: str) -> list[dict[str, Any]]:
    """Pull the operations list out of a model response.

    Tolerates a code fence and leading prose because small models add both;
    refuses to guess beyond that. A response we cannot parse yields no
    operations, which leaves the observations pending for the next pass.
    """
    if not raw:
        return []
    text = raw.strip()

    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()

    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return []
        text = text[start:end + 1]

    try:
        parsed = json.loads(text)
    except ValueError:
        logger.warning("lore curator: unparseable model response (%d chars)", len(raw))
        return []

    ops = parsed.get("operations") if isinstance(parsed, dict) else parsed
    return [op for op in (ops or []) if isinstance(op, dict)]


# ── Operation application ────────────────────────────────────────────────


def _clean_sources(op: dict[str, Any], valid_ids: set[int]) -> list[int]:
    """Keep only observation ids that were actually in this batch.

    A model that invents source ids would otherwise attach an entry to somebody
    else's observation, and provenance is the thing that makes lore
    trustworthy. Silently dropping the invented ones is right; refusing the
    whole operation over it is not.
    """
    raw = op.get("sources") or []
    out: list[int] = []
    for value in raw if isinstance(raw, list) else []:
        try:
            oid = int(value)
        except (TypeError, ValueError):
            continue
        if oid in valid_ids:
            out.append(oid)
    return out


def _valid_new_entry(op: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Validate the add/supersede payload. Returns (kind, subject, title, body)."""
    kind = str(op.get("kind") or "").strip()
    if kind not in ENTRY_KINDS:
        return None
    title = str(op.get("title") or "").strip().rstrip(".")
    body = str(op.get("body") or "").strip()
    if len(title) < 4 or len(body) < 10:
        return None
    subject = normalise_subject(op.get("subject"))
    return kind, subject, title[:240], redact(body)[:20000]


def _confidence(op: dict[str, Any], default: int = 55) -> int:
    try:
        return max(0, min(100, int(op.get("confidence", default))))
    except (TypeError, ValueError):
        return default


async def apply_operations(
    client_code: str,
    app_code: str,
    operations: Sequence[dict[str, Any]],
    *,
    batch_ids: set[int],
    known_entry_ids: set[int],
    updated_by: int = 0,
) -> dict[str, int]:
    """Apply validated operations. Returns per-op-type counters.

    Every path here is defensive on purpose: this runs unattended against a
    store that people will later trust as documentation.
    """
    counters = {"added": 0, "confirmed": 0, "revised": 0, "retired": 0, "rejected": 0}

    for op in operations:
        kind_of_op = str(op.get("op") or "").strip().lower()
        sources = _clean_sources(op, batch_ids)

        if kind_of_op == "add":
            parsed = _valid_new_entry(op)
            if not parsed:
                counters["rejected"] += 1
                continue
            entry_kind, subject, title, body = parsed
            result = await store.add_entry(
                client_code, app_code,
                kind=entry_kind, subject=subject, title=title, body=body,
                tags=[str(t)[:40] for t in (op.get("tags") or [])][:8],
                confidence=_confidence(op), created_by=updated_by,
                source_ids=sources,
            )
            counters["added" if result["created"] else "confirmed"] += 1

        elif kind_of_op == "confirm":
            entry_id = _int_or_none(op.get("id"))
            if entry_id is None or entry_id not in known_entry_ids:
                counters["rejected"] += 1
                continue
            await store.confirm_entry(entry_id, source_ids=sources, confidence=_confidence(op, 0) or None)
            counters["confirmed"] += 1

        elif kind_of_op == "revise":
            entry_id = _int_or_none(op.get("id"))
            body = str(op.get("body") or "").strip()
            if entry_id is None or entry_id not in known_entry_ids or len(body) < 10:
                counters["rejected"] += 1
                continue
            revised = await store.revise_entry(
                entry_id,
                title=(str(op.get("title")).strip().rstrip(".") if op.get("title") else None),
                body=redact(body)[:20000],
                confidence=_confidence(op),
                updated_by=updated_by,
                message=str(op.get("reason") or "curator revision")[:512],
                source_ids=sources,
            )
            # revise_entry returns None for a pinned or missing entry — both are
            # a refusal, not a failure of this pass.
            counters["revised" if revised else "rejected"] += 1

        elif kind_of_op == "retire":
            entry_id = _int_or_none(op.get("id"))
            if entry_id is None or entry_id not in known_entry_ids:
                counters["rejected"] += 1
                continue
            ok = await store.set_entry_status(entry_id, "retired", updated_by=updated_by)
            counters["retired" if ok else "rejected"] += 1

        elif kind_of_op == "supersede":
            old_id = _int_or_none(op.get("id"))
            parsed = _valid_new_entry(op)
            if old_id is None or old_id not in known_entry_ids or not parsed:
                counters["rejected"] += 1
                continue
            entry_kind, subject, title, body = parsed
            result = await store.add_entry(
                client_code, app_code,
                kind=entry_kind, subject=subject, title=title, body=body,
                tags=[str(t)[:40] for t in (op.get("tags") or [])][:8],
                confidence=_confidence(op, 70), created_by=updated_by,
                source_ids=sources,
            )
            new_id = result["id"]
            if new_id != old_id:
                ok = await store.set_entry_status(
                    old_id, "superseded", superseded_by=new_id, updated_by=updated_by,
                )
                if not ok:
                    # Pinned entries cannot be superseded. The new entry still
                    # stands; the two now coexist and a human can resolve it.
                    await store.add_link(new_id, old_id, "contradicts")
            counters["added" if result["created"] else "confirmed"] += 1

        elif kind_of_op == "contradict":
            # Two entries disagree and the curator cannot tell which is right.
            # Recording that is far more useful than silently picking one:
            # both drop in confidence, both surface as contested, and a person
            # settles it. This is the mechanism that replaced time decay.
            a_id = _int_or_none(op.get("id"))
            b_id = _int_or_none(op.get("other_id"))
            if (
                a_id is None or b_id is None or a_id == b_id
                or a_id not in known_entry_ids
                or b_id not in known_entry_ids
            ):
                counters["rejected"] += 1
                continue
            await store.add_link(a_id, b_id, "contradicts")
            counters["contradicted"] = counters.get("contradicted", 0) + 1

        else:
            counters["rejected"] += 1

    return counters


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── The pass ─────────────────────────────────────────────────────────────


async def curate(
    client_code: str,
    app_code: str,
    *,
    trigger_source: str = "manual",
    batch_size: int = BATCH_SIZE,
    updated_by: int = 0,
    provider_name: str | None = None,
) -> dict[str, Any]:
    """Run one curation pass for an app.

    Returns a summary dict. Safe to call often: it no-ops when there is nothing
    pending and refuses to run concurrently with itself for the same app.
    """
    if await store.has_open_run(client_code, app_code):
        return {"status": "skipped", "reason": "a curation pass is already running for this app"}

    pending = await store.pending_observations(client_code, app_code, limit=batch_size)
    if not pending:
        return {"status": "idle", "reason": "no uncurated observations", "considered": 0}

    run_id = await store.open_run(client_code, app_code, trigger_source)
    counters: dict[str, int] = {"considered": len(pending)}
    error = ""

    try:
        # Show the curator the FULL inheritance chain, so it can see what the
        # app owner already established and confirm or skip it instead of
        # re-deriving a near-duplicate under this client. It may only WRITE to
        # this client's own entries, which `known_entry_ids` below enforces.
        chain = await _read_chain(client_code, app_code)
        existing = await store.list_entries(
            chain, app_code, status="active", limit=CONTEXT_ENTRIES,
        )
        provider = get_llm_provider(provider_name or settings.APPBUILDER_PROVIDER)
        response = await provider.create_completion(
            system_prompt=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(app_code, pending, existing)}],
            model_tier=MODEL_TIER,
            max_tokens=4000,
        )
        operations = parse_operations(response.get("content") or "")
        applied = await apply_operations(
            client_code, app_code, operations,
            batch_ids={o.id for o in pending},
            # Only this client's own entries are writable. An inherited entry
            # belongs to the app owner: the curator may read it, and a PERSON
            # may override it, but an unattended pass must not.
            known_entry_ids={e.id for e in existing if e.client_code == client_code},
            updated_by=updated_by,
        )
        counters.update(applied)

        # Mark them curated whatever happened. An observation that produced no
        # entry has still been considered; leaving it pending would make every
        # later pass re-read the same uninformative rows forever.
        await store.mark_observations_curated([o.id for o in pending])

    except Exception as exc:  # noqa: BLE001 — a curation failure must not surface to a user turn
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("lore curator failed for %s/%s", client_code, app_code)
    finally:
        await store.close_run(run_id, counters, error)

    return {
        "status": "error" if error else "ok",
        "run_id": run_id,
        "error": error or None,
        **counters,
    }


async def curate_all(
    *, min_pending: int = 5, max_apps: int = 10, trigger_source: str = "sweep",
) -> list[dict[str, Any]]:
    """Curate every app that has accumulated enough pending observations.

    Intended for a scheduled sweep. Bounded by `max_apps` so one invocation
    cannot run away.
    """
    targets = await store.apps_needing_curation(min_pending=min_pending, limit=max_apps)
    results = []
    for target in targets:
        result = await curate(
            target["client_code"], target["app_code"], trigger_source=trigger_source,
        )
        results.append({**target, **result})
    return results
