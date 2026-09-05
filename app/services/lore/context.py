"""Pushing lore into the model's context, rather than waiting to be asked.

Tools are a pull: the agent has to decide to look. That is fine for a specific
question and useless for the thing lore is actually for, which is stopping an
agent from confidently doing something this app decided against in March.

So lore is also PUSHED, at two scales:

  **Big picture** — the app's briefing, folded into the system prompt once per
  request. Purpose, the rules that must hold, the conventions, recent decisions.
  This is what a new person would be told before touching anything.

  **Small picture** — what is known about the object currently being worked on,
  injected as a per-turn reminder when the focus changes. An agent editing
  `page:jobsToday` should see the three things known about that page without
  having to guess that they exist.

Both are budgeted and both fail silent: lore is a nice-to-have that must never
break a turn.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config import settings
from app.config import settings
from app.services.lore import access, retrieval
from app.services.lore.models import BRIEF_ORDER
from app.services.lore.access import LoreAccessError
from app.services.lore.models import normalise_subject

logger = logging.getLogger(__name__)

# Characters of app briefing folded into the system prompt. Big enough for the
# rules and conventions of a real app, small enough not to crowd out the tool
# catalogue. Overridable by LORE_BIG_PICTURE_BUDGET.
#
# Raised from 2600 because that fitted 10-12 entries, and a seeded app carries
# around 60 app-level ones: measured on `appbuilder`, a 6000-char brief rendered
# 13 of 22 and reported truncated. Most of a hand-authored seed would have been
# invisible to the agent unless it thought to call a tool.
BIG_PICTURE_BUDGET = 3800

# The half of the briefing that must never be the part the budget drops. A
# constraint is the entry whose violation breaks the app, and purpose is what
# makes the rest legible, so they are rendered first under their own budget and
# everything else competes for what is left.
RULES_KINDS: tuple[str, ...] = ("purpose", "constraint")
RULES_SHARE = 0.38

# Characters of per-object lore in a turn reminder. Deliberately tight: this
# rides along on every turn while the focus holds.
SMALL_PICTURE_BUDGET = 1200

# Session-context keys.
FOCUS_KEY = "lore_focus"          # the subject currently being worked on
FOCUS_SENT_KEY = "lore_focus_sent"  # subjects already pushed this session


class _Auth:
    def __init__(self, client_code: str) -> None:
        self.client_code = client_code


def _enabled() -> bool:
    return bool(getattr(settings, "LORE_ENABLED", True))


def _identity(session: Any) -> tuple[str, str] | None:
    """(client_code, app_code) for a session, or None when it has no tenant."""
    auth = getattr(session, "auth", None)
    if not auth:
        return None
    client_code = getattr(auth, "client_code", "") or ""
    ctx = getattr(session, "context", None) or {}
    app_code = ctx.get("app_code") or getattr(auth, "app_code", "") or ""
    if not client_code or not app_code:
        return None
    return client_code, app_code


async def big_picture(session: Any, *, budget: int | None = None) -> str:
    """The app briefing, for the system prompt. "" when there is nothing to say.

    Read access is checked like every other lore read: an agent must not narrate
    an app's knowledge to a user who cannot see the app.
    """
    if not _enabled():
        return ""
    # Off by default: the briefing competed with the tool catalogue for prompt
    # space and lost, rendering a ranked fraction of what the app knew. The
    # model reaches all of it through `lore_index` instead.
    if not getattr(settings, "LORE_PUSH_BRIEF", False):
        return ""
    ident = _identity(session)
    if not ident:
        return ""
    client_code, app_code = ident
    if budget is None:
        budget = int(getattr(settings, "LORE_BIG_PICTURE_BUDGET", BIG_PICTURE_BUDGET)
                     or BIG_PICTURE_BUDGET)

    try:
        scope = await access.resolve_scope(_Auth(client_code), app_code)
        if not scope.can_read:
            return ""
        # Two passes with separate budgets. One pass ordered by BRIEF_ORDER
        # would put purpose and constraints first and then let the budget cut
        # somewhere in the middle, which is fine until an app has enough
        # entries that the cut lands before the constraints are done.
        rules_budget = max(600, int(budget * RULES_SHARE))
        rules = await retrieval.brief(scope, budget=rules_budget, kinds=RULES_KINDS)
        rest = await retrieval.brief(
            scope, budget=budget - rules_budget,
            kinds=tuple(k for k in BRIEF_ORDER if k not in RULES_KINDS),
        )
    except LoreAccessError:
        return ""
    except Exception:  # noqa: BLE001 — never break a turn over lore
        logger.debug("lore: big-picture context skipped", exc_info=True)
        return ""

    if not rules.get("entry_count") and not rest.get("entry_count"):
        return ""

    blocks: list[str] = []
    if rules.get("entry_count"):
        blocks.append(
            "These are this app's purpose and its non-negotiable rules. "
            "Breaking one of them breaks the app.\n\n" + rules["markdown"]
        )
    if rest.get("entry_count"):
        blocks.append(rest["markdown"])
    result = {
        "entry_count": rules.get("entry_count", 0) + rest.get("entry_count", 0),
        "markdown": "\n\n".join(blocks),
    }

    inherited_note = ""
    if scope.is_override:
        inherited_note = (
            f"\nSome of this is inherited from {scope.base_client}, who own the app; "
            f"lines marked `from {scope.base_client}` are theirs. Anything you record "
            f"is saved as {scope.client_code}'s and does not change what they see.\n"
        )

    return (
        "\n\n## What is already known about this app\n\n"
        "Accumulated from previous sessions and from what people have written down. "
        "Treat it as established unless the user says otherwise. Do not re-decide "
        "something recorded here as a decision; if you disagree, say so and ask.\n"
        f"{inherited_note}\n"
        f"{result['markdown']}\n\n"
        "Use `lore_search` for anything not covered above, `lore_about` before editing "
        "an object you did not create, and `lore_add` when the user states a new fact."
    )


async def small_picture(
    session: Any, subject: str, *, budget: int = SMALL_PICTURE_BUDGET,
) -> str:
    """What is known about one object, for a turn reminder. "" when nothing."""
    if not _enabled():
        return ""
    if not getattr(settings, "LORE_PUSH_SUBJECT", True):
        return ""
    ident = _identity(session)
    if not ident:
        return ""
    client_code, app_code = ident
    subject = normalise_subject(subject)
    if subject == "app":
        return ""   # the big picture already covers the app level

    try:
        scope = await access.resolve_scope(_Auth(client_code), app_code)
        if not scope.can_read:
            return ""
        result = await retrieval.brief(scope, subject=subject, budget=budget)
    except LoreAccessError:
        return ""
    except Exception:  # noqa: BLE001
        logger.debug("lore: small-picture context skipped", exc_info=True)
        return ""

    if not result.get("entry_count"):
        return ""
    return (
        f"\n\n[Known about `{subject}`, from this app's accumulated knowledge]\n"
        f"{result['markdown']}\n"
    )


# ── Focus tracking ───────────────────────────────────────────────────────
# Which object is the agent working on right now? Derived from tool inputs,
# because that is the only signal that exists without asking every tool author
# to remember to declare it.

# Parameter name -> subject type. Ordered: the first match wins, so a tool
# taking both `page_name` and `storage_name` is read as being about the page.
_SUBJECT_PARAMS: tuple[tuple[str, str], ...] = (
    ("page_name", "page"),
    ("pageName", "page"),
    ("storage_name", "storage"),
    ("storageName", "storage"),
    ("function_name", "function"),
    ("functionName", "function"),
    ("schema_name", "schema"),
    ("template_name", "template"),
    ("connection_name", "connection"),
    ("theme_name", "theme"),
    ("style_name", "style"),
    ("uri_path", "uripath"),
)

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-/{}]{1,120}$")


def subject_from_tool_call(tool_name: str, params: dict[str, Any] | None) -> str | None:
    """Derive `<type>:<name>` from a tool call, or None.

    Heuristic and deliberately conservative: a wrong subject would push
    irrelevant knowledge at the model every turn, which is worse than pushing
    none. Only recognised parameter names count, and only plausible values.
    """
    if not isinstance(params, dict):
        return None
    for key, subject_type in _SUBJECT_PARAMS:
        value = params.get(key)
        if isinstance(value, str) and value.strip() and _NAME_RE.match(value.strip()):
            return normalise_subject(f"{subject_type}:{value.strip()}")
    return None


def note_focus(session: Any, tool_name: str, params: dict[str, Any] | None) -> None:
    """Record what the agent is working on, for the next turn's small picture."""
    subject = subject_from_tool_call(tool_name, params)
    if not subject:
        return
    ctx = getattr(session, "context", None)
    if ctx is None:
        return
    ctx[FOCUS_KEY] = subject


