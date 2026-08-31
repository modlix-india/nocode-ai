"""The edit classifier: which tool calls become lore observations.

This is the path that carries lore's real evidence, and it is also the one that
can quietly poison an app's knowledge, because it runs on every tool call
without anyone looking. Two properties matter and both are pinned here:

  * it never classifies a read as an edit (that would record changes that never
    happened), and
  * it never classifies a local file or cache operation as a definition edit.

The registry-wide tests deliberately assert against the REAL tool list, so a
newly added tool that the rules cannot place shows up as a failure here rather
than as silence in production.
"""

from __future__ import annotations

import pytest

from app.services.lore.watch import EditFact, action_for, classify


# ── Reads must never look like edits ─────────────────────────────────────


@pytest.mark.parametrize("tool_name", [
    "get_page", "list_pages", "read", "search_pages", "count_storage_rows",
    "query_storage_rows", "validate_page", "decompile_function", "drive_page",
    "screenshot_page", "whoami", "which_environment", "tail_service_logs",
    "execute_function", "kb_app_get", "pattern_search", "code_read",
    "get_component_subtree", "list_storages", "find_schema", "lookup_api",
])
def test_read_tools_are_not_edits(tool_name):
    assert action_for(tool_name) is None


@pytest.mark.parametrize("tool_name", [
    "crop_image", "recolor_image", "resize_image_to_path", "make_favicon",
    "generate_image", "composite_images", "convert_image_format",
    "pad_image_canvas", "trim_transparent_borders", "apply_image_filter",
    "clear_cache", "close_browser_session", "reload_auth_token",
])
def test_local_and_cache_operations_are_not_definition_edits(tool_name):
    assert action_for(tool_name) is None


def test_lore_write_tools_are_not_observed():
    """Otherwise lore feeds on its own output and corroborates itself."""
    for tool_name in ("lore_add", "lore_note", "lore_correct"):
        assert action_for(tool_name) is None


# ── Writes are classified with the right verb ────────────────────────────


@pytest.mark.parametrize("tool_name,expected", [
    ("create_page", "create"),
    ("add_component", "create"),
    ("update_component_props", "update"),
    ("patch_component_props", "update"),
    ("bulk_patch_component_props", "update"),
    ("set_bindings", "update"),
    ("save_page_event_function_from_text", "update"),
    ("replace_page_definition", "update"),
    ("delete_page", "delete"),
    ("remove_component", "delete"),
    ("rename_component", "rename"),
    ("move_component", "update"),
    ("create_storage", "create"),
    ("update_theme", "update"),
])
def test_write_verbs(tool_name, expected):
    assert action_for(tool_name) == expected


def test_the_crud_router_is_classified_by_its_own_name():
    assert action_for("create") == "create"
    assert action_for("update") == "update"
    assert action_for("delete") == "delete"
    assert action_for("read") is None
    assert action_for("list") is None


# ── Subject and detail ───────────────────────────────────────────────────


def test_page_edit_carries_the_page_as_its_subject():
    fact = classify(
        "patch_component_props",
        {"page_name": "dashboard", "app_code": "chitfundb",
         "component_key": "duesTable", "properties": {"visibility": True}},
        summary="Patched 1 property on duesTable",
    )
    assert isinstance(fact, EditFact)
    assert fact.subject == "page:dashboard"
    assert fact.object_type == "page"
    assert fact.object_name == "dashboard"
    assert fact.action == "update"
    assert "component_key=duesTable" in fact.detail
    assert "visibility" in fact.detail
    assert "Patched 1 property" in fact.detail


def test_property_values_are_not_recorded_only_their_names():
    """Values churn and often carry data; the KEYS are the durable fact."""
    fact = classify(
        "patch_component_props",
        {"page_name": "login", "properties": {"label": "Enter your PAN 1234"}},
        summary="ok",
    )
    assert "label" in fact.detail
    assert "1234" not in fact.detail


def test_router_call_takes_its_type_from_the_parameter():
    fact = classify(
        "create",
        {"object_type": "page", "name": "auctionEntry", "app_code": "chitfundb"},
        summary="Created page auctionEntry",
    )
    assert fact.subject == "page:auctionEntry"
    assert fact.action == "create"


def test_app_level_tools_land_in_the_app_bucket():
    """Not `application:chitfundb`, which would fragment app-level lore."""
    fact = classify("update_app", {"app_code": "chitfundb"}, summary="Updated")
    assert fact.subject == "app"
    assert fact.object_type == "application"
    assert fact.object_name == "chitfundb"


def test_a_write_with_no_identifiable_subject_is_skipped():
    """Better to lose an observation than to file it against the wrong object."""
    assert classify("upload_static_asset", {"file_path": "/tmp/logo.png"}) is None


def test_a_failed_call_is_not_an_edit():
    params = {"page_name": "dashboard", "properties": {"x": 1}}
    assert classify("patch_component_props", params, success=False) is None
    assert classify("patch_component_props", params, success=True) is not None


def test_non_dict_params_are_survivable():
    assert classify("create_page", None) is None
    assert classify("create_page", "dashboard") is None


def test_detail_is_bounded():
    fact = classify(
        "update_page",
        {"page_name": "dashboard", "properties": {"a": 1}},
        summary="x" * 5000,
    )
    assert len(fact.detail) <= 600


# ── Against the real registry ────────────────────────────────────────────


def _registry_names() -> list[str]:
    from app.agents.appbuilder.tools.registry import ALL_TOOLS
    return sorted(t.name for t in ALL_TOOLS)


def test_no_read_only_prefix_in_the_registry_classifies_as_a_write():
    """A read misclassified as a write records changes that never happened."""
    read_prefixes = (
        "get_", "list_", "search_", "read_", "count_", "query_", "find_",
        "filter_", "decompile_", "validate_", "format_", "compile_",
        "platform_doc_", "pattern_", "code_", "kb_app_", "inspect_", "verify_",
        "tail_", "screenshot_", "download_", "build_", "export_",
    )
    misclassified = [
        name for name in _registry_names()
        if name.startswith(read_prefixes) and action_for(name) is not None
    ]
    assert misclassified == []


def test_the_core_page_authoring_tools_are_all_observed():
    """If these stop being classified, lore goes back to observing nothing."""
    must_observe = (
        "create_page", "add_component", "update_component_props",
        "patch_component_props", "patch_component_bindings", "set_bindings",
        "set_styles", "remove_component", "rename_component", "move_component",
        "create_storage", "create_server_function", "create_theme",
        "save_page_event_function_from_text",
    )
    registry = set(_registry_names())
    for name in must_observe:
        assert name in registry, f"{name} vanished from the registry"
        assert action_for(name) is not None, f"{name} is no longer observed"
