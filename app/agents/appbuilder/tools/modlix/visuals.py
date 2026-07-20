"""Visual asset tools — preview URLs, file uploads/downloads, image generation.

Consolidates the non-browser visual surface from modlix-mcp:
  preview.py    → 2 tools (get_preview_url, validate_page)
  files.py      → 9 tools (build_*_url, upload_*, image_to_base64,
                            generate/download secured keys, resize)
  image_gen.py  → 1 tool  (generate_image via Gemini Nano Banana)

The browser-driven tools (screenshot_page, drive_page, list/close sessions)
live in visuals_browser.py because Playwright is heavyweight.

All file-upload tools take LOCAL file paths and stream bytes via multipart —
inline content would blow the LLM's context budget. Generated images land
locally first, then upload to the platform's static asset space and return
both the path (for re-use) and the public URL (for wiring into pages).
"""

from __future__ import annotations

import asyncio
import base64
import os
import urllib.parse
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from . import _page_ops as p_ops


# Shared param-description constants.
_DESC_APP_CODE = "appCode; defaults to session"
_DESC_CLIENT_CODE = "clientCode; defaults to session"


def _client_and_headers(context: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context.get("headers") or {}


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> tuple[str, ToolResult | None]:
    ac = params.get("app_code") or context.get("app_code", "")
    if not ac:
        return "", ToolResult(success=False, error="No appCode set. Pass `app_code` or set it on the chat request.")
    return ac, None


def _resolve_client_code(params: dict[str, Any], context: dict[str, Any]) -> str:
    return params.get("client_code") or context.get("client_code", "") or ""


def _gateway_url() -> str:
    from app.config import settings
    return settings.GATEWAY_URL.rstrip("/")


_MIME_PNG = "image/png"
_MIME_JPEG = "image/jpeg"
_MIME_OCTET = "application/octet-stream"

_EXT_TO_MIME: dict[str, str] = {
    "png":  _MIME_PNG,
    "jpg":  _MIME_JPEG,
    "jpeg": _MIME_JPEG,
    "gif":  "image/gif",
    "svg":  "image/svg+xml",
    "webp": "image/webp",
}


def _mime_for_path(path: Path, fallback: str | None = None) -> str:
    """Resolve MIME type from a file path's extension.

    Hits the known-image whitelist first; otherwise returns the supplied
    fallback. When fallback is None, returns `image/{ext}` for unknown
    extensions (matching the prior inline behaviour in image-edit calls)
    or `application/octet-stream` when the file has no extension.
    """
    ext = path.suffix.lower().lstrip(".")
    if ext in _EXT_TO_MIME:
        return _EXT_TO_MIME[ext]
    if fallback is not None:
        return fallback
    return f"image/{ext}" if ext else _MIME_OCTET


# ═════════════════════════════════════════════════════════════════════════
#  PREVIEW (2 tools)
# ═════════════════════════════════════════════════════════════════════════


async def _execute_get_preview_url(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    if not page_name:
        return ToolResult(success=False, error="`page_name` is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    cc = _resolve_client_code(params, context)
    from app.config import settings
    host = getattr(settings, "PREVIEW_HOST", "") or ""
    if not host:
        parsed = urlparse(settings.GATEWAY_URL)
        host = f"{parsed.scheme}://{parsed.netloc}"
    host = host.rstrip("/")
    url = f"{host}/{ac}/{cc}/page/{page_name}"
    segments = params.get("path_segments")
    if segments:
        url += "/" + "/".join(str(s).strip("/") for s in segments if s)
    if params.get("query"):
        url += "?" + str(params["query"]).lstrip("?")
    return ToolResult(success=True, summary=url)


get_preview_url_tool = ToolDefinition(
    name="get_preview_url",
    description="Build the live preview URL for a page: <preview-host>/<appCode>/<clientCode>/page/<pageName>[/seg...][?query]. No API call — pure string construction. Falls back to gateway host when PREVIEW_HOST isn't set.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name to preview"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="path_segments", type="array", required=False, description="Path parts after /page/<pageName>/ (e.g. ['12'] → /page/<page>/12 → Url.pathParts[1]='12')", items={"type": "string"}),
        ToolParameter(name="query", type="string", required=False, description="Query string (no leading '?')"),
    ],
    execute=_execute_get_preview_url,
)


# (validate_page_tool moved to pages.py, now does a deeper shape check incl. property/style/binding wraps + event-fn refs. See pages._execute_validate_page.)


# ═════════════════════════════════════════════════════════════════════════
#  FILES (9 tools)
# ═════════════════════════════════════════════════════════════════════════


def _validate_local_file(local_path: str) -> tuple[Path | None, str | None]:
    p = Path(local_path).expanduser().resolve()
    if not p.exists():
        return None, f"local file not found: {p}"
    if not p.is_file():
        return None, f"not a regular file: {p}"
    return p, None


async def _multipart_post(
    path: str, file_path: str, headers: dict[str, str],
    fields: dict[str, str] | None = None, query: dict[str, Any] | None = None,
) -> tuple[bool, Any, str]:
    p, err = _validate_local_file(file_path)
    if err:
        return False, None, err
    assert p is not None
    h = dict(headers)
    h.pop("Content-Type", None)  # let httpx set the multipart boundary
    url = _gateway_url() + path
    from app.config import settings
    timeout = getattr(settings, "HTTP_TIMEOUT", 30.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            with open(p, "rb") as fh:
                files = {"file": (p.name, fh, "application/octet-stream")}
                resp = await client.post(url, headers=h, files=files, data=fields or {}, params=query)
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"
    if resp.status_code >= 400:
        return False, None, f"HTTP {resp.status_code}: {resp.text[:400]}"
    try:
        return True, resp.json(), ""
    except Exception:  # noqa: BLE001
        return True, resp.text, ""


# ── build_static_asset_url ───────────────────────────────────────────────


async def _execute_build_static_asset_url(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    path = (params.get("path") or "").strip()
    if not path:
        return ToolResult(success=False, error="`path` is required")
    # ac is accepted for symmetry but doesn't appear in the URL — static is client-scoped.
    ac_check, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    cc = _resolve_client_code(params, context)
    clean = path.lstrip("/")
    rel = f"/api/files/static/file/{urllib.parse.quote(cc)}/{clean}"
    url = (_gateway_url() + rel) if bool(params.get("absolute")) else rel
    return ToolResult(success=True, summary=url)


build_static_asset_url_tool = ToolDefinition(
    name="build_static_asset_url",
    description="Build the public static-asset URL: /api/files/static/file/<client>/<path>. No API call. NOTE: static URLs are CLIENT-scoped, NOT app-scoped, even though uploads route through the app's URL prefix.",
    parameters=[
        ToolParameter(name="path", type="string", description="Path under the static root (e.g. 'images/hero.jpg', 'favicons/main.ico')"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="absolute", type="boolean", required=False, default=False, description="Return absolute URL (with gateway origin) vs platform-relative"),
    ],
    execute=_execute_build_static_asset_url,
)


# ── build_secured_asset_url ──────────────────────────────────────────────


async def _execute_build_secured_asset_url(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    path = (params.get("path") or "").strip()
    if not path:
        return ToolResult(success=False, error="`path` is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    cc = _resolve_client_code(params, context)
    clean = path.lstrip("/")
    rel = f"/api/files/secured/file/{urllib.parse.quote(cc)}/{urllib.parse.quote(ac)}/{clean}"
    url = (_gateway_url() + rel) if bool(params.get("absolute")) else rel
    return ToolResult(success=True, summary=url)


build_secured_asset_url_tool = ToolDefinition(
    name="build_secured_asset_url",
    description="Build the secured-asset URL: /api/files/secured/file/<client>/<app>/<path>. Auth required at fetch time. For sharing with non-logged-in viewers use generate_secured_access_key.",
    parameters=[
        ToolParameter(name="path", type="string", description="Path under the secured root (e.g. 'uploads/contract.pdf')"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="absolute", type="boolean", required=False, default=False, description="Return absolute URL vs relative path"),
    ],
    execute=_execute_build_secured_asset_url,
)


# ── upload_static_asset ──────────────────────────────────────────────────


async def _execute_upload_static_asset(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    local_path = (params.get("local_path") or "").strip()
    if not local_path:
        return ToolResult(success=False, error="`local_path` is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    cc = _resolve_client_code(params, context)
    p, err = _validate_local_file(local_path)
    if err:
        return ToolResult(success=False, error=err)
    assert p is not None
    on_server_name = params.get("filename") or p.name
    page_clean = (params.get("page_name") or "global").strip("/") or "global"
    folder_clean = (params.get("folder") or "").strip("/")
    path_parts = [ac, page_clean] + ([folder_clean] if folder_clean else [])
    path_segment = "/" + "/".join(path_parts)
    upload_path = f"/{ac}/{cc}/page/api/files/static{path_segment}"

    headers = dict(context.get("headers") or {})
    headers["clientCode"] = cc
    headers.pop("Content-Type", None)
    url = _gateway_url() + upload_path
    from app.config import settings
    query: dict[str, Any] = {"clientCode": cc}
    if bool(params.get("overwrite", True)):
        query["override"] = "true"
    try:
        async with httpx.AsyncClient(timeout=getattr(settings, "HTTP_TIMEOUT", 30.0)) as client:
            with open(p, "rb") as fh:
                files = {"file": (on_server_name, fh, "application/octet-stream")}
                resp = await client.post(url, headers=headers, files=files, params=query)
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"{type(e).__name__}: {e}")
    if resp.status_code >= 400:
        return ToolResult(success=False, error=f"HTTP {resp.status_code}: {resp.text[:400]}")
    dl_path = f"/api/files/static/file/{cc}{path_segment}/{on_server_name}"
    absolute = _gateway_url() + dl_path
    return ToolResult(
        success=True,
        summary=(
            f"Uploaded to static asset space.\n"
            f"  POST {upload_path}\n"
            f"  → public path:    {dl_path}\n"
            f"  → absolute URL:   {absolute}\n"
            f"server response: {resp.text[:300]}"
        ),
    )


upload_static_asset_tool = ToolDefinition(
    name="upload_static_asset",
    description=(
        "Upload a LOCAL file to the app's static (public) asset space, namespaced as "
        "`<appCode>/<pageName>/[<folder>/]<filename>`. Bytes are sent via multipart "
        "— never enter the conversation. Returns the public path + absolute URL "
        "so the caller can wire it into a page immediately. Use page_name='global' "
        "for app-wide assets (logos, favicons)."
    ),
    parameters=[
        ToolParameter(name="local_path", type="string", description="Path to a LOCAL file on the agent host"),
        ToolParameter(name="page_name", type="string", required=False, default="global", description="Page scope ('global' or page name)"),
        ToolParameter(name="folder", type="string", required=False, default="", description="Optional sub-folder under <app>/<page>/"),
        ToolParameter(name="filename", type="string", required=False, description="Override the on-server filename"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="overwrite", type="boolean", required=False, default=True, description="Overwrite if name collides"),
    ],
    execute=_execute_upload_static_asset,
)


# ── upload_client_file ───────────────────────────────────────────────────


async def _execute_upload_client_file(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    local_path = (params.get("local_path") or "").strip()
    if not local_path:
        return ToolResult(success=False, error="`local_path` is required")
    headers = dict(context.get("headers") or {})
    fields: dict[str, str] = {}
    if params.get("width"):
        fields["width"] = str(params["width"])
    if params.get("height"):
        fields["height"] = str(params["height"])
    query = {"clientId": params["client_id"]} if params.get("client_id") else None
    ok, body, err = await _multipart_post("/api/files/generic/client", local_path, headers, fields, query)
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Uploaded to client storage:\n{body}")


upload_client_file_tool = ToolDefinition(
    name="upload_client_file",
    description="Upload a LOCAL file to the platform's client-scoped storage. Returns FileDetail with the URL the agent can use in page/template references.",
    parameters=[
        ToolParameter(name="local_path", type="string", description="LOCAL file path on the agent host"),
        ToolParameter(name="client_id", type="string", required=False, description="Optional clientId scope; defaults to authenticated client"),
        ToolParameter(name="width", type="string", required=False, description="Optional resize width (px, image only)"),
        ToolParameter(name="height", type="string", required=False, description="Optional resize height (px)"),
    ],
    execute=_execute_upload_client_file,
)


# ── upload_user_file ─────────────────────────────────────────────────────


async def _execute_upload_user_file(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    local_path = (params.get("local_path") or "").strip()
    if not local_path:
        return ToolResult(success=False, error="`local_path` is required")
    headers = dict(context.get("headers") or {})
    fields: dict[str, str] = {}
    if params.get("width"):
        fields["width"] = str(params["width"])
    if params.get("height"):
        fields["height"] = str(params["height"])
    query = {"userId": params["user_id"]} if params.get("user_id") else None
    ok, body, err = await _multipart_post("/api/files/generic/user", local_path, headers, fields, query)
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Uploaded to user storage:\n{body}")


upload_user_file_tool = ToolDefinition(
    name="upload_user_file",
    description="Upload a LOCAL file to per-user (secured) storage.",
    parameters=[
        ToolParameter(name="local_path", type="string", description="LOCAL file path"),
        ToolParameter(name="user_id", type="string", required=False, description="Optional userId; defaults to authenticated user"),
        ToolParameter(name="width", type="string", required=False, description="Optional resize width"),
        ToolParameter(name="height", type="string", required=False, description="Optional resize height"),
    ],
    execute=_execute_upload_user_file,
)


# ── resize_image_to_path ─────────────────────────────────────────────────


async def _execute_resize_image_to_path(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    local_path = (params.get("local_path") or "").strip()
    output_path = (params.get("output_path") or "").strip()
    if not local_path or not output_path:
        return ToolResult(success=False, error="`local_path` and `output_path` are required")
    p, err = _validate_local_file(local_path)
    if err:
        return ToolResult(success=False, error=err)
    assert p is not None
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    headers = dict(context.get("headers") or {})
    headers.pop("Content-Type", None)
    data: dict[str, str] = {}
    if params.get("width"):
        data["width"] = str(params["width"])
    if params.get("height"):
        data["height"] = str(params["height"])
    if params.get("to_format"):
        data["toFormat"] = str(params["to_format"])
    from app.config import settings
    url = _gateway_url() + "/api/files/generic/resize"
    try:
        async with httpx.AsyncClient(timeout=getattr(settings, "HTTP_TIMEOUT", 30.0)) as client:
            with open(p, "rb") as fh:
                files = {"file": (p.name, fh, "application/octet-stream")}
                resp = await client.post(url, headers=headers, files=files, data=data)
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"{type(e).__name__}: {e}")
    if resp.status_code >= 400:
        return ToolResult(success=False, error=f"HTTP {resp.status_code}: {resp.text[:400]}")
    out.write_bytes(resp.content)
    return ToolResult(success=True, summary=f"Resized image written to {out} ({len(resp.content):,} bytes, {resp.headers.get('content-type', '')}).")


resize_image_to_path_tool = ToolDefinition(
    name="resize_image_to_path",
    description="Send a local image through the platform's resize endpoint; write the result to output_path. JPG/PNG only. Useful for generating manifest icons, OG images.",
    parameters=[
        ToolParameter(name="local_path", type="string", description="Source image"),
        ToolParameter(name="output_path", type="string", description="Where to write the resized image"),
        ToolParameter(name="width", type="string", required=False, description="Target width (px)"),
        ToolParameter(name="height", type="string", required=False, description="Target height (px)"),
        ToolParameter(name="to_format", type="string", required=False, description="'PNG' or 'JPG' (auto if omitted)"),
    ],
    execute=_execute_resize_image_to_path,
)


# ── image_to_base64 ──────────────────────────────────────────────────────


async def _execute_image_to_base64(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    local_path = (params.get("local_path") or "").strip()
    if not local_path:
        return ToolResult(success=False, error="`local_path` is required")
    try:
        max_kb = max(1, min(int(params.get("max_kb") or 64), 512))
    except (TypeError, ValueError):
        max_kb = 64
    p, err = _validate_local_file(local_path)
    if err:
        return ToolResult(success=False, error=err)
    assert p is not None
    size_kb = p.stat().st_size // 1024
    if size_kb > max_kb:
        return ToolResult(success=False, error=f"file is {size_kb} KB; cap is {max_kb}. Upload via upload_client_file and reference the URL instead.")
    mime = _mime_for_path(p, fallback=_MIME_OCTET)
    encoded = base64.b64encode(p.read_bytes()).decode("ascii")
    return ToolResult(success=True, summary=f"data:{mime};base64,{encoded}")


image_to_base64_tool = ToolDefinition(
    name="image_to_base64",
    description="Read a SMALL local image (≤64 KB by default) and return a base64 data URI. Use for inlining tiny assets into email template bodies / page properties. Larger files: use upload_client_file.",
    parameters=[
        ToolParameter(name="local_path", type="string", description="Local image file path"),
        ToolParameter(name="max_kb", type="integer", required=False, default=64, description="Refuse if file is larger than this (cap 512)"),
    ],
    execute=_execute_image_to_base64,
)


# ── generate_secured_access_key ──────────────────────────────────────────


async def _execute_generate_secured_access_key(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    path = (params.get("path") or "").strip()
    if not path:
        return ToolResult(success=False, error="`path` is required (under the secured root, no leading slash)")
    headers = context.get("headers") or {}
    url = _gateway_url() + "/api/files/secured/createKey/" + path.lstrip("/")
    req_params: dict[str, Any] = {}
    if params.get("time_span_seconds") is not None:
        req_params["timeSpan"] = int(params["time_span_seconds"])
    from app.config import settings
    try:
        async with httpx.AsyncClient(timeout=getattr(settings, "HTTP_TIMEOUT", 30.0)) as client:
            resp = await client.get(url, headers=headers, params=req_params)
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"{type(e).__name__}: {e}")
    if resp.status_code >= 400:
        return ToolResult(success=False, error=f"HTTP {resp.status_code}: {resp.text[:400]}")
    key = resp.text.strip().strip('"')
    return ToolResult(success=True, summary=f"Secured access key: {key}\nDownload URL: {_gateway_url()}/api/files/secured/downloadFileByKey/{key}")


generate_secured_access_key_tool = ToolDefinition(
    name="generate_secured_access_key",
    description="Create a short-TTL token granting access to a secured file. Recipients fetch via /api/files/secured/downloadFileByKey/<key> with no auth — until the token expires. Use for sharing protected assets with non-authenticated viewers.",
    parameters=[
        ToolParameter(name="path", type="string", description="Path under the secured root (e.g. 'uploads/contract.pdf')"),
        ToolParameter(name="time_span_seconds", type="integer", required=False, description="Validity window (seconds). Backend default if unset."),
    ],
    execute=_execute_generate_secured_access_key,
)


# ── download_secured_file_by_key ─────────────────────────────────────────


async def _execute_download_secured_file_by_key(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    key = (params.get("key") or "").strip()
    output_path = (params.get("output_path") or "").strip()
    if not key or not output_path:
        return ToolResult(success=False, error="`key` and `output_path` are required")
    headers = context.get("headers") or {}
    url = _gateway_url() + f"/api/files/secured/downloadFileByKey/{key}"
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    from app.config import settings
    try:
        async with httpx.AsyncClient(timeout=getattr(settings, "HTTP_TIMEOUT", 30.0)) as client:
            resp = await client.get(url, headers=headers)
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"{type(e).__name__}: {e}")
    if resp.status_code >= 400:
        return ToolResult(success=False, error=f"HTTP {resp.status_code}: {resp.text[:400]}")
    out.write_bytes(resp.content)
    return ToolResult(success=True, summary=f"Downloaded {len(resp.content):,} bytes to {out}.")


download_secured_file_by_key_tool = ToolDefinition(
    name="download_secured_file_by_key",
    description="Download a secured file by a previously-issued key. Streams to disk — bytes never enter the conversation.",
    parameters=[
        ToolParameter(name="key", type="string", description="Access key from generate_secured_access_key"),
        ToolParameter(name="output_path", type="string", description="Local path to write the downloaded file"),
    ],
    execute=_execute_download_secured_file_by_key,
)


# ═════════════════════════════════════════════════════════════════════════
#  IMAGE GEN (1 tool — Gemini Nano Banana)
# ═════════════════════════════════════════════════════════════════════════


_GEMINI_URL_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_IMAGE_MODEL = "gemini-2.5-flash-image"
_ASPECT_PROFILES: dict[str, dict[str, Any]] = {
    "1:1":  {"label": "square 1:1", "size": (1024, 1024)},
    "16:9": {"label": "landscape 16:9", "size": (1920, 1080)},
    "9:16": {"label": "portrait 9:16", "size": (1080, 1920)},
    "4:3":  {"label": "landscape 4:3",  "size": (1024, 768)},
    "3:4":  {"label": "portrait 3:4",   "size": (768, 1024)},
}


async def _generate_via_gemini(
    api_key: str, prompt: str, model: str,
    input_images: list[tuple[str, bytes]] | None = None,
) -> tuple[bytes | None, str]:
    """Call Gemini generateContent; return (png_bytes, error)."""
    url = f"{_GEMINI_URL_BASE}/{model}:generateContent"
    parts: list[dict[str, Any]] = []
    if input_images:
        for mime, raw in input_images:
            parts.append({
                "inline_data": {
                    "mime_type": mime,
                    "data": base64.b64encode(raw).decode("ascii"),
                },
            })
    parts.append({"text": prompt})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    timeout_s = 300.0 if input_images else 60.0
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        return None, f"{type(e).__name__}: {e}"
    if resp.status_code >= 400:
        return None, f"Gemini HTTP {resp.status_code}: {resp.text[:600]}"
    try:
        data = resp.json()
    except ValueError:
        return None, f"Gemini response not JSON: {resp.text[:200]}"
    for cand in data.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                try:
                    return base64.b64decode(inline["data"]), ""
                except Exception as e:  # noqa: BLE001
                    return None, f"base64 decode failed: {e}"
    return None, f"Gemini returned no image. Response: {str(data)[:300]}"


async def _upload_generated_static(
    local_path: Path, page_name: str, folder: str, filename: str,
    app_code: str, client_code: str, headers: dict[str, str],
) -> tuple[str | None, str | None, str]:
    """Multipart-POST the generated image to the static asset space."""
    page_clean = (page_name or "global").strip("/") or "global"
    folder_clean = (folder or "").strip("/")
    path_parts = [app_code, page_clean] + ([folder_clean] if folder_clean else [])
    path_segment = "/" + "/".join(path_parts)
    upload_path = f"/{app_code}/{client_code}/page/api/files/static{path_segment}"
    h = dict(headers)
    h["clientCode"] = client_code
    h.pop("Content-Type", None)
    url = _gateway_url() + upload_path
    from app.config import settings
    try:
        async with httpx.AsyncClient(timeout=getattr(settings, "HTTP_TIMEOUT", 30.0)) as client:
            with open(local_path, "rb") as fh:
                files = {"file": (filename, fh, _MIME_PNG)}
                resp = await client.post(url, headers=h, files=files, params={"clientCode": client_code, "override": "true"})
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"
    if resp.status_code >= 400:
        return None, None, f"upload HTTP {resp.status_code}: {resp.text[:400]}"
    rel = f"/api/files/static/file/{client_code}{path_segment}/{filename}"
    absolute = _gateway_url() + rel
    return rel, absolute, ""


async def _execute_generate_image(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    prompt = (params.get("prompt") or "").strip()
    filename = (params.get("filename") or "").strip()
    if not prompt or not filename:
        return ToolResult(success=False, error="`prompt` and `filename` are required")
    aspect = (params.get("aspect_ratio") or "1:1").strip()
    if aspect not in _ASPECT_PROFILES:
        return ToolResult(success=False, error=f"aspect_ratio must be one of {list(_ASPECT_PROFILES)}, got {aspect!r}")
    from app.config import settings
    api_key = getattr(settings, "GOOGLE_API_KEY", "") or ""
    if not api_key:
        return ToolResult(success=False, error="GOOGLE_API_KEY not set in nocode-ai settings. Set it and reload.")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    cc = _resolve_client_code(params, context)
    profile = _ASPECT_PROFILES[aspect]
    model = (params.get("model") or _DEFAULT_IMAGE_MODEL).strip()
    style_notes = (params.get("style_notes") or "").strip()

    # Build prompt — aspect hint only on text-to-image (image-edit has its own composition).
    full_prompt = prompt
    if style_notes:
        full_prompt += f"\n\nStyle notes: {style_notes}"
    input_image_paths = params.get("input_image_paths") or None
    if not input_image_paths:
        full_prompt += f"\n\nAspect: {profile['label']} (target {profile['size'][0]}x{profile['size'][1]} px)."

    input_images: list[tuple[str, bytes]] | None = None
    if input_image_paths:
        input_images = []
        for ip in input_image_paths:
            ipath = Path(ip).expanduser().resolve()
            if not ipath.exists() or not ipath.is_file():
                return ToolResult(success=False, error=f"input_image_path not found: {ipath}")
            input_images.append((_mime_for_path(ipath), ipath.read_bytes()))

    png, gen_err = await _generate_via_gemini(api_key, full_prompt, model, input_images=input_images)
    if gen_err:
        return ToolResult(success=False, error=gen_err)
    assert png is not None
    out_dir = Path("/tmp/cfa-generated-images")
    out_dir.mkdir(parents=True, exist_ok=True)
    local = out_dir / filename
    local.write_bytes(png)
    rel, absolute, up_err = await _upload_generated_static(local, params.get("page_name") or "global", params.get("folder") or "", filename, ac, cc, context.get("headers") or {})
    if up_err:
        return ToolResult(success=False, error=f"{up_err}\n(image saved at {local} — upload manually with upload_static_asset)")
    return ToolResult(
        success=True,
        summary=(
            f"Generated + uploaded image ({len(png):,} bytes)\n"
            f"  prompt:        {prompt!r}\n"
            f"  aspect:        {aspect}\n"
            f"  local path:    {local}\n"
            f"  public URL:    {rel}\n"
            f"  absolute URL:  {absolute}\n"
            f"\nWire into a page with: patch_component_props on an Image with properties={{'src': {{'value': {rel!r}}}}}"
        ),
    )


generate_image_tool = ToolDefinition(
    name="generate_image",
    description=(
        "Generate or edit an image with Gemini Nano Banana, save locally, upload to "
        "the app's static asset space, return its public URL. Text-to-image when "
        "only `prompt` is given; image-to-image edit when input_image_paths is set. "
        "Requires GOOGLE_API_KEY in nocode-ai settings."
    ),
    parameters=[
        ToolParameter(name="prompt", type="string", description="Natural-language description"),
        ToolParameter(name="filename", type="string", description="On-server filename (e.g. 'hero.png')"),
        ToolParameter(name="page_name", type="string", required=False, default="global", description="Page scope ('global' or page name)"),
        ToolParameter(name="folder", type="string", required=False, default="", description="Optional sub-folder"),
        ToolParameter(name="aspect_ratio", type="string", required=False, default="1:1", description="1:1 | 16:9 | 9:16 | 4:3 | 3:4"),
        ToolParameter(name="style_notes", type="string", required=False, description="Extra styling instructions appended to prompt"),
        ToolParameter(name="model", type="string", required=False, default=_DEFAULT_IMAGE_MODEL, description="Gemini image model id"),
        ToolParameter(name="input_image_paths", type="array", required=False, description="Optional local images for image-to-image edit", items={"type": "string"}),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
    ],
    execute=_execute_generate_image,
)


# ── describe_image ────────────────────────────────────────────────────────
# Vision adapter: feeds an image through Gemini Flash and returns text.
# Lets text-only providers (DeepSeek) reason about screenshots / images, and
# gives vision-capable providers a way to journal a description for KB/audit.

_DESCRIBE_DEFAULT_MODEL = "gemini-2.5-flash"
_DESCRIBE_BASE_PROMPT = (
    "Describe this UI screenshot for a vision-blind LLM that needs to reason "
    "about the layout. Cover, in order:\n"
    "1. Overall page structure — header/nav/sidebar/main/footer regions and "
    "their positions.\n"
    "2. Per region: visible components (buttons, inputs, tables, cards, "
    "images) with their text labels and spatial relationships.\n"
    "3. Color palette (primary accent, background, text contrast).\n"
    "4. Visible content (table rows, form values, headings).\n"
    "5. Anything visually broken — overlapping elements, cut-off text, "
    "alignment issues, blank regions where content should be.\n"
    "Be specific. Avoid generic phrases like 'a clean design' — describe what "
    "is actually on screen."
)


async def _describe_via_gemini(
    api_key: str, image_data: bytes, mime: str, prompt: str, model: str,
) -> tuple[str, str]:
    """Call Gemini generateContent with image + prompt; return (text, error)."""
    url = f"{_GEMINI_URL_BASE}/{model}:generateContent"
    parts: list[dict[str, Any]] = [
        {"inline_data": {
            "mime_type": mime,
            "data": base64.b64encode(image_data).decode("ascii"),
        }},
        {"text": prompt},
    ]
    body = {"contents": [{"parts": parts}]}
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        return "", f"{type(e).__name__}: {e}"
    if resp.status_code >= 400:
        return "", f"Gemini HTTP {resp.status_code}: {resp.text[:600]}"
    try:
        data = resp.json()
    except ValueError:
        return "", f"Gemini response not JSON: {resp.text[:200]}"
    texts: list[str] = []
    for cand in data.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            if part.get("text"):
                texts.append(part["text"])
    if not texts:
        return "", f"Gemini returned no text. Response: {str(data)[:300]}"
    return "\n".join(texts).strip(), ""


def _resolve_describe_image_payload(
    params: dict[str, Any],
) -> tuple[bytes | None, str, ToolResult | None]:
    """Validate the describe_image input shape; return (bytes, mime, err_result).

    Exactly one of `image_base64` or `image_path` must be set. On error,
    returns (None, "", ToolResult(success=False)) so the caller can early-return.
    """
    img_b64 = (params.get("image_base64") or "").strip()
    img_path = (params.get("image_path") or "").strip()
    if not img_b64 and not img_path:
        return None, "", ToolResult(success=False, error="One of `image_base64` or `image_path` is required.")
    if img_b64 and img_path:
        return None, "", ToolResult(success=False, error="Pass only one of `image_base64` or `image_path`, not both.")
    if img_b64:
        try:
            data = base64.b64decode(img_b64)
        except Exception as e:  # noqa: BLE001
            return None, "", ToolResult(success=False, error=f"image_base64 decode failed: {e}")
        return data, (params.get("mime_type") or _MIME_PNG).strip(), None
    ipath = Path(img_path).expanduser().resolve()
    if not ipath.exists() or not ipath.is_file():
        return None, "", ToolResult(success=False, error=f"image_path not found: {ipath}")
    return ipath.read_bytes(), _mime_for_path(ipath), None


async def _execute_describe_image(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    image_data, mime, err_result = _resolve_describe_image_payload(params)
    if err_result is not None:
        return err_result
    assert image_data is not None  # narrows for type-checkers

    from app.config import settings
    api_key = getattr(settings, "GOOGLE_API_KEY", "") or ""
    if not api_key:
        return ToolResult(success=False, error="GOOGLE_API_KEY not set in nocode-ai settings. Set it and reload.")

    focus_hint = (params.get("focus_hint") or "").strip()
    prompt = _DESCRIBE_BASE_PROMPT
    if focus_hint:
        prompt += f"\n\nFocus particularly on: {focus_hint}"
    model = (params.get("model") or _DESCRIBE_DEFAULT_MODEL).strip()

    text, err = await _describe_via_gemini(api_key, image_data, mime, prompt, model)
    if err:
        return ToolResult(success=False, error=err)
    return ToolResult(
        success=True,
        summary=f"Image description ({len(image_data):,} bytes, model={model}):\n\n{text}",
        data={"description": text, "model": model, "bytes": len(image_data)},
    )


describe_image_tool = ToolDefinition(
    name="describe_image",
    description=(
        "Run a vision-capable model (Gemini Flash) over an image and return a "
        "textual description. Use when your provider is text-only (e.g. DeepSeek) "
        "and you need to reason about a screenshot, or when you want a written "
        "record of an image for KB/audit. Accepts the base64 output from "
        "screenshot_page directly via `image_base64`, OR a local `image_path`. "
        "Optional `focus_hint` steers the description toward a specific concern "
        "(layout, color contrast, form fields, etc.). "
        "Requires GOOGLE_API_KEY in nocode-ai settings."
    ),
    parameters=[
        ToolParameter(name="image_base64", type="string", required=False, description="Base64-encoded image bytes (e.g. screenshot_page's data['image_base64'])"),
        ToolParameter(name="image_path", type="string", required=False, description="Absolute path to a local image file (alternative to image_base64)"),
        ToolParameter(name="mime_type", type="string", required=False, default=_MIME_PNG, description="MIME type when using image_base64 (default image/png)"),
        ToolParameter(name="focus_hint", type="string", required=False, description="Optional natural-language steer (e.g. 'focus on form layout and spacing')"),
        ToolParameter(name="model", type="string", required=False, default=_DESCRIBE_DEFAULT_MODEL, description="Gemini vision model id"),
    ],
    execute=_execute_describe_image,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    # Preview (2)
    get_preview_url_tool,
    # validate_page_tool — moved to pages.py
    # Files — URL builders (2)
    build_static_asset_url_tool,
    build_secured_asset_url_tool,
    # Files — uploads (3)
    upload_static_asset_tool,
    upload_client_file_tool,
    upload_user_file_tool,
    # Files — transforms (2)
    resize_image_to_path_tool,
    image_to_base64_tool,
    # Files — secured (2)
    generate_secured_access_key_tool,
    download_secured_file_by_key_tool,
    # Image gen + vision (2)
    generate_image_tool,
    describe_image_tool,
]