def take_unsent_focus(session: Any) -> str | None:
    """The current focus, if it has not been pushed to the model yet.

    Push each subject ONCE per session. Repeating the same block every turn
    while an agent works through a page would waste most of the reminder budget
    restating what it was told three turns ago.
    """
    ctx = getattr(session, "context", None)
    if not ctx:
        return None
    subject = ctx.get(FOCUS_KEY)
    if not subject:
        return None
    sent = ctx.setdefault(FOCUS_SENT_KEY, [])
    if subject in sent:
        return None
    sent.append(subject)
    return subject


# ── Advice that arrives before the write, not after it ───────────────────

# Session-context key for subjects already advised, mirroring FOCUS_SENT_KEY.
ADVISED_KEY = "lore_advised"

# Tight on purpose: this rides on a tool result, so it competes with the
# result's own content for the model's attention.
PRE_WRITE_BUDGET = 700

# Only the kinds that would stop someone doing the wrong thing. A `purpose` or
# a `glossary` entry is orientation and belongs in the system prompt; a
# constraint, a trap or a convention is the thing you needed to know one line
# before you wrote.
PRE_WRITE_KINDS: tuple[str, ...] = ("constraint", "gotcha", "convention")


async def pre_write_advice(
    session: Any, tool_name: str, params: dict[str, Any] | None,
) -> str:
    """What this app already knows about the object being written, right now.

    Lore is already consulted before execution — `note_focus` runs above the
    dispatch — but the resulting `small_picture` is injected on the NEXT turn.
    In a turn that issues many writes in parallel, that advice lands after every
    one of them. This returns text to append to the offending tool's own result,
    so the constraint is in front of the model inside the same turn.

    Once per subject per session. Returns "" for anything that is not a write,
    for app-level calls, and whenever lore has nothing specific to say — which
    is most of the time, and costs one indexed query.

    Deliberately advisory. Lore is a claim, not a permission system: `access.py`
    is the thing that fails closed, and an entry that wrongly blocked an edit
    would be the fastest way to get lore switched off.
    """
    if not _enabled() or not getattr(settings, "LORE_ADVISE_BEFORE_EDITS", False):
        return ""
    ident = _identity(session)
    if not ident:
        return ""
    client_code, app_code = ident

    try:
        from app.services.lore import watch as _watch

        # The same classifier the edit observer uses, so "is this a write" has
        # one answer in this codebase rather than two.
        fact = _watch.classify(tool_name, params, summary="", success=True)
        if fact is None or fact.subject == "app":
            return ""
        subject = fact.subject

        advised = session.context.get(ADVISED_KEY)
        if not isinstance(advised, list):
            advised = []
        if subject in advised:
            return ""

        scope = await access.resolve_scope(_Auth(client_code), app_code)
        if not scope.can_read:
            return ""
        result = await retrieval.brief(
            scope, subject=subject, budget=PRE_WRITE_BUDGET, kinds=PRE_WRITE_KINDS,
        )
        # Record the subject as advised even when there was nothing to say, so a
        # page with no lore is not re-queried on every write to it.
        advised.append(subject)
        session.context[ADVISED_KEY] = advised[-40:]
        if not result.get("entry_count"):
            return ""
    except LoreAccessError:
        return ""
    except Exception:  # noqa: BLE001 — never break a tool call over lore
        logger.debug("lore: pre-write advice skipped", exc_info=True)
        return ""

    return (
        f"\n\n<lore subject=\"{subject}\">\n"
        f"Before you change `{subject}` again — this is what this app already "
        f"knows about it. Follow it, or say plainly that you disagree and why.\n\n"
        f"{result['markdown']}\n</lore>"
    )
