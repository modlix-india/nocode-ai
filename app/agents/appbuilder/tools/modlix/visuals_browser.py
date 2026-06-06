"""Browser-driven visual tools — screenshot_page + drive_page + session mgmt.

Heavyweight Playwright-dependent tools, separated from visuals.py so:
  - import-time cost is contained (Playwright pulls in a lot of code)
  - environments without Playwright still get the rest of the visual surface
  - the persistent BrowserSession state stays local to one module

Identity model (ported from modlix-mcp):
  1. anonymous=True   → no auth; renders pages exactly as logged-out viewer
  2. one-shot creds   → username + password on the call; runs /authenticate,
                         injects the resulting token into the headless browser's
                         localStorage before navigation
  3. session app_user → context['get_app_user_token']() resolves the
                         end-user token from ChatRequest.app_user
                         (token-or-credentials). Right for QA'ing the
                         customer's app as a real end user.
  4. anonymous fallback if no identity is available — better than failing
     when capturing a public marketing/login page.

NOTE: We deliberately don't fall back to the caller's developer JWT here.
The dev JWT is for the AUTHORING gateway; rendering pages as the builder
identity isn't what these tools are for. Pass app_user.{token | username,
password} on the chat request for an authenticated render.
"""

from __future__ import annotations

import asyncio
import base64
import json as _json
import logging
import time as _time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

# nocode-ui localStorage keys for the auth token (index.tsx + ssoModule.ts).
# Token value is JSON-stringified; expiry is raw Unix seconds (no JSON.stringify).
_LS_TOKEN_KEY = "AuthToken"
_LS_EXPIRY_KEY = "AuthTokenExpiry"

# Modlix URL path segment separating (app, client) prefix from page name + parts.
_PAGE_PATH_SEGMENT = "/page/"

# Idle TTL for persistent browser sessions. After 10 min of inactivity a
# session is auto-closed on the next drive_page call (reaper runs lazily).
_SESSION_IDLE_TTL_SECONDS = 600


# Param description constants.
_DESC_APP_CODE = "appCode; defaults to session"
_DESC_CLIENT_CODE = "clientCode; defaults to session"
_DESC_PAGE_NAME = "Page name to render (e.g. 'homeTwo')"


# ── BrowserSession + registry ────────────────────────────────────────────


@dataclass
class BrowserSession:
    """One persistent Playwright session surviving across drive_page calls."""
    session_id: str
    playwright: Any
    browser: Any
    context: Any
    page: Any
    current_page_name: str | None = None
    current_app_code: str | None = None
    current_client_code: str | None = None
    last_used: float = field(default_factory=_time.monotonic)
    capture_console: bool = True
    capture_network: bool = True
    console_buf: list[str] = field(default_factory=list)
    network_log: list[dict[str, Any]] = field(default_factory=list)
    network_starts: dict[int, float] = field(default_factory=dict)


_sessions: dict[str, BrowserSession] = {}


async def _close_session(sess: BrowserSession) -> None:
    """Best-effort cleanup; swallows errors."""
    try:
        await sess.browser.close()
    except Exception:  # noqa: BLE001
        logger.exception("error closing browser for session %s", sess.session_id)
    try:
        await sess.playwright.stop()
    except Exception:  # noqa: BLE001
        logger.exception("error stopping playwright for session %s", sess.session_id)


async def _reap_idle_sessions() -> list[str]:
    """Close sessions idle past the TTL. Returns session_ids closed."""
    now = _time.monotonic()
    stale = [sid for sid, s in _sessions.items() if now - s.last_used > _SESSION_IDLE_TTL_SECONDS]
    for sid in stale:
        s = _sessions.pop(sid, None)
        if s is not None:
            await _close_session(s)
            logger.info("Reaped idle session %s (idle %.1fs)", sid, now - s.last_used)
    return stale


# ── Identity ─────────────────────────────────────────────────────────────


