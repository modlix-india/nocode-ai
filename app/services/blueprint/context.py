"""Pushing the plan into the model's context, rather than waiting to be asked.

A tool is a pull: the agent has to decide to look. That is fine for a specific
question and useless for what a blueprint is actually for, which is stopping an
agent from confidently rebuilding something the app decided against in March.

So the app's plan is also PUSHED — folded into the uncached tail of the system
prompt, once per request, as a short brief: what the app is for, which features
it has, and how many objects are planned. Short on purpose. The full plan is a
tool call away and can be large; what belongs in every turn is enough for the
agent to know a plan EXISTS and roughly what it says, because an agent that does
not know to ask will not ask.

It goes in `build_dynamic_context`, which is the per-request tail, and never in
`set_static_suffix`, which is for process-static text. Putting a per-app brief
in the static suffix would serve one app's plan to every other app's session.

Fails silent, always. A plan is a nice-to-have and must never break a turn.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.config import settings
from app.core.session import session_app_code
from app.services.blueprint import objects
from app.services.blueprint.objects import BlueprintObjectError
from app.services.lore import access
from app.services.lore.access import LoreAccessError

logger = logging.getLogger(__name__)

#: Characters of plan brief folded into the system prompt. Tight: this rides on
#: every request and competes with the tool catalogue, which it must not
#: crowd out. The whole plan is one `blueprint_get` away.
BRIEF_BUDGET = 1600

#: Features listed by name before the list is truncated.
MAX_FEATURES = 12

#: Reading a plan costs two gateway round-trips — a filtered list to resolve the
#: id, then the detail route, because the list projection deliberately omits
#: `blueprint`. Paying that on every turn of a conversation to restate an app
#: plan that changes a few times a day would be absurd, so the rendered brief is
#: cached per (client, app) for a minute. A plan the agent itself just wrote
#: through `blueprint_set` is at most this stale in the brief, and exactly
#: current through `blueprint_get`.
_CACHE_TTL_SECONDS = 60.0
_cache: dict[tuple[str, str], tuple[float, str]] = {}


class _Auth:
    def __init__(self, client_code: str) -> None:
        self.client_code = client_code


def _identity(session: Any) -> tuple[str, str] | None:
    auth = getattr(session, "auth", None)
    if not auth:
        return None
    client_code = getattr(auth, "client_code", "") or ""
    app_code = session_app_code(session)
    if not client_code or not app_code:
        return None
    return client_code, app_code


def _headers(session: Any) -> dict[str, str]:
    context = getattr(session, "context", None) or {}
    headers = context.get("headers")
    if isinstance(headers, dict) and headers:
        return dict(headers)
    auth = getattr(session, "auth", None)
    if not auth:
        return {}
    built = {"Authorization": getattr(auth, "token", "") or "",
             "clientCode": getattr(auth, "client_code", "") or ""}
    if getattr(auth, "access_app_code", ""):
        built["appCode"] = auth.access_app_code
    return built


async def app_brief(session: Any, *, budget: int = BRIEF_BUDGET) -> str:
    """The app's plan in a paragraph, for the system prompt. "" when there is none."""
    if not getattr(settings, "BLUEPRINT_PUSH_BRIEF", True):
        return ""
    identity = _identity(session)
    if not identity:
        return ""
    client_code, app_code = identity

    key = (client_code, app_code)
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _CACHE_TTL_SECONDS:
        return hit[1]

    try:
        scope = await access.resolve_scope(_Auth(client_code), app_code)
        if not scope.can_read:
            return ""
        current = await objects.read_blueprint("application", app_code, "", _headers(session))
    except (LoreAccessError, BlueprintObjectError):
        # An app with no UI document, or one this caller cannot read. Cached as
        # empty so a session in such an app does not retry twice a turn.
        _cache[key] = (time.monotonic(), "")
        return ""
    except Exception:  # noqa: BLE001 — never break a turn over a brief
        logger.debug("blueprint: app brief skipped", exc_info=True)
        return ""

    brief = render_brief(current.get("blueprint") or {}, budget=budget)
    _cache[key] = (time.monotonic(), brief)
    return brief


def invalidate(client_code: str | None = None, app_code: str | None = None) -> int:
    """Drop cached briefs. Called after a plan is written, and by tests."""
    if client_code is None and app_code is None:
        count = len(_cache)
        _cache.clear()
        return count
    doomed = [
        k for k in _cache
        if (client_code is None or k[0] == client_code)
        and (app_code is None or k[1] == app_code)
    ]
    for k in doomed:
        _cache.pop(k, None)
    return len(doomed)


def render_brief(blueprint: dict[str, Any], *, budget: int = BRIEF_BUDGET) -> str:
    """The brief text for a plan. Pure, so it can be tested without a network.

    Returns "" for a plan with nothing in it. An app with no plan is the normal
    case on day one and saying "this app has no plan" every turn would spend the
    budget on a non-event.
    """
    if not isinstance(blueprint, dict) or not blueprint:
        return ""

    plan = blueprint.get("plan") if isinstance(blueprint.get("plan"), dict) else {}
    intent = str(blueprint.get("intent") or "").strip()
    audience = str(plan.get("audience") or "").strip()
    features = plan.get("features") if isinstance(plan.get("features"), dict) else {}
    planned = plan.get("objects") if isinstance(plan.get("objects"), dict) else {}

    if not (intent or audience or features or planned):
        return ""

    lines: list[str] = []
    if intent:
        lines.append(intent)
    if audience:
        lines.append(f"Audience: {audience}")

    if features:
        ordered = sorted(
            (f for f in features.values() if isinstance(f, dict)),
            key=lambda f: f.get("order") if isinstance(f.get("order"), int) else 0,
        )
        named = [
            f"{f.get('name') or 'unnamed'} ({f.get('status') or 'planned'})"
            for f in ordered[:MAX_FEATURES]
        ]
        if len(ordered) > MAX_FEATURES:
            named.append(f"and {len(ordered) - MAX_FEATURES} more")
        lines.append("Features: " + ", ".join(named))

    if planned:
        lines.append(f"{len(planned)} objects are planned for this app.")

    body = "\n".join(lines)
    if len(body) > budget:
        body = body[:budget].rsplit("\n", 1)[0] + "\n…(truncated; call blueprint_get for the whole plan)"

    return (
        "\n\n## What this app is meant to be\n\n"
        "This is the app's PLAN, recorded deliberately. Treat it as settled unless "
        "the user says otherwise, and do not re-decide something it already decided; "
        "if you disagree, say so and ask.\n\n"
        f"{body}\n\n"
        "Use `blueprint_get` for the full plan or for one object's, `blueprint_drift` "
        "before assuming a page matches its plan, and `blueprint_set` when the user "
        "states intent that should outlive this conversation."
    )
