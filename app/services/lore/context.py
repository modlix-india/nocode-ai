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
from app.services.lore import access, retrieval
from app.services.lore.access import LoreAccessError
from app.services.lore.models import normalise_subject

logger = logging.getLogger(__name__)

# Characters of app briefing folded into the system prompt. Big enough for the
# rules and conventions of a real app, small enough not to crowd out the tool
# catalogue.
BIG_PICTURE_BUDGET = 2600

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


async def big_picture(session: Any, *, budget: int = BIG_PICTURE_BUDGET) -> str:
    """The app briefing, for the system prompt. "" when there is nothing to say.

    Read access is checked like every other lore read: an agent must not narrate
    an app's knowledge to a user who cannot see the app.
    """
    if not _enabled():
        return ""
    ident = _identity(session)
    if not ident:
        return ""
    client_code, app_code = ident

    try:
        scope = await access.resolve_scope(_Auth(client_code), app_code)
        if not scope.can_read:
            return ""
        result = await retrieval.brief(scope, budget=budget)
    except LoreAccessError:
        return ""
    except Exception:  # noqa: BLE001 — never break a turn over lore
        logger.debug("lore: big-picture context skipped", exc_info=True)
        return ""

    if not result.get("entry_count"):
        return ""

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