async def _login_one_shot(gateway: str, username: str, password: str) -> tuple[str | None, int | None, str | None]:
    """POST /api/security/authenticate. Returns (token, expiry_sec, error)."""
    url = gateway.rstrip("/") + "/api/security/authenticate"
    payload = {"userName": username, "password": password, "rememberMe": False}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json=payload)
    except httpx.HTTPError as e:
        return None, None, f"login HTTP error: {e}"
    if r.status_code >= 400:
        return None, None, f"login {r.status_code}: {r.text[:200]}"
    try:
        body = r.json()
    except ValueError:
        return None, None, f"login response not JSON: {r.text[:120]}"
    token = body.get("accessToken") or body.get("AuthToken") or body.get("token")
    expiry = body.get("accessTokenExpiryAt") or body.get("AuthTokenExpiry")
    if not token:
        return None, None, f"login response missing token: keys={list(body.keys())}"
    if not expiry:
        expiry = int(_time.time()) + 3600
    return token, int(expiry), None


async def _resolve_identity(
    params: dict[str, Any], context: dict[str, Any],
) -> tuple[tuple[str, int] | None, str | None]:
    """Returns ((token, expirySec) | None, error). None token = anonymous.

    Precedence:
      1. params.anonymous=True → anonymous
      2. params.username + params.password → one-shot login
      3. session.get_app_user_token() (via context callable) → cached app-user
      4. anonymous fallback (no dev-token fallback — see module docstring)
    """
    if bool(params.get("anonymous")):
        return None, None
    username = params.get("username")
    password = params.get("password")
    from app.config import settings
    gateway = settings.GATEWAY_URL
    if username and password:
        tok, exp, err = await _login_one_shot(gateway, username, password)
        if err:
            return None, err
        return (tok, exp or int(_time.time()) + 3600), None
    get_app_user_token = context.get("get_app_user_token")
    if callable(get_app_user_token):
        try:
            tok = await get_app_user_token()
            return (tok, int(_time.time()) + 3600), None
        except RuntimeError:
            # No app-user creds configured — fall through to anonymous.
            pass
    return None, None


def _build_url(app_code: str, client_code: str, page_name: str,
               path_segments: list[str] | None, query: str | None) -> str:
    from app.config import settings
    gateway = settings.GATEWAY_URL.rstrip("/")
    url = f"{gateway}/{app_code}/{client_code}{_PAGE_PATH_SEGMENT}{page_name}"
    if path_segments:
        url += "/" + "/".join(str(s).strip("/") for s in path_segments if s)
    if query:
        url += "?" + str(query).lstrip("?")
    return url


# ── Capture wiring ───────────────────────────────────────────────────────


def _is_data_call(req) -> bool:
    return req.resource_type in ("xhr", "fetch", "document", "eventsource", "websocket")


def _wire_session_capture(sess: BrowserSession) -> None:
    """Attach console + network listeners to sess.page's events."""
    page = sess.page
    if sess.capture_console:
        def _on_console(msg, _buf=sess.console_buf):
            _buf.append(f"[{msg.type.upper()}] {msg.text}")
            if len(_buf) > 200:
                del _buf[: len(_buf) - 200]
        page.on("console", _on_console)
        page.on("pageerror", lambda exc, _buf=sess.console_buf: _buf.append(f"[PAGEERROR] {exc}"))
        page.on(
            "requestfailed",
            lambda req, _buf=sess.console_buf: _buf.append(f"[NETFAIL] {req.method} {req.url} — {req.failure}"),
        )

    if sess.capture_network:
        def _on_request(req, _starts=sess.network_starts):
            if _is_data_call(req):
                _starts[id(req)] = _time.monotonic()

        def _on_response(resp, _log=sess.network_log, _starts=sess.network_starts):
            req = resp.request
            if not _is_data_call(req):
                return
            started = _starts.pop(id(req), None)
            ms = int((_time.monotonic() - started) * 1000) if started else -1
            _log.append({"method": req.method, "status": resp.status, "ms": ms, "type": req.resource_type, "url": req.url})
            if len(_log) > 300:
                del _log[: len(_log) - 300]

        page.on("request", _on_request)
        page.on("response", _on_response)


