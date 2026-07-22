"""Map a captured tree (analysis.json full_tree) -> a Modlix page definition.

Produces the `componentDefinition` map + root key consumed by /api/ui/pages
(what the MCP create_page / replace_page_definition tools wrap). Each captured
node already carries Modlix-shaped `styleProperties`, so the heavy lifting is
type selection + properties; styling is attached as-is.

Mapping (visual-fidelity first; styleProperties does most of the work):
  - containers (div/section/body/html/nav/...) -> Grid
  - text leaves (h*/p/span/...) -> Text (+ textType)
  - img -> Image; svg -> Image with the captured markup as a data: URI
  - input -> TextBox; leaf <a> -> Link (+ linkPath); leaf <button> -> Button
  - an <a>/<button> WITH children stays a Grid so its contents (icon+label) render
"""

from __future__ import annotations

import base64
import re
import uuid
from typing import Any, Dict, Optional, Tuple

from app.services.page_analyzer.models import ComponentNode

_VAR_RE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*([^)]*))?\)")


def _resolve_vars(value: str, css_vars: Dict[str, str]) -> str:
    """Replace var(--x[, fallback]) with concrete values. Modlix scopes style
    docs so a :root block doesn't reach the document root; resolving here makes
    colors/backgrounds concrete and independent of :root injection."""
    if not isinstance(value, str) or "var(" not in value:
        return value

    def rep(m: "re.Match") -> str:
        name, fb = m.group(1), m.group(2)
        if name in css_vars:
            return css_vars[name]
        return fb.strip() if fb else m.group(0)

    for _ in range(4):  # vars may reference vars
        new = _VAR_RE.sub(rep, value)
        if new == value:
            break
        value = new
    return value


def _resolve_vars_in_styleprops(sp: Dict[str, Any], css_vars: Dict[str, str]) -> None:
    for rule in sp.values():
        if not isinstance(rule, dict):
            continue
        for block in (rule.get("resolutions") or {}).values():
            if not isinstance(block, dict):
                continue
            for leaf in block.values():
                if isinstance(leaf, dict) and isinstance(leaf.get("value"), str):
                    leaf["value"] = _resolve_vars(leaf["value"], css_vars)

# CSS-inherited typography. Modlix components do NOT inherit CSS across the
# component tree the way the DOM does, so we push the effective value onto each
# text-bearing leaf (else headings lose their font-size/family and go tiny).
_INHERIT = {
    "fontFamily", "fontSize", "fontWeight", "fontStyle", "lineHeight", "letterSpacing",
    "color", "textAlign", "textTransform", "whiteSpace", "wordSpacing", "fontStretch",
    "fontVariantLigatures", "fontFeatureSettings", "textShadow",
}


def _base_all_block(sp: Dict[str, Any], create: bool = False) -> Optional[Dict[str, Any]]:
    """The ALL resolution of the base (non-pseudo) rule; created if asked."""
    for rule in sp.values():
        if isinstance(rule, dict) and not rule.get("pseudoState"):
            res = rule.get("resolutions")
            if isinstance(res, dict):
                if create:
                    return res.setdefault("ALL", {})
                return res.get("ALL")
    if create:
        rid = uuid.uuid4().hex
        sp[rid] = {"resolutions": {"ALL": {}}}
        return sp[rid]["resolutions"]["ALL"]
    return None

_TEXT_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "strong", "em", "blockquote", "li", "label"}
_TEXT_TYPE = {"h1": "H1", "h2": "H2", "h3": "H3", "h4": "H4", "h5": "H5", "h6": "H6", "p": "PARAGRAPH", "span": "SPAN"}


def _svg_data_uri(svg_html: str) -> str:
    b64 = base64.b64encode(svg_html.encode("utf-8")).decode("ascii")
    return "data:image/svg+xml;base64," + b64


def _pick_type(tag: str, has_children: bool) -> str:
    if tag == "img" or tag == "svg":
        return "Image"
    if tag == "input":
        return "TextBox"
    if tag == "textarea":
        return "TextArea"
    if tag == "a" and not has_children:
        return "Link"
    if tag == "button" and not has_children:
        return "Button"
    if tag in _TEXT_TAGS and not has_children:
        return "Text"
    return "Grid"


