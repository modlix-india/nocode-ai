"""Style leaves must reach the platform in ComponentProperty shape.

A leaf is `{"value": "24px"}` or `{"location": {...}}`. The merge helper used to
wrap whatever it was given, so passing the shape the tool's own description
documents produced `{"value": {"value": "24px"}}`. The platform stores that
without complaint and the renderer then reads an object where it wants a string,
so every font size, colour and background set through patch_component_styles
silently did nothing. Nothing errored; the style just never appeared.
"""

from __future__ import annotations

from app.agents.appbuilder.tools.modlix.pages import _merge_css_into_styleprops


def _leaves(css_props: dict) -> dict:
    style_props: dict = {}
    _merge_css_into_styleprops(style_props, css_props, "ALL", "", "")
    (rule,) = style_props.values()
    return rule["resolutions"]["ALL"]


def test_an_already_wrapped_leaf_is_not_wrapped_again():
    assert _leaves({"fontSize": {"value": "44px"}})["fontSize"] == {"value": "44px"}


def test_a_bare_scalar_is_wrapped():
    assert _leaves({"fontSize": "44px"})["fontSize"] == {"value": "44px"}


def test_an_expression_leaf_survives_intact():
    loc = {"location": {"type": "EXPRESSION", "value": "Theme.primaryColor"}}
    assert _leaves({"backgroundColor": loc})["backgroundColor"] == loc


def test_a_dict_that_is_not_a_component_property_is_still_wrapped():
    """Only `value`/`location` mean 'already a property'; anything else is data."""
    odd = {"r": 1, "g": 2}
    assert _leaves({"filter": odd})["filter"] == {"value": odd}