async def _new_session(
    session_id: str, app_code: str, client_code: str, page_name: str,
    identity: tuple[str, int] | None,
    width: int, height: int,
    capture_console: bool, capture_network: bool,
) -> tuple[BrowserSession | None, str | None]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None, "playwright not installed; pip install playwright && python -m playwright install chromium"
    pw = await async_playwright().start()
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(viewport={"width": width, "height": height}, ignore_https_errors=True)
    if identity is not None:
        tok, exp = identity
        script = (
            f"window.localStorage.setItem({_json.dumps(_LS_TOKEN_KEY)}, JSON.stringify({_json.dumps(tok)}));"
            f"window.localStorage.setItem({_json.dumps(_LS_EXPIRY_KEY)}, {_json.dumps(str(exp))});"
        )
        await ctx.add_init_script(script)
    page = await ctx.new_page()
    sess = BrowserSession(
        session_id=session_id, playwright=pw, browser=browser, context=ctx, page=page,
        current_page_name=page_name, current_app_code=app_code, current_client_code=client_code,
        capture_console=capture_console, capture_network=capture_network,
    )
    _wire_session_capture(sess)
    return sess, None


# ── screenshot_page ──────────────────────────────────────────────────────


async def _execute_screenshot_page(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    if not page_name:
        return ToolResult(success=False, error="`page_name` is required")
    ac = params.get("app_code") or context.get("app_code", "")
    cc = params.get("client_code") or context.get("client_code", "") or ""
    if not ac:
        return ToolResult(success=False, error="No appCode set. Pass `app_code` or set it on the chat request.")

    identity, idl_err = await _resolve_identity(params, context)
    if idl_err:
        return ToolResult(success=False, error=idl_err)

    url = _build_url(ac, cc, page_name, params.get("path_segments"), params.get("query"))

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ToolResult(success=False, error="playwright not installed; pip install playwright && python -m playwright install chromium")

    width = int(params.get("width") or 1440)
    height = int(params.get("height") or 900)
    full_page = bool(params.get("full_page", True))
    wait_ms = int(params.get("wait_ms") or 2000)
    capture_console = bool(params.get("capture_console", False))
    capture_network = bool(params.get("capture_network", False))
    effective_capture_console = capture_console or capture_network

    console_buf: list[str] = []
    network_log: list[dict[str, Any]] = []
    network_starts: dict[int, float] = {}

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx = await browser.new_context(viewport={"width": width, "height": height}, ignore_https_errors=True)
            if identity is not None:
                tok, exp = identity
                script = (
                    f"window.localStorage.setItem({_json.dumps(_LS_TOKEN_KEY)}, JSON.stringify({_json.dumps(tok)}));"
                    f"window.localStorage.setItem({_json.dumps(_LS_EXPIRY_KEY)}, {_json.dumps(str(exp))});"
                )
                await ctx.add_init_script(script)
            page = await ctx.new_page()
            if effective_capture_console:
                page.on("console", lambda msg: (console_buf.append(f"[{msg.type.upper()}] {msg.text}") or None) if len(console_buf) < 200 else None)
                page.on("pageerror", lambda exc: console_buf.append(f"[PAGEERROR] {exc}"))
                page.on("requestfailed", lambda req: console_buf.append(f"[NETFAIL] {req.method} {req.url} — {req.failure}"))
            if capture_network:
                def _on_req(req):
                    if _is_data_call(req):
                        network_starts[id(req)] = _time.monotonic()
                def _on_resp(resp):
                    req = resp.request
                    if not _is_data_call(req):
                        return
                    started = network_starts.pop(id(req), None)
                    ms = int((_time.monotonic() - started) * 1000) if started else -1
                    network_log.append({"method": req.method, "status": resp.status, "ms": ms, "type": req.resource_type, "url": req.url})
                    if len(network_log) > 300:
                        del network_log[: len(network_log) - 300]
                page.on("request", _on_req)
                page.on("response", _on_resp)
            try:
                await page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception:  # noqa: BLE001
                pass  # networkidle may never fire on long-poll apps; continue
            if params.get("wait_for_selector"):
                try:
                    await page.wait_for_selector(params["wait_for_selector"], timeout=10000)
                except Exception as e:  # noqa: BLE001
                    return ToolResult(success=False, error=f"selector {params['wait_for_selector']!r} not found within 10s: {e}")
            if wait_ms:
                await page.wait_for_timeout(wait_ms)
            for sel in params.get("click_selectors") or []:
                try:
                    await page.locator(sel).first.click(timeout=5000)
                    await page.wait_for_timeout(400)
                except Exception as e:  # noqa: BLE001
                    return ToolResult(success=False, error=f"click_selector {sel!r} failed: {type(e).__name__}: {e}")
            if params.get("hover_selector"):
                try:
                    await page.locator(params["hover_selector"]).first.hover(timeout=3000)
                    await page.wait_for_timeout(250)
                except Exception as e:  # noqa: BLE001
                    return ToolResult(success=False, error=f"hover_selector {params['hover_selector']!r} failed: {type(e).__name__}: {e}")
            png = await page.screenshot(full_page=full_page, type="png")
            await browser.close()
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"render error: {type(e).__name__}: {e}")

    encoded = base64.b64encode(png).decode("ascii")
    parts: list[str] = [
        f"Screenshot of {url} captured ({len(png):,} bytes).",
        f"Embed via: <img src=\"data:image/png;base64,{encoded[:32]}...\">  (base64 stored in result.data['image_base64'])",
    ]
    if effective_capture_console:
        log_text = "\n".join(console_buf) if console_buf else "(no console messages captured)"
        parts.append(f"\nConsole ({len(console_buf)} messages):\n{log_text}")
    if capture_network:
        if network_log:
            rows = [f"  {r['method']:<6} {r['status']:<3} {r['ms']:>5}ms  [{r['type']}]  {r['url']}" for r in network_log]
            parts.append(f"\nNetwork ({len(network_log)} requests):\n" + "\n".join(rows))
        else:
            parts.append("\nNetwork: (no data-call requests captured)")
    return ToolResult(
        success=True,
        summary="\n".join(parts),
        data={"image_base64": encoded, "image_mime": "image/png", "url": url, "console": console_buf, "network": network_log},
    )


