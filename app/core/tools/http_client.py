"""Async HTTP client for calling nocode-saas Gateway APIs.

All agent tools route their API calls through this client.
Auth headers (Authorization, clientCode, appCode) are passed per-call
from the session context — the client itself is stateless.

Usage:
    client = SaasClient("http://localhost:8080")

    result = await client.get("/api/ui/pages", headers=auth_headers, params={"appCode": "myapp"})
    if result.success:
        pages = result.data
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.tools.base import ToolResult
from app.core.tools import draft_registry as drafts

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0  # seconds

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

_SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie"}


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of headers with sensitive values redacted for logging."""
    return {
        k: ("***" if k.lower() in _SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }


class SaasClient:
    """Async HTTP client for the nocode-saas Gateway.

    Creates a shared httpx.AsyncClient on first use and reuses it
    for connection pooling. Must call close() on shutdown.
    """

    def __init__(self, gateway_url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.gateway_url,
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── HTTP methods ────────────────────────────────────────────

    async def get(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        """GET request to the gateway."""
        return await self._request("GET", path, headers=headers, params=params)

    async def post(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        """POST request to the gateway."""
        return await self._request("POST", path, headers=headers, json=json, params=params)

    async def put(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        """PUT request to the gateway."""
        return await self._request("PUT", path, headers=headers, json=json, params=params)

    async def patch(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        """PATCH request to the gateway."""
        return await self._request("PATCH", path, headers=headers, json=json, params=params)

    async def delete(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        """DELETE request to the gateway."""
        return await self._request("DELETE", path, headers=headers, params=params)

    # ── Internal ────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        bypass_drafts: bool = False,
    ) -> ToolResult:
        """Execute an HTTP request and return a structured ToolResult.

        Before anything reaches the network, a call that names an object the user
        has open and unsaved is served from, or held in, their copy instead. See
        `draft_registry` for why the intercept lives here and not in the fifty
        places that mutate.

        `bypass_drafts` is for the one caller that must reach the network anyway:
        the intercept itself, fetching the saved object so the user's unsaved
        difference has something to sit on top of.
        """
        if not bypass_drafts:
            held = await self._serve_from_draft(method, path, json, headers)
            if held is not None:
                return held
            refused = self._refuse_live_only(method, path)
            if refused is not None:
                return refused
            params = self._draft_params(method, path, params)

        client = self._get_client()
        url = path if path.startswith("/") else f"/{path}"

        # Standalone mode: extract path prefix from headers and prepend to URL.
        # The X-Path-Prefix header is set by the webpack proxy and carried in
        # the tool context headers — it is stripped before forwarding to the backend.
        if headers and "X-Path-Prefix" in headers:
            url = headers.pop("X-Path-Prefix") + url

        logger.info(f"→ {method} {self.gateway_url}{url}")

        try:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=json,
                params=params,
            )

            logger.info(f"← {method} {url} → {response.status_code}")

            if response.status_code >= 400:
                error_result = self._error_result(response)
                logger.warning(f"  ERROR: {error_result.error}")
                return error_result

            # Parse response body
            data = None
            if response.content:
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    data = response.json()
                else:
                    data = response.text

            if method.upper() in _MUTATING:
                await self._announce_real_write(method, path, json, data, params)

            return ToolResult(
                success=True,
                data=data,
                summary=f"{method} {url} → {response.status_code}",
            )

        except httpx.TimeoutException:
            logger.warning(f"← TIMEOUT: {method} {url} (after {self.timeout}s)")
            return ToolResult(
                success=False,
                error=f"Request timed out after {self.timeout}s: {method} {url}",
            )
        except httpx.ConnectError:
            logger.error(f"← CONNECT_ERROR: {method} {url} (gateway: {self.gateway_url})")
            return ToolResult(
                success=False,
                error=f"Cannot connect to gateway at {self.gateway_url}. Is nocode-saas running?",
            )
        except Exception as e:
            logger.exception(f"← EXCEPTION: {method} {url}")
            return ToolResult(
                success=False,
                error=f"Unexpected error: {type(e).__name__}: {e}",
            )

    # ── Routing writes to the server's draft ────────────────────

    # Verbs that read or replace a whole object, which are the two the draft
    # surface understands. A collection POST is a CREATE and is deliberately
    # absent: the backend would still write a real live document, and a Draft
    # row keyed on a name with no live counterpart has nothing to publish over.
    _DRAFTABLE_VERBS = {"GET", "PUT"}

    def _draft_params(
        self, method: str, path: str, params: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Add `?draft=true` to a whole-object read or write while drafting.

        Here rather than at the twenty-four call sites that PUT, for the reason
        the intercept below is also here: twenty-four edits are twenty-four
        chances to miss one, and a missed one writes LIVE while the agent tells
        the user their change is waiting for review. Confidently wrong about
        where someone's work went is the exact failure the draft surface exists
        to remove, so it must not be reintroduced by an unconverted tool.

        Only a call that names one object is flagged. A collection listing is
        left alone: the draft surface has no opinion about lists, and a flag the
        backend ignores would read as support that is not there.
        """
        if not drafts.drafting.get():
            return params
        if method.upper() not in self._DRAFTABLE_VERBS:
            return params
        kind, obj_id, sub = drafts.DraftRegistry.resolve(path)
        if not drafts.is_draftable(kind) or not obj_id or sub:
            return params
        out = dict(params or {})
        out.setdefault("draft", "true")
        return out

    def _refuse_live_only(self, method: str, path: str) -> ToolResult | None:
        """Refuse a part-of-an-object write that cannot be drafted.

        `PATCH /pages/{id}/components/{key}` and `PUT /pages/{id}/events/{name}`
        take no draft parameter: they mutate the stored document in place, and
        there is no draft counterpart to send them to. Calling one while the
        turn is drafting publishes the change to everyone the moment it is
        written, while every other write in the same turn waits for review.

        The page tools already avoid these two routes when drafting and do a
        whole-document read-modify-write instead. This is the guard for
        everything else: a tool that has not been converted, or a route added
        later, fails loudly here rather than leaking silently.
        """
        if not drafts.drafting.get():
            return None
        if method.upper() not in _MUTATING:
            return None
        kind, obj_id, sub = drafts.DraftRegistry.resolve(path)
        if not sub or not drafts.is_draftable(kind) or not obj_id:
            return None

        logger.warning("refused %s %s: no draft counterpart for this partial write", method, path)
        return ToolResult(
            success=False,
            error=(
                f"{method} {path} writes part of an object directly and has no draft "
                f"counterpart, so it would go live while this turn's other changes wait "
                f"for review. Use a tool that reads the whole object and saves it back."
            ),
        )

    # ── The open-draft intercept ────────────────────────────────

    async def _serve_from_draft(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> ToolResult | None:
        """Serve a read from, or hold a write in, the user's open copy.

        Returns None to mean "not ours, go to the network", which is the answer
        for everything except a read or an update of an object the client declared
        open. Creates and deletes are deliberately never held: a create has no id
        yet and everything downstream needs one, and a delete has nothing to show
        for itself in a draft.
        """
        reg = drafts.registry()
        if reg is None:
            return None

        kind, obj_id, sub = reg.resolve(path)
        if kind is None:
            return None

        verb = method.upper()

        # The server will draft this one, so the browser is the wrong place to
        # keep it: the write goes to the draft (see `_draft_params`) and the tab
        # showing it refetches. Reads follow the same surface, which means the
        # agent stops seeing edits the user has typed and not saved. That cost
        # is deliberate and is the same trade the page editor already makes:
        # saving is cheap now, because it saves to the draft, so the answer is
        # to save first rather than to hold the write.
        if drafts.is_draftable(kind) and drafts.drafting.get():
            return None

        if obj_id and sub:
            return await self._serve_sub_resource(reg, kind, obj_id, sub, verb, path, body, headers)

        if obj_id:
            entry = reg.entry(kind, obj_id)
            if entry is None:
                return None
            if verb in ("GET", "PUT", "PATCH"):
                await self._hydrate(reg, entry, path, headers)
            if verb == "GET":
                logger.info(f"⇢ held read  {kind}:{obj_id} served from the user's draft")
                # A copy, so a tool that mutates what it read and then fails
                # before saving cannot leave its half-change in the draft.
                return ToolResult(
                    success=True,
                    data=drafts.snapshot(entry.doc),
                    summary=f"GET {path} → open draft",
                )
            if verb in ("PUT", "PATCH") and isinstance(body, dict):
                await reg.stage(entry, body)
                logger.info(f"⇢ held write {kind}:{obj_id} kept in the user's draft, not saved")
                return ToolResult(
                    success=True,
                    data=entry.doc,
                    summary=f"{verb} {path} → held in the open draft (not saved)",
                )
            return None

        # A collection POST is normally a create, but it is also how an inherited
        # page is saved: save_page strips the id and POSTs so the backend forks an
        # override for this client. Appbuilder's own pages are SYSTEM-owned, so
        # that is the ordinary path there, and treating it as a create would send
        # every held edit straight to the database.
        if verb == "POST" and isinstance(body, dict) and not body.get("id"):
            entry = self._match_by_name(reg, kind, body)
            if entry is not None:
                await self._hydrate(reg, entry, f"{path.rstrip('/')}/{entry.id}", headers)
                await reg.stage(entry, body)
                logger.info(
                    f"⇢ held write {kind}:{entry.id} (override save) kept in the user's draft"
                )
                return ToolResult(
                    success=True,
                    data=entry.doc,
                    summary=f"POST {path} → held in the open draft (not saved)",
                )
        return None

    # Surgical endpoints that write PART of an object, and how to apply each one
    # to a draft instead. Keyed by (kind, first sub-segment).
    #
    # These are the exception to "one choke point handles everything", and they
    # earned it the hard way: the resolver used to return nothing for a path with
    # an extra segment, so patch_component_props — the most-used editing tool in
    # the service — wrote straight to the database while the agent reported the
    # change as unsaved. Anything in this shape that is NOT handled below now
    # fails closed rather than leaking.

    async def _serve_sub_resource(
        self,
        reg: "drafts.DraftRegistry",
        kind: str,
        obj_id: str,
        sub: str,
        verb: str,
        path: str,
        body: Any,
        headers: dict[str, str] | None,
    ) -> ToolResult | None:
        """Apply a partial write to the draft, or refuse it."""
        entry = reg.entry(kind, obj_id)
        if entry is None:
            return None  # Not open. Normal behaviour, straight to the platform.
        if verb == "GET":
            return None  # Reads of a sub-resource are harmless; let them through.

        await self._hydrate(reg, entry, f"{path.split('/' + sub)[0]}", headers)
        parts = sub.split("/")
        head = parts[0]
        doc = drafts.snapshot(entry.doc)

        # PATCH /api/ui/pages/{id}/components/{key} — body {componentData, ...}
        if kind == "page" and head == "components" and len(parts) == 2 and isinstance(body, dict):
            comp = body.get("componentData")
            if isinstance(comp, dict):
                doc.setdefault("componentDefinition", {})[parts[1]] = comp
                await reg.stage(entry, doc)
                logger.info(f"⇢ held write {kind}:{obj_id} component {parts[1]} kept in the draft")
                return ToolResult(success=True, data=comp,
                                  summary=f"{verb} {path} → held in the open draft (not saved)")

        # PUT /api/ui/pages/{id}/events/{key} — body {definition, ...}
        if kind == "page" and head == "events" and len(parts) == 2 and isinstance(body, dict):
            definition = body.get("definition")
            if isinstance(definition, dict):
                doc.setdefault("eventFunctions", {})[parts[1]] = definition
                await reg.stage(entry, doc)
                logger.info(f"⇢ held write {kind}:{obj_id} event {parts[1]} kept in the draft")
                return ToolResult(success=True, data=definition,
                                  summary=f"{verb} {path} → held in the open draft (not saved)")

        # Unknown partial write against an object the user is reviewing. Refusing
        # is the only safe answer: letting it through would save behind the user's
        # back, and the agent would report it as pending. The error names the way
        # out so the model can retry through a whole-document tool.
        logger.warning(f"refused {verb} {path}: no draft-safe handler for this partial write")
        return ToolResult(
            success=False,
            error=(
                f"'{entry.name or obj_id}' is open and unsaved in the user's editor, so it "
                f"cannot be edited through the partial-update endpoint {path}. Use a tool "
                f"that reads the whole object and saves it back (for a page: "
                f"add_components / update_component / replace_page_definition)."
            ),
        )

    async def _hydrate(
        self,
        reg: "drafts.DraftRegistry",
        entry: "drafts.DraftEntry",
        path: str,
        headers: dict[str, str] | None,
    ) -> None:
        """Fetch the saved object once, so the user's unsaved difference has a base.

        Deferred to the first touch rather than done when the turn starts, because
        most turns never go near the open object and a 1.4MB page is not worth
        fetching on the chance that this one does.
        """
        if entry.loaded:
            return
        saved = None
        # Fetch when the caller sent a difference to apply, and also when it sent
        # nothing at all: a clean object is byte-for-byte the saved one, so the
        # client declares it without paying to upload a copy we can read here.
        if entry.overlay is not None or not entry.doc:
            probe = await self._request(
                "GET", path, headers=dict(headers or {}), bypass_drafts=True,
            )
            if probe.success and isinstance(probe.data, dict):
                saved = probe.data
            else:
                logger.warning(
                    f"draft {entry.kind}:{entry.id} could not read the saved copy "
                    f"({probe.error or 'no body'}); using the caller's overlay alone"
                )
        reg.hydrate(entry, saved)

    @staticmethod
    def _match_by_name(
        reg: "drafts.DraftRegistry", kind: str, body: dict,
    ) -> "drafts.DraftEntry | None":
        """Find the open draft an id-less save is aimed at.

        Safe as an identity test because the collision it would need cannot occur:
        an object open in the editor already exists, so a create under the same
        name and app is not something anyone can ask for. The document-shape check
        is belt and braces, so that an action payload that happens to carry a
        `name` field cannot be mistaken for a save.
        """
        name = body.get("name")
        if not name or not (body.get("appCode") or body.get("clientCode")):
            return None
        app_code = body.get("appCode") or ""
        for entry in reg.entries():
            if entry.kind != kind or entry.name != name:
                continue
            if app_code and entry.app_code and entry.app_code != app_code:
                continue
            return entry
        return None

    async def _announce_real_write(
        self, method: str, path: str, body: Any, data: Any,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Tell the client about a write that actually reached the database.

        This is the other half of the rule, and the half that is easy to forget:
        objects the user does not have open are still fair game, and the client
        needs to know so it can refresh whatever is showing them. The sharpest
        case is a theme edited from the page editor, which saves app-wide and must
        still appear on the canvas.

        Whether the write went to the draft surface is carried explicitly. The
        client cannot infer it: a draft write and a live write are the same verb
        on the same path, and an editor that refetched the wrong surface would
        show the user a page that does not contain the change it was just told
        about.
        """
        reg = drafts.announcer()
        if reg is None or reg.stream is None:
            return

        kind, obj_id, _sub = reg.resolve(path)
        if kind is None:
            return

        payload = body if isinstance(body, dict) else {}
        returned = data if isinstance(data, dict) else {}
        await reg.stream.emit_object_changed(
            kind=kind,
            obj_id=obj_id or str(returned.get("id") or payload.get("id") or ""),
            name=payload.get("name") or returned.get("name") or "",
            app_code=payload.get("appCode") or returned.get("appCode") or "",
            operation=method.upper(),
            draft=str((params or {}).get("draft", "")).lower() == "true",
        )

    def _error_result(self, response: httpx.Response) -> ToolResult:
        """Build a ToolResult from an HTTP error response."""
        try:
            body = response.json()
            # nocode-saas error format: {"message": "...", "data": {...}}
            message = body.get("message", response.text[:500])
        except Exception:
            message = response.text[:500] if response.text else f"HTTP {response.status_code}"

        return ToolResult(
            success=False,
            error=f"HTTP {response.status_code}: {message}",
        )
