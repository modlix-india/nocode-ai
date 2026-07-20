"""Authored CSS declarations -> Modlix styleProperties.

CSS helpers (kebab->camel, shorthand expansion, normalize) are ported verbatim
from modlix-mcp/modlix_mcp/html_compiler.py (that repo is being archived, so we
keep a local copy rather than depend on it). The styleProperties shape and the
rule-level `pseudoState` storage mirror `_merge_css_into_styleprops` in
app/agents/appbuilder/tools/modlix/pages.py.

Breakpoint key choice (verified in nocode-ui styleProcessor.ts):
  Modlix `_SCREEN` keys are min-width (mobile-first); `_ONLY`/`_SMALL` are
  max-width bounded. Authored sites are predominantly desktop-first (max-width
  media queries), so we use ALL as the desktop base and max-width-bounded keys
  for the narrower overrides, inserted in cascade order (ALL -> tablet -> mobile)
  so the later, equal-specificity, max-width rule wins where ranges overlap.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

# Modlix resolution keys for a 3-sample (desktop/tablet/mobile) capture.
BP_DESKTOP = "ALL"  # base, applies at every width
BP_TABLET = "TABLET_LANDSCAPE_SCREEN_SMALL"  # @media (max-width: 1024px)
BP_MOBILE = "MOBILE_POTRAIT_SCREEN_ONLY"  # @media (max-width: 480px)

_CSS_SKIPLIST = frozenset({"all"})

_SHORTHAND_EXPANSIONS: Dict[str, tuple] = {
    "margin": ("marginTop", "marginRight", "marginBottom", "marginLeft"),
    "padding": ("paddingTop", "paddingRight", "paddingBottom", "paddingLeft"),
    "borderRadius": (
        "borderTopLeftRadius",
        "borderTopRightRadius",
        "borderBottomRightRadius",
        "borderBottomLeftRadius",
    ),
    "borderColor": ("borderTopColor", "borderRightColor", "borderBottomColor", "borderLeftColor"),
    "borderWidth": ("borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth"),
    "borderStyle": ("borderTopStyle", "borderRightStyle", "borderBottomStyle", "borderLeftStyle"),
}


def _kebab_to_camel(prop: str) -> str:
    if "-" not in prop:
        return prop
    head, *rest = prop.split("-")
    if not head:  # vendor prefix -webkit-x -> webkitX
        rest = [rest[0]] + rest[1:] if rest else []
        return "".join(p.capitalize() if i > 0 else p for i, p in enumerate(rest))
    return head + "".join(p.capitalize() for p in rest)


def _expand_shorthand_values(values: str):
    parts = values.split()
    if len(parts) == 1:
        return parts[0], parts[0], parts[0], parts[0]
    if len(parts) == 2:
        return parts[0], parts[1], parts[0], parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2], parts[1]
    if len(parts) >= 4:
        return parts[0], parts[1], parts[2], parts[3]
    return values, values, values, values


def normalize_css_props(raw: Dict[str, str]) -> Dict[str, str]:
    """CSS map (kebab or camel) -> long-form camelCase Modlix prop names."""
    out: Dict[str, str] = {}
    for prop, value in raw.items():
        if prop in _CSS_SKIPLIST:
            continue
        camel = _kebab_to_camel(prop)
        if camel in _SHORTHAND_EXPANSIONS:
            top, right, bottom, left = _expand_shorthand_values(str(value))
            t, r, b, l = _SHORTHAND_EXPANSIONS[camel]
            out[t], out[r], out[b], out[l] = top, right, bottom, left
        else:
            out[camel] = str(value)
    return out


def _leaf(css_prop: str, sub_component: str = "") -> str:
    """Leaf key. pseudo-state lives at the rule level, never in the leaf."""
    return f"{sub_component}-{css_prop}" if sub_component else css_prop


def _wrap_block(props: Dict[str, str], sub_component: str) -> Dict[str, Any]:
    return {_leaf(p, sub_component): {"value": v} for p, v in props.items()}


def build_style_properties(
    desktop: Optional[Dict[str, str]],
    tablet: Optional[Dict[str, str]] = None,
    mobile: Optional[Dict[str, str]] = None,
    *,
    hover: Optional[Dict[str, str]] = None,
    sub_component: str = "",
) -> Dict[str, Any]:
    """Build a Modlix styleProperties dict from authored declarations resolved
    at desktop/tablet/mobile. Narrower buckets carry only deltas vs the cascade
    so far. Resolutions are inserted ALL -> tablet -> mobile for correct cascade.
    """
    all_props = normalize_css_props(desktop or {})
    style_props: Dict[str, Any] = {}

    base_resolutions: Dict[str, Any] = {}
    if all_props:
        base_resolutions[BP_DESKTOP] = _wrap_block(all_props, sub_component)

    tab = normalize_css_props(tablet or {})
    tab_delta = {p: v for p, v in tab.items() if all_props.get(p) != v}
    effective_tab = {**all_props, **tab_delta}
    if tab_delta:
        base_resolutions[BP_TABLET] = _wrap_block(tab_delta, sub_component)

    mob = normalize_css_props(mobile or {})
    mob_delta = {p: v for p, v in mob.items() if effective_tab.get(p) != v}
    if mob_delta:
        base_resolutions[BP_MOBILE] = _wrap_block(mob_delta, sub_component)

    if base_resolutions:
        style_props[uuid.uuid4().hex] = {"resolutions": base_resolutions}

    if hover:
        hov = normalize_css_props(hover)
        hov_delta = {p: v for p, v in hov.items() if all_props.get(p) != v}
        if hov_delta:
            style_props[uuid.uuid4().hex] = {
                "pseudoState": "hover",
                "resolutions": {BP_DESKTOP: _wrap_block(hov_delta, sub_component)},
            }

    return style_props