screenshot_page_tool = ToolDefinition(
    name="screenshot_page",
    description=(
        "Render a Modlix page in headless Chromium and capture a PNG screenshot. "
        "Returns base64-encoded PNG in result.data['image_base64']. Identity comes "
        "from (in order): anonymous=true → username+password → session app_user → "
        "anonymous fallback. Optional capture_console + capture_network buffers "
        "for debugging."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description=_DESC_PAGE_NAME),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="username", type="string", required=False, description="One-shot end-user login (with password)"),
        ToolParameter(name="password", type="string", required=False, description="Password for the username login"),
        ToolParameter(name="anonymous", type="boolean", required=False, default=False, description="Skip auth — public/login page capture"),
        ToolParameter(name="width", type="integer", required=False, default=1440, description="Viewport width (CSS px, 320-3840)"),
        ToolParameter(name="height", type="integer", required=False, default=900, description="Viewport height (CSS px, 320-2160)"),
        ToolParameter(name="full_page", type="boolean", required=False, default=True, description="Capture entire scroll height"),
        ToolParameter(name="path_segments", type="array", required=False, description="Path parts after /page/<name>/", items={"type": "string"}),
        ToolParameter(name="wait_for_selector", type="string", required=False, description="CSS selector to wait for before snapping"),
        ToolParameter(name="wait_ms", type="integer", required=False, default=2000, description="Extra wait after load (ms, max 60000)"),
        ToolParameter(name="hover_selector", type="string", required=False, description="Hover this element before snapping (triggers :hover)"),
        ToolParameter(name="query", type="string", required=False, description="URL query string (no leading '?')"),
        ToolParameter(name="click_selectors", type="array", required=False, description="Click these in order before snapping", items={"type": "string"}),
        ToolParameter(name="capture_console", type="boolean", required=False, default=False, description="Capture browser console + page errors"),
        ToolParameter(name="capture_network", type="boolean", required=False, default=False, description="Capture XHR/fetch requests (implies capture_console)"),
    ],
    execute=_execute_screenshot_page,
)


# ── drive_page action dispatch ───────────────────────────────────────────


