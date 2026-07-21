"""Section segmentation + DOM walk/prune/collapse -> ComponentNode tree.

Two in-browser scripts run on the (already data-mxa-id-stamped) page:
  _SEGMENT_JS  -> ordered top-level sections with a deterministic role.
  _WALK_JS     -> the kept (meaningful) elements, each linked to its nearest
                  kept ancestor (so no-paint wrapper divs collapse away) and to
                  its containing section.

Python then assembles ComponentNode trees per section.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from app.services.page_analyzer.classify import refine_component_type
from app.services.page_analyzer.models import ComponentNode, Rect, SectionAnalysis

logger = logging.getLogger(__name__)


# Identify ordered sections; stamp data-mxa-section; classify role deterministically.
_SEGMENT_JS = r"""
() => {
  const VW = innerWidth, VH = innerHeight;
  const cs = (el) => getComputedStyle(el);
  const hidden = (s) => s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') === 0;
  const kebab = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || null;
  const vis = (el) => { const r = el.getBoundingClientRect(); if (r.height < 60 || r.width < 40) return false; return !hidden(cs(el)); };
  const body = document.body, main = document.querySelector('main') || body;
  const pageH = Math.max(document.documentElement.scrollHeight, body.getBoundingClientRect().height || 1);
  const LANDMARKS = ['HEADER','NAV','FOOTER','SECTION','ASIDE','ARTICLE','MAIN'];
  const blockKids = (el) => {
    const W = el.getBoundingClientRect().width || VW;
    return Array.from(el.children).filter(vis).filter((c) => {
      const r = c.getBoundingClientRect();
      return r.width >= W * 0.5 || LANDMARKS.includes(c.tagName);
    });
  };
  let cand = [];
  for (const sel of ['header', 'nav']) {
    const el = body.querySelector(sel);
    if (el && vis(el) && !main.contains(el)) cand.push(el);
  }
  let base = blockKids(main);
  if (base.length === 0 && vis(main)) base = [main];
  // Drill: unwrap a single dominant, tall wrapper into its children. Handles
  // SPA layouts where the whole page lives in one <div> (e.g. iii.dev's
  // body > div.sheet > [nav, hero, ...sections, footer]).
  for (let pass = 0; pass < 5; pass++) {
    let domIdx = -1;
    for (let j = 0; j < base.length; j++) {
      const r = base[j].getBoundingClientRect();
      if (r.height >= pageH * 0.55 && blockKids(base[j]).length >= 3) { domIdx = j; break; }
    }
    if (domIdx < 0) break;
    const expanded = blockKids(base[domIdx]);
    base = base.slice(0, domIdx).concat(expanded, base.slice(domIdx + 1));
  }
  cand.push(...base);
  const foot = body.querySelector('footer');
  if (foot && vis(foot) && !cand.includes(foot)) cand.push(foot);

  const seen = new Set();
  cand = cand.filter((el) => { const id = el.getAttribute('data-mxa-id'); if (!id || seen.has(id)) return false; seen.add(id); return true; });
  cand.sort((a, b) => (a.getBoundingClientRect().top + scrollY) - (b.getBoundingClientRect().top + scrollY));
  cand = cand.slice(0, 30);
  if (cand.length === 0 && vis(body)) cand = [body];

  const isNav = (el) => {
    const tag = el.tagName.toLowerCase();
    const r = el.getBoundingClientRect();
    const links = el.querySelectorAll('a').length;
    return tag === 'nav' || tag === 'header' || (r.top < 120 && links >= 3 && r.height < 160);
  };
  // hero = first non-nav candidate that owns an h1 and has decent height.
  let heroIdx = -1;
  for (let i = 0; i < cand.length; i++) {
    if (isNav(cand[i])) continue;
    const r = cand[i].getBoundingClientRect();
    if (cand[i].querySelector('h1') && r.height >= VH * 0.4) { heroIdx = i; break; }
  }
  const classify = (el, idx, total) => {
    const r = el.getBoundingClientRect();
    const cls = (el.getAttribute('class') || '');
    const links = el.querySelectorAll('a').length;
    const btns = el.querySelectorAll('button, [role=button]').length;
    const heads = el.querySelectorAll('h1, h2, h3').length;
    const tl = (el.textContent || '').trim().length;
    if (isNav(el)) return 'nav';
    if (el.tagName === 'FOOTER' || el.id === 'footer' || /(^|[^a-z])foot/.test(cls)) return 'footer';
    if (idx === total - 1 && links >= 6) return 'footer';
    if (idx === heroIdx) return 'hero';
    if (r.height < 320 && (btns >= 1 || links >= 1) && heads <= 1 && tl < 400) return 'cta';
    return 'content';
  };

  const total = cand.length;
  return cand.map((el, i) => {
    const ref = el.getAttribute('aria-labelledby');
    let name = ref && document.getElementById(ref) ? kebab(document.getElementById(ref).textContent) : null;
    if (!name) { const h = el.querySelector('h1, h2'); if (h) name = kebab(h.textContent); }
    if (!name) name = 'section-' + (i + 1);
    el.setAttribute('data-mxa-section', String(i));
    const r = el.getBoundingClientRect();
    return {
      index: i, name, role: classify(el, i, total), tag: el.tagName.toLowerCase(),
      mxa_id: el.getAttribute('data-mxa-id'),
      rect: { x: Math.round(r.left + scrollX), y: Math.round(r.top + scrollY), w: Math.round(r.width), h: Math.round(r.height) },
      heading_text: ((el.querySelector('h1, h2, h3') || {}).textContent || '').trim().slice(0, 200),
    };
  });
}
"""


# Walk the body, keep meaningful nodes, link each to nearest kept ancestor +
# section. `sectionIds` is the array of section-root data-mxa-id values.
_WALK_JS = r"""
(sectionIds) => {
  const SEC = new Set(sectionIds);
  const MAXN = 1500, MAXD = 20;
  const cs = (el) => getComputedStyle(el);
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','BR','HR','HEAD','META','LINK','TITLE']);
  const TEXT = new Set(['H1','H2','H3','H4','H5','H6','P','SPAN','LI','A','BUTTON','LABEL','STRONG','EM','BLOCKQUOTE']);
  const SELF = new Set(['IMG','SVG','VIDEO','IFRAME','INPUT','SELECT','TEXTAREA','TABLE','FORM','CANVAS','PICTURE','BUTTON','A']);
  const hidden = (el) => { const s = cs(el); if (s.display==='none'||s.visibility==='hidden'||parseFloat(s.opacity||'1')===0) return true; const r = el.getBoundingClientRect(); return r.width < 1 || r.height < 1; };
  const ownText = (el) => { let t = ''; for (const n of el.childNodes) { if (n.nodeType === 3) t += n.nodeValue; } return t.trim(); };
  const paint = (el) => {
    const s = cs(el);
    const bg = s.backgroundColor;
    if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') return true;
    if (s.backgroundImage && s.backgroundImage !== 'none') return true;
    if (['Top','Right','Bottom','Left'].some(d => parseFloat(s['border'+d+'Width']) > 0 && s['border'+d+'Style'] !== 'none')) return true;
    if (s.boxShadow && s.boxShadow !== 'none') return true;
    return false;
  };
  const rectOf = (el) => { const r = el.getBoundingClientRect(); return { x: Math.round(r.left+scrollX), y: Math.round(r.top+scrollY), w: Math.round(r.width), h: Math.round(r.height) }; };
  const classifyTag = (el) => {
    const tag = el.tagName.toLowerCase();
    if (['h1','h2','h3','h4','h5','h6','p','span','strong','em','blockquote'].includes(tag)) return 'Text';
    if (tag === 'input') { const t = (el.getAttribute('type')||'text').toLowerCase();
      return ({text:'TextBox',email:'TextBox',password:'TextBox',number:'TextBox',tel:'PhoneNumber',url:'TextBox',search:'TextBox',date:'Calendar','datetime-local':'Calendar',time:'Calendar',checkbox:'CheckBox',radio:'RadioButton',file:'FileUpload',color:'ColorPicker',range:'RangeSlider',hidden:'TextBox'})[t] || 'TextBox'; }
    if (tag === 'svg') return el.querySelector('text') ? 'Image' : 'Icon';
    const m = {div:'Grid',section:'Grid',main:'Grid',header:'Grid',footer:'Grid',nav:'Grid',article:'Grid',aside:'Grid',ul:'Grid',ol:'Grid',li:'Grid',form:'Form',button:'Button',img:'Image',a:'Link',iframe:'Iframe',video:'Video',audio:'Audio',select:'Dropdown',textarea:'TextArea',label:'Label',table:'Table'};
    return m[tag] || 'Grid';
  };
  const visKids = (el) => [...el.children].filter(c => c.nodeType === 1 && !SKIP.has(c.tagName) && !hidden(c));
  const keep = (el) => {
    const tag = el.tagName;
    if (SELF.has(tag)) return true;
    if (TEXT.has(tag) && ownText(el)) return true;
    if (paint(el)) return true;
    if (visKids(el).length >= 2) return true;
    return false;
  };
  const out = [];
  function walk(el, depth, keptParent, sectionId) {
    if (!el || el.nodeType !== 1 || SKIP.has(el.tagName) || depth > MAXD || out.length >= MAXN) return;
    const id = el.getAttribute('data-mxa-id');
    if (SEC.has(id)) sectionId = id;
    if (hidden(el)) return;
    let myParent = keptParent;
    if (keep(el)) {
      const tag = el.tagName.toLowerCase();
      out.push({
        mxa_id: id, tag, component: classifyTag(el),
        parent_mxa: keptParent, section_mxa: sectionId,
        text: ownText(el).slice(0, 400) || null,
        href: (el.getAttribute && el.getAttribute('href')) || null,
        src: el.currentSrc || el.src || null,
        alt: (el.getAttribute && el.getAttribute('alt')) || null,
        input_type: tag === 'input' ? (el.getAttribute('type') || 'text') : null,
        classes: (el.getAttribute('class') || '').split(/\s+/).filter(Boolean).slice(0, 12),
        role_attr: el.getAttribute('role') || null,
        has_paint: paint(el),
        child_count: visKids(el).length,
        rect: rectOf(el),
      });
      myParent = id;
    }
    for (const c of el.children) walk(c, depth + 1, myParent, sectionId);
  }
  walk(document.body, 0, null, null);
  return out;
}
"""


def _make_node(raw: Dict[str, Any]) -> ComponentNode:
    node = ComponentNode(
        mxa_id=raw["mxa_id"],
        component_type=raw.get("component") or "Grid",
        tag=raw.get("tag") or "",
        text=raw.get("text"),
        href=raw.get("href"),
        src=raw.get("src"),
        alt=raw.get("alt"),
        input_type=raw.get("input_type"),
        classes=raw.get("classes") or [],
        role_attr=raw.get("role_attr"),
    )
    return refine_component_type(node, raw)


def build_sections(
    segments: List[Dict[str, Any]], kept: List[Dict[str, Any]]
) -> List[SectionAnalysis]:
    """Assemble ComponentNode trees grouped by section."""
    nodes: Dict[str, ComponentNode] = {}
    for raw in kept:
        mid = raw.get("mxa_id")
        if not mid:
            continue
        nodes[mid] = _make_node(raw)

    sec_of = {raw["mxa_id"]: raw.get("section_mxa") for raw in kept if raw.get("mxa_id")}

    # Link children (kept is in document order, so child order is preserved).
    for raw in kept:
        mid = raw.get("mxa_id")
        if not mid or mid not in nodes:
            continue
        parent = raw.get("parent_mxa")
        if parent and parent in nodes:
            nodes[parent].children.append(nodes[mid])

    sections: List[SectionAnalysis] = []
    for seg in segments:
        sid = seg.get("mxa_id")
        roots: List[ComponentNode] = []
        for raw in kept:
            mid = raw.get("mxa_id")
            if not mid or raw.get("section_mxa") != sid:
                continue
            parent = raw.get("parent_mxa")
            # A section root is a kept node whose parent is absent or sits in a
            # different section (i.e. it's the top of this section's subtree).
            if not parent or sec_of.get(parent) != sid:
                roots.append(nodes[mid])
        rect = seg.get("rect") or {}
        sections.append(
            SectionAnalysis(
                index=seg.get("index", 0),
                mxa_id=sid,
                name=seg.get("name") or f"section-{seg.get('index', 0) + 1}",
                role=seg.get("role") or "content",
                rect=Rect(**rect) if rect else None,
                heading_text=seg.get("heading_text") or "",
                roots=roots,
            )
        )
    return sections


def iter_nodes(sections: List[SectionAnalysis]):
    """Yield every ComponentNode across all sections (depth-first)."""

    def walk(n: ComponentNode):
        yield n
        for c in n.children:
            yield from walk(c)

    for s in sections:
        for r in s.roots:
            yield from walk(r)


def count_nodes(sections: List[SectionAnalysis]) -> int:
    return sum(1 for _ in iter_nodes(sections))
