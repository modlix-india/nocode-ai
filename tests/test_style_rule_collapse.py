"""One style rule per (condition, pseudoState), enforced by collapsing.

Modlix keys style rules by UUID. A component carrying several UNCONDITIONED
rules for the same pseudoState silently loses all but the one the platform
resolves last, so a writer that merges into the *first* match can have its work
discarded with nothing to show for it. These tests pin the collapse.

Ported from the same fix in modlix-mcp's `composition_v2.py` (2026-08-26).
"""

from __future__ import annotations

from app.agents.appbuilder.tools.modlix.pages import _merge_css_into_styleprops


def _leaf(props: dict, rule_key: str, breakpoint: str = "ALL") -> dict:
    return props[rule_key]["resolutions"][breakpoint]


def test_writes_into_an_empty_component():
    props: dict = {}
    applied = _merge_css_into_styleprops(props, {"gap": "24px"}, "ALL", "", "")
    assert len(props) == 1
    rule_key = next(iter(props))
    assert _leaf(props, rule_key)["gap"] == {"value": "24px"}
    assert applied == ["gap"]


def test_reuses_the_existing_rule_rather_than_minting_a_second():
    props = {"aaa": {"resolutions": {"ALL": {"color": {"value": "red"}}}}}
    _merge_css_into_styleprops(props, {"gap": "24px"}, "ALL", "", "")
    assert set(props) == {"aaa"}
    assert _leaf(props, "aaa") == {
        "color": {"value": "red"}, "gap": {"value": "24px"},
    }


def test_duplicate_unconditioned_rules_are_collapsed_into_one():
    """The bug: merging into the first leaves the second to win instead."""
    props = {
        "aaa": {"resolutions": {"ALL": {"color": {"value": "red"}}}},
        "bbb": {"resolutions": {"ALL": {"padding": {"value": "8px"}}}},
    }
    _merge_css_into_styleprops(props, {"gap": "24px"}, "ALL", "", "")
    assert len(props) == 1, "the duplicate rule must be removed, not left behind"
    rule_key = next(iter(props))
    assert _leaf(props, rule_key) == {
        "color": {"value": "red"},
        "padding": {"value": "8px"},
        "gap": {"value": "24px"},
    }


def test_collapse_keeps_later_rules_winning_on_a_clash():
    """Iteration order decides today, so the last writer's value must survive."""
    props = {
        "aaa": {"resolutions": {"ALL": {"color": {"value": "red"}}}},
        "bbb": {"resolutions": {"ALL": {"color": {"value": "blue"}}}},
    }
    _merge_css_into_styleprops(props, {}, "ALL", "", "")
    rule_key = next(iter(props))
    assert _leaf(props, rule_key)["color"] == {"value": "blue"}


def test_collapse_spans_breakpoints():
    props = {
        "aaa": {"resolutions": {"ALL": {"color": {"value": "red"}}}},
        "bbb": {"resolutions": {"TABLET_LANDSCAPE_SCREEN": {"gap": {"value": "8px"}}}},
    }
    _merge_css_into_styleprops(props, {"padding": "4px"}, "ALL", "", "")
    assert len(props) == 1
    rule_key = next(iter(props))
    resolutions = props[rule_key]["resolutions"]
    assert resolutions["ALL"] == {"color": {"value": "red"}, "padding": {"value": "4px"}}
    assert resolutions["TABLET_LANDSCAPE_SCREEN"] == {"gap": {"value": "8px"}}


def test_conditioned_rules_are_never_touched():
    """They merge cleanly on the platform side; absorbing them would drop the condition."""
    props = {
        "aaa": {"resolutions": {"ALL": {"color": {"value": "red"}}}},
        "ccc": {"condition": {"x": 1}, "resolutions": {"ALL": {"color": {"value": "green"}}}},
    }
    _merge_css_into_styleprops(props, {"gap": "24px"}, "ALL", "", "")
    assert "ccc" in props
    assert props["ccc"]["condition"] == {"x": 1}
    assert props["ccc"]["resolutions"]["ALL"] == {"color": {"value": "green"}}


def test_a_different_pseudo_state_is_a_different_scope():
    props = {"hov": {"pseudoState": "hover",
                     "resolutions": {"ALL": {"color": {"value": "blue"}}}}}
    _merge_css_into_styleprops(props, {"color": "red"}, "ALL", "", "")
    assert len(props) == 2, "the hover rule must not absorb the base rule"
    assert props["hov"]["resolutions"]["ALL"]["color"] == {"value": "blue"}


def test_writing_a_pseudo_state_collapses_only_that_pseudo_state():
    props = {
        "base": {"resolutions": {"ALL": {"color": {"value": "black"}}}},
        "h1": {"pseudoState": "hover", "resolutions": {"ALL": {"color": {"value": "blue"}}}},
        "h2": {"pseudoState": "hover", "resolutions": {"ALL": {"padding": {"value": "2px"}}}},
    }
    _merge_css_into_styleprops(props, {"gap": "1px"}, "ALL", "", "hover")
    assert "base" in props
    assert props["base"]["resolutions"]["ALL"] == {"color": {"value": "black"}}
    hover_keys = [k for k, v in props.items() if v.get("pseudoState") == "hover"]
    assert len(hover_keys) == 1
    assert props[hover_keys[0]]["resolutions"]["ALL"] == {
        "color": {"value": "blue"}, "padding": {"value": "2px"}, "gap": {"value": "1px"},
    }


def test_pseudo_state_is_preserved_on_the_surviving_rule():
    props: dict = {}
    _merge_css_into_styleprops(props, {"color": "red"}, "ALL", "", "hover")
    rule_key = next(iter(props))
    assert props[rule_key]["pseudoState"] == "hover"


def test_no_pseudo_state_key_is_written_for_the_base_scope():
    """The platform treats a missing pseudoState and an empty one alike, but
    real definitions omit the key, and we match them."""
    props: dict = {}
    _merge_css_into_styleprops(props, {"color": "red"}, "ALL", "", "")
    rule_key = next(iter(props))
    assert "pseudoState" not in props[rule_key]


def test_sub_component_lands_in_the_leaf_key():
    props: dict = {}
    applied = _merge_css_into_styleprops(props, {"color": "red"}, "ALL", "header", "")
    assert applied != ["color"], "the sub-component must qualify the leaf key"
    rule_key = next(iter(props))
    assert applied[0] in _leaf(props, rule_key)
