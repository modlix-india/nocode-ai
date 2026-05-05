"""Shared utilities for appbuilder tools.

Provides the SaasClient singleton and common helpers used by all tools.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.tools.base import ToolResult
from app.core.tools.http_client import SaasClient

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-zA-Z]+$")


def require_app_code(context: dict) -> tuple[str, ToolResult | None]:
    """Extract appCode from context, returning an error if missing.

    Returns:
        Tuple of (app_code, error). If error is not None, the tool should return it immediately.
    """
    app_code = context.get("app_code", "")
    if not app_code:
        return "", ToolResult(
            success=False,
            error="No appCode set. Use list_applications first to search for the application and determine its appCode before calling this tool.",
        )
    return app_code, None


def validate_name(name: str) -> ToolResult | None:
    """Validate an entity name — must contain only letters (a-z, A-Z).

    Returns a ToolResult with an error if invalid, or None if valid.
    """
    if not name:
        return ToolResult(success=False, error="Name must not be empty.")
    if not _NAME_RE.match(name):
        return ToolResult(
            success=False,
            error=f"Invalid name '{name}'. Names must contain only alphabetic characters (a-z, A-Z), no numbers, spaces, or special characters.",
        )
    return None

async def save_entity(
    client: SaasClient,
    api_path: str,
    entity_id: str,
    entity_data: dict,
    headers: dict[str, str],
    user_client_code: str,
    message: str = "",
) -> ToolResult:
    """Save an entity with override-awareness.

    If the entity's ``clientCode`` matches the user's client, performs a normal
    PUT update.  Otherwise the user is editing a shared object — strip the
    ``id`` and POST so the backend creates an override for the user's client.

    Callers should set ``entity_data["message"]`` to the commit message before
    calling this function (or pass it via the ``message`` argument).
    """
    # Pass through the message set by the caller; allow override via arg
    entity_data = {**entity_data, "message": message or entity_data.get("message", "")}

    object_client = entity_data.get("clientCode", "")

    if object_client and object_client != user_client_code:
        # Editing another client's object → create override (POST without id)
        override_data = {k: v for k, v in entity_data.items() if k != "id"}
        result = await client.post(api_path, headers=headers, json=override_data)
    else:
        # Own object → normal update
        result = await client.put(f"{api_path}/{entity_id}", headers=headers, json=entity_data)

    if not result.success:
        return ToolResult(success=False, error=f"Failed to save: {result.error}")

    return ToolResult(success=True, data=result.data)


_client: SaasClient | None = None


def get_saas_client() -> SaasClient:
    """Get the shared SaasClient singleton."""
    global _client
    if _client is None:
        from app.config import settings
        _client = SaasClient(settings.GATEWAY_URL)
    return _client


async def close_saas_client() -> None:
    """Close the SaasClient (call on shutdown)."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


# ── Modlix site detection ──────────────────────────────────────────


@dataclass
class ModlixPageData:
    """Data extracted from a Modlix-built site via getStore()."""
    page_name: str
    app_code: str
    client_code: str
    component_definition: dict[str, Any]
    event_functions: dict[str, Any] = field(default_factory=dict)
    root_component: str = "root"
    properties: dict[str, Any] = field(default_factory=dict)
    translations: dict[str, Any] = field(default_factory=dict)
    page_id: str = ""


async def detect_modlix_site(url: str) -> ModlixPageData | None:
    """Load a URL in Playwright and check if it's a Modlix-built site.

    If the site has a global ``getStore()`` function that returns a
    ``pageDefinition`` with ``componentDefinition``, it's a Modlix site
    and we can extract the page JSON directly — 100% fidelity clone.

    Returns ModlixPageData if detected, None otherwise.
    """
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                except Exception as nav_err:
                    logger.warning("Modlix detection: navigation failed: %s", nav_err)
                    await browser.close()
                    return None

            # Wait for any JS framework to initialize
            await asyncio.sleep(3)

            # Check if getStore exists and has pageDefinition
            detection = await page.evaluate("""() => {
                try {
                    if (typeof getStore !== 'function') return null;
                    const store = getStore();
                    if (!store || !store.pageDefinition) return null;
                    const pd = store.pageDefinition;
                    const pageNames = Object.keys(pd);
                    if (pageNames.length === 0) return null;

                    // Return page names and basic info (not the full data yet)
                    const info = {};
                    for (const name of pageNames) {
                        const entry = pd[name];
                        info[name] = {
                            appCode: entry.appCode || '',
                            clientCode: entry.clientCode || '',
                            id: entry.id || '',
                            hasComponentDef: !!entry.componentDefinition,
                            componentCount: entry.componentDefinition
                                ? Object.keys(entry.componentDefinition).length : 0,
                        };
                    }
                    return { pageNames, info };
                } catch (e) {
                    return null;
                }
            }""")

            if not detection:
                logger.info("Modlix detection: not a Modlix site (%s)", url)
                await browser.close()
                return None

            page_names = detection.get("pageNames", [])
            info = detection.get("info", {})
            logger.info("Modlix site detected! Pages: %s", page_names)

            # Determine which page to extract — try to match from URL path
            # Modlix URLs: /appCode/clientCode/page/pageName or custom routes
            target_page = page_names[0]  # default: first page
            url_path = url.rstrip("/").split("/")
            # Check if URL ends with a page name that matches
            for name in page_names:
                if name.lower() in [seg.lower() for seg in url_path[-3:]]:
                    target_page = name
                    break

            page_info = info.get(target_page, {})
            logger.info("Extracting page '%s': appCode=%s, clientCode=%s, %d components",
                        target_page, page_info.get("appCode", "?"),
                        page_info.get("clientCode", "?"),
                        page_info.get("componentCount", 0))

            # Now extract the full page data (componentDefinition can be large)
            full_data = await page.evaluate("""(pageName) => {
                try {
                    const entry = getStore().pageDefinition[pageName];
                    if (!entry) return null;
                    return {
                        componentDefinition: entry.componentDefinition || {},
                        eventFunctions: entry.eventFunctions || {},
                        rootComponent: entry.rootComponent || 'root',
                        properties: entry.properties || {},
                        translations: entry.translations || {},
                        appCode: entry.appCode || '',
                        clientCode: entry.clientCode || '',
                        id: entry.id || '',
                    };
                } catch (e) {
                    return null;
                }
            }""", target_page)

            await browser.close()

            if not full_data or not full_data.get("componentDefinition"):
                logger.warning("Modlix detection: page '%s' has no componentDefinition", target_page)
                return None

            return ModlixPageData(
                page_name=target_page,
                app_code=full_data["appCode"],
                client_code=full_data["clientCode"],
                component_definition=full_data["componentDefinition"],
                event_functions=full_data["eventFunctions"],
                root_component=full_data["rootComponent"],
                properties=full_data["properties"],
                translations=full_data["translations"],
                page_id=full_data.get("id", ""),
            )

    except Exception as e:
        logger.warning("Modlix detection failed: %s", e)
        return None
