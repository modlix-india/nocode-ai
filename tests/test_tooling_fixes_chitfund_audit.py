"""Regression tests for the tooling fixes from the Chit Fund build audit
(docs/chitfund-build-audit-2026-08-26.md).

Each test names the failure it guards against. The audit found that most of
the damage in that run traced to tools silently doing the wrong thing (a
dropped parameter, a fabricated catalog, a missing hint) rather than to the
model, so these lock the corrected behaviour at the tool boundary.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agents.appbuilder.tools import registry
from app.agents.appbuilder.tools.modlix import _conventions as c
from app.agents.appbuilder.tools.modlix import _page_ops as p_ops
from app.agents.appbuilder.tools.modlix.kirun import _hint_for_compile_error
from app.core.agent import BaseAgent
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

_TOOLS = {t.name: t for t in registry.ALL_TOOLS}


def _tool(name: str) -> ToolDefinition:
    assert name in _TOOLS, f"{name} not registered"
    return _TOOLS[name]


def _created(data: dict) -> ToolResult:
    return ToolResult(success=True, data=data, summary="(mock)")


# ── create_role: app scoping ─────────────────────────────────────────────
#
# Calls 32-33 of the audited run passed app_code="chitfund"; the tool only knew
# app_id and dropped it, so the roles landed client-scoped while every page
# gate used Authorities.CHITFUND.ROLE_*. Nobody could ever enter the app.


def _program_app_lookup(mock_client, app_code: str, app_id: int) -> None:
    mock_client.respond_to(
        "GET", "/api/security/applications",
        _created({"content": [{"id": app_id, "appCode": app_code, "status": "ACTIVE"}]}),
    )


@pytest.mark.asyncio
async def test_create_role_resolves_app_code_to_app_id_and_echoes_authority(mock_client, tool_context):
    _program_app_lookup(mock_client, "chitfund", 789)
    mock_client.set_default(_created({"id": 293, "name": "Owner"}))

    result = await _tool("create_role").execute({"name": "Owner", "app_code": "chitfund"}, tool_context)

    assert result.success, result.error
    post = mock_client.calls.by_method("POST")[0]
    assert "/api/security/rolev2" in post.path
    assert post.json["appId"] == "789"
    assert "Authorities.CHITFUND.ROLE_Owner" in result.summary
    assert "app=chitfund" in result.summary


@pytest.mark.asyncio
async def test_create_role_defaults_to_the_session_app(mock_client, tool_context):
    """No app_code param: scope to the session app like every other modlix tool."""
    _program_app_lookup(mock_client, tool_context["app_code"], 42)
    mock_client.set_default(_created({"id": 1}))

    result = await _tool("create_role").execute({"name": "Agent"}, tool_context)

    assert result.success, result.error
    assert mock_client.calls.by_method("POST")[0].json["appId"] == "42"
    assert f"Authorities.{tool_context['app_code'].upper()}.ROLE_Agent" in result.summary


@pytest.mark.asyncio
async def test_create_role_client_scoped_skips_lookup_and_warns(mock_client, tool_context):
    mock_client.set_default(_created({"id": 5}))

    result = await _tool("create_role").execute(
        {"name": "Reviewer", "client_scoped": True}, tool_context,
    )

    assert result.success, result.error
    assert mock_client.calls.by_method("GET") == []
    assert "appId" not in mock_client.calls.by_method("POST")[0].json
    assert "CLIENT-SCOPED" in result.summary
    assert "Authorities.ROLE_Reviewer" in result.summary
    assert "will NOT satisfy" in result.summary


@pytest.mark.asyncio
async def test_create_role_refuses_reserved_client_scoped_name(mock_client, tool_context):
    """Client-scoped 'Owner' IS Authorities.ROLE_Owner, the client-owner super-authority."""
    result = await _tool("create_role").execute({"name": "Owner", "client_scoped": True}, tool_context)

    assert result.success is False
    assert "Authorities.ROLE_Owner" in result.error
    assert "reserved" in result.error
    assert len(mock_client.calls) == 0, "must refuse before any HTTP call"


@pytest.mark.asyncio
async def test_create_role_fails_loudly_when_app_missing(mock_client, tool_context):
    """A missing security app must not silently degrade to a client-scoped role."""
    mock_client.set_default(_created({"content": []}))

    result = await _tool("create_role").execute({"name": "Owner", "app_code": "ghost"}, tool_context)

    assert result.success is False
    assert "ghost" in result.error
    assert mock_client.calls.by_method("POST") == []


@pytest.mark.asyncio
async def test_create_role_explicit_app_id_still_wins(mock_client, tool_context):
    mock_client.set_default(_created({"id": 7}))

    result = await _tool("create_role").execute({"name": "R", "app_id": "app-7"}, tool_context)

    assert result.success, result.error
    assert len(mock_client.calls) == 1
    assert mock_client.calls.last().json["appId"] == "app-7"


# ── dispatcher: unknown parameters are an error, not a no-op ─────────────


def _decl(*names: str, allow_unknown: bool = False) -> ToolDefinition:
    return ToolDefinition(
        name="t", description="d",
        parameters=[ToolParameter(name=n, type="string", description=n, required=False) for n in names],
        allow_unknown_params=allow_unknown,
    )


def test_unknown_param_is_rejected_with_nearest_name_hint():
    result = BaseAgent._reject_unknown_params(_decl("name", "app_id"), {"name": "x", "app_code": "y"})

    assert result is not None and result.success is False
    assert "'app_code'" in result.error
    assert "did you mean 'app_id'" in result.error
    assert "Nothing was executed" in result.error


def test_known_params_pass_through():
    assert BaseAgent._reject_unknown_params(_decl("name", "app_id"), {"name": "x"}) is None


def test_tools_without_declared_params_or_with_opt_out_are_not_judged():
    assert BaseAgent._reject_unknown_params(_decl(), {"anything": 1}) is None
    assert BaseAgent._reject_unknown_params(_decl("a", allow_unknown=True), {"zzz": 1}) is None
    assert BaseAgent._reject_unknown_params(_decl("a"), "not a dict") is None


def test_schema_advertises_no_additional_properties():
    schema = _decl("a").to_anthropic_tool()["input_schema"]
    assert schema["additionalProperties"] is False
    relaxed = _decl("a", allow_unknown=True).to_anthropic_tool()["input_schema"]
    assert "additionalProperties" not in relaxed


# ── Kirun DSL: operator-in-argument hint, on every save path ─────────────

_REAL_PARSER_ERROR = (
    "DSLParserError: Expected RIGHT_PAREN at Line 6, Column 69 (pos 478-479)\n"
    "Expected: RIGHT_PAREN\nActual: OPERATOR (+)"
)


def test_operator_in_argument_hint_wins_over_generic_expected_rule():
    hint = _hint_for_compile_error(_REAL_PARSER_ERROR)
    assert "parentheses" in hint
    assert "GenerateEvent" not in hint, "the generic 'expected' rule must not shadow the specific one"


@pytest.mark.asyncio
async def test_page_event_save_surfaces_the_compile_hint(mock_client, tool_context):
    """save_page_event_function_from_text used to return the bare parser error."""
    dsl = (
        "FUNCTION showReceipt\n    NAMESPACE _\n    LOGIC\n"
        '        receipt: UIEngine.SetStore(path = "Store.receipt", value = "Member: " + Parent.name)\n'
    )
    result = await _tool("save_page_event_function_from_text").execute(
        {"page_name": "route", "event_name": "showReceipt", "text": dsl}, tool_context,
    )

    assert result.success is False
    assert "Compile error" in result.error
    assert "Next step:" in result.error
    assert "parentheses" in result.error
    assert len(mock_client.calls) == 0, "compile failures must not reach the gateway"


# ── UIEngine catalog: generated from nocode-ui, served locally ───────────


def test_uiengine_catalog_has_real_functions_and_no_fabricated_ones():
    for real in ("FetchData", "SendData", "DeleteData", "SetStore", "Login", "Message", "CopyTextToClipboard"):
        assert real in c.UIENGINE_PRIMITIVES
    for fake in ("Read", "Create", "Update", "Delete", "GetStore", "OpenModal", "CloseModal", "Reload", "HTTPRequest", "Toast"):
        assert fake not in c.UIENGINE_PRIMITIVES, f"{fake} does not exist in nocode-ui"


def test_uiengine_catalog_matches_nocode_ui_checkout():
    """Drift guard: regenerate with scripts/gen_uiengine_catalog.py when this fails."""
    all_ts = Path(__file__).resolve().parents[2] / "nocode-ui" / "ui-app" / "client" / "src" / "functions" / "all.ts"
    if not all_ts.exists():
        pytest.skip("nocode-ui checkout not present next to nocode-ai")
    exported = set(re.findall(r"from\s+'\./(\w+)'", all_ts.read_text(encoding="utf-8")))
    assert exported == set(c.UIENGINE_SIGNATURES), (
        f"catalog drift: missing={sorted(exported - set(c.UIENGINE_SIGNATURES))} "
        f"stale={sorted(set(c.UIENGINE_SIGNATURES) - exported)}"
    )


@pytest.mark.asyncio
async def test_get_kirun_primitive_serves_uiengine_signatures_locally(mock_client, tool_context):
    result = await _tool("get_kirun_primitive").execute(
        {"namespace": "UIEngine", "name": "FetchData"}, tool_context,
    )
    assert result.success, result.error
    assert '"url"' in result.summary and '"output"' in result.summary
    assert len(mock_client.calls) == 0, "UIEngine builtins are answered without a platform call"


@pytest.mark.asyncio
async def test_get_kirun_primitive_names_the_real_set_for_a_fabricated_name(mock_client, tool_context):
    """Used to return success=True with a literal 'null' body for UIEngine.Read."""
    result = await _tool("get_kirun_primitive").execute({"namespace": "UIEngine", "name": "Read"}, tool_context)
    assert result.success is False
    assert "does not exist" in result.error
    assert "FetchData" in result.error
    assert len(mock_client.calls) == 0


# ── create_page / create_pages: permission at birth ──────────────────────


@pytest.mark.asyncio
async def test_create_page_carries_permission_and_merges_properties(mock_client, tool_context):
    mock_client.set_default(_created({"id": "pg1"}))

    result = await _tool("create_page").execute(
        {"name": "home", "title": "Home", "permission": "Authorities.Logged_IN", "properties": {"layout": {"value": "FLEX"}}},
        tool_context,
    )

    assert result.success, result.error
    body = mock_client.calls.by_method("POST")[0].json
    assert body["permission"] == "Authorities.Logged_IN"
    assert body["properties"]["title"] == {"name": {"value": "Home"}}, "skeleton title must survive the merge"
    assert body["properties"]["layout"] == {"value": "FLEX"}
    assert "permission='Authorities.Logged_IN'" in result.summary


@pytest.mark.asyncio
async def test_create_pages_validates_every_name_before_any_io(mock_client, tool_context):
    result = await _tool("create_pages").execute(
        {"pages": [{"name": "good"}, {"name": "bad-name"}, {"name": "good"}]}, tool_context,
    )
    assert result.success is False
    assert "Nothing was created" in result.error
    assert "item 1" in result.error and "item 2" in result.error and "duplicate" in result.error
    assert len(mock_client.calls) == 0


@pytest.mark.asyncio
async def test_create_pages_posts_each_page_with_its_permission(mock_client, tool_context):
    mock_client.set_default(_created({"id": "x"}))

    result = await _tool("create_pages").execute(
        {"pages": [
            {"name": "login", "title": "Sign in"},
            {"name": "home", "permission": "Authorities.Logged_IN"},
            {"name": "admin", "permission": "Authorities.Logged_IN and Authorities.TESTAPP.ROLE_Admin"},
        ]},
        tool_context,
    )

    assert result.success, result.error
    posts = mock_client.calls.by_method("POST")
    assert [p.json["name"] for p in posts] == ["login", "home", "admin"]
    assert "permission" not in posts[0].json
    assert posts[1].json["permission"] == "Authorities.Logged_IN"
    assert posts[2].json["permission"].endswith("ROLE_Admin")
    assert "Created 3 of 3" in result.summary


# ── add_components: one save, parent-first, all errors at once ───────────


def _page_doc(client_code: str = "SYSTEM") -> dict:
    page = p_ops.new_page_skeleton("route", "testapp", client_code)
    page["id"] = "pg1"
    return page


def _program_page_fetch(mock_client) -> None:
    # Detail route first: the list route's substring would also match the
    # detail path, and routes are consumed in order per matching call.
    mock_client.respond_to("GET", f"{p_ops.API_PREFIX}/pg1", _created(_page_doc()))
    mock_client.respond_to("GET", p_ops.API_PREFIX, _created({"content": [{"id": "pg1", "name": "route"}]}))


@pytest.mark.asyncio
async def test_add_components_writes_a_whole_subtree_in_one_save(mock_client, tool_context):
    _program_page_fetch(mock_client)
    mock_client.set_default(_created({"id": "pg1"}))

    result = await _tool("add_components").execute(
        {"page_name": "route", "components": [
            {"parent_key": "root", "component_type": "Grid", "component_key": "card",
             "style_properties": {"display": "flex", "gap": "8px"}},
            {"parent_key": "card", "component_type": "Text", "component_key": "title", "properties": {"text": "Page.heading"}},
            {"parent_key": "card", "component_type": "Button", "component_key": "btn", "properties": {"label": "Go"}},
        ]},
        tool_context,
    )

    assert result.success, result.error
    puts = mock_client.calls.by_method("PUT")
    assert len(puts) == 1, "the batch must be a single page save"
    comps = puts[0].json["componentDefinition"]
    assert set(comps) == {"root", "card", "title", "btn"}
    assert "title" in comps["card"]["children"] and "btn" in comps["card"]["children"]
    # Same auto-coercions as add_component: bare "Page.x" became an expression.
    assert comps["title"]["properties"]["text"]["location"]["type"] == "EXPRESSION"
    assert "Added 3 components" in result.summary


@pytest.mark.asyncio
async def test_add_components_rejects_the_whole_batch_and_lists_every_defect(mock_client, tool_context):
    result = await _tool("add_components").execute(
        {"page_name": "route", "components": [
            {"parent_key": "root", "component_type": "TextBox", "component_key": "a",
             "binding_paths": {"bindingPath": "Bogus.email"}},          # invalid store prefix
            {"parent_key": "root", "component_type": "Text", "component_key": "b",
             "style_properties": "display:flex"},                        # not a dict
            {"parent_key": "root", "component_type": "Text", "component_key": "a"},  # duplicate key
        ]},
        tool_context,
    )

    assert result.success is False
    assert "Nothing was added" in result.error
    assert "item 0" in result.error and "item 1" in result.error and "duplicate" in result.error
    assert len(mock_client.calls) == 0, "validation must finish before the page is even fetched"


@pytest.mark.asyncio
async def test_add_components_child_before_parent_fails_with_ordering_hint(mock_client, tool_context):
    _program_page_fetch(mock_client)

    result = await _tool("add_components").execute(
        {"page_name": "route", "components": [
            {"parent_key": "card", "component_type": "Text", "component_key": "title"},
            {"parent_key": "root", "component_type": "Grid", "component_key": "card"},
        ]},
        tool_context,
    )

    assert result.success is False
    assert "BEFORE their children" in result.error
    assert mock_client.calls.by_method("PUT") == [], "a failed batch must not save"


@pytest.mark.asyncio
async def test_add_component_single_still_works_through_shared_preparer(mock_client, tool_context):
    _program_page_fetch(mock_client)
    mock_client.set_default(_created({"id": "pg1"}))

    result = await _tool("add_component").execute(
        {"page_name": "route", "parent_key": "root", "component_type": "Button",
         "component_key": "go", "properties": {"label": "Go"}},
        tool_context,
    )

    assert result.success, result.error
    assert "Added Button 'go' under 'root'" in result.summary
    assert "go" in mock_client.calls.by_method("PUT")[0].json["componentDefinition"]


# ── registration / prompt plumbing ───────────────────────────────────────


def test_new_batch_tools_are_registered_and_hot():
    from app.agents.appbuilder.context import HOT_TOOLS
    for name in ("add_components", "create_pages", "create_role", "platform_doc_read"):
        assert name in _TOOLS
        assert name in HOT_TOOLS
    assert "describe_image" not in HOT_TOOLS


def test_persona_quotes_the_real_turn_limit():
    from app.agents.appbuilder.context import AGENT_PERSONA
    from app.config import settings
    assert "__MAX_TURNS__" not in AGENT_PERSONA
    assert f"hard turn limit is {settings.MAX_AGENT_TURNS} tool calls" in AGENT_PERSONA
    assert "DO NOT pass `app_id`" not in AGENT_PERSONA, "the roles guidance that contradicted the page gates"
    assert "defaultTheme: ..." not in AGENT_PERSONA, "dead key; runtime reads properties.themes"
    assert "__SOFT_TURNS__" not in AGENT_PERSONA
    assert f"Past {int(settings.MAX_AGENT_TURNS * 0.7)}" in AGENT_PERSONA
    assert 'type="Grid"' not in AGENT_PERSONA and "type=TextBox" not in AGENT_PERSONA, (
        "recipes must use component_type; `type` is not a parameter and is now rejected"
    )


# ── review follow-ups (adversarial review of the fixes, 2026-08-26) ─────────


def test_hot_tools_are_all_registered_and_the_audit_set_is_hot():
    from app.agents.appbuilder.context import HOT_TOOLS, _TOOL_NAME_TO_GROUP
    names = {t.name for t in registry.ALL_TOOLS}
    assert HOT_TOOLS <= names, sorted(HOT_TOOLS - names)
    audit_set = {
        "add_components", "create_pages", "platform_doc_list", "platform_doc_read", "pattern_search",
        "pattern_read", "kb_app_list_sections", "which_environment", "create_role", "assign_role",
        "build_authority", "list_users", "remove_component_styles",
    }
    assert audit_set <= HOT_TOOLS, sorted(audit_set - HOT_TOOLS)
    # Recent use of the batch tools must pull in the page_operations detail block.
    assert _TOOL_NAME_TO_GROUP.get("add_components") == "page_operations"
    assert _TOOL_NAME_TO_GROUP.get("create_pages") == "page_operations"


def test_operator_hint_also_covers_and_or_in_argument_position():
    err = "DSLParserError: Expected RIGHT_PAREN at Line 3, Column 40\nExpected: RIGHT_PAREN\nActual: IDENTIFIER (and)"
    assert "parentheses" in _hint_for_compile_error(err)


@pytest.mark.asyncio
async def test_add_components_siblings_render_in_list_order(mock_client, tool_context):
    """display_order defaulted to 0 for every item, and the runtime breaks ties
    by key name: [header, body, footer] rendered as body, footer, header."""
    _program_page_fetch(mock_client)
    mock_client.set_default(_created({"id": "pg1"}))

    result = await _tool("add_components").execute(
        {"page_name": "route", "components": [
            {"parent_key": "root", "component_type": "Grid", "component_key": "header"},
            {"parent_key": "root", "component_type": "Grid", "component_key": "body"},
            {"parent_key": "root", "component_type": "Grid", "component_key": "footer"},
            {"parent_key": "body", "component_type": "Text", "component_key": "bodyText"},
        ]},
        tool_context,
    )

    assert result.success, result.error
    comps = mock_client.calls.by_method("PUT")[0].json["componentDefinition"]
    assert [comps[k]["displayOrder"] for k in ("header", "body", "footer")] == [0, 1, 2]
    assert comps["bodyText"]["displayOrder"] == 0, "first child of a new parent starts at 0"


@pytest.mark.asyncio
async def test_add_component_appends_after_existing_siblings(mock_client, tool_context):
    page = _page_doc()
    page["componentDefinition"]["existing"] = {"key": "existing", "type": "Text", "displayOrder": 5, "children": {}}
    page["componentDefinition"]["root"]["children"] = {"existing": True}
    mock_client.respond_to("GET", f"{p_ops.API_PREFIX}/pg1", _created(page))
    mock_client.respond_to("GET", p_ops.API_PREFIX, _created({"content": [{"id": "pg1", "name": "route"}]}))
    mock_client.set_default(_created({"id": "pg1"}))

    result = await _tool("add_component").execute(
        {"page_name": "route", "parent_key": "root", "component_type": "Button", "component_key": "late"},
        tool_context,
    )

    assert result.success, result.error
    comps = mock_client.calls.by_method("PUT")[0].json["componentDefinition"]
    assert comps["late"]["displayOrder"] == 6
    # An explicit value is still honoured.
    _program_page_fetch(mock_client)
    result = await _tool("add_component").execute(
        {"page_name": "route", "parent_key": "root", "component_type": "Button", "component_key": "first", "display_order": 0},
        tool_context,
    )
    assert mock_client.calls.by_method("PUT")[-1].json["componentDefinition"]["first"]["displayOrder"] == 0


@pytest.mark.asyncio
async def test_add_components_aggregates_bad_display_order_instead_of_raising(mock_client, tool_context):
    result = await _tool("add_components").execute(
        {"page_name": "route", "components": [
            {"parent_key": "root", "component_type": "Text", "component_key": "a", "display_order": "second"},
            {"parent_key": "root", "component_type": "Text", "component_key": 42},
        ]},
        tool_context,
    )
    assert result.success is False
    assert "display_order must be an integer" in result.error
    assert len(mock_client.calls) == 0


@pytest.mark.asyncio
async def test_add_components_refuses_over_cap_before_any_io(mock_client, tool_context):
    items = [{"parent_key": "root", "component_type": "Text"}] * 61
    result = await _tool("add_components").execute({"page_name": "route", "components": items}, tool_context)
    assert result.success is False
    assert "max 60" in result.error
    assert len(mock_client.calls) == 0


@pytest.mark.asyncio
async def test_add_component_required_field_error_is_unchanged_by_the_shared_preparer(mock_client, tool_context):
    result = await _tool("add_component").execute({"page_name": "route", "component_type": "TextBox"}, tool_context)
    assert result.error == "`page_name`, `parent_key`, `component_type` are required"
    assert len(mock_client.calls) == 0


@pytest.mark.asyncio
async def test_create_pages_reports_partial_failure_per_page(mock_client, tool_context):
    mock_client.enqueue(
        _created({"id": "p1"}),
        ToolResult(success=False, error="HTTP 409: already present"),
        _created({"id": "p3"}),
    )
    result = await _tool("create_pages").execute(
        {"pages": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}, tool_context,
    )
    assert result.success is True, "two of three landed; the caller must learn which"
    assert "Created 2 of 3" in result.summary
    assert "FAILED" in result.summary and "b: HTTP 409" in result.summary


@pytest.mark.asyncio
async def test_create_pages_all_failed_is_an_error(mock_client, tool_context):
    mock_client.set_default(ToolResult(success=False, error="HTTP 500"))
    result = await _tool("create_pages").execute({"pages": [{"name": "a"}]}, tool_context)
    assert result.success is False
    assert "Created 0 of 1" in result.error


@pytest.mark.asyncio
async def test_create_page_normalises_a_bare_title_inside_properties(mock_client, tool_context):
    mock_client.set_default(_created({"id": "pg1"}))
    result = await _tool("create_page").execute(
        {"name": "home", "properties": {"title": "Plain"}}, tool_context,
    )
    assert result.success, result.error
    body = mock_client.calls.by_method("POST")[0].json
    assert body["properties"]["title"] == {"name": {"value": "Plain"}}


@pytest.mark.asyncio
async def test_create_role_explicit_app_id_does_not_borrow_the_session_apps_authority(mock_client, tool_context):
    """Explicit app_id for a different app: the echoed token must not claim the session app."""
    mock_client.set_default(_created({"id": 9}))
    result = await _tool("create_role").execute({"name": "Viewer", "app_id": "555"}, tool_context)
    assert result.success, result.error
    assert mock_client.calls.by_method("GET") == [], "no lookup without an app_code to check"
    assert f"Authorities.{tool_context['app_code'].upper()}." not in result.summary
    assert "Authorities.<APPCODE>.ROLE_Viewer" in result.summary
    assert "app_id=555" in result.summary


@pytest.mark.asyncio
async def test_create_role_rejects_app_id_that_contradicts_app_code(mock_client, tool_context):
    _program_app_lookup(mock_client, "chitfund", 789)
    result = await _tool("create_role").execute(
        {"name": "Viewer", "app_id": "555", "app_code": "chitfund"}, tool_context,
    )
    assert result.success is False
    assert "does not belong" in result.error
    assert mock_client.calls.by_method("POST") == []


def test_auto_confirm_stamp_only_targets_tools_that_declare_confirmed():
    """Regression: the harness stamped confirmed=True into copy/delete, which
    never declared it, so the new unknown-param check rejected every headless
    call to them."""
    import inspect
    src = inspect.getsource(BaseAgent._run_tool_block)
    assert 'any(p.name == "confirmed" for p in tool.parameters)' in src
    for name in ("copy", "delete"):
        tool = _TOOLS.get(name)
        if tool is None:
            continue
        assert "confirmed" not in {p.name for p in tool.parameters}
        # and so the dispatcher would have refused the stamped key:
        assert BaseAgent._reject_unknown_params(tool, {"object_type": "page", "id": "x", "confirmed": True}) is not None


def test_adzump_set_campaign_spec_declares_its_prompt_instructed_flags():
    from app.agents.adzump.tools.campaign_data import ALLOWED_FIELDS  # noqa: F401
    from app.agents.adzump.tools import campaign_data
    tool = next(t for t in vars(campaign_data).values() if isinstance(t, ToolDefinition) and t.name == "set_campaign_spec")
    declared = {p.name for p in tool.parameters}
    assert {"ig_page_declined", "competitive_analysis_declined"} <= declared