_ACTION_TYPES: tuple[str, ...] = (
    "wait", "click", "dblclick", "hover", "type", "press", "clear",
    "scroll", "select", "check", "uncheck", "screenshot",
    "goto", "back", "forward", "reload",
    "wait_for_url", "wait_for_response",
    "read_text", "read_attr", "read_value", "read_count",
    "eval", "set_viewport", "drag",
)


async def _do_scroll(page, action: dict[str, Any]) -> dict[str, Any]:
    sel = action.get("selector")
    to = action.get("to")
    by = action.get("by")
    if sel:
        loc = page.locator(sel).first
        await loc.scroll_into_view_if_needed(timeout=5000)
        return {"ok": True, "type": "scroll", "selector": sel}
    if to == "top":
        await page.evaluate("window.scrollTo(0, 0)")
        return {"ok": True, "type": "scroll", "to": "top"}
    if to == "bottom":
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        return {"ok": True, "type": "scroll", "to": "bottom"}
    if by is not None:
        await page.evaluate(f"window.scrollBy(0, {int(by)})")
        return {"ok": True, "type": "scroll", "by": by}
    return {"ok": False, "type": "scroll", "error": "scroll needs selector | to=top|bottom | by=<px>"}


async def _run_action(page, base_url: str, action: dict[str, Any]) -> dict[str, Any]:
    """Run one action. Never raises — errors captured in result so the log sees them."""
    atype = action.get("type")
    if atype not in _ACTION_TYPES:
        return {"ok": False, "type": atype, "error": f"unknown action type {atype!r}; valid: {_ACTION_TYPES}"}
    try:
        if atype == "wait":
            sel = action.get("selector")
            if sel:
                await page.wait_for_selector(sel, timeout=action.get("timeout", 10000))
            else:
                await page.wait_for_timeout(action.get("ms", 500))
            return {"ok": True, "type": atype}
        if atype == "click":
            await page.locator(action["selector"]).first.click(
                timeout=action.get("timeout", 5000),
                button=action.get("button", "left"),
                click_count=action.get("click_count", 1),
            )
            return {"ok": True, "type": atype, "selector": action["selector"]}
        if atype == "dblclick":
            await page.locator(action["selector"]).first.dblclick(timeout=action.get("timeout", 5000))
            return {"ok": True, "type": atype, "selector": action["selector"]}
        if atype == "hover":
            await page.locator(action["selector"]).first.hover(timeout=action.get("timeout", 5000))
            return {"ok": True, "type": atype, "selector": action["selector"]}
        if atype == "type":
            loc = page.locator(action["selector"]).first
            if action.get("clear"):
                await loc.fill("")
            await loc.type(action["text"], delay=action.get("delay", 0))
            return {"ok": True, "type": atype, "selector": action["selector"]}
        if atype == "press":
            await page.keyboard.press(action["key"])
            return {"ok": True, "type": atype, "key": action["key"]}
        if atype == "clear":
            await page.locator(action["selector"]).first.fill("")
            return {"ok": True, "type": atype, "selector": action["selector"]}
        if atype == "scroll":
            return await _do_scroll(page, action)
        if atype == "select":
            loc = page.locator(action["selector"]).first
            if "value" in action:
                values = await loc.select_option(value=action["value"])
            elif "label" in action:
                values = await loc.select_option(label=action["label"])
            elif "index" in action:
                values = await loc.select_option(index=action["index"])
            else:
                return {"ok": False, "type": atype, "error": "select needs value, label, or index"}
            return {"ok": True, "type": atype, "selected": values}
        if atype == "check":
            await page.locator(action["selector"]).first.check()
            return {"ok": True, "type": atype, "selector": action["selector"]}
        if atype == "uncheck":
            await page.locator(action["selector"]).first.uncheck()
            return {"ok": True, "type": atype, "selector": action["selector"]}
        if atype == "goto":
            target = action.get("path") or action.get("url") or ""
            if not target.startswith("http"):
                target = base_url.rsplit(_PAGE_PATH_SEGMENT, 1)[0] + _PAGE_PATH_SEGMENT + target.lstrip("/")
            await page.goto(target, wait_until="networkidle", timeout=20000)
            return {"ok": True, "type": atype, "url": target}
        if atype == "back":
            await page.go_back(wait_until="networkidle")
            return {"ok": True, "type": atype}
        if atype == "forward":
            await page.go_forward(wait_until="networkidle")
            return {"ok": True, "type": atype}
        if atype == "reload":
            await page.reload(wait_until="networkidle")
            return {"ok": True, "type": atype}
        if atype == "wait_for_url":
            await page.wait_for_url(action["pattern"], timeout=action.get("timeout", 10000))
            return {"ok": True, "type": atype, "url": page.url}
        if atype == "wait_for_response":
            resp = await page.wait_for_event(
                "response",
                predicate=lambda r, p=action["url_pattern"]: p in r.url,
                timeout=action.get("timeout", 10000),
            )
            return {"ok": True, "type": atype, "url": resp.url, "status": resp.status}
        if atype == "read_text":
            text = await page.locator(action["selector"]).first.inner_text(timeout=action.get("timeout", 5000))
            return {"ok": True, "type": atype, "selector": action["selector"], "text": text}
        if atype == "read_attr":
            val = await page.locator(action["selector"]).first.get_attribute(action["attr"])
            return {"ok": True, "type": atype, "selector": action["selector"], "attr": action["attr"], "value": val}
        if atype == "read_value":
            val = await page.locator(action["selector"]).first.input_value(timeout=action.get("timeout", 5000))
            return {"ok": True, "type": atype, "selector": action["selector"], "value": val}
        if atype == "read_count":
            n = await page.locator(action["selector"]).count()
            return {"ok": True, "type": atype, "selector": action["selector"], "count": n}
        if atype == "eval":
            result = await page.evaluate(action["js"])
            try:
                _json.dumps(result, default=str)
                return {"ok": True, "type": atype, "result": result}
            except (TypeError, ValueError):
                return {"ok": True, "type": atype, "result": repr(result)[:500]}
        if atype == "set_viewport":
            await page.set_viewport_size({"width": action["width"], "height": action["height"]})
            return {"ok": True, "type": atype, "width": action["width"], "height": action["height"]}
        if atype == "drag":
            await page.locator(action["from_selector"]).first.drag_to(page.locator(action["to_selector"]).first)
            return {"ok": True, "type": atype}
        if atype == "screenshot":
            # Marker — caller takes the actual screenshot since PNG bytes need to land in result.data
            return {"ok": True, "type": atype, "_capture_screenshot": True,
                    "label": action.get("label"), "full_page": bool(action.get("full_page", False)),
                    "selector": action.get("selector")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "type": atype, "error": f"{type(e).__name__}: {e}"}
    return {"ok": False, "type": atype, "error": "action handler missing — file a bug"}


