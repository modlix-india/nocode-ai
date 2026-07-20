"""Tests for app/agents/appbuilder/tools/modlix/_page_ops.py — pure helpers only.

The async fetch_page_by_name / save_page are HTTP-bound and covered by
endpoint-contract tests; here we cover the synchronous tree manipulation +
summarization + validation helpers.
"""

from __future__ import annotations

import pytest

from app.agents.appbuilder.tools.modlix._page_ops import (
    add_component,
    build_component_tree,
    build_page_summary,
    build_subtree,
    move_component,
    new_page_skeleton,
    remove_component,
    search_components,
    summarize_component,
    update_component,
    validate_page_structure,
)


# ── new_page_skeleton ────────────────────────────────────────────────────────


def test_new_page_skeleton_has_required_top_level_keys():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    for key in ("name", "appCode", "clientCode", "rootComponent", "componentDefinition"):
        assert key in page, f"missing top-level key: {key}"
    assert page["name"] == "home"
    assert page["appCode"] == "testapp"
    assert page["clientCode"] == "SYSTEM"


def test_new_page_skeleton_root_component_present_and_is_grid():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root_key = page["rootComponent"]
    comp_def = page["componentDefinition"]
    assert root_key in comp_def
    root = comp_def[root_key]
    assert root["type"] == "Grid"
    assert root["children"] == {}
    assert root["key"] == root_key


def test_new_page_skeleton_with_title_populates_properties():
    page = new_page_skeleton("home", "testapp", "SYSTEM", title="Home Page")
    title_obj = page["properties"]["title"]["name"]
    assert title_obj == {"value": "Home Page"}


# ── add_component ────────────────────────────────────────────────────────────


def test_add_component_appends_child_and_registers_in_comp_def():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root_key = page["rootComponent"]

    err = add_component(
        page,
        parent_key=root_key,
        component_key="btn1",
        component_type="Button",
        name="submitBtn",
        properties={"label": "Submit"},
    )
    assert err is None

    # Registered under componentDefinition.
    assert "btn1" in page["componentDefinition"]
    new_comp = page["componentDefinition"]["btn1"]
    assert new_comp["type"] == "Button"
    assert new_comp["name"] == "submitBtn"
    # Raw value got wrapped into {"value": ...} shape.
    assert new_comp["properties"]["label"] == {"value": "Submit"}

    # Parent's children map now points at the new key.
    assert page["componentDefinition"][root_key]["children"].get("btn1") is True


def test_add_component_with_unknown_parent_returns_error():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    err = add_component(
        page,
        parent_key="does-not-exist",
        component_key="btn1",
        component_type="Button",
    )
    assert err is not None
    assert "does-not-exist" in err
    assert "btn1" not in page["componentDefinition"]


def test_add_component_duplicate_key_rejected():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root = page["rootComponent"]
    assert add_component(page, parent_key=root, component_key="k", component_type="Text") is None
    err = add_component(page, parent_key=root, component_key="k", component_type="Text")
    assert err is not None
    assert "already exists" in err


# ── update_component ─────────────────────────────────────────────────────────


def test_update_component_merges_props_and_preserves_others():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root = page["rootComponent"]
    add_component(
        page,
        parent_key=root,
        component_key="t1",
        component_type="Text",
        name="header",
        properties={"label": "Hi", "color": "red"},
    )

    err = update_component(page, component_key="t1", properties={"label": "Hello"})
    assert err is None

    t1 = page["componentDefinition"]["t1"]
    assert t1["properties"]["label"] == {"value": "Hello"}
    # Unchanged props preserved.
    assert t1["properties"]["color"] == {"value": "red"}
    # Identity fields untouched.
    assert t1["type"] == "Text"
    assert t1["name"] == "header"


def test_update_component_unknown_key_returns_error():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    err = update_component(page, component_key="ghost", properties={"x": 1})
    assert err is not None
    assert "ghost" in err


# ── remove_component ─────────────────────────────────────────────────────────


def test_remove_leaf_detaches_from_parent_and_drops_entry():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root = page["rootComponent"]
    add_component(page, parent_key=root, component_key="leaf", component_type="Text")
    assert "leaf" in page["componentDefinition"]

    err = remove_component(page, component_key="leaf")
    assert err is None
    assert "leaf" not in page["componentDefinition"]
    assert "leaf" not in page["componentDefinition"][root]["children"]


def test_remove_subtree_drops_all_descendants():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root = page["rootComponent"]
    add_component(page, parent_key=root, component_key="g1", component_type="Grid")
    add_component(page, parent_key="g1", component_key="g2", component_type="Grid")
    add_component(page, parent_key="g2", component_key="t1", component_type="Text")
    add_component(page, parent_key="g2", component_key="t2", component_type="Text")

    err = remove_component(page, component_key="g1")
    assert err is None
    for k in ("g1", "g2", "t1", "t2"):
        assert k not in page["componentDefinition"]
    assert "g1" not in page["componentDefinition"][root]["children"]


def test_remove_root_is_rejected():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    err = remove_component(page, component_key=page["rootComponent"])
    assert err is not None
    assert page["rootComponent"] in page["componentDefinition"]


