"""Who may read and write an app's lore, and whose knowledge they see.

Three rules, and everything else here is machinery for them.

  1. **Writes always land under the logged-in user's client code.** Never the
     app owner's, never one supplied in a request. A CLIENTA user working on a
     SYSTEM-owned app writes CLIENTA lore.

  2. **App edit access is required to write, read access to read.** Checked
     against the security service on every call. Before this module existed,
     any authenticated user could write lore for any app code they could name.

  3. **Reads follow the app's inheritance chain**, base client first, caller
     last, exactly as every other overridable object in the platform does.
     CLIENTA sees SYSTEM's knowledge about the app, with CLIENTA's own on top.

The chain comes from `applications/internal/appInheritance`, which is the same
call the ui service makes to resolve overrides. Note the `/internal/` routes
under `applications` are `permitAll` in the security service's own filter chain:
they answer factual questions and do no auth of their own, which is precisely
why the clientCode we pass must come from the verified JWT and nowhere else.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Security's own caches make these cheap, but three HTTP hops per request is
# still three too many for a briefing the agent asks for every turn.
_CACHE_TTL_SECONDS = 60.0
_cache: dict[tuple[str, str], tuple[float, "LoreScope"]] = {}

_TIMEOUT = 8.0

# The platform treats the SYSTEM client as able to reach every app
# (`ContextAuthentication.isSystemClient()`), and `hasWriteAccess` does not
# special-case it. Mirror that here or a SYSTEM builder loses access to apps
# owned by other clients.
_SYSTEM_CLIENT = "SYSTEM"


class LoreAccessError(Exception):
    """Access refused, or the security service could not answer.

    Carries an HTTP-ish status so routers and tools can render it the same way.
    """

    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class LoreScope:
    """The resolved answer to "what may this caller do with this app's lore"."""

    app_code: str
    client_code: str            # the logged-in user's client. Writes land here.
    read_chain: tuple[str, ...]  # base-first, caller-last
    can_read: bool
    can_write: bool

    @property
    def base_client(self) -> str | None:
        """The owning client, when the caller is not it."""
        if len(self.read_chain) < 2:
            return None
        return self.read_chain[0]

    @property
    def is_override(self) -> bool:
        """Is this caller writing overrides on top of somebody else's app?"""
        return self.base_client is not None

    def owns(self, entry_client_code: str) -> bool:
        """May this caller edit that entry in place, or must they fork it?"""
        return entry_client_code == self.client_code

    def require_read(self) -> None:
        if not self.can_read:
            raise LoreAccessError(
                f"No read access to app '{self.app_code}' for client "
                f"'{self.client_code}'.", status=403,
            )

    def require_write(self) -> None:
        self.require_read()
        if not self.can_write:
            raise LoreAccessError(
                f"Editing the knowledge of app '{self.app_code}' needs edit access "
                f"to the app. Client '{self.client_code}' has read access only.",
                status=403,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_code": self.app_code,
            "client_code": self.client_code,
            "read_chain": list(self.read_chain),
            "base_client": self.base_client,
            "is_override": self.is_override,
            "can_read": self.can_read,
            "can_write": self.can_write,
        }


# ── Security calls ───────────────────────────────────────────────────────


def _base_url() -> str:
    return (settings.SECURITY_SERVICE_URL or settings.GATEWAY_URL or "").rstrip("/")


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
    url = f"{_base_url()}{path}"
    response = await client.get(url, params=params)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


async def _fetch_scope(app_code: str, client_code: str) -> LoreScope:
    """Ask security the three questions. One connection, three calls."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        chain = await _get(
            http, "/api/security/applications/internal/appInheritance",
            {"appCode": app_code, "urlClientCode": client_code, "clientCode": client_code},
        )
        can_read = await _get(
            http, "/api/security/applications/internal/hasReadAccess",
            {"appCode": app_code, "clientCode": client_code},
        )
        can_write = await _get(
            http, "/api/security/applications/internal/hasWriteAccess",
            {"appCode": app_code, "clientCode": client_code},
        )

    # A chain of None means security does not know this app code at all.
    if not chain:
        raise LoreAccessError(f"Unknown app '{app_code}'.", status=404)

    chain = _normalise_chain(chain, client_code)

    is_system = client_code.upper() == _SYSTEM_CLIENT
    return LoreScope(
        app_code=app_code,
        client_code=client_code,
        read_chain=chain,
        can_read=bool(can_read) or is_system,
        can_write=bool(can_write) or is_system,
    )


def _normalise_chain(chain: Sequence[str], client_code: str) -> tuple[str, ...]:
    """Dedupe, drop blanks, and guarantee the caller's own client is last.

    appInheritance returns [owner] when the caller IS the owner and
    [owner, caller] otherwise, but it does no access checking and we should not
    depend on its exact shape: a caller must always be able to see and write
    their own lore, whatever security says about inheritance.
    """
    seen: list[str] = []
    for code in chain:
        if code and code not in seen:
            seen.append(code)
    if client_code in seen:
        seen.remove(client_code)
    seen.append(client_code)
    return tuple(seen)


# ── Public entry point ───────────────────────────────────────────────────


async def resolve_scope(
    auth: Any, app_code: str, *, use_cache: bool = True,
) -> LoreScope:
    """Resolve read/write scope for (this caller, this app).

    `auth` is an AuthContext or anything with `client_code`. The client code is
    read from it and never from a caller-supplied field: that is rule 1.

    Raises LoreAccessError when the app is unknown or security is unreachable.
    Does NOT itself refuse on missing access — call `require_read()` /
    `require_write()` on the result, so a caller can render the difference
    between "you cannot see this" and "you can see it but not change it".
    """
    client_code = (getattr(auth, "client_code", "") or "").strip()
    app_code = (app_code or "").strip()
    if not client_code:
        raise LoreAccessError("The token carries no clientCode.", status=401)
    if not app_code:
        raise LoreAccessError("app_code is required.", status=400)

    key = (client_code, app_code)
    if use_cache:
        hit = _cache.get(key)
        if hit and (time.monotonic() - hit[0]) < _CACHE_TTL_SECONDS:
            return hit[1]

    try:
        scope = await _fetch_scope(app_code, client_code)
    except LoreAccessError:
        raise
    except Exception as exc:  # noqa: BLE001 — network / 5xx from security
        # Fail closed. Lore is knowledge about somebody's application; guessing
        # that the caller probably has access is not an acceptable default.
        logger.warning(
            "lore: could not resolve access for %s/%s: %s", client_code, app_code, exc,
        )
        raise LoreAccessError(
            "Could not verify app access with the security service.", status=503,
        ) from exc

    _cache[key] = (time.monotonic(), scope)
    return scope


def invalidate(client_code: str | None = None, app_code: str | None = None) -> int:
    """Drop cached scopes. Called after an access change, and by tests."""
    if client_code is None and app_code is None:
        n = len(_cache)
        _cache.clear()
        return n
    doomed = [
        k for k in _cache
        if (client_code is None or k[0] == client_code)
        and (app_code is None or k[1] == app_code)
    ]
    for k in doomed:
        _cache.pop(k, None)
    return len(doomed)
