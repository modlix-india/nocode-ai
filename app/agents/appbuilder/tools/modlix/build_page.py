"""build_page_from_url — deterministic URL -> Modlix page (no LLM authoring).

A CFA tool that, given a URL, runs the Playwright+CDP page analyzer, maps the
captured DOM/authored-CSS into a Modlix componentDefinition, and saves it as a
real page via /api/ui/pages. The full component map is built server-side, so the
agent only passes {url, page_name} (a tiny payload) instead of emitting thousands
of components itself. Pair with an app-global style doc for :root vars / fonts /
keyframes and wrapShell=false so the page renders bare.

See app/services/page_analyzer for the analyzer + mapper this orchestrates.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

_PAGES_API = "/api/ui/pages"


def _gateway_url() -> str:
    from app.config import settings
    return settings.GATEWAY_URL.rstrip("/")


def _build_global_css(analysis) -> str:
    parts = []
    rv = analysis.root_custom_properties or {}
    if rv:
        parts.append(":root {\n" + "\n".join(f"  {k}: {v};" for k, v in rv.items()) + "\n}")
    parts.extend(analysis.font_faces or [])
    parts.extend(analysis.keyframes or [])
    return "\n".join(parts)


async def _execute_build_page_from_url(params: Dict[str, Any], context: Dict[str, Any]) -> ToolResult:
    url = (params.get("url") or "").strip()
    page_name = (params.get("page_name") or "").strip()
    ac = (params.get("app_code") or context.get("app_code") or "").strip()
    cc = (params.get("client_code") or context.get("client_code") or "SYSTEM").strip()
    if not url or not page_name:
        return ToolResult(success=False, error="`url` and `page_name` are required")
    if not ac:
        return ToolResult(success=False, error="No appCode. Pass `app_code` or set it on the chat request.")

    try:
        cap = int(params.get("max_components") or 6000)
    except (TypeError, ValueError):
        cap = 6000

    from app.agents.appbuilder.tools._shared import get_saas_client
    from app.agents.appbuilder.tools.modlix.clone_ops import _ensure_style_doc
    from app.services.page_analyzer.browser import run_full_dom
    from app.services.page_analyzer.to_page_definition import build_page_definition

    # 1) Analyze the live page (Playwright + CDP, ~60-90s for a dense page).
    try:
        analysis = await run_full_dom(url, headless=True)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(success=False, error=f"page analysis failed: {exc}")
    if analysis.full_tree is None:
        return ToolResult(success=False, error=f"analysis produced no DOM tree for {url}")

    # 2) Map the captured tree -> Modlix componentDefinition.
    comps, root = build_page_definition(
        analysis.full_tree, cap=cap, css_vars=analysis.root_custom_properties,
    )

    # 3) Upsert the page via /api/ui/pages using the caller's auth context.
    client = get_saas_client()
    headers = dict(context.get("headers") or {})
    headers["clientCode"] = cc
    headers["appCode"] = ac

    pid = None
    gr = await client.get(_PAGES_API, headers=headers, params={"name": page_name, "appCode": ac, "size": 20})
    if gr.success and isinstance(gr.data, dict):
        for row in gr.data.get("content", []) or []:
            if isinstance(row, dict) and row.get("name") == page_name:
                pid = row.get("id")
                break

    if pid:
        pr = await client.get(f"{_PAGES_API}/{pid}", headers=headers)
        if not pr.success or not isinstance(pr.data, dict):
            return ToolResult(success=False, error=f"could not load existing page {page_name}: {pr.error}")
        page = pr.data
        page["rootComponent"] = root
        page["componentDefinition"] = comps
        page.setdefault("properties", {})["wrapShell"] = False
        page["message"] = "Built from URL via CFA"
        save = await client.put(f"{_PAGES_API}/{pid}", headers=headers, json=page)
    else:
        page = {
            "name": page_name, "appCode": ac, "clientCode": cc, "rootComponent": root,
            "componentDefinition": comps, "eventFunctions": {},
            "properties": {"title": {"name": {"value": page_name}}, "wrapShell": False},
            "translations": {}, "message": "Built from URL via CFA",
        }
        save = await client.post(_PAGES_API, headers=headers, json=page)

    if not save.success:
        return ToolResult(success=False, error=f"page save failed: {save.error}")

    # 4) App-global style doc: :root vars + @font-face + @keyframes (safe globals).
    style_note = ""
    css = _build_global_css(analysis)
    if css.strip():
        ok, serr = await _ensure_style_doc(ac, cc, headers, page_name + "Globals", css)
        style_note = " +globals" if ok else f" (globals failed: {serr})"

    preview = f"{_gateway_url()}/{ac}/{cc}/page/{page_name}"
    notes = []
    if analysis.warnings:
        notes.append(f"{len(analysis.warnings)} analysis warning(s)")
    return ToolResult(
        success=True,
        summary=(
            f"Built page '{page_name}' in app '{ac}' from {url}: {len(comps)} components, "
            f"wrapShell=false{style_note}. Note: JS-driven visuals (canvas/SVG diagrams, "
            f"runtime-animated text) are not statically reproducible. Preview: {preview}"
            + (f" [{'; '.join(notes)}]" if notes else "")
        ),
    )


build_page_from_url_tool = ToolDefinition(
    name="build_page_from_url",
    description=(
        "Deterministically clone a live web page into a real Modlix page. Runs the "
        "Playwright+CDP analyzer (authored CSS at desktop/tablet/mobile, fonts, hover, "
        "SVG), maps it to a Modlix componentDefinition built SERVER-SIDE, and saves it "
        "via /api/ui/pages with an app-global style doc (vars/fonts/keyframes) and "
        "wrapShell=false. No LLM authoring; pass only url + page_name. Takes ~60-120s. "
        "JS-driven visuals (canvas, runtime-positioned SVG, animated text) won't reproduce."
    ),
    parameters=[
        ToolParameter(name="url", type="string", required=True, description="Live page URL to clone."),
        ToolParameter(name="page_name", type="string", required=True, description="Modlix page name (letters/digits)."),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to the chat's app."),
        ToolParameter(name="client_code", type="string", required=False, description="clientCode; defaults to SYSTEM."),
        ToolParameter(name="max_components", type="integer", required=False, default=6000, description="Cap on components (safety)."),
    ],
    execute=_execute_build_page_from_url,
)

TOOLS = [build_page_from_url_tool]
