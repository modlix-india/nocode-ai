"""API catalog — loads and formats REST API endpoint metadata.

The API catalog describes all REST endpoints across the backend services
(UI, Core, Security, Files) that the AI agent can call via tools or
reference when writing event functions (FetchData steps).

Follows the same pattern as ComponentCatalog — loads a JSON file at
startup and provides to_prompt_context() for system prompt injection.
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
        """Format catalog as concise markdown for system prompt.

        Produces a compact API reference suitable for inclusion in the
        agent's system prompt, covering:
        - Standard CRUD patterns (defined once)
        - Per-service entity listings with descriptions
        - Custom endpoints for each entity
        - Entity schemas (key fields)
        """
        if not self._catalog:
            return ""

        lines: list[str] = []
        lines.append("## API Reference\n")
        lines.append(
            "All API calls go through the gateway. "
            "Required headers: `Authorization: Bearer {token}`, `X-Forwarded-Host`, `X-Forwarded-Port`, `clientCode`, `appCode`.\n"
        )

        # CRUD patterns
        lines.append("### Standard CRUD Patterns\n")
        patterns = self._catalog.get("crudPatterns", {})
        for pattern_name, pattern in patterns.items():
            endpoints = pattern.get("endpoints", [])
            ep_summary = ", ".join(
                f"{ep['method']} {ep['path']}" for ep in endpoints
            )
            lines.append(f"**{pattern_name}**: {pattern.get('description', '')}")
            lines.append(f"Endpoints: {ep_summary}\n")

        # Common patterns
        common = self._catalog.get("commonPatterns", {})
        operators = common.get("conditionOperators", [])
        if operators:
            lines.append(f"**Condition operators**: {', '.join(operators)}\n")

        # Services
        services = self._catalog.get("services", {})
        for svc_name, svc in services.items():
            base_path = svc.get("basePath", "")
            lines.append(f"### {svc_name.upper()} Service (`{base_path}`)\n")
            lines.append(f"{svc.get('description', '')}\n")

            entities = svc.get("entities", {})
            for ent_name, ent in entities.items():
                ent_path = ent.get("path", "")
                pattern = ent.get("pattern", "custom")
                full_path = f"{base_path}{ent_path}"

                lines.append(f"**{ent_name}** (`{full_path}`)")
                lines.append(f"{ent.get('description', '')}")

                if pattern != "custom":
                    lines.append(f"CRUD: `{pattern}` pattern")

                # Schema key fields
                schema = ent.get("schema", {})
                if schema:
                    field_parts = []
                    for field_name, field_desc in schema.items():
                        if isinstance(field_desc, str):
                            field_parts.append(f"{field_name}: {field_desc}")
                        elif isinstance(field_desc, dict):
                            # Nested schema (e.g. properties, variables, definition)
                            sub_fields = ", ".join(field_desc.keys())
                            field_parts.append(f"{field_name}: {{{sub_fields}}}")
                    if field_parts:
                        lines.append(f"Fields: {'; '.join(field_parts)}")

                # Custom endpoints
                custom = ent.get("customEndpoints", [])
                for ep in custom:
                    method = ep.get("method", "GET")
                    path = ep.get("path", "")
                    desc = ep.get("description", "")
                    params = ep.get("params", {})
                    param_str = ""
                    if params:
                        param_parts = [f"{k}={v}" for k, v in params.items()]
                        param_str = f"?{'&'.join(param_parts)}"
                    lines.append(f"  + `{method} {full_path}{path}{param_str}` — {desc}")

                lines.append("")

        return "\n".join(lines)
