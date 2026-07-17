"""Pure-function tests for app/agents/appbuilder/tools/modlix/_conventions.py."""
from __future__ import annotations

import pytest

from app.agents.appbuilder.tools.modlix._conventions import (
    BREAKPOINTS,
    EXPRESSION_PREFIXES,
    extract_expression_refs,
    find_style_rule_for_leaf,
    make_css_prop_key,
    make_dependency_key,
    make_dependent_statements,
    make_expression_prop,
    make_expression_ref,
    make_parameter_map,
    make_role_authority,
    make_style_rule,
    make_value_ref,
    parse_authority,
    parse_css_prop_key,
    steps_referenced,
    is_multi_valued_shape,
    unwrap_component_props,
    validate_authority,
    validate_breakpoint,
    validate_expression,
    validate_simple_name,
    wrap_component_props,
    wrap_multi_valued,
)


# ── validate_simple_name ──────────────────────────────────────────────────────

def test_validate_simple_name_accepts_camel_case():
    assert validate_simple_name("fooBar") is None


def test_validate_simple_name_accepts_single_letter():
    assert validate_simple_name("a") is None


def test_validate_simple_name_rejects_empty():
    err = validate_simple_name("")
    assert err is not None
    assert "empty" in err.lower()


def test_validate_simple_name_rejects_space():
    assert validate_simple_name("foo bar") is not None


def test_validate_simple_name_rejects_dash():
    assert validate_simple_name("foo-bar") is not None


def test_validate_simple_name_rejects_leading_digit():
    assert validate_simple_name("1foo") is not None


# ── validate_breakpoint ───────────────────────────────────────────────────────

def test_validate_breakpoint_all():
    assert validate_breakpoint("ALL") is None


def test_validate_breakpoint_desktop_screen():
    assert validate_breakpoint("DESKTOP_SCREEN") is None


def test_validate_breakpoint_mobile_potrait_only_typo():
    # The platform uses "POTRAIT" (misspelled) — verify the convention preserves it.
    assert "MOBILE_POTRAIT_SCREEN_ONLY" in BREAKPOINTS
    assert validate_breakpoint("MOBILE_POTRAIT_SCREEN_ONLY") is None


def test_validate_breakpoint_rejects_unknown():
    err = validate_breakpoint("INVALID")
    assert err is not None
    assert "INVALID" in err


def test_validate_breakpoint_rejects_correctly_spelled_portrait():
    # Common mistake: corrected spelling. The platform's typo wins.
    assert validate_breakpoint("MOBILE_PORTRAIT_SCREEN") is not None


# ── extract_expression_refs / steps_referenced ────────────────────────────────

def test_extract_expression_refs_finds_known_prefixes():
    # The regex has a single capture group around the prefix, so findall
    # returns just the prefix names.
    refs = extract_expression_refs("Steps.foo.bar then Page.user.email")
    assert "Steps" in refs
    assert "Page" in refs


def test_extract_expression_refs_ignores_unknown_prefixes():
    # "user" and "app" are NOT in EXPRESSION_PREFIXES.
    assert "user" not in EXPRESSION_PREFIXES
    refs = extract_expression_refs("user.emailId and app.foo")
    assert refs == []


def test_steps_referenced_returns_step_names():
    expr = "Steps.create.output + Steps.fetch.value + Page.x"
    assert steps_referenced(expr) == {"create", "fetch"}


def test_steps_referenced_empty_for_empty_expr():
    assert steps_referenced("") == set()
    assert steps_referenced(None) == set()


# ── validate_expression ───────────────────────────────────────────────────────

def test_validate_expression_accepts_clean_kirun():
    assert validate_expression("Steps.x.output = 1 and Page.y > 2") is None


def test_validate_expression_rejects_js_double_equals():
    err = validate_expression("Steps.x == 1")
    assert err is not None
    assert "==" in err


def test_validate_expression_rejects_js_logical_and():
    err = validate_expression("a && b")
    assert err is not None
    assert "&&" in err


def test_validate_expression_rejects_non_string():
    err = validate_expression(123)
    assert err is not None


# ── wrap / unwrap component props ─────────────────────────────────────────────

