"""API catalog lookup tool — on-demand endpoint detail retrieval.

Instead of injecting the full API catalog into every system prompt,
the agent gets a compact summary and uses this tool to look up
detailed endpoint info only when needed.
"""

from __future__ import annotations

from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

# ── Module-level singleton (set during startup) ───────────────

_api_catalog: Any = None


def set_api_catalog(catalog: Any) -> None:
    """Set the ApiCatalog singleton for tool use. Called once at startup."""
    global _api_catalog
    _api_catalog = catalog


# ── Tool execute function ─────────────────────────────────────

async def _lookup_api_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Look up detailed API endpoint info from the in-memory catalog."""
    if not _api_catalog:
        return ToolResult(success=False, error="API catalog not loaded.")

    service = params["service"]
    entity = params.get("entity")

    result_text = _api_catalog.lookup(service, entity)
    return ToolResult(success=True, summary=result_text)


# ── Tool definition ───────────────────────────────────────────

lookup_api = ToolDefinition(
    name="lookup_api",
    display_name="Lookup API",
    description=(
        "Look up detailed API endpoint information for a backend service or entity. "
        "Returns full endpoint paths, request/response schemas, CRUD pattern details, "
        "and custom endpoints. Use this when you need to make API calls "
        "(e.g., FetchData steps, API connections, data queries)."
    ),
    parameters=[
        ToolParameter(
            name="service",
            type="string",
            description="Backend service name.",
            required=True,
            enum=["ui", "core", "security", "files"],
        ),
        ToolParameter(
            name="entity",
            type="string",
            description=(
                "Entity name within the service (e.g. 'pages', 'users', 'storages'). "
                "If omitted, lists all entities in the service with descriptions."
            ),
            required=False,
        ),
    ],
    execute=_lookup_api_execute,
)

API_CATALOG_TOOLS: list[ToolDefinition] = [lookup_api]