def _inject_browser_defaults(style_props: Dict[str, Any]) -> None:
    """Emit CSS initial-values that the BROWSER implies but Modlix doesn't.

    Modlix Grid containers default to flex-direction:column, while the browser
    defaults flex to row. Authored CSS that relies on the row default omits
    flex-direction, so Modlix stacks vertically. Make it explicit. Same idea for
    grid display (Modlix may not switch the container to grid on its own)."""
    for rule in style_props.values():
        if not isinstance(rule, dict):
            continue
        res = rule.get("resolutions") or {}
        has_dir = any("flexDirection" in (b or {}) for b in res.values())
        for block in res.values():
            if not isinstance(block, dict):
                continue
            disp = (block.get("display") or {}).get("value")
            if disp in ("flex", "inline-flex") and not has_dir and "flexDirection" not in block:
                block["flexDirection"] = {"value": "row"}


def build_page_definition(
    root: ComponentNode, *, cap: int = 6000, css_vars: Optional[Dict[str, str]] = None
) -> Tuple[Dict[str, Any], str]:
    """Return (componentDefinition, root_key)."""
    comps: Dict[str, Any] = {}

    def make(node: ComponentNode, order: int, inherited: Dict[str, str]) -> Optional[str]:
        if len(comps) >= cap:
            return None
        tag = (node.tag or "div").lower()
        has_children = bool(node.children)
        ctype = _pick_type(tag, has_children)
        key = node.mxa_id
        sp = node.style_properties or {}
        _inject_browser_defaults(sp)
        comp: Dict[str, Any] = {
            "key": key,
            "type": ctype,
            "name": (tag or ctype)[:40],
            "displayOrder": order,
            "children": {},
            "properties": {},
            "styleProperties": sp,
        }

        # Effective inherited typography = ancestors' values overridden by own.
        all_block = _base_all_block(sp) or {}
        own = {
            p: all_block[p]["value"]
            for p in _INHERIT
            if isinstance(all_block.get(p), dict) and "value" in all_block[p]
        }
        merged = {**inherited, **own}
        # Modlix doesn't inherit CSS: push effective typography onto text leaves.
        if ctype in ("Text", "Link", "Button"):
            blk = _base_all_block(sp, create=True)
            for prop, val in merged.items():
                blk.setdefault(prop, {"value": val})

        props = comp["properties"]
        if ctype == "Text":
            if node.text:
                props["text"] = {"value": node.text}
            tt = _TEXT_TYPE.get(tag)
            if tt:
                props["textType"] = {"value": tt}
        elif ctype == "Image":
            if tag == "svg" and node.svg_html:
                props["src"] = {"value": _svg_data_uri(node.svg_html)}
            elif node.src:
                props["src"] = {"value": node.src}
            if node.alt:
                props["alt"] = {"value": node.alt}
        elif ctype in ("Link", "Button"):
            if node.text:
                props["label"] = {"value": node.text}
            if ctype == "Link" and node.href:
                props["linkPath"] = {"value": node.href}

        comps[key] = comp  # register before recursing so the cap counts it

        if ctype == "Grid":
            child_order = 0
            # preserve a container's own text (rare: text + element children)
            if node.text:
                tkey = key + "_t"
                tblock = {p: {"value": v} for p, v in merged.items()}
                comps[tkey] = {
                    "key": tkey, "type": "Text", "name": "text", "displayOrder": child_order,
                    "children": {}, "properties": {"text": {"value": node.text}},
                    "styleProperties": {uuid.uuid4().hex: {"resolutions": {"ALL": tblock}}} if tblock else {},
                }
                comp["children"][tkey] = True
                child_order += 1
            for child in node.children:
                ck = make(child, child_order, merged)
                if ck:
                    comp["children"][ck] = True
                    child_order += 1
        return key

    make(root, 0, {})

    if css_vars:
        # pre-resolve vars that reference other vars, then resolve everywhere
        cv = {k: _resolve_vars(v, css_vars) for k, v in css_vars.items()}
        for comp in comps.values():
            _resolve_vars_in_styleprops(comp.get("styleProperties") or {}, cv)

    return comps, root.mxa_id