async def _capture_action_screenshot(page, result: dict[str, Any]) -> bytes | None:
    """Take the screenshot a screenshot-marker action requested."""
    try:
        sel = result.get("selector")
        if sel:
            return await page.locator(sel).first.screenshot(type="png")
        return await page.screenshot(full_page=bool(result.get("full_page")), type="png")
    except Exception:  # noqa: BLE001
        return None


# ── drive_page ───────────────────────────────────────────────────────────


async def _execute_drive_page(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    actions = params.get("actions") or []
    if not page_name:
        return ToolResult(success=False, error="`page_name` is required")
    if not isinstance(actions, list) or not actions:
        return ToolResult(success=False, error="`actions` (non-empty list) is required")
    ac = params.get("app_code") or context.get("app_code", "")
    cc = params.get("client_code") or context.get("client_code", "") or ""
    if not ac:
        return ToolResult(success=False, error="No appCode set. Pass `app_code` or set it on the chat request.")

    await _reap_idle_sessions()

    session_id = (params.get("session_id") or "").strip()
    width = int(params.get("width") or 1440)
    height = int(params.get("height") or 900)
    capture_console = bool(params.get("capture_console", False))
    capture_network = bool(params.get("capture_network", False))
    final_screenshot_mode = (params.get("final_screenshot") or "viewport").strip()  # 'viewport'|'full'|'none'

    identity, idl_err = await _resolve_identity(params, context)
    if idl_err:
        return ToolResult(success=False, error=idl_err)

    url = _build_url(ac, cc, page_name, params.get("path_segments"), params.get("query"))

    # Resolve session: reuse if session_id matches a live session AND
    # (app, client) match; else create a new persistent session.
    sess: BrowserSession | None = None
    created_session = False
    if session_id and session_id in _sessions:
        candidate = _sessions[session_id]
        if (candidate.current_app_code == ac and candidate.current_client_code == cc):
            sess = candidate
            # clear capture buffers for fresh call
            candidate.console_buf.clear()
            candidate.network_log.clear()
            candidate.network_starts.clear()
            candidate.last_used = _time.monotonic()
    if sess is None:
        new_sid = session_id or f"sess_{_time.time_ns():x}"
        sess, err = await _new_session(
            new_sid, ac, cc, page_name, identity, width, height,
            capture_console, capture_network,
        )
        if sess is None:
            return ToolResult(success=False, error=err)
        _sessions[new_sid] = sess
        created_session = True

    page = sess.page
    # If session.current_page_name doesn't match or we just created, navigate.
    if created_session or sess.current_page_name != page_name:
        try:
            await page.goto(url, wait_until="networkidle", timeout=20000)
        except Exception:  # noqa: BLE001
            pass
        sess.current_page_name = page_name

    # Run actions
    action_log: list[dict[str, Any]] = []
    screenshots: list[tuple[str, str]] = []  # (label, base64_png)
    for i, action in enumerate(actions):
        if not isinstance(action, dict) or "type" not in action:
            action_log.append({"ok": False, "type": "?", "error": f"action #{i} missing 'type' field"})
            continue
        result = await _run_action(page, url, action)
        if result.get("_capture_screenshot"):
            png = await _capture_action_screenshot(page, result)
            if png is not None:
                label = result.get("label") or f"action_{i}"
                screenshots.append((label, base64.b64encode(png).decode("ascii")))
                result["screenshot_bytes"] = len(png)
            result.pop("_capture_screenshot", None)
        action_log.append(result)

    # Final screenshot if requested
    if final_screenshot_mode != "none":
        try:
            png = await page.screenshot(full_page=(final_screenshot_mode == "full"), type="png")
            screenshots.append(("final", base64.b64encode(png).decode("ascii")))
        except Exception:  # noqa: BLE001
            pass

    sess.last_used = _time.monotonic()

    # Build summary
    lines = [
        f"drive_page on {url}",
        f"  session_id: {sess.session_id} ({'created' if created_session else 'reused'})",
        f"  actions: {len(action_log)}",
        f"  screenshots: {len(screenshots)}",
    ]
    for i, entry in enumerate(action_log):
        ok = "✓" if entry.get("ok") else "✗"
        extras = {k: v for k, v in entry.items() if k not in ("ok", "type") and not k.startswith("_")}
        extra_str = " " + ", ".join(f"{k}={v!r}" for k, v in extras.items()) if extras else ""
        lines.append(f"  {i:2d}. {ok} {entry.get('type')}{extra_str}")
    if capture_console and sess.console_buf:
        lines.append(f"\nConsole ({len(sess.console_buf)} messages):")
        lines.extend(f"  {m}" for m in sess.console_buf)
    if capture_network and sess.network_log:
        lines.append(f"\nNetwork ({len(sess.network_log)} requests):")
        lines.extend(f"  {r['method']:<6} {r['status']:<3} {r['ms']:>5}ms  [{r['type']}]  {r['url']}" for r in sess.network_log)
    return ToolResult(
        success=True,
        summary="\n".join(lines),
        data={
            "session_id": sess.session_id,
            "url": url,
            "actions": action_log,
            "screenshots": [{"label": label, "image_base64": b64, "image_mime": "image/png"} for label, b64 in screenshots],
            "console": list(sess.console_buf) if capture_console else [],
            "network": list(sess.network_log) if capture_network else [],
        },
    )


drive_page_tool = ToolDefinition(
    name="drive_page",
    description=(
        "Drive a Modlix page through a sequence of actions (click, type, scroll, "
        "screenshot, etc.) in a headless browser. Supports persistent sessions "
        "(pass `session_id` to reuse one across calls — cookies/localStorage/"
        "scroll state survive). Returns action log + screenshots (base64 in "
        "result.data) + optional console/network buffers. Use for form fills, "
        "debug-panel flows, multi-step interactions that need state evolution. "
        "Action types: wait, click, dblclick, hover, type, press, clear, scroll, "
        "select, check, uncheck, screenshot, goto, back, forward, reload, "
        "wait_for_url, wait_for_response, read_text, read_attr, read_value, "
        "read_count, eval, set_viewport, drag."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description=_DESC_PAGE_NAME),
        ToolParameter(name="actions", type="array", description="List of action dicts; each has a `type` plus type-specific fields", items={"type": "object"}),
        ToolParameter(name="session_id", type="string", required=False, description="Reuse a persistent browser session by id (preserves state); omit to create one"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="username", type="string", required=False, description="One-shot end-user login (with password)"),
        ToolParameter(name="password", type="string", required=False, description="Password for the username login"),
        ToolParameter(name="anonymous", type="boolean", required=False, default=False, description="Skip auth"),
        ToolParameter(name="width", type="integer", required=False, default=1440, description="Viewport width"),
        ToolParameter(name="height", type="integer", required=False, default=900, description="Viewport height"),
        ToolParameter(name="path_segments", type="array", required=False, description="Path parts after /page/<name>/", items={"type": "string"}),
        ToolParameter(name="query", type="string", required=False, description="URL query string (no leading '?')"),
        ToolParameter(name="capture_console", type="boolean", required=False, default=False, description="Capture browser console + page errors"),
        ToolParameter(name="capture_network", type="boolean", required=False, default=False, description="Capture XHR/fetch requests"),
        ToolParameter(name="final_screenshot", type="string", required=False, default="viewport", description="'viewport' | 'full' | 'none' — final snap after actions"),
    ],
    execute=_execute_drive_page,
)


