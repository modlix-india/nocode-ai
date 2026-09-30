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

from app.core.tools.draft_registry import (
    DRAFTABLE_KINDS,
    PAGE_ONLY_KINDS,
    DraftScope,
    drafting,
    drafts_kind,
    to_scope,
)

logger = logging.getLogger(__name__)

PUBLISH_API = "/api/ui/publish/app"
DRAFT_URL_API = "/api/security/clienturls/draft"

# What the caller asked for this turn: LIVE, DRAFT, or PAGE_ONLY_DRAFT. Set by
# the agent from the chat request.
#
# Defaults to DRAFT, so a caller that says nothing gets a change it can look at
# before anyone else sees it. That is only safe because nothing is drafted until
# `supported()` has confirmed the deployment honours the flag -- on a stale
# backend this whole module folds back to writing live.
draft_mode: ContextVar[DraftScope] = ContextVar("draft_mode", default=DraftScope.DRAFT)

# appCode -> does this deployment honour ?draft=true. Process-local and never
# expired: whether a running backend has the routes cannot change without a
# restart, which takes the process with it.
_supported: dict[str, bool] = {}


def wanted() -> DraftScope:
    """What did the caller ask for this turn? Says nothing about support."""
    return to_scope(draft_mode.get())


def wants_kind(kind: str) -> bool:
    """Did the caller ask for THIS kind to be drafted? Ignores support."""
    scope = wanted()
    if scope is DraftScope.LIVE:
        return False
    if scope is DraftScope.PAGE_ONLY_DRAFT:
        return kind in PAGE_ONLY_KINDS
    return kind in DRAFTABLE_KINDS


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


async def active(
    client: Any, headers: dict[str, str], app_code: str, kind: str = "page",
) -> bool:
    """Should a write to `kind` carry `?draft=true` this turn?

    Reads the decision the agent already made for the turn when there is one,
    so a tool and the HTTP choke point can never disagree about where a write
    went. Falls back to deciding for itself, which is what a headless caller and
    the tests get.

    `kind` defaults to "page" because every caller of this is page work; a tool
    that edits something else has to say so, and gets the right answer under
    PAGE_ONLY_DRAFT instead of the page's answer.
    """
    if drafting.get() is not DraftScope.LIVE:
        return drafts_kind(kind)
    return wants_kind(kind) and await supported(client, headers, app_code)


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
