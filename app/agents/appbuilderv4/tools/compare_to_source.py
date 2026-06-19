"""`compare_to_source` — diff a built Modlix page against a cached source
screenshot via MiniMax-M3 vision; return structured JSON the agent can
act on.

Workflow:
  1. `screenshot_external_url(...)` captures source frames and caches each
     under a `source_handle` in `session.context["_v4_clone_source_shots"]`.
  2. The agent builds (or mutates) a Modlix page.
  3. The agent calls `compare_to_source(page_name, source_handle)`. We:
     - Render the Modlix page via Playwright (anonymous).
     - Fetch the cached source PNG by handle.
     - Send both images to MiniMax-M3 with a strict-JSON diff prompt
       (OpenAI-compatible `image_url` data-URI shape).
     - Return `[{section, severity, copy_diff, layout_diff, color_diff,
                 missing_elements, fix_suggestion}, ...]`.
  4. Agent reads the diff, fixes severity=high issues, re-compares.

The compare LLM call is independent of the agent's main turn loop — it's
a separate API request inside the tool, billed alongside the agent's
budget. We reuse the MiniMaxProvider's cached OpenAI client so the API
key and base URL stay in one place.
"""

from __future__ import annotations

import asyncio
import base64
import json as _json
import re
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult


_MIME_PNG = "image/png"


_COMPARE_PROMPT = (
    "You are shown TWO screenshots: the SOURCE site (first image) and the "
    "current MODLIX BUILD (second image). The build is meant to clone the "
    "source.\n\n"
    "Return a JSON array of diff entries. Each entry has exactly these keys: "
    "section, severity, copy_diff, layout_diff, color_diff, missing_elements, "
    "fix_suggestion.\n\n"
    "- `section`: short name of the visual region (e.g. 'hero', 'navbar', "
    "'feature-cards', 'footer').\n"
    "- `severity`: 'high' (must fix), 'medium' (visible but ship-able), "
    "'low' (cosmetic).\n"
    "- `copy_diff`: wrong / missing / extra text. Empty string when copy matches.\n"
    "- `layout_diff`: structural differences (order, overlap, alignment, "
    "missing sections). One concrete sentence.\n"
    "- `color_diff`: palette / contrast differences. Empty when matching.\n"
    "- `missing_elements`: array of source elements absent from the build "
    "(empty when nothing is missing).\n"
    "- `fix_suggestion`: ONE actionable sentence telling the build agent "
    "what to change.\n\n"
    "Be strict: missing navbar, wrong section order, missing imagery → high. "
    "Wrong font weight or small color drift → medium. Spacing nits → low.\n\n"
    "Reply with ONLY the JSON array. No prose, no markdown fences."
)


