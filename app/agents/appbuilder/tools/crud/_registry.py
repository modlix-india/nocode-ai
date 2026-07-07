"""Object type registry - maps each object_type to its API config.

Central routing table used by all generic CRUD handlers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ObjectTypeConfig:
    """Configuration for a single object type in the generic CRUD system."""

    object_type: str
    display_name: str
    api_path: str  # Primary API endpoint

    # Endpoint overrides (None = use api_path)
    list_api_path: str | None = None
    create_api_path: str | None = None
    delete_api_path: str | None = None

    # Behavioral flags
    uses_name_lookup: bool = False  # Page: fetch by name, not ID
    has_variables: bool = False  # Theme: uses variables instead of definition
    requires_confirmation: bool = False  # Theme: confirmed=true gate
    has_namespace: bool = False  # Function: extra namespace param
    has_special_create: bool = False  # Application: custom create body
    has_page_sub_ops: bool = False  # Page: component/event sub-operations


# All supported object types
OBJECT_TYPE_ENUM = [
    "page", "connection", "workflow", "template", "uripath",
    "function", "schema", "theme", "style", "application",
]


OBJECT_TYPES: dict[str, ObjectTypeConfig] = {
    "page": ObjectTypeConfig(
        object_type="page",
        display_name="Page",
        api_path="/api/ui/pages",
        uses_name_lookup=True,
        has_page_sub_ops=True,
    ),
    "connection": ObjectTypeConfig(
        object_type="connection",
        display_name="API Connection",
        api_path="/api/core/connections",
    ),
    "workflow": ObjectTypeConfig(
        object_type="workflow",
        display_name="Workflow",
        api_path="/api/core/workflows",
    ),
    "template": ObjectTypeConfig(
        object_type="template",
        display_name="Template",
        api_path="/api/core/templates",
    ),
    "uripath": ObjectTypeConfig(
        object_type="uripath",
        display_name="URI Path",
        api_path="/api/ui/uripaths",
    ),
    "function": ObjectTypeConfig(
        object_type="function",
        display_name="Function",
        api_path="/api/ui/functions",
        has_namespace=True,
    ),
    "schema": ObjectTypeConfig(
        object_type="schema",
        display_name="Schema",
        api_path="/api/ui/schemas",
    ),
    "theme": ObjectTypeConfig(
        object_type="theme",
        display_name="Theme",
        api_path="/api/ui/themes",
        has_variables=True,
        requires_confirmation=True,
    ),
    "style": ObjectTypeConfig(
        object_type="style",
        display_name="Style",
        api_path="/api/ui/styles",
    ),
    "application": ObjectTypeConfig(
        object_type="application",
        display_name="Application",
        api_path="/api/ui/applications",
        list_api_path="/api/security/applications/query",
        create_api_path="/api/multi/application",
        delete_api_path="/api/multi/application",
        has_special_create=True,
    ),
}