def test_wrap_component_props_wraps_raw_value():
    out = wrap_component_props({"label": "Hello"})
    assert out == {"label": {"value": "Hello"}}


def test_wrap_component_props_preserves_already_wrapped():
    pre = {"label": {"value": "Hello"}, "src": {"location": {"type": "EXPRESSION", "value": "Page.x"}}}
    out = wrap_component_props(pre)
    assert out == pre


def test_wrap_unwrap_round_trip():
    raw = {"label": "Hi", "count": 5, "flag": True}
    wrapped = wrap_component_props(raw)
    assert unwrap_component_props(wrapped) == raw


def test_make_expression_prop_shape():
    out = make_expression_prop("Page.user.email")
    assert out == {"location": {"type": "EXPRESSION", "value": "Page.user.email"}}


# ── value / expression refs ───────────────────────────────────────────────────

def test_make_value_ref_shape():
    ref = make_value_ref("hi", order=2, key="k1")
    assert ref == {
        "key": "k1",
        "type": "VALUE",
        "value": "hi",
        "expression": "",
        "order": 2,
    }


def test_make_value_ref_generates_key_when_omitted():
    ref = make_value_ref(42)
    assert ref["type"] == "VALUE"
    assert ref["value"] == 42
    assert ref["order"] == 1
    assert isinstance(ref["key"], str) and len(ref["key"]) > 0


def test_make_expression_ref_shape():
    ref = make_expression_ref("Page.x", order=3, key="k2")
    assert ref == {
        "key": "k2",
        "type": "EXPRESSION",
        "expression": "Page.x",
        "order": 3,
    }


def test_make_expression_ref_rejects_js_syntax():
    with pytest.raises(ValueError):
        make_expression_ref("a && b")


# ── make_parameter_map ────────────────────────────────────────────────────────

def test_make_parameter_map_literal_value():
    pm = make_parameter_map({"text": "hello"})
    assert set(pm.keys()) == {"text"}
    inner = pm["text"]
    assert len(inner) == 1
    [(_key, ref)] = inner.items()
    assert ref["type"] == "VALUE"
    assert ref["value"] == "hello"


def test_make_parameter_map_expression_via_prefix():
    pm = make_parameter_map({"text": "Page.user.name"})
    [(_k, ref)] = pm["text"].items()
    assert ref["type"] == "EXPRESSION"
    assert ref["expression"] == "Page.user.name"


def test_make_parameter_map_list_creates_multiple_refs_ordered():
    pm = make_parameter_map({"values": ["a", "b", "c"]})
    refs = list(pm["values"].values())
    assert len(refs) == 3
    assert [r["order"] for r in refs] == [1, 2, 3]
    assert [r["value"] for r in refs] == ["a", "b", "c"]


# ── dependency keys / statements ──────────────────────────────────────────────

def test_make_dependency_key_with_event():
    assert make_dependency_key("create", "output") == "Steps.create.output"


def test_make_dependency_key_without_event():
    # bare step name when event is None
    assert make_dependency_key("create") == "create"
    assert make_dependency_key("create", None) == "create"


def test_make_dependent_statements_from_strings_uses_output_convention():
    out = make_dependent_statements("create", "fetch")
    assert out == {"Steps.create.output": True, "Steps.fetch.output": True}


def test_make_dependent_statements_from_tuples_uses_explicit_event():
    out = make_dependent_statements(("if", "true"), ("if", "false"))
    assert out == {"Steps.if.true": True, "Steps.if.false": True}


# ── authorities ───────────────────────────────────────────────────────────────

def test_make_role_authority_with_app_code():
    assert make_role_authority("Admin", "leadzump") == "Authorities.LEADZUMP.ROLE_Admin"


def test_make_role_authority_without_app_code():
    assert make_role_authority("Owner") == "Authorities.ROLE_Owner"


def test_make_role_authority_replaces_spaces():
    assert make_role_authority("Super Admin", "app1") == "Authorities.APP1.ROLE_Super_Admin"


def test_parse_authority_round_trip_role_with_app():
    s = make_role_authority("Admin", "leadzump")
    parsed = parse_authority(s)
    assert parsed == {"kind": "role", "app_code": "LEADZUMP", "name": "Admin"}


