"""Section-based extraction pipeline — Phase 1 of the clone process.

Uses Playwright to:
1. Unwrap SPA containers and discover page sections
2. Extract per-section specs using getComputedStyle (full values, not parent-diffed)
3. Discover and catalog all assets (images, videos, SVGs, backgrounds)
4. Extract responsive diffs at tablet (768px) and mobile (390px)
5. Extract pseudo-class styles from stylesheets

JS extraction scripts adapted from ai-website-cloner-template SKILL.md.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "tablet": {"width": 768, "height": 1024},
    "mobile": {"width": 390, "height": 844},
}


@dataclass
class SectionSpec:
    """Complete specification for one page section — everything needed to build it."""
    name: str
    selector: str
    index: int  # display order (top to bottom)
    screenshot_b64: str = ""
    element_tree: dict = field(default_factory=dict)
    content_text: list = field(default_factory=list)
    images: list = field(default_factory=list)
    iframes: list = field(default_factory=list)
    links: list = field(default_factory=list)
    responsive_diffs: dict = field(default_factory=dict)  # {viewport: changed_styles}
    pseudo_styles: dict = field(default_factory=dict)
    bg_color: str = ""
    bg_image: str = ""
    height: int = 0
    width: int = 0
    is_row: bool = False
    tag: str = "div"


@dataclass
class PageExtraction:
    """Complete extraction results for a page."""
    url: str
    sections: list  # List[SectionSpec]
    full_screenshot_b64: str = ""
    body_font: str = ""
    body_color: str = ""
    body_bg: str = ""
    all_fonts: list = field(default_factory=list)
    assets: dict = field(default_factory=dict)  # {url: {src, alt, w, h, type}}
    keyframes: list = field(default_factory=list)  # [{name, body}] @keyframes rules


# ── JS: SPA Unwrap + Section Discovery ──

_DISCOVER_SECTIONS_JS = """() => {
    // Unwrap SPA root containers (React, Vue, Angular, Gatsby, Next.js, SvelteKit)
    // Handles `display: contents` wrappers too (SvelteKit wraps root in one of these).
    let contentRoot = document.body;
    for (let i = 0; i < 8; i++) {
        const visible = [];
        let displayContentsWrap = null;
        for (const child of contentRoot.children) {
            const tag = (child.tagName || '').toLowerCase();
            if (['script','style','noscript','link','meta'].includes(tag)) continue;
            const cs = getComputedStyle(child);
            if (cs.display === 'contents') {
                // Zero bounding box but children are visible — unwrap through
                displayContentsWrap = child;
                continue;
            }
            const r = child.getBoundingClientRect();
            if (r.width < 10 || r.height < 10) continue;
            visible.push(child);
        }
        // If the only child is a display:contents wrapper and there are no
        // other visible siblings, step into it.
        if (visible.length === 0 && displayContentsWrap) {
            contentRoot = displayContentsWrap;
            continue;
        }
        if (visible.length === 1 && ['div','main','section'].includes(visible[0].tagName.toLowerCase())) {
            contentRoot = visible[0];
            continue;
        }
        break;
    }

    // Discover sections — top-level visible children of the content root
    const sections = [];
    let idx = 0;
    for (const child of contentRoot.children) {
        const tag = (child.tagName || '').toLowerCase();
        if (['script','style','noscript','link','meta'].includes(tag)) continue;
        const rect = child.getBoundingClientRect();
        if (rect.width < 10 || rect.height < 10) continue;

        const cs = getComputedStyle(child);
        const display = cs.display;
        const flexDir = cs.flexDirection;
        const isRow = (display === 'flex' || display === 'inline-flex') && flexDir === 'row';

        // Build a unique selector for this element.
        // Use [id="..."] instead of #id because IDs starting with digits
        // (common in Modlix-generated pages with UUID keys like "2yGmk...")
        // are invalid CSS #id selectors and cause querySelector to throw.
        let selector = '';
        if (child.id) {
            selector = '[id="' + child.id + '"]';
        } else {
            // Use nth-of-type which counts only same-tag siblings
            const sameTagBefore = [...contentRoot.children].slice(0, [...contentRoot.children].indexOf(child))
                .filter(s => s.tagName === child.tagName).length;
            selector = ':scope > ' + tag + ':nth-of-type(' + (sameTagBefore + 1) + ')';
        }

        // Drill disabled — sub-sections lose parent flex/grid context and
        // render collapsed. Reverted to plain top-level discovery.
        if (false) {
            let drill = child;
            let drillSelector = selector;
            for (let d = 0; d < 4; d++) {
                const kids = [...drill.children].filter(k => {
                    const kt = (k.tagName || '').toLowerCase();
                    if (['script','style','noscript','link','meta'].includes(kt)) return false;
                    const kr = k.getBoundingClientRect();
                    return kr.width >= 10 && kr.height >= 10;
                });
                if (kids.length === 1) {
                    const k = kids[0];
                    const kt = (k.tagName || '').toLowerCase();
                    const sameTag = [...drill.children].slice(0, [...drill.children].indexOf(k))
                        .filter(s => s.tagName === k.tagName).length;
                    drillSelector = drillSelector + ' > ' + kt + ':nth-of-type(' + (sameTag + 1) + ')';
                    drill = k;
                    continue;
                }
                break;
            }
            if (drill.children.length >= 3) {
                let subIdx = 0;
                for (const gc of drill.children) {
                    const gt = (gc.tagName || '').toLowerCase();
                    if (['script','style','noscript','link','meta'].includes(gt)) continue;
                    const gr = gc.getBoundingClientRect();
                    if (gr.width < 10 || gr.height < 10) continue;
                    const gcs = getComputedStyle(gc);
                    const sameTagBefore = [...drill.children].slice(0, [...drill.children].indexOf(gc))
                        .filter(s => s.tagName === gc.tagName).length;
                    const subSelector = drillSelector + ' > ' + gt + ':nth-of-type(' + (sameTagBefore + 1) + ')';
                    sections.push({
                        tag: gt, id: gc.id || '', selector: subSelector,
                        index: idx, y: Math.round(gr.y), h: Math.round(gr.height), w: Math.round(gr.width),
                        bgColor: gcs.backgroundColor,
                        bgImage: gcs.backgroundImage !== 'none' ? gcs.backgroundImage : '',
                        position: gcs.position,
                        isRow: (gcs.display === 'flex' || gcs.display === 'inline-flex') && gcs.flexDirection === 'row',
                        childCount: gc.children.length,
                    });
                    idx++; subIdx++;
                }
                if (subIdx > 0) continue;
            }
        }
        if (false) {
            let subIdx = 0;
            for (const gc of child.children) {
                const gt = (gc.tagName || '').toLowerCase();
                if (['script','style','noscript','link','meta'].includes(gt)) continue;
                const gr = gc.getBoundingClientRect();
                if (gr.width < 10 || gr.height < 10) continue;
                const gcs = getComputedStyle(gc);
                const sameTagBefore = [...child.children].slice(0, [...child.children].indexOf(gc))
                    .filter(s => s.tagName === gc.tagName).length;
                const subSelector = selector + ' > ' + gt + ':nth-of-type(' + (sameTagBefore + 1) + ')';
                sections.push({
                    tag: gt,
                    id: gc.id || '',
                    selector: subSelector,
                    index: idx,
                    y: Math.round(gr.y),
                    h: Math.round(gr.height),
                    w: Math.round(gr.width),
                    bgColor: gcs.backgroundColor,
                    bgImage: gcs.backgroundImage !== 'none' ? gcs.backgroundImage : '',
                    position: gcs.position,
                    isRow: (gcs.display === 'flex' || gcs.display === 'inline-flex') && gcs.flexDirection === 'row',
                    childCount: gc.children.length,
                });
                idx++;
                subIdx++;
            }
            if (subIdx > 0) continue;  // don't also add the parent
        }

        sections.push({
            tag,
            id: child.id || '',
            selector,
            index: idx,
            y: Math.round(rect.y),
            h: Math.round(rect.height),
            w: Math.round(rect.width),
            bgColor: cs.backgroundColor,
            bgImage: cs.backgroundImage !== 'none' ? cs.backgroundImage : '',
            position: cs.position,
            isRow,
            childCount: child.children.length,
        });
        idx++;
    }
    return sections;
}"""


# ── JS: Per-Section Deep Extraction (from ai-website-cloner-template SKILL.md) ──

_EXTRACT_SECTION_JS = """(selector) => {
    // Find the content root (same SPA unwrap as discovery, incl. display:contents)
    let contentRoot = document.body;
    for (let i = 0; i < 8; i++) {
        const visible = [];
        let displayContentsWrap = null;
        for (const child of contentRoot.children) {
            const tag = (child.tagName || '').toLowerCase();
            if (['script','style','noscript','link','meta'].includes(tag)) continue;
            const cs = getComputedStyle(child);
            if (cs.display === 'contents') {
                displayContentsWrap = child;
                continue;
            }
            const r = child.getBoundingClientRect();
            if (r.width < 10 || r.height < 10) continue;
            visible.push(child);
        }
        if (visible.length === 0 && displayContentsWrap) {
            contentRoot = displayContentsWrap;
            continue;
        }
        if (visible.length === 1 && ['div','main','section'].includes(visible[0].tagName.toLowerCase())) {
            contentRoot = visible[0];
            continue;
        }
        break;
    }

    const el = selector.startsWith(':scope')
        ? contentRoot.querySelector(selector)
        : document.querySelector(selector);
    if (!el) return JSON.stringify({error: 'Not found: ' + selector});

    const PROPS = [
        // Typography
        'fontSize','fontWeight','fontFamily','fontStyle','fontVariant','fontStretch',
        'lineHeight','letterSpacing','wordSpacing',
        'color','textAlign','textIndent','textTransform','textDecoration',
        'textDecorationLine','textDecorationStyle','textDecorationColor','textShadow',
        'whiteSpace','textOverflow','wordBreak','wordWrap','overflowWrap','direction',
        // Background
        'backgroundColor','background','backgroundImage','backgroundSize',
        'backgroundPosition','backgroundPositionX','backgroundPositionY',
        'backgroundRepeat','backgroundAttachment','backgroundClip','backgroundOrigin',
        // Box model
        'padding','paddingTop','paddingRight','paddingBottom','paddingLeft',
        'margin','marginTop','marginRight','marginBottom','marginLeft',
        'width','height','maxWidth','minWidth','maxHeight','minHeight','aspectRatio',
        'boxSizing',
        // Layout
        'display','flexDirection','justifyContent','justifyItems','justifySelf',
        'alignItems','alignSelf','alignContent','placeContent','placeItems','placeSelf',
        'gap','rowGap','columnGap','flex','flexWrap','flexGrow','flexShrink','flexBasis',
        'order',
        'gridTemplateColumns','gridTemplateRows','gridTemplateAreas',
        'gridColumn','gridRow','gridArea','gridAutoColumns','gridAutoRows','gridAutoFlow',
        'columnCount','columnGap','columnWidth','columns','columnRule',
        // Border / outline
        'borderRadius','borderTopLeftRadius','borderTopRightRadius',
        'borderBottomLeftRadius','borderBottomRightRadius',
        'borderTopWidth','borderTopStyle','borderTopColor',
        'borderRightWidth','borderRightStyle','borderRightColor',
        'borderBottomWidth','borderBottomStyle','borderBottomColor',
        'borderLeftWidth','borderLeftStyle','borderLeftColor',
        'borderWidth','borderStyle','borderColor',
        'outline','outlineColor','outlineStyle','outlineWidth','outlineOffset',
        // Effects
        'boxShadow','opacity','filter','backdropFilter','mixBlendMode','isolation',
        'clipPath','mask',
        // Overflow / visibility
        'overflow','overflowX','overflowY','overflowWrap','visibility','pointerEvents',
        'userSelect',
        // Positioning
        'position','top','right','bottom','left','zIndex','inset',
        // Transform / Animation / Transition
        'transform','transformOrigin','transformStyle','perspective','perspectiveOrigin',
        'backfaceVisibility','willChange',
        'transition','transitionProperty','transitionDuration','transitionTimingFunction',
        'transitionDelay',
        'animation','animationName','animationDuration','animationTimingFunction',
        'animationDelay','animationIterationCount','animationDirection',
        'animationFillMode','animationPlayState',
        // Object / image
        'objectFit','objectPosition','imageRendering',
        // Misc
        'cursor','listStyle','listStyleType','listStylePosition','listStyleImage',
        'scrollBehavior','scrollSnapType','scrollSnapAlign','resize','caretColor',
        'accentColor',
    ];

    const DEFAULTS = new Set([
        'rgba(0, 0, 0, 0)', 'none', 'normal', 'auto', '0px', 'static',
        'visible', '1', 'row', 'nowrap', 'start', 'stretch', 'baseline',
        '0 1 auto', '0', 'clip', 'fill', 'border-box', 'repeat',
        'scroll', 'content-box',
    ]);

    // Properties whose "default" value is meaningful enough that it needs
    // to be explicitly recorded on responsive viewports (so a desktop
    // position:absolute can be reset to position:static on mobile).
    // We stash these in a separate object that `_diff_styles` can consult.
    const RESET_CANDIDATES = new Set(['position', 'display', 'flexDirection']);

    function extractStyles(element) {
        const cs = getComputedStyle(element);
        const styles = {};
        const defaults = {};
        for (const p of PROPS) {
            let v;
            try { v = cs[p]; } catch(e) { continue; }
            if (!v) continue;
            // Capture "default" values for reset-candidates so diffs can undo overrides
            if (DEFAULTS.has(v)) {
                if (RESET_CANDIDATES.has(p)) defaults[p] = v;
                continue;
            }
            // Skip full-viewport widths (computed px > 1400)
            if (p === 'width' && v.includes('px') && parseFloat(v) > 1400) continue;
            // Cap excessive heights (scroll-computed)
            if ((p === 'height' || p === 'minHeight') && v.includes('px') && parseFloat(v) > 1200) continue;
            styles[p] = v;
        }
        // Stash defaults so the Python-side diff can see "became default" transitions
        if (Object.keys(defaults).length > 0) styles.__defaults = defaults;
        return styles;
    }

    function extractPseudoContent(element, pseudo) {
        try {
            const ps = getComputedStyle(element, pseudo);
            let c = ps.content;
            if (!c || c === 'none' || c === 'normal') return null;
            if (c.startsWith('"') || c.startsWith("'")) {
                c = c.slice(1, -1);
            } else if (c.startsWith('url(')) {
                const m = c.match(/url\\((['"]?)(.*?)\\1\\)/);
                if (!m) return null;
                return {type: 'image', src: m[2], styles: {
                    width: ps.width, height: ps.height,
                    margin: ps.margin, display: ps.display,
                }};
            } else if (c.startsWith('counter') || c.startsWith('attr')) {
                return null;
            }
            if (!c.trim()) return null;
            const psStyles = {};
            for (const p of ['color','fontSize','fontWeight','fontFamily','fontStyle',
                             'marginLeft','marginRight','marginTop','marginBottom',
                             'paddingLeft','paddingRight','paddingTop','paddingBottom',
                             'backgroundColor','backgroundImage','width','height',
                             'display','borderRadius','textTransform','letterSpacing',
                             'lineHeight','verticalAlign']) {
                let v;
                try { v = ps[p]; } catch(e) { continue; }
                if (!v || DEFAULTS.has(v)) continue;
                psStyles[p] = v;
            }
            return {type: 'text', content: c, styles: psStyles};
        } catch(e) { return null; }
    }

    function walk(element, depth) {
        if (depth > 8) return null;
        const tag = (element.tagName || '').toLowerCase();
        if (['script','style','noscript','link','meta'].includes(tag)) return null;
        const rect = element.getBoundingClientRect();
        if (rect.width < 2 && rect.height < 2) return null;
        if (rect.top > 15000) return null;

        // SVG → serialize as data URI
        if (tag === 'svg') {
            try {
                const svgStr = new XMLSerializer().serializeToString(element);
                const svgB64 = btoa(unescape(encodeURIComponent(svgStr)));
                return {
                    tag: 'img', id: element.id || '', styles: {},
                    src: 'data:image/svg+xml;base64,' + svgB64,
                    alt: element.getAttribute('aria-label') || 'icon',
                    rect: {x: Math.round(rect.x), y: Math.round(rect.y),
                           w: Math.round(rect.width), h: Math.round(rect.height)},
                    text: null, href: '', children: []
                };
            } catch(e) { return null; }
        }

        const styles = extractStyles(element);
        const children = [...element.children];

        // Detect row layout — use raw computed values, not filtered styles
        // (flexDirection:'row' is a default and gets filtered out of styles)
        const cs = getComputedStyle(element);

        // NOTE: margin-auto heuristic removed — it caused visible misalignment
        // (elements pushed to one end) on real sites. Browser getComputedStyle
        // resolves `auto` margins to pixels, so we can't reliably reverse-engineer
        // centering without false positives on elements whose equal left/right
        // margins were intentional, or elements inside flex/grid parents that
        // already handle centering via justify-content.
        const rawDisplay = cs.display;
        const rawFlexDir = cs.flexDirection;
        const rawGridCols = cs.gridTemplateColumns;
        const isRow = ((rawDisplay === 'flex' || rawDisplay === 'inline-flex') && rawFlexDir === 'row') ||
                      ((rawDisplay === 'grid' || rawDisplay === 'inline-grid') && rawGridCols && rawGridCols !== 'none');
        if (isRow) styles._isRow = true;

        // Bounding-box row detection fallback
        if (!isRow && children.length >= 2) {
            const r1 = children[0].getBoundingClientRect();
            const r2 = children[1].getBoundingClientRect();
            if (Math.abs(r1.top - r2.top) < 20 && r1.width > 10 && r2.width > 10) {
                styles._isRow = true;
            }
        }

        // Direct text content (only if this element has text nodes directly)
        let text = null;
        for (const node of element.childNodes) {
            if (node.nodeType === 3) {
                const t = node.textContent.trim();
                if (t) { text = (text || '') + (text ? ' ' : '') + t; }
            }
        }
        if (text) text = text.substring(0, 500);

        const data = {
            tag, id: element.id || '',
            styles,
            rect: {x: Math.round(rect.x), y: Math.round(rect.y),
                   w: Math.round(rect.width), h: Math.round(rect.height)},
            text,
            src: null, alt: '', href: '', placeholder: '',
            children: [],
        };

        // ::before / ::after pseudo-element content (icons, quote marks, decorators)
        const pBefore = extractPseudoContent(element, '::before');
        const pAfter = extractPseudoContent(element, '::after');
        if (pBefore) data.pseudoBefore = pBefore;
        if (pAfter) data.pseudoAfter = pAfter;

        // UL / OL — capture list marker style so builder can inject glyphs
        if (tag === 'ul' || tag === 'ol') {
            const lst = cs.listStyleType;
            if (lst && lst !== 'none') data.listStyleType = lst;
        }

        // Element-specific data
        if (tag === 'img') {
            data.src = element.src || element.dataset?.src || element.dataset?.lazy || '';
            data.alt = element.alt || '';
        }
        if (tag === 'picture') {
            const source = element.querySelector('source');
            const img = element.querySelector('img');
            data.src = (source?.srcset?.split(',')[0]?.trim()?.split(' ')[0]) ||
                       (img?.src || img?.dataset?.src || '');
            data.alt = img?.alt || '';
            data.tag = 'img';
        }
        if (tag === 'a') data.href = element.href || '';
        if (tag === 'iframe') data.src = element.src || element.dataset?.src || '';
        if (tag === 'video') {
            data.poster = element.poster || '';
            const source = element.querySelector('source');
            data.videoSrc = (source && source.src) || element.src || '';
            data.autoplay = !!element.autoplay;
            data.loop = !!element.loop;
            data.muted = !!element.muted;
            data.controls = !!element.controls;
        }
        if (tag === 'input' || tag === 'textarea') {
            data.placeholder = element.placeholder || '';
        }

        // Recurse children
        for (let i = 0; i < children.length && i < 60; i++) {
            const child = walk(children[i], depth + 1);
            if (child) data.children.push(child);
        }

        return data;
    }

    return JSON.stringify(walk(el, 0));
}"""


# ── JS: Asset Discovery (from ai-website-cloner-template SKILL.md) ──

_DISCOVER_ASSETS_JS = """() => {
    return JSON.stringify({
        images: [...document.querySelectorAll('img')].map(img => {
            const rect = img.getBoundingClientRect();
            return {
                src: img.src || img.currentSrc || img.dataset?.src || '',
                alt: img.alt || '',
                width: img.naturalWidth || Math.round(rect.width),
                height: img.naturalHeight || Math.round(rect.height),
                y: Math.round(rect.y),
            };
        }).filter(i => i.src && !i.src.startsWith('data:')),
        videos: [...document.querySelectorAll('video')].map(v => ({
            src: v.src || (v.querySelector('source') || {}).src || '',
            poster: v.poster || '',
            autoplay: v.autoplay, loop: v.loop, muted: v.muted,
        })).filter(v => v.src || v.poster),
        backgroundImages: [...document.querySelectorAll('*')].filter(el => {
            const bg = getComputedStyle(el).backgroundImage;
            return bg && bg !== 'none' && !bg.startsWith('linear-gradient') && !bg.startsWith('radial-gradient');
        }).slice(0, 20).map(el => ({
            url: getComputedStyle(el).backgroundImage,
            tag: el.tagName.toLowerCase(),
            y: Math.round(el.getBoundingClientRect().y),
        })),
        iframes: [...document.querySelectorAll('iframe')].map(f => ({
            src: f.src || '', y: Math.round(f.getBoundingClientRect().y),
            w: Math.round(f.getBoundingClientRect().width),
            h: Math.round(f.getBoundingClientRect().height),
        })).filter(f => f.src),
        fonts: [...new Set(
            [...document.querySelectorAll('*')].slice(0, 200)
            .map(el => getComputedStyle(el).fontFamily)
        )],
    });
}"""


# ── JS: Pseudo-State Extraction from Stylesheets ──

_EXTRACT_PSEUDO_JS = """() => {
    const PSEUDO_RE = /:(hover|focus|active|disabled|visited)(?![\\w-])/;
    const PROPS = [
        'color','backgroundColor','backgroundImage','borderColor',
        'boxShadow','textDecoration','textDecorationLine',
        'opacity','transform','cursor','outline',
        'fontSize','fontWeight','letterSpacing',
        'paddingTop','paddingRight','paddingBottom','paddingLeft',
    ];

    function kebabToCamel(s) { return s.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }
    function camelToKebab(s) { return s.replace(/([A-Z])/g, '-$1').toLowerCase(); }

    const pseudoMap = {};
    try {
        for (const sheet of document.styleSheets) {
            let rules;
            try { rules = sheet.cssRules || sheet.rules; } catch(e) { continue; }
            if (!rules) continue;

            for (const rule of rules) {
                if (rule.type !== 1) continue;
                const sel = rule.selectorText || '';
                const match = sel.match(PSEUDO_RE);
                if (!match) continue;

                const pseudoState = match[1];
                const baseSel = sel.replace(PSEUDO_RE, '').replace(/:+$/, '').trim();
                if (!baseSel) continue;

                let matchedEls;
                try { matchedEls = document.querySelectorAll(baseSel); } catch(e) { continue; }
                if (!matchedEls.length) continue;

                const styles = {};
                for (const prop of PROPS) {
                    const val = rule.style.getPropertyValue(camelToKebab(prop));
                    if (val && val !== 'initial' && val !== 'inherit' && val !== 'unset') {
                        styles[kebabToCamel(prop)] = val.trim();
                    }
                }
                if (Object.keys(styles).length === 0) continue;

                for (const el of matchedEls) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 1 && rect.height < 1) continue;
                    if (rect.top > 5000) continue;

                    const path = [];
                    let node = el;
                    while (node && node !== document.body && node.parentElement) {
                        const parent = node.parentElement;
                        const idx = Array.from(parent.children).indexOf(node);
                        path.unshift(idx);
                        node = parent;
                    }
                    const pathKey = path.join('.');

                    if (!pseudoMap[pathKey]) pseudoMap[pathKey] = {pseudoStyles: {}};
                    if (!pseudoMap[pathKey].pseudoStyles[pseudoState]) {
                        pseudoMap[pathKey].pseudoStyles[pseudoState] = {};
                    }
                    Object.assign(pseudoMap[pathKey].pseudoStyles[pseudoState], styles);
                }
            }
        }
    } catch(e) {}
    return pseudoMap;
}"""


_EXTRACT_KEYFRAMES_JS = """() => {
    // Collect all @keyframes rules from the document's stylesheets.
    // Returns a list of {name, body} where body is the "0% {...} 100% {...}" block
    // (without the outer braces — matching Modlix page.properties.classes format).
    const out = [];
    try {
        for (const sheet of document.styleSheets) {
            let rules;
            try { rules = sheet.cssRules || sheet.rules; } catch(e) { continue; }
            if (!rules) continue;
            for (const rule of rules) {
                // CSSKeyframesRule: type === 7 (@keyframes)
                if (rule.type !== 7 && !(rule.cssRules && rule.name)) continue;
                const name = rule.name;
                if (!name) continue;
                // Build the body: "0% { opacity: 0; } 100% { opacity: 1; } ..."
                const parts = [];
                for (const kf of rule.cssRules) {
                    const txt = kf.style && kf.style.cssText;
                    if (txt) parts.push(kf.keyText + ' { ' + txt + ' }');
                }
                const body = parts.join(' ');
                if (body) out.push({ name, body });
            }
        }
    } catch(e) {}
    // Dedupe by name (later rules win)
    const byName = {};
    for (const k of out) byName[k.name] = k;
    return Object.values(byName);
}"""


async def extract_page(url: str) -> PageExtraction:
    """Run the full extraction pipeline on a URL.

    Returns a PageExtraction with all sections fully specified.
    """
    from playwright.async_api import async_playwright

    logger.info("Extraction: starting for %s", url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORTS["desktop"])
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            # Fallback for slow sites — domcontentloaded is enough for most content
            logger.info("networkidle timed out, retrying with domcontentloaded")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(4)  # Extra wait for JS rendering on heavy SPAs

        # ── Full-page screenshot ──
        full_screenshot = await page.screenshot(full_page=True)
        full_screenshot_b64 = base64.b64encode(full_screenshot).decode("ascii")
        logger.info("Full screenshot: %d bytes", len(full_screenshot))

        # ── Body styles ──
        body_styles = await page.evaluate("""() => {
            const cs = getComputedStyle(document.body);
            return {
                fontFamily: cs.fontFamily,
                fontSize: cs.fontSize,
                color: cs.color,
                backgroundColor: cs.backgroundColor,
            };
        }""")
        logger.info("Body: font=%s, color=%s", body_styles.get("fontFamily", "")[:40], body_styles.get("color", ""))

        # ── Section discovery ──
        raw_sections = await page.evaluate(_DISCOVER_SECTIONS_JS)
        logger.info("Discovered %d sections", len(raw_sections))

        # ── Per-section extraction at desktop ──
        sections: list[SectionSpec] = []
        for i, sec_info in enumerate(raw_sections):
            name = _section_name(sec_info, i)
            spec = SectionSpec(
                name=name,
                selector=sec_info["selector"],
                index=i,
                tag=sec_info["tag"],
                bg_color=sec_info.get("bgColor", ""),
                bg_image=sec_info.get("bgImage", ""),
                height=sec_info.get("h", 0),
                width=sec_info.get("w", 0),
                is_row=sec_info.get("isRow", False),
            )

            # Deep extraction
            try:
                raw_tree = await page.evaluate(_EXTRACT_SECTION_JS, sec_info["selector"])
                if isinstance(raw_tree, str):
                    import json
                    spec.element_tree = json.loads(raw_tree)
                else:
                    spec.element_tree = raw_tree
                if spec.element_tree.get("error"):
                    logger.warning("Section %s extraction error: %s", name, spec.element_tree["error"])
                    continue
            except Exception as e:
                logger.warning("Section %s extraction failed: %s", name, e)
                continue

            # Per-section screenshot (scroll to section, capture viewport)
            try:
                await page.evaluate(f"window.scrollTo(0, {sec_info['y']})")
                await asyncio.sleep(0.3)
                sec_screenshot = await page.screenshot()
                spec.screenshot_b64 = base64.b64encode(sec_screenshot).decode("ascii")
            except Exception:
                pass

            # Collect text, images, links, iframes from the tree
            _collect_content(spec.element_tree, spec)

            sections.append(spec)
            logger.info("  Section %d/%d: '%s' (%s) h=%d children=%d texts=%d images=%d",
                         i + 1, len(raw_sections), name, sec_info["tag"],
                         sec_info["h"], sec_info.get("childCount", 0),
                         len(spec.content_text), len(spec.images))

        # ── Asset discovery ──
        try:
            raw_assets = await page.evaluate(_DISCOVER_ASSETS_JS)
            import json
            assets = json.loads(raw_assets) if isinstance(raw_assets, str) else raw_assets
        except Exception as e:
            logger.warning("Asset discovery failed: %s", e)
            assets = {}

        # ── Pseudo-state extraction ──
        pseudo_map = await page.evaluate(_EXTRACT_PSEUDO_JS)
        logger.info("Pseudo styles: %d elements", len(pseudo_map))

        # ── @keyframes extraction ──
        try:
            keyframes = await page.evaluate(_EXTRACT_KEYFRAMES_JS) or []
            logger.info("Keyframes: %d @keyframes rules", len(keyframes))
        except Exception as e:
            logger.warning("Keyframes extraction failed: %s", e)
            keyframes = []

        # ── Responsive extraction (tablet + mobile) ──
        for vp_name in ["tablet", "mobile"]:
            vp = VIEWPORTS[vp_name]
            await page.set_viewport_size(vp)
            await asyncio.sleep(0.5)

            for spec in sections:
                try:
                    raw = await page.evaluate(_EXTRACT_SECTION_JS, spec.selector)
                    import json
                    vp_tree = json.loads(raw) if isinstance(raw, str) else raw
                    if not vp_tree.get("error"):
                        # Diff against desktop tree
                        diffs = _diff_styles(spec.element_tree, vp_tree)
                        if diffs:
                            spec.responsive_diffs[vp_name] = diffs
                except Exception:
                    pass

            logger.info("Responsive extraction (%s): %d sections with diffs",
                         vp_name, sum(1 for s in sections if vp_name in s.responsive_diffs))

        await browser.close()

    # Build all_fonts list
    all_fonts = set()
    body_font = body_styles.get("fontFamily", "")
    primary = body_font.split(",")[0].strip().strip("'\"") if body_font else ""
    if primary:
        all_fonts.add(primary)
    for spec in sections:
        _collect_fonts(spec.element_tree, all_fonts)

    result = PageExtraction(
        url=url,
        sections=sections,
        full_screenshot_b64=full_screenshot_b64,
        body_font=primary,
        body_color=body_styles.get("color", ""),
        body_bg=body_styles.get("backgroundColor", ""),
        all_fonts=sorted(all_fonts),
        assets=assets,
        keyframes=keyframes,
    )

    logger.info("Extraction complete: %d sections, %d fonts, %d images",
                 len(sections), len(all_fonts), len(assets.get("images", [])))

    return result


def _section_name(sec_info: dict, index: int) -> str:
    """Generate a readable name for a section."""
    tag = sec_info.get("tag", "div")
    eid = sec_info.get("id", "")
    if eid:
        return re.sub(r"[^a-zA-Z0-9]", "_", eid)[:30]

    tag_names = {
        "header": "header", "footer": "footer", "nav": "nav",
        "main": "main", "article": "article", "aside": "sidebar",
    }
    base = tag_names.get(tag, f"section{index}")
    return base


def _collect_content(tree: dict, spec: SectionSpec) -> None:
    """Walk element tree and collect text, images, links, iframes into spec."""
    if not tree:
        return

    text = tree.get("text")
    if text and len(text.strip()) > 2:
        spec.content_text.append({
            "text": text.strip(),
            "y": tree.get("rect", {}).get("y", 0),
        })

    src = tree.get("src")
    tag = tree.get("tag", "")
    if tag == "img" and src:
        spec.images.append({
            "src": src,
            "alt": tree.get("alt", ""),
            "w": tree.get("rect", {}).get("w", 0),
            "h": tree.get("rect", {}).get("h", 0),
        })
    elif tag == "iframe" and src:
        spec.iframes.append({
            "src": src,
            "w": tree.get("rect", {}).get("w", 0),
            "h": tree.get("rect", {}).get("h", 0),
        })

    href = tree.get("href", "")
    if href and text:
        spec.links.append({"href": href, "text": text.strip()[:100]})

    for child in tree.get("children", []):
        _collect_content(child, spec)


def _collect_fonts(tree: dict, fonts: set) -> None:
    """Collect all font families from a tree."""
    if not tree:
        return
    ff = tree.get("styles", {}).get("fontFamily", "")
    if ff:
        primary = ff.split(",")[0].strip().strip("'\"")
        if primary and len(primary) > 1:
            fonts.add(primary)
    for child in tree.get("children", []):
        _collect_fonts(child, fonts)


def _diff_styles(desktop_tree: dict, vp_tree: dict) -> dict:
    """Diff styles between desktop and viewport trees. Returns changed properties."""
    if not desktop_tree or not vp_tree:
        return {}

    diffs = {}
    desk_styles = desktop_tree.get("styles", {})
    vp_styles = vp_tree.get("styles", {})

    for prop, val in vp_styles.items():
        if prop.startswith("_"):
            continue
        desk_val = desk_styles.get(prop, "")
        if val != desk_val and val:
            diffs[prop] = val

    # Reset overrides — desktop has a non-default value (e.g. position:absolute)
    # but viewport falls back to default (e.g. position:static). Without this,
    # the mobile rendering inherits the desktop absolute positioning and
    # breaks responsive layout (common cause of mobile score gap).
    vp_defaults = vp_styles.get("__defaults") or {}
    for prop, default_val in vp_defaults.items():
        if prop in vp_styles:
            continue  # vp has explicit value, already diffed
        desk_val = desk_styles.get(prop, "")
        if desk_val and desk_val != default_val:
            diffs[prop] = default_val

    # Recurse into children (match by index)
    desk_children = desktop_tree.get("children", [])
    vp_children = vp_tree.get("children", [])
    for i, vp_child in enumerate(vp_children):
        if i < len(desk_children):
            child_diffs = _diff_styles(desk_children[i], vp_child)
            if child_diffs:
                diffs[f"child_{i}"] = child_diffs

    return diffs
