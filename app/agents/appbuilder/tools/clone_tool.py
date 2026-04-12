"""clone_website tool — scrapes a URL and creates a Modlix page from the HTML.

This is the high-level tool that combines:
1. Scrape HTML from URL
2. Convert HTML → Modlix componentDefinition (programmatic, no LLM)
3. Create or update the page via API

The LLM calls this tool instead of trying to construct the page manually.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.tools.base import (
    ToolDefinition,
    ToolParameter,
    ToolResult,
    ResultTier,
)

logger = logging.getLogger(__name__)


async def _execute_clone_website(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Scrape a website and create/update a Modlix page from it."""
    url = params.get("url", "")
    page_name = params.get("page_name", "home")
    app_code = params.get("app_code") or context.get("app_code", "")
    replace_existing = params.get("replace_existing", True)

    if not url:
        return ToolResult(success=False, error="url is required.")
    if not app_code:
        return ToolResult(success=False, error="app_code is required.")

    # Step 1: Scrape and convert
    from app.agents.appbuilder.tools.html_to_modlix import scrape_and_convert

    try:
        logger.info("Cloning %s → page '%s' in app '%s'", url, page_name, app_code)
        page_def = await scrape_and_convert(
            url=url,
            page_name=page_name,
            app_code=app_code,
            client_code=context.get("client_code", ""),
        )
    except Exception as e:
        logger.error("Clone scrape/convert failed: %s", e)
        return ToolResult(success=False, error=f"Failed to scrape/convert {url}: {e}")

    comp_count = len(page_def.get("componentDefinition", {}))
    logger.info("Converted %s to %d components", url, comp_count)

    # Step 2: LLM layout refinement (if screenshot available)
    screenshot = context.get("session_context", {}).get("scraped_screenshot")
    if not screenshot:
        # Try to take a fresh screenshot
        try:
            from app.agents.appbuilder.tools.web_scraper import _take_screenshot
            screenshot = await _take_screenshot(url)
        except Exception:
            pass

    if screenshot:
        try:
            from app.agents.appbuilder.tools.layout_refiner import refine_layout_with_vision
            fixes = await refine_layout_with_vision(
                screenshot, page_def["componentDefinition"], url,
            )
            if fixes:
                comp_count = len(page_def.get("componentDefinition", {}))
                logger.info("After refinement: %d components, %d fixes applied", comp_count, len(fixes))
        except Exception as e:
            logger.warning("Layout refinement failed: %s", e)

    # Step 3: Check if page exists
    from app.agents.appbuilder.tools._shared import get_saas_client
    client = get_saas_client()
    headers = context["headers"]

    existing_page = None
    if replace_existing:
        list_result = await client.get(
            "/api/ui/pages",
            headers=headers,
            params={"page": 0, "size": 1, "appCode": app_code, "name": page_name},
        )
        if list_result.success:
            content = list_result.data.get("content", []) if isinstance(list_result.data, dict) else []
            if content:
                existing_page = content[0]

    # Step 3: Create or update
    # Step 4: Save page (create or update)
    page_id = None
    if existing_page:
        page_id = existing_page.get("id")
        full_result = await client.get(f"/api/ui/pages/{page_id}", headers=headers)
        if full_result.success:
            existing_data = full_result.data
            existing_data["componentDefinition"] = page_def["componentDefinition"]
            existing_data["rootComponent"] = page_def["rootComponent"]
            existing_data["message"] = f"Cloned from {url}"
            save_result = await client.put(f"/api/ui/pages/{page_id}", headers=headers, json=existing_data)
            if not save_result.success:
                return ToolResult(success=False, error=f"Failed to update page: {save_result.error}")
        else:
            return ToolResult(success=False, error=f"Failed to read existing page: {full_result.error}")
    else:
        save_result = await client.post("/api/ui/pages", headers=headers, json=page_def)
        if save_result.success:
            page_id = save_result.data.get("id", "?") if isinstance(save_result.data, dict) else "?"
        else:
            return ToolResult(success=False, error=f"Failed to create page: {save_result.error}")

    # Step 5: Visual QA — screenshot generated page, compare with source, fix
    qa_summary = ""
    try:
        # Build the preview URL for the generated page
        session_ctx = context.get("session_context", {})
        auth = context.get("headers", {})
        forwarded_host = auth.get("X-Forwarded-Host", "apps.local.modlix.com")
        client_code = context.get("client_code", "SYSTEM")
        generated_url = f"https://{forwarded_host}/{app_code}/{client_code}/page/{page_name}"

        from app.agents.appbuilder.tools.visual_qa import iterative_visual_fix
        fixes = await iterative_visual_fix(
            source_url=url,
            generated_page_url=generated_url,
            comp_def=page_def["componentDefinition"],
            max_iterations=2,
        )

        if fixes:
            # Save the fixed version
            comp_count = len(page_def.get("componentDefinition", {}))
            if page_id:
                fix_result = await client.get(f"/api/ui/pages/{page_id}", headers=headers)
                if fix_result.success:
                    fix_data = fix_result.data
                    fix_data["componentDefinition"] = page_def["componentDefinition"]
                    fix_data["message"] = f"Visual QA fixes ({len(fixes)} corrections)"
                    await client.put(f"/api/ui/pages/{page_id}", headers=headers, json=fix_data)
            qa_summary = f"\nVisual QA: {len(fixes)} layout corrections applied."
    except Exception as e:
        logger.warning("Visual QA step failed: %s", e)
        qa_summary = f"\nVisual QA: skipped ({e})"

    return ToolResult(
        success=True,
        summary=(
            f"{'Updated' if existing_page else 'Created'} page '{page_name}' "
            f"with {comp_count} components cloned from {url}.\n"
            f"Page ID: {page_id}{qa_summary}\n"
            f"Preview: Use read(object_type='page', name='{page_name}') to see the structure."
        ),
        result_tier=ResultTier.COMPACT,
    )


CLONE_WEBSITE = ToolDefinition(
    name="clone_website",
    display_name="Clone Website",
    description=(
        "Clone a website by URL — scrapes the HTML, converts it to Modlix components "
        "with proper styleProperties, and creates/updates a page. Handles layout, images, "
        "text, navigation, and inline CSS automatically. Use this instead of manually "
        "building components when cloning an existing website."
    ),
    parameters=[
        ToolParameter(name="url", type="string", description="URL to clone.", required=True),
        ToolParameter(name="page_name", type="string", description="Name for the page (default 'home').", required=True),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
        ToolParameter(name="replace_existing", type="boolean", description="Replace existing page with same name (default true).", required=False),
    ],
    execute=_execute_clone_website,
    is_deferred=True,
    search_hint="clone website scrape HTML convert copy duplicate URL page",
    result_tier=ResultTier.COMPACT,
)
