"""CDP-based AUTHORED CSS extraction.

Reads the CSS rules that apply to an element via Chrome DevTools Protocol
`CSS.getMatchedStylesForNode` (the same data the DevTools "Styles" panel shows).
This gives ORIGINAL authored values (`100%`, `repeat(3,1fr)`, unitless
line-height), each rule's `@media` condition, and pseudo-state rules -- unlike
`getComputedStyle` (used-values) or naive in-page CSSOM (throws cross-origin).

For each element we resolve the authored cascade at three sample widths
(desktop/tablet/mobile) by filtering rules whose media conditions match that
width, then taking last-wins per property with `!important` and inline overrides
respected. Hover is captured by forcing the `:hover` pseudo-state and diffing.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_WIDTH_RE = re.compile(r"(min|max)-width\s*:\s*([0-9.]+)\s*px", re.IGNORECASE)


def media_matches(media_text: str, width: int) -> bool:
    """Does a single @media condition apply at `width` (px)? Width-only logic;
    non-screen (print) is excluded; conditions without width tokens match."""
    t = (media_text or "").lower()
    if "print" in t and "screen" not in t:
        return False
    for kind, num in _WIDTH_RE.findall(t):
        n = float(num)
        if kind == "min" and width < n:
            return False
        if kind == "max" and width > n:
            return False
    return True


def _all_media_match(media_list: Optional[List[Dict[str, Any]]], width: int) -> bool:
    for m in media_list or []:
        if not media_matches(m.get("text") or "", width):
            return False
    return True


def _decl_value(d: Dict[str, Any]):
    """Return (name, value, important) for a CDP cssProperty, or None to skip."""
    name = d.get("name")
    val = d.get("value")
    if not name or val in (None, ""):
        return None
    if d.get("disabled"):
        return None
    important = bool(d.get("important"))
    if isinstance(val, str) and "!important" in val:
        important = True
        val = val.replace("!important", "").strip()
    return name, val, important


class CDPStyleExtractor:
    """Wraps a Playwright CDP session for authored-CSS extraction."""

    def __init__(self, cdp_session: Any):
        self.cdp = cdp_session
        self._root_node_id: Optional[int] = None

    async def enable(self) -> None:
        await self.cdp.send("DOM.enable")
        await self.cdp.send("CSS.enable")
        doc = await self.cdp.send("DOM.getDocument", {"depth": 0})
        self._root_node_id = doc["root"]["nodeId"]

    async def node_id_for(self, mxa_id: str) -> Optional[int]:
        try:
            res = await self.cdp.send(
                "DOM.querySelector",
                {"nodeId": self._root_node_id, "selector": f'[data-mxa-id="{mxa_id}"]'},
            )
            return res.get("nodeId") or None
        except Exception as exc:  # noqa: BLE001
            logger.debug("node_id_for(%s) failed: %s", mxa_id, exc)
            return None

    async def _matched(self, node_id: int) -> Dict[str, Any]:
        # Some nodes (iframe/shadow content, detached) can't be styled; skip them
        # gracefully so a single failure doesn't abort a full-DOM capture.
        try:
            return await self.cdp.send(
                "CSS.getMatchedStylesForNode", {"nodeId": node_id}
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("getMatchedStylesForNode(%s) failed: %s", node_id, exc)
            return {}

    def _resolve(self, matched: Dict[str, Any], width: int) -> Dict[str, str]:
        """Effective authored declarations at `width`. Precedence (low->high):
        author non-important (source order) < inline non-important <
        author/inline important."""
        normal: Dict[str, str] = {}
        important: Dict[str, str] = {}

        # matchedCSSRules are returned low->high priority; last wins.
        for entry in matched.get("matchedCSSRules") or []:
            rule = entry.get("rule") or {}
            # Skip the browser's User-Agent stylesheet (the built-in defaults like
            # `p { display:block; margin-block:1em }`). We want AUTHORED CSS only;
            # a UA-equal value is kept only when the author's own rule re-declares
            # it (origin "regular"/"inspector"/"injected").
            if rule.get("origin") == "user-agent":
                continue
            if not _all_media_match(rule.get("media"), width):
                continue
            style = rule.get("style") or {}
            for d in style.get("cssProperties") or []:
                parsed = _decl_value(d)
                if not parsed:
                    continue
                name, val, imp = parsed
                if name.startswith("--"):
                    continue
                (important if imp else normal)[name] = val

        for d in (matched.get("inlineStyle") or {}).get("cssProperties") or []:
            parsed = _decl_value(d)
            if not parsed:
                continue
            name, val, imp = parsed
            if name.startswith("--"):
                continue
            (important if imp else normal)[name] = val  # inline > author (non-imp)

        out = dict(normal)
        out.update(important)
        return out

    async def root_custom_properties(self) -> Dict[str, str]:
        """Collect `:root` CSS custom properties (candidate theme tokens).

        Fetches a fresh document root each call: a prior DOM.getDocument (e.g.
        id_map) can invalidate cached nodeIds, which silently returned 0 vars.
        """
        try:
            doc = await self.cdp.send("DOM.getDocument", {"depth": 0})
            root_id = doc["root"]["nodeId"]
            res = await self.cdp.send(
                "DOM.querySelector", {"nodeId": root_id, "selector": ":root"}
            )
            nid = res.get("nodeId")
            if not nid:
                return {}
            matched = await self._matched(nid)
            out: Dict[str, str] = {}
            for entry in matched.get("matchedCSSRules") or []:
                style = (entry.get("rule") or {}).get("style") or {}
                for d in style.get("cssProperties") or []:
                    name = d.get("name")
                    val = d.get("value")
                    if name and name.startswith("--") and val:
                        out[name] = str(val).replace("!important", "").strip()
            return out
        except Exception as exc:  # noqa: BLE001
            logger.debug("root_custom_properties failed: %s", exc)
            return {}

    async def styles_for_node(
        self, nid: int, widths: Dict[str, int], with_hover: bool = True
    ) -> Dict[str, Any]:
        """Resolve authored styles for a CDP nodeId at each sample width + hover."""
        matched = await self._matched(nid)
        per = {name: self._resolve(matched, w) for name, w in widths.items()}

        hover: Dict[str, str] = {}
        if with_hover:
            desktop_w = widths.get("desktop") or next(iter(widths.values()), 1440)
            try:
                await self.cdp.send(
                    "CSS.forcePseudoState",
                    {"nodeId": nid, "forcedPseudoClasses": ["hover"]},
                )
                hovered = await self._matched(nid)
                await self.cdp.send(
                    "CSS.forcePseudoState", {"nodeId": nid, "forcedPseudoClasses": []}
                )
                h = self._resolve(hovered, desktop_w)
                base = per.get("desktop") or self._resolve(matched, desktop_w)
                hover = {p: v for p, v in h.items() if base.get(p) != v}
            except Exception as exc:  # noqa: BLE001
                logger.debug("hover extract failed for node %s: %s", nid, exc)

        return {"per_breakpoint": per, "hover": hover}

    async def extract(
        self, mxa_id: str, widths: Dict[str, int]
    ) -> Optional[Dict[str, Any]]:
        """Resolve authored styles for one stamped element (by data-mxa-id)."""
        nid = await self.node_id_for(mxa_id)
        if not nid:
            return None
        return await self.styles_for_node(nid, widths, with_hover=True)

    async def resolved_at(self, nid: int, width: int) -> Dict[str, str]:
        """Resolved authored declarations for a nodeId at the CURRENT viewport.

        CDP only returns viewport-ACTIVE media rules, so the caller must have
        already resized the page to `width` before calling this.
        """
        return self._resolve(await self._matched(nid), width)

    async def hover_at(self, nid: int, width: int) -> Dict[str, str]:
        """Resolved declarations with :hover forced (caller diffs vs base)."""
        try:
            await self.cdp.send(
                "CSS.forcePseudoState", {"nodeId": nid, "forcedPseudoClasses": ["hover"]}
            )
            matched = await self._matched(nid)
            await self.cdp.send(
                "CSS.forcePseudoState", {"nodeId": nid, "forcedPseudoClasses": []}
            )
            return self._resolve(matched, width)
        except Exception as exc:  # noqa: BLE001
            logger.debug("hover_at(%s) failed: %s", nid, exc)
            return {}

    async def id_map(self) -> Dict[str, int]:
        """One DOM.getDocument(-1) -> {data-mxa-id: nodeId} for the whole tree.

        Avoids a per-element DOM.querySelector round-trip when styling many nodes.
        """
        out: Dict[str, int] = {}
        try:
            doc = await self.cdp.send("DOM.getDocument", {"depth": -1, "pierce": False})
        except Exception as exc:  # noqa: BLE001
            logger.debug("getDocument(-1) failed: %s", exc)
            return out

        def walk(node: Dict[str, Any]) -> None:
            attrs = node.get("attributes") or []
            # attributes is a flat [name, value, name, value, ...] list
            for i in range(0, len(attrs) - 1, 2):
                if attrs[i] == "data-mxa-id":
                    out[attrs[i + 1]] = node.get("nodeId")
                    break
            for child in node.get("children") or []:
                walk(child)
            cd = node.get("contentDocument")
            if cd:
                walk(cd)

        walk(doc.get("root", {}))
        return out