# ── move_component ───────────────────────────────────────────────────────────


def test_move_component_reparents_correctly():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root = page["rootComponent"]
    add_component(page, parent_key=root, component_key="g1", component_type="Grid")
    add_component(page, parent_key=root, component_key="g2", component_type="Grid")
    add_component(page, parent_key="g1", component_key="t1", component_type="Text")
    assert page["componentDefinition"]["g1"]["children"].get("t1") is True

    err = move_component(page, component_key="t1", new_parent_key="g2")
    assert err is None
    assert "t1" not in page["componentDefinition"]["g1"]["children"]
    assert page["componentDefinition"]["g2"]["children"].get("t1") is True


# ── build_component_tree ─────────────────────────────────────────────────────


def test_build_component_tree_renders_three_levels_with_indentation():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root = page["rootComponent"]
    add_component(page, parent_key=root, component_key="g1", component_type="Grid", name="outer")
    add_component(page, parent_key="g1", component_key="g2", component_type="Grid", name="middle")
    add_component(page, parent_key="g2", component_key="t1", component_type="Text", name="leaf")

    tree = build_component_tree(page)
    assert tree  # non-empty
    lines = tree.split("\n")
    # Find the line for t1 — it should be indented deeper than g1's line.
    line_root = next(l for l in lines if root in l)
    line_g1 = next(l for l in lines if "g1" in l)
    line_g2 = next(l for l in lines if "g2" in l)
    line_t1 = next(l for l in lines if "t1" in l)
    # Indentation strictly increases with depth (2 spaces per level).
    indent = lambda l: len(l) - len(l.lstrip(" "))
    assert indent(line_root) == 0
    assert indent(line_g1) == 2
    assert indent(line_g2) == 4
    assert indent(line_t1) == 6


def test_build_component_tree_empty_page_message():
    assert build_component_tree({}) == "(empty page)"


# ── validate_page_structure ──────────────────────────────────────────────────


def test_validate_well_formed_page_returns_empty_list():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root = page["rootComponent"]
    add_component(page, parent_key=root, component_key="g1", component_type="Grid")
    add_component(page, parent_key="g1", component_key="t1", component_type="Text")

    assert validate_page_structure(page) == []


def test_validate_dangling_child_reference_is_reported():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root = page["rootComponent"]
    # Forge a dangling child entry — referenced in parent but no comp_def entry.
    page["componentDefinition"][root]["children"]["ghost"] = True

    issues = validate_page_structure(page)
    assert issues  # non-empty
    assert any("ghost" in i for i in issues)


# ── build_page_summary + summarize_component ─────────────────────────────────


def test_build_page_summary_counts_and_root_children():
    page = new_page_skeleton("home", "testapp", "SYSTEM", title="Home")
    root = page["rootComponent"]
    add_component(page, parent_key=root, component_key="g1", component_type="Grid")
    add_component(page, parent_key=root, component_key="g2", component_type="Grid")
    add_component(page, parent_key="g1", component_key="t1", component_type="Text")

    summary = build_page_summary(page)
    assert summary["componentCount"] == 4  # root + g1 + g2 + t1
    assert summary["rootComponent"] == root
    assert summary["title"] == "Home"
    root_child_keys = {c["key"] for c in summary["rootChildren"]}
    assert root_child_keys == {"g1", "g2"}
    # subtree_size for g1 includes itself + t1.
    g1_row = next(c for c in summary["rootChildren"] if c["key"] == "g1")
    assert g1_row["subtree_size"] == 2


def test_summarize_component_returns_none_for_missing_key():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    assert summarize_component(page, "no-such-key") is None


def test_summarize_component_returns_details_for_existing_key():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root = page["rootComponent"]
    add_component(
        page,
        parent_key=root,
        component_key="btn",
        component_type="Button",
        name="submit",
        properties={"label": "Go"},
    )
    info = summarize_component(page, "btn")
    assert info is not None
    assert info["key"] == "btn"
    assert info["type"] == "Button"
    assert info["name"] == "submit"
    assert info["properties"]["label"] == {"value": "Go"}


# ── search_components + build_subtree ────────────────────────────────────────


def test_search_components_filters_by_type():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root = page["rootComponent"]
    add_component(page, parent_key=root, component_key="b1", component_type="Button", name="one")
    add_component(page, parent_key=root, component_key="b2", component_type="Button", name="two")
    add_component(page, parent_key=root, component_key="t1", component_type="Text", name="three")

    results = search_components(page, component_type="Button")
    keys = {r["key"] for r in results}
    assert keys == {"b1", "b2"}


def test_build_subtree_respects_max_depth():
    page = new_page_skeleton("home", "testapp", "SYSTEM")
    root = page["rootComponent"]
    add_component(page, parent_key=root, component_key="g1", component_type="Grid")
    add_component(page, parent_key="g1", component_key="g2", component_type="Grid")
    add_component(page, parent_key="g2", component_key="t1", component_type="Text")

    out = build_subtree(page, root, max_depth=1)
    assert "g1" in out
    # t1 is at depth 3 from root, beyond max_depth=1 — should be elided.
    assert "t1" not in out