# ── list_browser_sessions ────────────────────────────────────────────────


async def _execute_list_browser_sessions(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    reaped = await _reap_idle_sessions()
    if not _sessions:
        msg = "No live browser sessions."
        if reaped:
            msg += f" (reaped {len(reaped)} idle: {', '.join(reaped)})"
        return ToolResult(success=True, summary=msg)
    now = _time.monotonic()
    rows = []
    for sid, s in _sessions.items():
        rows.append({
            "session_id": sid,
            "page_name": s.current_page_name,
            "app_code": s.current_app_code,
            "client_code": s.current_client_code,
            "idle_seconds": round(now - s.last_used, 1),
            "console_msgs": len(s.console_buf),
            "network_log": len(s.network_log),
        })
    return ToolResult(success=True, summary=f"{len(rows)} live browser session(s):\n{_json.dumps(rows, indent=2, default=str)}")


list_browser_sessions_tool = ToolDefinition(
    name="list_browser_sessions",
    description="List currently-live persistent browser sessions held by drive_page. Reaps anything idle past TTL first.",
    parameters=[],
    execute=_execute_list_browser_sessions,
)


# ── close_browser_session ────────────────────────────────────────────────


async def _execute_close_browser_session(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    sid = (params.get("session_id") or "").strip()
    if not sid:
        return ToolResult(success=False, error="`session_id` is required")
    sess = _sessions.pop(sid, None)
    if sess is None:
        return ToolResult(success=True, summary=f"No live session with id '{sid}' (already closed or never existed).")
    await _close_session(sess)
    return ToolResult(success=True, summary=f"Closed browser session '{sid}'.")


close_browser_session_tool = ToolDefinition(
    name="close_browser_session",
    description="Close a persistent browser session by id, freeing its Chromium process. Use when you're done with a drive_page flow and want to release resources before the idle TTL kicks in.",
    parameters=[
        ToolParameter(name="session_id", type="string", description="Session id returned by drive_page"),
    ],
    execute=_execute_close_browser_session,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    screenshot_page_tool,
    drive_page_tool,
    list_browser_sessions_tool,
    close_browser_session_tool,
]