async def _render_modlix(page_name: str, app_code: str, client_code: str,
                          width: int, height: int, wait_ms: int) -> tuple[bytes | None, str | None]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None, "playwright not installed"
    from app.config import settings
    host = (getattr(settings, "PREVIEW_HOST", "") or "https://apps.local.modlix.com").rstrip("/")
    url = f"{host}/{app_code}/{client_code}/page/{page_name}"
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx = await browser.new_context(
                viewport={"width": width, "height": height},
                ignore_https_errors=True,
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(wait_ms)
            png = await page.screenshot(full_page=False, type="png")
            await browser.close()
            return png, None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _parse_diff_json(raw: str) -> tuple[list[dict] | None, str | None]:
    if not raw:
        return None, "empty response"
    text = raw.strip()
    # MiniMax-M3 emits a `<think>...</think>` reasoning preamble before
    # the answer. Strip it before searching for the JSON array.
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None, "no JSON array delimiters"
    try:
        parsed = _json.loads(text[start:end + 1])
    except Exception as e:  # noqa: BLE001
        return None, f"JSON parse error: {e}"
    if not isinstance(parsed, list):
        return None, "top-level value is not an array"
    return [x for x in parsed if isinstance(x, dict)], None


async def _execute_compare_to_source(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    source_handle = (params.get("source_handle") or "").strip()
    if not page_name:
        return ToolResult(success=False, error="`page_name` is required")
    if not source_handle:
        return ToolResult(success=False, error="`source_handle` is required (from screenshot_external_url)")

    app_code = (params.get("app_code") or context.get("app_code") or "").strip()
    client_code = (params.get("client_code") or context.get("client_code") or "").strip()
    if not app_code:
        return ToolResult(success=False, error="No app_code in context — pass `app_code` explicitly.")
    width = max(320, min(int(params.get("viewport_width") or 1440), 3840))
    height = max(320, min(int(params.get("viewport_height") or 900), 2160))
    wait_ms = max(200, min(int(params.get("wait_ms") or 2500), 30000))

    from app.agents.appbuilderv4.tools._shot_cache import get_shot, known_handles
    session_id = ""
    sc = context.get("session_context") if isinstance(context, dict) else None
    if isinstance(sc, dict):
        session_id = str(sc.get("session_id") or sc.get("_session_id") or "")
    if not session_id:
        session_id = "_unattached_"
    src = get_shot(session_id, source_handle)
    if not src:
        return ToolResult(success=False, error=(
            f"Unknown source_handle {source_handle!r}. Known: {known_handles(session_id)}. "
            "Call screenshot_external_url first (in this session)."
        ))
    src_b64 = src.get("image_base64")
    if not src_b64:
        return ToolResult(success=False, error=f"Cached source for {source_handle!r} has no image bytes.")

    build_png, ss_err = await _render_modlix(
        page_name=page_name, app_code=app_code, client_code=client_code,
        width=width, height=height, wait_ms=wait_ms,
    )
    if ss_err or build_png is None:
        return ToolResult(success=False, error=f"build screenshot failed: {ss_err}")
    build_b64 = base64.b64encode(build_png).decode("ascii")

    try:
        from app.config import settings
        from app.services.llm_provider import get_llm_provider
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"compare import failed: {type(e).__name__}: {e}")

    if not getattr(settings, "MINIMAX_API_KEY", ""):
        return ToolResult(success=False, error="MINIMAX_API_KEY not configured")

    try:
        provider = get_llm_provider("minimax")
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"MiniMax provider init failed: {type(e).__name__}: {e}")

    model = getattr(settings, "MINIMAX_MODEL_BALANCED", "MiniMax-M3")
    src_mime = src.get("image_mime") or _MIME_PNG

    try:
        msg = await asyncio.to_thread(
            provider.client.chat.completions.create,
            model=model,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": (
                    "You produce strict JSON diff arrays for site-clone QA. "
                    "Reply with ONLY the JSON array, no prose, no markdown fences."
                )},
                {"role": "user", "content": [
                    {"type": "text", "text": "SOURCE (target to clone):"},
                    {"type": "image_url", "image_url": {"url": f"data:{src_mime};base64,{src_b64}"}},
                    {"type": "text", "text": "MODLIX BUILD (current state):"},
                    {"type": "image_url", "image_url": {"url": f"data:{_MIME_PNG};base64,{build_b64}"}},
                    {"type": "text", "text": _COMPARE_PROMPT},
                ]},
            ],
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"MiniMax compare call failed: {type(e).__name__}: {e}")

    raw = ""
    try:
        for choice in (msg.choices or []):
            content = getattr(choice.message, "content", "") or ""
            if isinstance(content, str):
                raw += content
            elif isinstance(content, list):
                # OpenAI-compat servers occasionally return content as a
                # list of parts; harvest text parts only.
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        raw += part.get("text", "")
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"MiniMax response shape unexpected: {type(e).__name__}: {e}")
    diffs, parse_err = _parse_diff_json(raw)
    if diffs is None:
        return ToolResult(
            success=False,
            error=f"could not parse diff JSON: {parse_err}\n--- raw ---\n{raw[:1200]}",
        )

    rank = {"high": 0, "medium": 1, "low": 2}
    diffs_sorted = sorted(diffs, key=lambda d: rank.get(str(d.get("severity", "")).lower(), 3))
    counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    for d in diffs_sorted:
        sev = str(d.get("severity", "")).lower()
        if sev in counts:
            counts[sev] += 1

    lines = [
        f"compare_to_source({page_name}) vs {source_handle} → {len(diffs_sorted)} diffs.",
        f"Severity: high={counts['high']} medium={counts['medium']} low={counts['low']}",
        "",
    ]
    for d in diffs_sorted[:15]:
        sev = str(d.get("severity", "?")).upper()
        section = d.get("section", "?")
        lines.append(f"[{sev}] {section}")
        if d.get("layout_diff"):
            lines.append(f"  layout:  {d['layout_diff']}")
        if d.get("copy_diff"):
            lines.append(f"  copy:    {d['copy_diff']}")
        if d.get("color_diff"):
            lines.append(f"  color:   {d['color_diff']}")
        if d.get("missing_elements"):
            lines.append(f"  missing: {d['missing_elements']}")
        if d.get("fix_suggestion"):
            lines.append(f"  fix:     {d['fix_suggestion']}")

    return ToolResult(
        success=True,
        summary="\n".join(lines),
        data={
            "page_name": page_name,
            "source_handle": source_handle,
            "severity_counts": counts,
            "diffs": diffs_sorted,
            # Build PNG attached so the agent can see the current state too.
            "image_base64": build_b64,
            "image_mime": _MIME_PNG,
        },
    )


compare_to_source_tool = ToolDefinition(
    name="compare_to_source",
    description=(
        "Diff a Modlix page against a cached source screenshot (from "
        "screenshot_external_url). Returns a JSON array of structured "
        "diffs (section / severity / copy_diff / layout_diff / color_diff / "
        "missing_elements / fix_suggestion). Use after EACH region you "
        "build during a clone. Iterate fixes until severity=high diffs "
        "are zero before moving to the next region.\n\n"
        "The current Modlix render is also attached as an image block so "
        "you can see exactly what the user would see."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description="Modlix page to render and diff"),
        ToolParameter(name="source_handle", type="string",
                      description="A source_handle returned by screenshot_external_url"),
        ToolParameter(name="app_code", type="string", required=False, description="Defaults to session app_code"),
        ToolParameter(name="client_code", type="string", required=False, description="Defaults to session client_code"),
        ToolParameter(name="viewport_width", type="integer", required=False, default=1440,
                      description="Build render viewport width (320-3840)."),
        ToolParameter(name="viewport_height", type="integer", required=False, default=900,
                      description="Build render viewport height (320-2160)."),
        ToolParameter(name="wait_ms", type="integer", required=False, default=2500,
                      description="Milliseconds to wait after build page loads before snapping."),
    ],
    execute=_execute_compare_to_source,
)