def test_parse_authority_logged_in_system():
    assert parse_authority("Authorities.Logged_IN") == {
        "kind": "system", "app_code": None, "name": "Logged_IN",
    }


def test_parse_authority_returns_none_for_garbage():
    assert parse_authority("not-an-authority") is None


def test_validate_authority_accepts_canonical_role():
    assert validate_authority("Authorities.LEADZUMP.ROLE_Admin") is None


def test_validate_authority_rejects_garbage():
    err = validate_authority("nope")
    assert err is not None


# ── CSS prop key encoding ─────────────────────────────────────────────────────

def test_make_css_prop_key_plain():
    assert make_css_prop_key("fontSize") == "fontSize"


def test_make_css_prop_key_with_sub_component():
    assert make_css_prop_key("fontSize", sub_component="text") == "text-fontSize"


def test_make_css_prop_key_with_pseudo_state():
    assert make_css_prop_key("transform", pseudo_state="hover") == "transform:hover"


def test_make_css_prop_key_full():
    assert make_css_prop_key("animationName", sub_component="step", pseudo_state="hover") == "step-animationName:hover"


def test_parse_css_prop_key_round_trip_plain():
    leaf = make_css_prop_key("fontSize")
    assert parse_css_prop_key(leaf) == {"sub_component": "", "css_prop": "fontSize", "pseudo_state": ""}


def test_parse_css_prop_key_round_trip_full():
    leaf = make_css_prop_key("animationName", sub_component="step", pseudo_state="hover")
    assert parse_css_prop_key(leaf) == {
        "sub_component": "step",
        "css_prop": "animationName",
        "pseudo_state": "hover",
    }


# ── style rules ──────────────────────────────────────────────────────────────

def test_make_style_rule_default_breakpoint():
    rule = make_style_rule("fontSize", "16px")
    assert rule == {"resolutions": {"ALL": {"fontSize": {"value": "16px"}}}}


def test_make_style_rule_with_sub_and_pseudo():
    rule = make_style_rule("transform", "scale(1.1)", sub_component="text", pseudo_state="hover", breakpoint="DESKTOP_SCREEN")
    assert rule == {
        "resolutions": {
            "DESKTOP_SCREEN": {"text-transform:hover": {"value": "scale(1.1)"}}
        }
    }


def test_make_style_rule_rejects_unknown_breakpoint():
    with pytest.raises(ValueError):
        make_style_rule("fontSize", "16px", breakpoint="BOGUS_SCREEN")


def test_find_style_rule_for_leaf_locates_existing():
    rule = make_style_rule("fontSize", "16px")
    style_properties = {"rule-uuid-1": rule}
    assert find_style_rule_for_leaf(style_properties, "fontSize") == "rule-uuid-1"


def test_find_style_rule_for_leaf_returns_none_when_missing():
    rule = make_style_rule("fontSize", "16px")
    style_properties = {"rule-uuid-1": rule}
    assert find_style_rule_for_leaf(style_properties, "marginTop") is None


def test_find_style_rule_for_leaf_handles_empty():
    assert find_style_rule_for_leaf({}, "fontSize") is None
    assert find_style_rule_for_leaf(None, "fontSize") is None


# ── multi-valued helpers ─────────────────────────────────────────────────────

def test_is_multi_valued_shape_true_for_entries_dict():
    val = {"abc": {"order": 0, "property": {"value": "x"}}}
    assert is_multi_valued_shape(val) is True


def test_is_multi_valued_shape_false_for_empty_or_scalar():
    assert is_multi_valued_shape({}) is False
    assert is_multi_valued_shape("hi") is False
    assert is_multi_valued_shape({"foo": "bar"}) is False


def test_wrap_multi_valued_from_list_creates_ordered_entries():
    out = wrap_multi_valued(["a", "b"])
    assert is_multi_valued_shape(out)
    orders = sorted(entry["order"] for entry in out.values())
    assert orders == [0, 1]
    values = sorted(entry["property"]["value"] for entry in out.values())
    assert values == ["a", "b"]
