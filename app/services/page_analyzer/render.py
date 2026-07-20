"""Render a PageAnalysis back into a standalone HTML file for visual QA.

This reconstructs the page from what we extracted so you can eyeball fidelity
against the live site. Per the chosen approach:
  - base (ALL) authored CSS  -> INLINE style="" on each element
  - hover + responsive (tablet/mobile) -> id-level rules in a <style> block
    (#mxaId:hover {...}, @media (max-width:1024px){#mxaId{...}}, etc.)
  - :root custom properties (design tokens) so var(--x) resolves
Background-image / src URLs are absolutized against the page origin so they load.

Known gaps (honest): SVG/icon inner markup is not captured (icons render empty);
wrapper containers drilled through during segmentation (e.g. a centered
max-width sheet) are not nodes, so global centering can be lost.
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin, urlparse

from app.services.page_analyzer.models import ComponentNode, PageAnalysis
from app.services.page_analyzer.to_modlix import BP_MOBILE, BP_TABLET

_VOID_TAGS = {"img", "input", "br", "hr", "source", "meta", "link", "area", "embed"}
# Modlix breakpoint key -> CSS media condition used in the live styleProcessor.
_BP_MEDIA = {BP_TABLET: "(max-width: 1024px)", BP_MOBILE: "(max-width: 480px)"}
_VENDOR = ("webkit", "moz", "ms", "o")


def _camel_to_kebab(prop: str) -> str:
    out = re.sub(r"([A-Z])", r"-\1", prop).lower()
    for v in _VENDOR:
        if out.startswith(v + "-"):
            return "-" + out
    return out


def _absolutize(value: str, base_url: str) -> str:
    """Resolve relative url(...) references in a CSS value against the origin."""
    if "url(" not in value:
        return value

    def repl(m: "re.Match") -> str:
        raw = m.group(1).strip().strip("'\"")
        if raw.startswith(("http://", "https://", "data:")):
            return m.group(0)
        return f'url("{urljoin(base_url, raw)}")'

    return re.sub(r"url\(([^)]*)\)", repl, value)


def _decls(block: Dict[str, Any], base_url: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for leaf, wrapped in (block or {}).items():
        if not isinstance(wrapped, dict):
            continue
        val = wrapped.get("value")
        if val is None:
            continue
        out.append((_camel_to_kebab(leaf), _absolutize(str(val), base_url)))
    return out


def _node_styles(
    node: ComponentNode, base_url: str
) -> Tuple[str, List[str]]:
    """Return (inline_style_for_ALL, [id-level css rules for media + hover])."""
    inline_parts: List[str] = []
    rules: List[str] = []
    sel = f"#{node.mxa_id}"

    for rule in node.style_properties.values():
        if not isinstance(rule, dict):
            continue
        resolutions = rule.get("resolutions") or {}
        pseudo = rule.get("pseudoState") or ""

        for bp, block in resolutions.items():
            decls = _decls(block, base_url)
            if not decls:
                continue
            if bp == "ALL" and not pseudo:
                # Base goes inline.
                inline_parts.append(" ".join(f"{k}:{v};" for k, v in decls))
                continue
            # Responsive + hover live in the <style> block as #id rules. They must
            # win over the inline base (inline beats a #id selector), so !important.
            css = " ".join(f"{k}:{v} !important;" for k, v in decls)
            if pseudo:
                rules.append(f"{sel}:{pseudo} {{ {css} }}")
            elif bp in _BP_MEDIA:
                rules.append(f"@media {_BP_MEDIA[bp]} {{ {sel} {{ {css} }} }}")
            else:
                rules.append(f"{sel} {{ {css} }}")

    return " ".join(inline_parts), rules


# html/body can't nest inside our wrapper body; render them as plain divs.
_TAG_AS_DIV = {"html", "body", "head", "svg", "canvas", "main"}


def _render_node(node: ComponentNode, base_url: str, rules: List[str]) -> str:
    tag = (node.tag or "div").lower()
    tag_render = "div" if tag in _TAG_AS_DIV else tag

    inline, node_rules = _node_styles(node, base_url)
    rules.extend(node_rules)

    attrs = [f'id="{html.escape(node.mxa_id, quote=True)}"']
    if inline:
        attrs.append(f'style="{html.escape(inline, quote=True)}"')
    if tag_render == "a" and node.href:
        attrs.append(f'href="{html.escape(_abs(node.href, base_url), quote=True)}"')
    if tag_render == "img" and node.src:
        attrs.append(f'src="{html.escape(_abs(node.src, base_url), quote=True)}"')
        if node.alt:
            attrs.append(f'alt="{html.escape(node.alt, quote=True)}"')

    open_tag = f"<{tag_render} {' '.join(attrs)}>"
    if tag_render in _VOID_TAGS:
        return open_tag

    # Raw SVG markup captured during full-DOM walk: emit verbatim inside the box.
    if node.svg_html:
        return f"{open_tag}{_absolutize(node.svg_html, base_url)}</{tag_render}>"

    inner = []
    if node.text:
        inner.append(html.escape(node.text))
    for child in node.children:
        inner.append(_render_node(child, base_url, rules))
    return f"{open_tag}{''.join(inner)}</{tag_render}>"


def _abs(url: str, base_url: str) -> str:
    if not url or url.startswith(("http://", "https://", "data:", "#", "mailto:", "tel:")):
        return url
    return urljoin(base_url, url)


def render_preview_html(analysis: PageAnalysis) -> str:
    base_url = analysis.url or ""
    rules: List[str] = []
    body_parts: List[str] = []

    if analysis.full_tree is not None:
        # Faithful full-DOM render (includes body + all wrappers).
        body_parts.append(_render_node(analysis.full_tree, base_url, rules))
    else:
        # Fallback: pruned component plan (sections only).
        for sec in analysis.sections:
            body_parts.append(
                f"\n<!-- section [{sec.index}] {sec.role}: {html.escape(sec.name)} -->"
            )
            for root in sec.roots:
                body_parts.append(_render_node(root, base_url, rules))

    font_faces = "\n".join(analysis.font_faces or [])
    keyframes = "\n".join(analysis.keyframes or [])
    root_vars = "; ".join(
        f"{k}: {v}" for k, v in (analysis.root_custom_properties or {}).items()
    )
    title = html.escape(analysis.url or "page analysis preview")

    return (
        "<!doctype html>\n<html>\n<head>\n<meta charset='utf-8'>\n"
        f"<title>preview: {title}</title>\n"
        "<style>\n"
        + font_faces
        + "\n"
        + keyframes
        + "\n  *{box-sizing:border-box}\n"
        f"  :root {{ {root_vars} }}\n"
        "  html,body{margin:0;padding:0}\n"
        + "\n".join(rules)
        + "\n</style>\n</head>\n<body>\n"
        f"<!-- Reconstructed from analysis of {title}. base inline / hover+media as #id rules. -->\n"
        + "\n".join(body_parts)
        + "\n</body>\n</html>\n"
    )
