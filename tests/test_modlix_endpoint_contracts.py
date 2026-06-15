"""Broad endpoint-shape contracts for ported modlix tools.

For each representative tool we dispatch a minimal call against the
``mock_client`` SaasClient stand-in and assert the recorded HTTP method +
path matches the documented endpoint. This is the cross-cutting "do we
still hit the right URL?" guard rail — individual tool semantics are
covered by per-module test files.

Notes on coverage:
  - ``list_component_types`` is intentionally skipped from the parametrized
    list — it reads from the in-process catalog and never issues an HTTP
    call.
  - Tools that need a list-then-fetch (``get_*``) aren't in this file; the
    parametrized cases below are all single-call list/verify tools.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.appbuilder.tools.registry import ALL_TOOLS
from app.core.tools.base import ToolResult


# ── Endpoint contract table ──────────────────────────────────────────────
#
# (tool_name, params, expected_method, expected_path_substring)
#
# Path fragments are substring matches against the recorded request path
# so the assertions stay stable across query-string ordering changes.

ENDPOINT_CASES: list[tuple[str, dict[str, Any], str, str]] = [
    # app_admin
    ("list_apps", {}, "GET", "/api/security/applications"),
    ("whoami", {}, "GET", "/api/security/verifyToken"),
    ("list_themes", {}, "GET", "/api/ui/themes"),
    ("list_styles", {}, "GET", "/api/ui/styles"),
    ("list_uri_paths", {}, "GET", "/api/ui/uripaths"),
    # messaging
    ("list_notifications", {}, "GET", "/api/core/notifications"),
    ("list_connections", {}, "GET", "/api/core/connections"),
    ("list_templates", {}, "GET", "/api/core/templates"),
    ("list_event_definitions", {}, "GET", "/api/core/eventDefinitions"),
    ("list_event_actions", {}, "GET", "/api/core/eventActions"),
    # runtime
    ("list_personalizations", {}, "GET", "/api/ui/personalization"),
    ("count_personalizations", {}, "GET", "/api/ui/personalization"),
    # security
    ("list_users", {}, "GET", "/api/security/users"),
    ("list_clients", {}, "GET", "/api/security/clients"),
    ("list_roles", {}, "GET", "/api/security/roles"),
    ("list_profiles", {}, "GET", "/api/security/profile"),
    ("verify_token", {}, "GET", "/api/security/verifyToken"),
    # schemas — runtime='ui' so the routed path lands on /api/ui/schemas
    ("list_schemas", {"runtime": "ui"}, "GET", "/api/ui/schemas"),
    ("list_storages", {}, "GET", "/api/core/storages"),
    # pages
    ("list_pages", {}, "GET", "/api/ui/pages"),
    # kirun
    ("list_functions", {}, "GET", "/api/ui/functions"),
    # kirun_events — listing event functions reads the host page
    ("list_page_event_functions", {"page_name": "login"}, "GET", "/api/ui/pages"),
]


def _find_tool(name: str):
    return next((t for t in ALL_TOOLS if t.name == name), None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,params,expected_method,expected_path",
    ENDPOINT_CASES,
    ids=[c[0] for c in ENDPOINT_CASES],
)
async def test_modlix_endpoint_contract(
    tool_name, params, expected_method, expected_path,
    mock_client, tool_context,
):
    tool = _find_tool(tool_name)
    if tool is None:
        pytest.skip(f"tool {tool_name!r} not found in ALL_TOOLS")

    # Default mock response ({"content":[],"totalElements":0}) is enough for
    # every list-style call; tools that issue more than one request still
    # have their FIRST call recorded, which is what we assert on.
    result = await tool.execute(params, tool_context)

    assert isinstance(result, ToolResult), (
        f"{tool_name} did not return a ToolResult, got {type(result)!r}"
    )
    assert len(mock_client.calls) >= 1, (
        f"{tool_name} returned without issuing any HTTP call "
        f"(result.success={result.success}, error={result.error!r})"
    )

    first = mock_client.calls.calls[0]
    assert first.method == expected_method, (
        f"{tool_name}: expected method {expected_method}, got {first.method} "
        f"on path {first.path}"
    )
    assert expected_path in first.path, (
        f"{tool_name}: expected path containing {expected_path!r}, "
        f"got {first.path!r}"
    )


# ── Regression: POST body carries supplied fields ────────────────────────


@pytest.mark.asyncio
async def test_create_role_carries_payload(mock_client, tool_context):
    """create_role POSTs to /api/security/rolesV2 with name + description.

    Verifies the payload reaches the wire — not just the URL — so future
    refactors of the body shape stay caught.
    """
    tool = _find_tool("create_role")
    if tool is None:
        pytest.skip("create_role not found in ALL_TOOLS")

    # Programmed response: simulate a created role row.
    mock_client.set_default(ToolResult(
        success=True,
        data={"id": "role-123", "name": "Reviewer"},
        summary="(mock created)",
    ))

    result = await tool.execute(
        {
            "name": "Reviewer",
            "description": "Reviews submissions",
            "app_id": "app-7",
        },
        tool_context,
    )

    assert result.success is True, f"create_role failed: {result.error}"
    assert len(mock_client.calls) == 1
    call = mock_client.calls.last()
    assert call.method == "POST"
    assert "/api/security/rolesV2" in call.path
    assert isinstance(call.json, dict)
    assert call.json.get("name") == "Reviewer"
    assert call.json.get("description") == "Reviews submissions"
    # snake_case → camelCase conversion happens in the tool body.
    assert call.json.get("appId") == "app-7"


# ── Regression: missing app_code fails before any HTTP call ─────────────


@pytest.mark.asyncio
async def test_missing_app_code_errors_cleanly_no_http(mock_client, tool_context):
    """list_pages without an app_code must error out without hitting the wire.

    Uses an app_code-less context. list_pages is chosen because its tool
    fundamentally requires app_code (pages are app-scoped) — unlike
    list_apps / whoami / list_clients which are global.
    """
    tool = _find_tool("list_pages")
    if tool is None:
        pytest.skip("list_pages not found in ALL_TOOLS")

    bare_context = {
        "app_code": "",  # explicitly empty
        "client_code": "SYSTEM",
        "headers": dict(tool_context["headers"]),
    }

    result = await tool.execute({}, bare_context)

    assert result.success is False, (
        "list_pages should have refused without app_code; "
        f"got success=True summary={result.summary!r}"
    )
    assert result.error, "expected a non-empty error message"
    assert len(mock_client.calls) == 0, (
        f"list_pages issued {len(mock_client.calls)} HTTP call(s) "
        "before validating app_code; should refuse locally."
    )
