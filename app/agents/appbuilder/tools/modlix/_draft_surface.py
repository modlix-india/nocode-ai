"""The app's draft surface: a parallel copy the agent can edit and then LOOK at.

The agent's edits used to have two homes, and neither let it see its own work.
Committed to live, there was no state in which anyone could review them. Held in
the user's browser by `draft_registry`, they were invisible to `screenshot_page`,
which renders the live surface out of the database: a held change is not in the
database, so the agent screenshots the page as it was and concludes nothing
happened.

The backend already has the answer. A definition write carrying `?draft=true`
lands in a `Draft` row instead of the live document, a read carrying the same
flag prefers the draft, and the whole surface is reachable on its own hostname,
so a screenshot of THAT host shows unpublished work. The agent can change
something and then look at what it changed.

## The hazard this module exists to contain

`?draft=true` is an ordinary query parameter. A deployment that predates the
draft work does not reject it, does not warn about it, and does not honour it:
Spring drops unknown parameters and performs an ordinary live update. Verified
the hard way against a running local `ui` service, where a "draft" write bumped
the live version and published the change.

So drafting is never assumed. It is confirmed against the deployment once per
app, and when it cannot be confirmed the caller keeps today's behaviour rather
than writing live while telling the user their change is waiting for review.
That failure mode -- confidently wrong about where someone's work went -- is the
one this whole feature exists to remove, so it must not be reintroduced by the
feature itself.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

PUBLISH_API = "/api/ui/publish/app"
DRAFT_URL_API = "/api/security/clienturls/draft"

# True when the caller asked for this turn's definition writes to be drafted.
# Set by the agent from the chat request; absent everywhere else, so no existing
# caller changes behaviour by upgrading.
draft_mode: ContextVar[bool] = ContextVar("draft_mode", default=False)

# appCode -> does this deployment honour ?draft=true. Process-local and never
# expired: whether a running backend has the routes cannot change without a
# restart, which takes the process with it.
_supported: dict[str, bool] = {}


def wanted() -> bool:
    """Did the caller ask for drafting this turn? Says nothing about support."""
    return bool(draft_mode.get())


async def supported(client: Any, headers: dict[str, str], app_code: str) -> bool:
    """Does this deployment actually honour the draft flag?

    Probes the publish route, which exists only in a build that has the draft
    surface. The check is deliberately "did we get JSON back", not "did we get a
    2xx": on a stale deployment the gateway falls through to the app shell and
    returns **200 with an HTML page**, so a status check reads as support and
    every subsequent write goes live.
    """
    if not app_code:
        return False
    if app_code in _supported:
        return _supported[app_code]

    ok = False
    try:
        r = await client.get(f"{PUBLISH_API}/{app_code}/pending", headers=dict(headers or {}))
        ok = bool(r.success) and isinstance(r.data, (dict, list))
    except Exception:  # noqa: BLE001 - a probe must never break the turn
        logger.warning("draft support probe failed for %s", app_code, exc_info=True)
        ok = False

    _supported[app_code] = ok
    logger.info(
        "draft surface for '%s': %s", app_code,
        "available" if ok else "NOT available, writes stay live",
    )
    return ok


async def active(client: Any, headers: dict[str, str], app_code: str) -> bool:
    """Should this turn's definition writes carry `?draft=true`?"""
    return wanted() and await supported(client, headers, app_code)


def params_with_draft(params: dict[str, Any] | None, on: bool) -> dict[str, Any] | None:
    """Add the draft flag to a request's query parameters."""
    if not on:
        return params
    out = dict(params or {})
    out["draft"] = "true"
    return out


# ── The draft hostname ────────────────────────────────────────────────────────


async def get_draft_url(
    client: Any, headers: dict[str, str], app_code: str,
) -> tuple[str | None, str | None]:
    """The app's existing draft hostname, or (None, None) when none is minted."""
    r = await client.get(DRAFT_URL_API, headers=dict(headers or {}), params={"appCode": app_code})
    if not r.success:
        # 404 is the documented "nothing minted yet" answer, not a failure.
        if "404" in (r.error or ""):
            return None, None
        return None, r.error
    return _host_of(r.data), None


async def mint_draft_url(
    client: Any, headers: dict[str, str], app_code: str,
) -> tuple[str | None, str | None]:
    """Mint the app's draft hostname.

    This ROTATES: an existing link is replaced and thereby revoked. That matters
    because the link is a bearer credential for every unpublished change in the
    app, so minting when one already exists silently breaks whoever was given the
    old one. Callers that only need *a* link should go through `ensure_draft_url`.
    """
    r = await client.post(DRAFT_URL_API, headers=dict(headers or {}), params={"appCode": app_code})
    if not r.success:
        return None, r.error
    return _host_of(r.data), None


async def ensure_draft_url(
    client: Any, headers: dict[str, str], app_code: str,
) -> tuple[str | None, str | None]:
    """The app's draft hostname, minting one only if none exists.

    Get-then-mint rather than mint-always, because minting rotates and would
    revoke a link the user may already have shared.
    """
    url, err = await get_draft_url(client, headers, app_code)
    if err:
        return None, err
    if url:
        return url, None
    return await mint_draft_url(client, headers, app_code)


def _host_of(data: Any) -> str | None:
    """Pull the hostname out of a ClientUrl response."""
    if not isinstance(data, dict):
        return None
    pattern = data.get("urlPattern") or data.get("url") or data.get("pattern")
    if not pattern:
        return None
    pattern = str(pattern).strip()
    return pattern if pattern.startswith("http") else f"https://{pattern}"


def reset_support_cache() -> None:
    """Forget what we learned about which deployments support drafting."""
    _supported.clear()
