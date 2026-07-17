"""`modlix.components` — helpers for composing/modifying component
definitions WITHOUT producing the styleProperty UUID bloat that breaks
real-world clones.

The agent's natural pattern is:

    page = modlix.pages.get('home')
    hh = page['componentDefinition'][hh_key]
    hh['styleProperties'][modlix.uuid()] = {'resolutions': {'ALL': {...}}}
    modlix.pages.replace('home', {'componentDefinition': ...})

That APPENDS a new UUID every iteration. After 6 compares the
HeroHeading has 7 separate styleProperty UUIDs all setting overlapping
rules — CSS cascade picks whatever the engine resolves last and styling
never stabilises. The Modlix shape rule is ONE UUID entry per logical
rule group.

The helpers below enforce that shape:

    modlix.components.set_style(comp, {'fontFamily': "'Inter'", 'fontSize': '64px'})
    modlix.components.merge_style(comp, {'color': '#ff0000'})  # preserves prior keys
"""

from __future__ import annotations

import uuid as _uuid_lib
from typing import Any


def _new_uuid() -> str:
    return str(_uuid_lib.uuid4())


def set_style(component: dict, css: dict, resolution: str = "ALL") -> dict:
    """Replace ALL styleProperty UUID entries on `component` with ONE
    canonical entry containing `css`. Mutates and returns `component`.

    Args:
        component: a single component definition dict (NOT the whole
            componentDefinition map — pass `cd[component_key]`).
        css: flat CSS-prop → value dict, e.g.
            `{'fontFamily': "'Inter'", 'fontSize': '64px', 'color': '#fff'}`.
            Values are auto-wrapped as `{value: X}`.
        resolution: which breakpoint to write under. Default `'ALL'`.
            For responsive overrides use 'MOBILE_SCREEN_SMALL' etc. per
            the Modlix resolutions enum.

    Returns:
        The same `component` dict, mutated in place. Returned so you can
        chain (e.g. `set_style(set_style(c, {...}), {...}, 'MOBILE...')`).

    Discards every existing styleProperty UUID — use this when you want a
    clean replace. For incremental updates use `merge_style`.
    """
    if not isinstance(component, dict):
        raise TypeError(f"component must be a dict, got {type(component).__name__}")
    if not isinstance(css, dict):
        raise TypeError(f"css must be a dict, got {type(css).__name__}")
    component["styleProperties"] = {
        _new_uuid(): {
            "resolutions": {
                resolution: {k: {"value": v} for k, v in css.items()}
            }
        }
    }
    return component


def merge_style(component: dict, css: dict, resolution: str = "ALL") -> dict:
    """Merge `css` into the component's ONE canonical styleProperty entry.

    If the component already has multiple UUID entries (bloat from prior
    appends), CONSOLIDATE them into one — later-defined keys win on
    collisions, then `css` is applied on top. The result is always ONE
    UUID entry with a clean resolutions map.

    Use this when you want to update a few CSS keys without erasing the
    rest. For a clean replace use `set_style`.
    """
    if not isinstance(component, dict):
        raise TypeError(f"component must be a dict, got {type(component).__name__}")
    if not isinstance(css, dict):
        raise TypeError(f"css must be a dict, got {type(css).__name__}")
    sp_existing = component.get("styleProperties") or {}
    consolidated: dict[str, dict[str, dict[str, Any]]] = {}
    if isinstance(sp_existing, dict):
        for rule in sp_existing.values():
            if not isinstance(rule, dict):
                continue
            res = rule.get("resolutions") or {}
            if not isinstance(res, dict):
                continue
            for r_name, r_rules in res.items():
                if not isinstance(r_rules, dict):
                    continue
                consolidated.setdefault(r_name, {}).update(r_rules)
    consolidated.setdefault(resolution, {}).update(
        {k: {"value": v} for k, v in css.items()}
    )
    component["styleProperties"] = {
        _new_uuid(): {"resolutions": consolidated}
    }
    return component


def sanitize_styles(component_definition: dict) -> tuple[dict, int]:
    """Walk `component_definition` (the whole `cd` map) and consolidate
    every component's styleProperties to ONE canonical UUID entry. Useful
    one-time cleanup for a page that's already accumulated bloat.

    Returns (component_definition, n_components_repaired). Mutates in place.
    """
    if not isinstance(component_definition, dict):
        raise TypeError(
            f"component_definition must be a dict, got {type(component_definition).__name__}"
        )
    repaired = 0
    for comp in component_definition.values():
        if not isinstance(comp, dict):
            continue
        sp = comp.get("styleProperties") or {}
        if not isinstance(sp, dict) or len(sp) <= 1:
            continue
        # consolidate into one
        merge_style(comp, {})  # no-op CSS, but consolidates the existing
        repaired += 1
    return component_definition, repaired


def set_property(component: dict, name: str, value: Any) -> dict:
    """Set a literal property on a component (auto-wraps as {value: X})."""
    if not isinstance(component, dict):
        raise TypeError(f"component must be a dict")
    component.setdefault("properties", {})[name] = {"value": value}
    return component


def set_expression(component: dict, name: str, expression: str) -> dict:
    """Set an expression-bound property on a component.

    Wraps as `{location: {type: 'EXPRESSION', value: <expression>}}` per
    the Modlix property shape.
    """
    if not isinstance(component, dict):
        raise TypeError(f"component must be a dict")
    component.setdefault("properties", {})[name] = {
        "location": {"type": "EXPRESSION", "value": expression}
    }
    return component


def add_child(parent: dict, child_key: str) -> dict:
    """Add `child_key` to `parent.children` as the canonical `{childKey: True}`."""
    if not isinstance(parent, dict):
        raise TypeError(f"parent must be a dict")
    parent.setdefault("children", {})[child_key] = True
    return parent


def remove_child(parent: dict, child_key: str) -> dict:
    """Remove `child_key` from `parent.children` if present. No-op when absent."""
    if not isinstance(parent, dict):
        raise TypeError(f"parent must be a dict")
    ch = parent.get("children") or {}
    if isinstance(ch, dict):
        ch.pop(child_key, None)
    return parent
