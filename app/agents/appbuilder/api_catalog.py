"""API catalog - loads and formats REST API endpoint metadata.

The API catalog describes all REST endpoints across the backend services
(UI, Core, Security, Files) that the AI agent can call via tools or
reference when writing event functions (FetchData steps).

Loads a JSON file at startup and provides:
- to_prompt_context(): compact summary for system prompt injection
- lookup(): detailed endpoint info for the lookup_api tool
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default catalog path relative to the nocode-ai root
_DEFAULT_CATALOG_PATH = Path(__file__).parent.parent.parent.parent / "api-catalog.json"


class ApiCatalog:
    """Loads and formats the API catalog for system prompt injection."""

    def __init__(self, catalog_path: str | Path = "") -> None:
        self.catalog_path = Path(catalog_path) if catalog_path else _DEFAULT_CATALOG_PATH
        self._catalog: dict[str, Any] | None = None

    async def load(self) -> bool:
        """Load catalog from local JSON file. Returns True if loaded."""
        try:
            with open(self.catalog_path) as f:
                self._catalog = json.load(f)
            entity_count = sum(
                len(svc.get("entities", {}))
                for svc in self._catalog.get("services", {}).values()
            )
            logger.info(
                f"Loaded API catalog: {entity_count} entities across "
                f"{len(self._catalog.get('services', {}))} services from {self.catalog_path}"
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to load API catalog: {e}")
            self._catalog = None
            return False

    def get_service(self, service_name: str) -> dict[str, Any]:
        """Get a service definition (ui, core, security, files)."""
        if not self._catalog:
            return {}
        return self._catalog.get("services", {}).get(service_name, {})

    def get_entity(self, service_name: str, entity_name: str) -> dict[str, Any]:
        """Get an entity definition within a service."""
        svc = self.get_service(service_name)
        return svc.get("entities", {}).get(entity_name, {})

    def to_prompt_context(self) -> str:
        """Format catalog as a compact summary for system prompt injection.

        Returns a short overview of available services and entity names,
        directing the agent to use the lookup_api tool for full details.
        """
        if not self._catalog:
            return ""

        lines: list[str] = []
        lines.append("## Available APIs\n")
        lines.append(
            "All API calls go through the gateway. "
            "Required headers: `Authorization: Bearer {token}`, "
            "`X-Forwarded-Host`, `X-Forwarded-Port`, `clientCode`, `appCode`.\n"
        )

        # CRUD patterns - one-line summaries
        patterns = self._catalog.get("crudPatterns", {})
        if patterns:
            pattern_parts = []
            for name, pat in patterns.items():
                endpoints = pat.get("endpoints", [])
                methods = ", ".join(ep["method"] for ep in endpoints)
                pattern_parts.append(f"**{name}** ({methods})")
            lines.append(f"CRUD patterns: {'; '.join(pattern_parts)}\n")

        # Condition operators
        common = self._catalog.get("commonPatterns", {})
        operators = common.get("conditionOperators", [])
        if operators:
            lines.append(f"Condition operators: {', '.join(operators)}\n")

        # Services - just names and entity lists
        lines.append("### Services & Entities\n")
        services = self._catalog.get("services", {})
        for svc_name, svc in services.items():
            base_path = svc.get("basePath", "")
            entities = svc.get("entities", {})
            entity_names = ", ".join(entities.keys())
            lines.append(f"- **{svc_name.upper()}** (`{base_path}`): {entity_names}")

        lines.append("")
        lines.append(
            "Use the `lookup_api` tool to get full endpoint details, "
            "schemas, and custom endpoints for any service or entity."
        )

        return "\n".join(lines)

    def lookup(self, service: str, entity: str | None = None) -> str:
        """Look up detailed API info for a service or specific entity.

        Args:
            service: Service name (ui, core, security, files).
            entity: Optional entity name. If omitted, lists all entities
                    in the service with their descriptions.

        Returns:
            Formatted markdown string with endpoint details.
        """
        if not self._catalog:
            return "API catalog not loaded."

        svc = self._catalog.get("services", {}).get(service)
        if not svc:
            available = ", ".join(self._catalog.get("services", {}).keys())
            return f"Unknown service '{service}'. Available: {available}"

        base_path = svc.get("basePath", "")
        entities = svc.get("entities", {})

        # Service-level: list all entities with descriptions
        if not entity:
            lines = [f"## {service.upper()} Service (`{base_path}`)\n"]
            lines.append(f"{svc.get('description', '')}\n")
            lines.append(f"**{len(entities)} entities:**\n")
            for ent_name, ent in entities.items():
                pattern = ent.get("pattern", "custom")
                desc = ent.get("description", "")
                lines.append(f"- **{ent_name}** (`{base_path}{ent.get('path', '')}`) - {pattern} - {desc}")
            return "\n".join(lines)

        # Entity-level: full details
        ent = entities.get(entity)
        if not ent:
            available = ", ".join(entities.keys())
            return f"Unknown entity '{entity}' in {service}. Available: {available}"

        return self._format_entity_detail(base_path, entity, ent)

    def _format_entity_detail(
        self, base_path: str, entity_name: str, ent: dict[str, Any],
    ) -> str:
        """Format full detail for a single entity."""
        full_path = f"{base_path}{ent.get('path', '')}"
        pattern = ent.get("pattern", "custom")

        lines: list[str] = [
            f"## {entity_name} (`{full_path}`)\n",
            f"{ent.get('description', '')}\n",
        ]

        if pattern != "custom":
            self._append_crud_endpoints(lines, full_path, pattern)

        self._append_schema(lines, ent.get("schema", {}))
        self._append_custom_endpoints(lines, full_path, ent.get("customEndpoints", []))

        if pattern == "jooq_crud":
            self._append_query_format(lines)

        return "\n".join(lines)

    def _append_crud_endpoints(
        self, lines: list[str], full_path: str, pattern: str,
    ) -> None:
        """Append CRUD pattern endpoints to lines."""
        lines.append(f"**CRUD pattern:** `{pattern}`")
        pat_def = self._catalog.get("crudPatterns", {}).get(pattern, {})
        for ep in pat_def.get("endpoints", []):
            params = ep.get("params", {})
            param_str = ""
            if params:
                param_parts = [f"{k}: {v}" for k, v in params.items()]
                param_str = f" - params: {', '.join(param_parts)}"
            lines.append(
                f"  - `{ep.get('method', '')} {full_path}{ep.get('path', '')}` "
                f"- {ep.get('description', '')}{param_str}"
            )
        lines.append("")

    def _append_schema(self, lines: list[str], schema: dict[str, Any]) -> None:
        """Append schema fields to lines."""
        if not schema:
            return
        lines.append("**Schema fields:**")
        for field_name, field_desc in schema.items():
            if isinstance(field_desc, str):
                lines.append(f"  - `{field_name}`: {field_desc}")
            elif isinstance(field_desc, dict):
                lines.append(f"  - `{field_name}` (object):")
                for sub_name, sub_desc in field_desc.items():
                    lines.append(f"    - `{sub_name}`: {sub_desc}")
        lines.append("")

    def _append_custom_endpoints(
        self, lines: list[str], full_path: str, custom: list[dict[str, Any]],
    ) -> None:
        """Append custom endpoints to lines."""
        if not custom:
            return
        lines.append("**Custom endpoints:**")
        for ep in custom:
            params = ep.get("params", {})
            param_str = ""
            if params:
                param_parts = [f"{k}={v}" for k, v in params.items()]
                param_str = f"?{'&'.join(param_parts)}"
            body = ep.get("body", "")
            body_str = f" - body: {body}" if body else ""
            lines.append(
                f"  - `{ep.get('method', 'GET')} {full_path}{ep.get('path', '')}"
                f"{param_str}` - {ep.get('description', '')}{body_str}"
            )

    def _append_query_format(self, lines: list[str]) -> None:
        """Append jooq_crud query object format to lines."""
        common = self._catalog.get("commonPatterns", {})
        query_obj = common.get("queryObject")
        if not query_obj:
            return
        lines.append("")
        lines.append("**Query object format** (for POST /query):")
        for k, v in query_obj.items():
            lines.append(f"  - `{k}`: {json.dumps(v) if isinstance(v, (dict, list)) else v}")
