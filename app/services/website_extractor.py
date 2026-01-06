"""
Website Extractor Service - Multi-viewport visual extraction using Playwright.

Extracts computed styles at multiple viewport sizes to create responsive Nocode components.
"""
import asyncio
import base64
import re
import logging
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs, unquote
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def decode_nextjs_image_url(url: str, base_url: str) -> str:
    """
    Decode Next.js image optimization URLs to get direct image paths.
    
    Next.js uses URLs like:
    /_next/image?url=%2Fimages%2Fprofile.webp&w=3840&q=75
    
    This extracts the original path and constructs the direct URL.
    """
    if '/_next/image' not in url:
        return url
    
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        
        if 'url' in params:
            # Get the original image path (URL-encoded)
            original_path = unquote(params['url'][0])
            
            # Construct full URL from base
            if original_path.startswith('/'):
                # Absolute path - combine with base domain
                base_parsed = urlparse(base_url)
                direct_url = f"{base_parsed.scheme}://{base_parsed.netloc}{original_path}"
                logger.info(f"Decoded Next.js URL: {url[:60]}... -> {direct_url}")
                return direct_url
            elif original_path.startswith('http'):
                # Already a full URL
                return original_path
        
        return url
    except Exception as e:
        logger.warning(f"Failed to decode Next.js URL {url}: {e}")
        return url


# Viewport configurations matching Nocode resolutions
VIEWPORTS = [
    ("desktop", 1440, 900, "ALL"),
    ("tablet", 768, 1024, "TABLET_POTRAIT_SCREEN"),
    ("mobile", 375, 812, "MOBILE_POTRAIT_SCREEN"),
]


@dataclass
class ImageInfo:
    """Information about an image found on the page"""
    url: str
    alt_text: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    is_background: bool = False


@dataclass
class VisualElement:
    """
    An element extracted with its computed styles at each viewport.
    
    Styles are stored per viewport and later merged into Nocode resolution format.
    """
    id: str
    tag: str
    text: str = ""
    image_url: str = ""
    
    # Computed styles per viewport: {"desktop": {...}, "tablet": {...}, "mobile": {...}}
    styles: Dict[str, Dict[str, str]] = field(default_factory=dict)
    
    # Bounding box per viewport: {"desktop": {x, y, width, height}, ...}
    bounds: Dict[str, Dict[str, float]] = field(default_factory=dict)
    
    # Child elements
    children: List['VisualElement'] = field(default_factory=list)
    
    # Additional attributes (href, src, etc.)
    attributes: Dict[str, str] = field(default_factory=dict)


@dataclass
class VisualData:
    """Complete visual extraction data from a website"""
    url: str
    title: str = ""
    
    # Screenshot at desktop viewport (base64)
    screenshot: str = ""
    
    # Root element with all children
    elements: List[VisualElement] = field(default_factory=list)
    
    # All images found (for uploading)
    images: List[ImageInfo] = field(default_factory=list)
    
    # Root styles at each viewport
    root_styles: Dict[str, Dict[str, str]] = field(default_factory=dict)


class WebsiteExtractor:
    """
    Extracts visual data from websites using Playwright.
    
    Renders the page at multiple viewport sizes and extracts computed styles
    for each visible element, enabling accurate responsive layout recreation.
    """
    
    def __init__(
        self,
        screenshot_timeout: int = 60
    ):
        self.screenshot_timeout = screenshot_timeout
        self._playwright = None
        self._browser = None
    
    async def extract(self, url: str) -> VisualData:
        """
        Extract visual data from a URL at multiple viewport sizes.
        
        Args:
            url: The website URL to extract
            
        Returns:
            VisualData with elements, styles, and images
        """
        logger.info(f"Starting multi-viewport extraction for {url}")
        
        try:
            from playwright.async_api import async_playwright
            
            if not self._playwright:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)
            
            page = await self._browser.new_page()
            
            try:
                # Navigate to the page
                await page.goto(
                    url,
                    wait_until='networkidle',
                    timeout=self.screenshot_timeout * 1000
                )
                
                # Wait for animations to settle
                await asyncio.sleep(1)
                
                # Get page title
                title = await page.title()
                
                # Extract data at each viewport
                viewport_data = {}
                screenshot = ""
                
                for viewport_name, width, height, _ in VIEWPORTS:
                    await page.set_viewport_size({"width": width, "height": height})
                    await asyncio.sleep(0.5)  # Let layout settle
                    
                    # Take screenshot only at desktop size
                    if viewport_name == "desktop":
                        # Scroll to bottom and wait to trigger lazy loading
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(3)  # Wait for lazy-loaded content
                        # Scroll back to top
                        await page.evaluate("window.scrollTo(0, 0)")
                        await asyncio.sleep(0.5)  # Let layout settle after scroll

                        screenshot_bytes = await page.screenshot(full_page=True, type='png')
                        screenshot = base64.b64encode(screenshot_bytes).decode('utf-8')
                        logger.info(f"Screenshot captured ({len(screenshot_bytes)} bytes)")
                    
                    # Extract all visible elements with computed styles
                    elements_data = await self._extract_elements(page)
                    viewport_data[viewport_name] = elements_data
                    
                    logger.info(f"Extracted {len(elements_data.get('elements', []))} elements at {viewport_name} ({width}x{height})")
                
                # Merge viewport data into VisualElements
                elements = self._merge_viewport_data(viewport_data, url)
                
                # Extract root styles
                root_styles = self._extract_root_styles(viewport_data)
                
                # Extract all images
                images = self._extract_images(viewport_data.get("desktop", {}), url)
                
                logger.info(f"Extraction complete: {len(elements)} elements, {len(images)} images")
                
                return VisualData(
                    url=url,
                    title=title,
                    screenshot=screenshot,
                    elements=elements,
                    images=images,
                    root_styles=root_styles
                )
                
            finally:
                await page.close()
                
        except ImportError:
            logger.error("Playwright not installed. Run 'playwright install chromium'")
            raise ValueError("Playwright not available for website extraction")
        except Exception as e:
            logger.error(f"Extraction failed: {e}")
            raise ValueError(f"Failed to extract website: {str(e)}")
    
    async def _extract_elements(self, page) -> Dict[str, Any]:
        """Extract all visible elements with their SPECIFIED styles (not computed) from the current viewport."""
        return await page.evaluate('''() => {
            const viewportWidth = window.innerWidth;
            const viewportHeight = window.innerHeight;
            const pageHeight = document.documentElement.scrollHeight;

            // Track seen text content to avoid duplicates
            const seenTextContent = new Set();

            // Minimal visibility filtering - only skip display:none elements
            const isVisible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;

                const style = window.getComputedStyle(el);

                // Only skip elements with display:none
                if (style.display === 'none') return false;

                return true;
            };

            // Important CSS properties that affect visual appearance
            // INDIVIDUAL PROPERTIES ONLY - no shorthands (margin, padding, border, background, font, etc.)
            const IMPORTANT_PROPERTIES = new Set([
                // Layout
                'display', 'position', 'top', 'right', 'bottom', 'left', 'z-index',
                'float', 'clear', 'overflow', 'overflow-x', 'overflow-y',

                // Flexbox - individual only
                'flex-direction', 'flex-wrap', 'flex-grow', 'flex-shrink', 'flex-basis',
                'justify-content', 'align-items', 'align-self', 'align-content', 'order', 'gap', 'row-gap', 'column-gap',

                // Grid - individual only
                'grid-template-columns', 'grid-template-rows', 'grid-template-areas',
                'grid-column-start', 'grid-column-end', 'grid-row-start', 'grid-row-end',
                'grid-auto-flow', 'grid-auto-columns', 'grid-auto-rows',

                // Box model - INDIVIDUAL ONLY
                'width', 'height', 'min-width', 'max-width', 'min-height', 'max-height',
                'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
                'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
                'box-sizing',

                // Border - INDIVIDUAL ONLY
                'border-top-width', 'border-right-width', 'border-bottom-width', 'border-left-width',
                'border-top-style', 'border-right-style', 'border-bottom-style', 'border-left-style',
                'border-top-color', 'border-right-color', 'border-bottom-color', 'border-left-color',
                'border-top-left-radius', 'border-top-right-radius',
                'border-bottom-left-radius', 'border-bottom-right-radius',

                // Background - INDIVIDUAL ONLY
                'background-color', 'background-image', 'background-size',
                'background-position', 'background-repeat', 'background-attachment',
                'background-clip', 'background-origin',

                // Typography - INDIVIDUAL ONLY
                'color', 'font-family', 'font-size', 'font-weight', 'font-style',
                'line-height', 'letter-spacing', 'text-align', 'text-decoration', 'text-transform',
                'white-space', 'word-spacing', 'word-break', 'text-overflow',
                'vertical-align', 'text-shadow',

                // Visual effects
                'opacity', 'visibility', 'box-shadow', 'filter', 'backdrop-filter',
                'transform',

                // Object/image
                'object-fit', 'object-position',

                // Cursor and interaction
                'cursor', 'pointer-events', 'user-select',

                // Lists - INDIVIDUAL ONLY
                'list-style-type', 'list-style-position', 'list-style-image'
            ]);

            // Check if a value is valid (not a CSS variable, not empty, not framework-specific)
            const isValidCssValue = (prop, value) => {
                if (!value || value === '') return false;
                if (value === 'initial' || value === 'inherit' || value === 'unset') return false;

                // Skip CSS variable references - use computed value instead
                if (value.includes('var(')) return false;

                // Skip Tailwind and other framework-specific custom properties
                if (prop.startsWith('--tw-') || prop.startsWith('-tw-')) return false;
                if (prop.startsWith('--')) return false;  // All CSS custom properties

                return true;
            };

            // Check if a property is important (not framework-specific)
            const isImportantProperty = (prop) => {
                // Skip Tailwind custom properties
                if (prop.startsWith('--tw-') || prop.startsWith('-tw-') ||
                    prop.startsWith('-Tw') || prop.startsWith('--')) return false;
                // Skip vendor prefixes
                if (prop.startsWith('-webkit-') || prop.startsWith('-moz-') ||
                    prop.startsWith('-ms-') || prop.startsWith('-o-')) return false;
                return true;
            };

            // Get only SPECIFIED styles (inline + CSS rules), using COMPUTED values
            // This handles !important overrides correctly
            const getSpecifiedStyles = (el) => {
                const styles = {};
                const computed = window.getComputedStyle(el);
                const specifiedProps = new Set();

                // Method 1: Track which properties are specified in inline styles
                for (let i = 0; i < el.style.length; i++) {
                    const prop = el.style[i];
                    if (!isImportantProperty(prop)) continue;
                    specifiedProps.add(prop);
                }

                // Method 2: Track which properties are specified in CSS rules
                try {
                    const sheets = document.styleSheets;
                    for (let i = 0; i < sheets.length; i++) {
                        try {
                            const rules = sheets[i].cssRules || sheets[i].rules;
                            if (!rules) continue;

                            for (let j = 0; j < rules.length; j++) {
                                const rule = rules[j];
                                if (rule.type !== CSSRule.STYLE_RULE) continue;

                                try {
                                    if (el.matches(rule.selectorText)) {
                                        const ruleStyle = rule.style;
                                        for (let k = 0; k < ruleStyle.length; k++) {
                                            const prop = ruleStyle[k];
                                            if (!isImportantProperty(prop)) continue;
                                            specifiedProps.add(prop);
                                        }
                                    }
                                } catch (selectorErr) {
                                    // Invalid selector, skip
                                }
                            }
                        } catch (sheetErr) {
                            // Cross-origin stylesheet, skip
                        }
                    }
                } catch (e) {
                    // Fallback if CSS rules access fails
                }

                // Pre-check: Get border and outline widths to skip related properties if 0
                const borderWidths = {
                    top: computed.getPropertyValue('border-top-width'),
                    right: computed.getPropertyValue('border-right-width'),
                    bottom: computed.getPropertyValue('border-bottom-width'),
                    left: computed.getPropertyValue('border-left-width')
                };
                const outlineWidth = computed.getPropertyValue('outline-width');

                // Helper to check if border/outline property should be skipped
                const shouldSkipBorderOutline = (prop) => {
                    // Skip border properties if border width is 0 for that side
                    if (prop.startsWith('border-') && !prop.includes('radius')) {
                        if (prop.startsWith('border-image')) {
                            // Skip border-image if ALL borders are 0
                            return Object.values(borderWidths).every(w => w === '0px' || w === '0');
                        }
                        // Get the side
                        let side = '';
                        if (prop.includes('-top')) side = 'top';
                        else if (prop.includes('-right')) side = 'right';
                        else if (prop.includes('-bottom')) side = 'bottom';
                        else if (prop.includes('-left')) side = 'left';
                        if (side && (borderWidths[side] === '0px' || borderWidths[side] === '0')) {
                            return true;
                        }
                    }
                    // Skip outline properties if outline width is 0
                    if (prop.startsWith('outline') && (outlineWidth === '0px' || outlineWidth === '0')) {
                        return true;
                    }
                    return false;
                };

                // Method 3: For all specified properties, get the COMPUTED value
                // This correctly handles !important overrides and CSS variable resolution
                // CRITICAL: Always use getComputedStyle value, NOT the inline or CSS rule value
                for (const prop of specifiedProps) {
                    // Skip border/outline properties when width is 0
                    if (shouldSkipBorderOutline(prop)) continue;

                    // Get the COMPUTED value - this resolves !important, var(), and cascade
                    const computedValue = computed.getPropertyValue(prop);

                    if (isValidCssValue(prop, computedValue)) {
                        const camelProp = prop.replace(/-([a-z])/g, (g) => g[1].toUpperCase());
                        // Store the COMPUTED value, not the specified value
                        styles[camelProp] = computedValue;
                    }
                }

                // Method 3b: For critical layout properties, ALWAYS get the computed value
                // This ensures flex/grid layouts work correctly even when CSS rules aren't accessible
                const LAYOUT_CRITICAL_PROPS = new Set([
                    'display', 'position', 'flex-direction', 'flex-wrap', 'justify-content',
                    'align-items', 'align-content', 'align-self', 'gap', 'row-gap', 'column-gap',
                    'flex', 'flex-grow', 'flex-shrink', 'flex-basis', 'order',
                    'grid-template-columns', 'grid-template-rows', 'grid-column', 'grid-row',
                    'width', 'height', 'min-width', 'max-width', 'min-height', 'max-height',
                    'top', 'right', 'bottom', 'left', 'z-index'
                ]);

                // For layout-critical properties, always capture if they have meaningful values
                for (const prop of LAYOUT_CRITICAL_PROPS) {
                    const camelProp = prop.replace(/-([a-z])/g, (g) => g[1].toUpperCase());
                    if (styles[camelProp]) continue; // Already have it

                    const computedValue = computed.getPropertyValue(prop);
                    if (!computedValue || computedValue.includes('var(')) continue;

                    // For display, always capture if it's flex or grid
                    if (prop === 'display') {
                        if (computedValue === 'flex' || computedValue === 'inline-flex' ||
                            computedValue === 'grid' || computedValue === 'inline-grid' ||
                            computedValue === 'none' || computedValue === 'block' ||
                            computedValue === 'inline-block' || computedValue === 'inline') {
                            styles[camelProp] = computedValue;
                        }
                        continue;
                    }

                    // For position, capture if not static
                    if (prop === 'position') {
                        if (computedValue !== 'static') {
                            styles[camelProp] = computedValue;
                        }
                        continue;
                    }

                    // For flex/grid properties, capture if the parent is flex/grid
                    if (prop.startsWith('flex') || prop.startsWith('justify') ||
                        prop.startsWith('align') || prop === 'gap' || prop.startsWith('row-gap') ||
                        prop.startsWith('column-gap') || prop === 'order') {
                        const parentDisplay = el.parentElement ?
                            window.getComputedStyle(el.parentElement).display : '';
                        const selfDisplay = computed.display;

                        // If this element OR parent is flex/grid, capture these properties
                        if (selfDisplay === 'flex' || selfDisplay === 'inline-flex' ||
                            selfDisplay === 'grid' || selfDisplay === 'inline-grid' ||
                            parentDisplay === 'flex' || parentDisplay === 'inline-flex' ||
                            parentDisplay === 'grid' || parentDisplay === 'inline-grid') {

                            // Skip truly default/auto values that don't affect layout
                            if (computedValue === 'auto' || computedValue === 'normal' ||
                                computedValue === '0' || computedValue === '0px' ||
                                computedValue === 'none') {
                                continue;
                            }
                            styles[camelProp] = computedValue;
                        }
                        continue;
                    }

                    // For dimensions (width/height), ONLY capture if explicitly specified in CSS/inline
                    // Don't capture computed pixel values - they cause fixed layout issues
                    if (prop === 'width' || prop === 'height' ||
                        prop === 'min-width' || prop === 'max-width' ||
                        prop === 'min-height' || prop === 'max-height') {
                        // Only include if this property was already found in specifiedProps
                        // (from inline styles or CSS rules) - skip computed-only values
                        if (specifiedProps.has(prop)) {
                            if (computedValue !== 'auto' && computedValue !== 'none') {
                                styles[camelProp] = computedValue;
                            }
                        }
                        continue;
                    }

                    // For position offsets (top/right/bottom/left), capture non-auto values
                    if (prop === 'top' || prop === 'right' || prop === 'bottom' || prop === 'left') {
                        if (computedValue !== 'auto' && computedValue !== 'none' && computedValue !== '0px') {
                            styles[camelProp] = computedValue;
                        }
                        continue;
                    }

                    // For z-index, capture non-auto values
                    if (prop === 'z-index') {
                        if (computedValue !== 'auto') {
                            styles[camelProp] = computedValue;
                        }
                        continue;
                    }

                    // For grid properties, capture if element uses grid
                    if (prop.startsWith('grid')) {
                        const selfDisplay = computed.display;
                        const parentDisplay = el.parentElement ?
                            window.getComputedStyle(el.parentElement).display : '';

                        if (selfDisplay === 'grid' || selfDisplay === 'inline-grid' ||
                            parentDisplay === 'grid' || parentDisplay === 'inline-grid') {
                            if (computedValue !== 'auto' && computedValue !== 'none') {
                                styles[camelProp] = computedValue;
                            }
                        }
                    }
                }

                // Method 4: For other visual properties, check if computed differs from default
                const defaultEl = document.createElement(el.tagName);
                document.body.appendChild(defaultEl);
                const defaultComputed = window.getComputedStyle(defaultEl);

                for (const prop of IMPORTANT_PROPERTIES) {
                    if (!isImportantProperty(prop)) continue;
                    if (LAYOUT_CRITICAL_PROPS.has(prop)) continue; // Already handled above

                    const camelProp = prop.replace(/-([a-z])/g, (g) => g[1].toUpperCase());
                    if (styles[camelProp]) continue;

                    // Skip border/outline properties when width is 0
                    if (shouldSkipBorderOutline(prop)) continue;

                    const computedValue = computed.getPropertyValue(prop);
                    const defaultValue = defaultComputed.getPropertyValue(prop);

                    // If computed differs from default, this style was specified somewhere
                    if (computedValue && computedValue !== defaultValue) {
                        // Skip if it contains unresolved var()
                        if (computedValue.includes('var(')) continue;
                        // Skip truly empty/transparent values for non-layout props
                        if (computedValue === 'rgba(0, 0, 0, 0)' || computedValue === 'transparent') {
                            continue;
                        }
                        styles[camelProp] = computedValue;
                    }
                }

                defaultEl.remove();

                return styles;
            };
            
            // Get bounding rect
            const getBounds = (el) => {
                const rect = el.getBoundingClientRect();
                return {
                    x: rect.x,
                    y: rect.y,
                    width: rect.width,
                    height: rect.height
                };
            };
            
            // Generate a deterministic ID based on element's DOM path
            // This ensures the same element gets the same ID across viewports
            const generateDeterministicId = (el) => {
                if (el.id) return el.id;

                // Build a path from body to this element
                const pathParts = [];
                let current = el;
                while (current && current !== document.body && current.parentElement) {
                    const parent = current.parentElement;
                    const siblings = Array.from(parent.children);
                    const index = siblings.indexOf(current);
                    const tag = current.tagName.toLowerCase();
                    pathParts.unshift(`${tag}[${index}]`);
                    current = parent;
                }

                // Create a hash from the path
                const pathStr = pathParts.join('/');
                let hash = 0;
                for (let i = 0; i < pathStr.length; i++) {
                    const char = pathStr.charCodeAt(i);
                    hash = ((hash << 5) - hash) + char;
                    hash = hash & hash; // Convert to 32bit integer
                }

                return `elem_${Math.abs(hash).toString(36)}`;
            };

            // Extract element recursively - NO DEPTH LIMIT
            const extractElement = (el, depth = 0) => {
                if (!isVisible(el)) return null;

                const tag = el.tagName.toLowerCase();

                // Skip only truly non-content elements (keep SVG for icons)
                if (['script', 'style', 'link', 'meta', 'noscript'].includes(tag)) {
                    return null;
                }

                // Get text content
                let text = '';
                // For interactive elements and headings, get ALL inner text
                const useInnerText = ['a', 'button', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'label'].includes(tag);

                if (useInnerText) {
                    // Get ALL inner text (from all nested elements)
                    text = el.innerText || el.textContent || '';
                    text = text.trim().replace(/\s+/g, ' ').substring(0, 300);
                } else {
                    // For other elements, only get direct text nodes
                    for (const node of el.childNodes) {
                        if (node.nodeType === Node.TEXT_NODE) {
                            const t = node.textContent.trim();
                            if (t) text += t + ' ';
                        }
                    }
                    text = text.trim().substring(0, 500);
                }

                const data = {
                    id: generateDeterministicId(el),
                    tag: tag,
                    text: text,
                    styles: getSpecifiedStyles(el),
                    bounds: getBounds(el),
                    attributes: {},
                    children: []
                };
                
                // Get relevant attributes
                if (tag === 'a') {
                    data.attributes.href = el.getAttribute('href') || '';
                }
                if (tag === 'img') {
                    data.attributes.src = el.getAttribute('src') || '';
                    data.attributes.alt = el.getAttribute('alt') || '';
                    data.imageUrl = el.src || '';
                }
                if (tag === 'button' || tag === 'input') {
                    data.attributes.type = el.getAttribute('type') || '';
                }
                
                // Extract inline SVG as data URI for use as Image
                if (tag === 'svg') {
                    try {
                        const svgContent = el.outerHTML;
                        // Clean up the SVG and convert to data URI
                        const encoded = btoa(unescape(encodeURIComponent(svgContent)));
                        data.imageUrl = 'data:image/svg+xml;base64,' + encoded;
                        data.attributes.alt = 'SVG icon';
                        // Mark as SVG image for conversion
                        data.isSvgImage = true;
                    } catch (e) {
                        // If encoding fails, continue without image URL
                    }
                }
                
                // Extract children
                for (const child of el.children) {
                    const childData = extractElement(child, depth + 1);
                    if (childData) {
                        data.children.push(childData);
                    }
                }
                
                return data;
            };
            
            // Get ALL root styles from body and html - both specified and computed
            const body = document.body;
            const html = document.documentElement;
            const bodyComputed = window.getComputedStyle(body);
            const htmlComputed = window.getComputedStyle(html);

            // Detect theme
            const bgColor = bodyComputed.backgroundColor || htmlComputed.backgroundColor;
            let isDark = false;
            const rgbMatch = bgColor.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
            if (rgbMatch) {
                const [_, r, g, b] = rgbMatch.map(Number);
                const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
                isDark = luminance < 0.5;
            }

            // Capture body styles - INDIVIDUAL PROPERTIES ONLY (no shorthands)
            // These will be applied to the root component
            // NOTE: Only pick up SPECIFIED styles (from CSS rules or inline), not computed defaults
            const ROOT_STYLE_PROPS = [
                // Layout
                'display', 'position', 'top', 'right', 'bottom', 'left', 'z-index',
                'flex-direction', 'flex-wrap', 'justify-content',
                'align-items', 'align-content', 'align-self', 'gap', 'row-gap', 'column-gap',
                'float', 'clear',
                // Box model - INDIVIDUAL ONLY (no margin, padding shorthand)
                'width', 'height', 'min-width', 'max-width', 'min-height', 'max-height',
                'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
                'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
                'box-sizing',
                // Background - INDIVIDUAL ONLY (no background shorthand)
                'background-color', 'background-image', 'background-size',
                'background-position', 'background-repeat', 'background-attachment',
                'background-clip', 'background-origin',
                // Typography
                'color', 'font-family', 'font-size', 'font-weight', 'font-style',
                'line-height', 'letter-spacing', 'text-align', 'text-decoration',
                'text-transform', 'white-space', 'word-spacing', 'word-break',
                'text-overflow', 'vertical-align', 'text-shadow',
                // Visual effects
                'overflow', 'overflow-x', 'overflow-y', 'opacity', 'visibility',
                'box-shadow', 'filter', 'backdrop-filter',
                'transform',
                // Border - INDIVIDUAL ONLY (no border shorthand)
                'border-top-width', 'border-right-width', 'border-bottom-width', 'border-left-width',
                'border-top-style', 'border-right-style', 'border-bottom-style', 'border-left-style',
                'border-top-color', 'border-right-color', 'border-bottom-color', 'border-left-color',
                'border-top-left-radius', 'border-top-right-radius',
                'border-bottom-left-radius', 'border-bottom-right-radius',
                // Other
                'cursor', 'pointer-events', 'user-select',
                'clip-path', 'object-fit', 'object-position'
            ];

            const rootStyles = {
                theme: isDark ? 'dark' : 'light'
            };

            // Helper to check if a property is SPECIFIED (not just computed)
            // Check inline styles and CSS rules
            const isPropertySpecified = (el, prop) => {
                // Check inline style
                if (el.style.getPropertyValue(prop)) {
                    return true;
                }

                // Check CSS rules
                try {
                    const sheets = document.styleSheets;
                    for (let i = 0; i < sheets.length; i++) {
                        try {
                            const rules = sheets[i].cssRules || sheets[i].rules;
                            if (!rules) continue;

                            for (let j = 0; j < rules.length; j++) {
                                const rule = rules[j];
                                if (rule.type !== CSSRule.STYLE_RULE) continue;

                                try {
                                    // Check if rule matches body or html
                                    if (el.matches(rule.selectorText)) {
                                        if (rule.style.getPropertyValue(prop)) {
                                            return true;
                                        }
                                    }
                                } catch (e) {
                                    // Invalid selector
                                }
                            }
                        } catch (e) {
                            // Cross-origin stylesheet
                        }
                    }
                } catch (e) {
                    // Error accessing stylesheets
                }

                return false;
            };

            // Get ONLY specified styles from body/html (not computed defaults)
            for (const prop of ROOT_STYLE_PROPS) {
                const camelProp = prop.replace(/-([a-z])/g, (g) => g[1].toUpperCase());

                // Check if property is specified on body or html
                const specifiedOnBody = isPropertySpecified(body, prop);
                const specifiedOnHtml = isPropertySpecified(html, prop);

                if (!specifiedOnBody && !specifiedOnHtml) {
                    continue; // Not specified anywhere, skip
                }

                // Get the computed value (resolved)
                let value = bodyComputed.getPropertyValue(prop);

                // For background, also check html if body is transparent
                if ((prop === 'background-color' || prop === 'background-image') &&
                    (value === 'rgba(0, 0, 0, 0)' || value === 'none') && specifiedOnHtml) {
                    const htmlValue = htmlComputed.getPropertyValue(prop);
                    if (htmlValue && htmlValue !== 'rgba(0, 0, 0, 0)' && htmlValue !== 'none') {
                        value = htmlValue;
                    }
                }

                // Skip empty values or CSS variable references
                if (!value || value.includes('var(')) {
                    continue;
                }

                // Skip transparent/none values
                if (value === 'rgba(0, 0, 0, 0)' || value === 'transparent' ||
                    value === 'none' || value === 'auto' || value === 'normal') {
                    continue;
                }

                // Skip all border properties (color, style, width) if border width is 0
                if (prop.includes('border') && !prop.includes('radius')) {
                    // Extract the side (top, right, bottom, left)
                    let side = '';
                    if (prop.includes('-top-')) side = 'top';
                    else if (prop.includes('-right-')) side = 'right';
                    else if (prop.includes('-bottom-')) side = 'bottom';
                    else if (prop.includes('-left-')) side = 'left';

                    if (side) {
                        const widthProp = `border-${side}-width`;
                        const width = bodyComputed.getPropertyValue(widthProp);
                        if (width === '0px') {
                            continue; // Skip ALL border properties for this side if width is 0
                        }
                    }
                }

                rootStyles[camelProp] = value;
            }
            
            // Extract all direct children of body - NO DUPLICATE FILTERING
            const elements = [];
            
            for (const child of body.children) {
                const elemData = extractElement(child, 0);
                if (elemData) {
                    elements.push(elemData);
                }
            }
            
            // Sort by Y position (top to bottom)
            elements.sort((a, b) => (a.bounds?.y || 0) - (b.bounds?.y || 0));
            
            return {
                rootStyles: rootStyles,
                elements: elements
            };
        }''')
    
    def _merge_viewport_data(
        self, 
        viewport_data: Dict[str, Dict[str, Any]],
        base_url: str
    ) -> List[VisualElement]:
        """
        Merge element data from all viewports into VisualElement objects.
        
        Each element gets styles from all viewports for responsive support.
        Also filters out duplicates and sorts by position.
        Decodes Next.js CDN URLs to direct image paths.
        """
        desktop_data = viewport_data.get("desktop", {})
        tablet_data = viewport_data.get("tablet", {})
        mobile_data = viewport_data.get("mobile", {})
        
        desktop_elements = desktop_data.get("elements", [])
        
        def merge_element(desktop_elem: Dict, tablet_elems: List, mobile_elems: List) -> VisualElement:
            elem_id = desktop_elem.get("id", "")
            
            # Find matching elements in other viewports by position/content
            tablet_elem = self._find_matching_element(desktop_elem, tablet_elems)
            mobile_elem = self._find_matching_element(desktop_elem, mobile_elems)
            
            # Decode Next.js image URLs to direct paths
            raw_image_url = desktop_elem.get("imageUrl", "")
            decoded_image_url = decode_nextjs_image_url(raw_image_url, base_url) if raw_image_url else ""
            
            # Create VisualElement with styles from each viewport
            # If tablet/mobile element not found, use desktop styles as fallback
            # This ensures responsive styles still work even with imperfect matching
            desktop_styles = desktop_elem.get("styles", {})
            tablet_styles = tablet_elem.get("styles", {}) if tablet_elem else {}
            mobile_styles = mobile_elem.get("styles", {}) if mobile_elem else {}

            # Log matching results for first few elements
            if len(desktop_elements) > 0 and desktop_elem == desktop_elements[0]:
                logger.info(f"[Merge] First element match: tablet_found={tablet_elem is not None}, mobile_found={mobile_elem is not None}")
                if tablet_elem:
                    t_diffs = [k for k in tablet_styles if tablet_styles.get(k) != desktop_styles.get(k)]
                    logger.info(f"[Merge] Tablet style diffs: {t_diffs[:5] if t_diffs else 'NONE'}")

            element = VisualElement(
                id=elem_id,
                tag=desktop_elem.get("tag", "div"),
                text=desktop_elem.get("text", ""),
                image_url=decoded_image_url,
                styles={
                    "desktop": desktop_styles,
                    "tablet": tablet_styles,
                    "mobile": mobile_styles
                },
                bounds={
                    "desktop": desktop_elem.get("bounds", {}),
                    "tablet": tablet_elem.get("bounds", {}) if tablet_elem else {},
                    "mobile": mobile_elem.get("bounds", {}) if mobile_elem else {}
                },
                attributes=desktop_elem.get("attributes", {})
            )
            
            # Merge children recursively - NO DUPLICATE FILTERING
            desktop_children = desktop_elem.get("children", [])
            tablet_children = tablet_elem.get("children", []) if tablet_elem else []
            mobile_children = mobile_elem.get("children", []) if mobile_elem else []
            
            for child in desktop_children:
                merged_child = merge_element(child, tablet_children, mobile_children)
                element.children.append(merged_child)
            
            return element
        
        # Get tablet and mobile element lists
        tablet_elements = tablet_data.get("elements", [])
        mobile_elements = mobile_data.get("elements", [])

        # Merge ALL top-level elements - NO DUPLICATE FILTERING
        # Sort by Y position (top to bottom)
        desktop_elements.sort(key=lambda e: e.get("bounds", {}).get("y", 0))

        logger.info(f"Processing {len(desktop_elements)} elements (no filtering)")

        # Debug: Log sample of style differences across viewports
        if desktop_elements and tablet_elements:
            sample_desktop = desktop_elements[0] if desktop_elements else {}
            sample_tablet = tablet_elements[0] if tablet_elements else {}
            d_styles = sample_desktop.get("styles", {})
            t_styles = sample_tablet.get("styles", {})
            diff_props = [k for k in set(list(d_styles.keys()) + list(t_styles.keys()))
                         if d_styles.get(k) != t_styles.get(k)]
            logger.info(f"[Viewport Debug] First element style diff count: {len(diff_props)}")
            if diff_props[:3]:
                logger.info(f"[Viewport Debug] Sample diffs: {diff_props[:3]}")
        
        merged = []
        for desktop_elem in desktop_elements:
            merged.append(merge_element(desktop_elem, tablet_elements, mobile_elements))
        
        return merged
    
    def _find_matching_element(
        self,
        target: Dict,
        candidates: List[Dict]
    ) -> Optional[Dict]:
        """Find the matching element in another viewport by id, tag, and text similarity."""
        target_id = target.get("id", "")
        target_tag = target.get("tag", "")
        target_text = target.get("text", "")[:50]
        target_bounds = target.get("bounds", {})

        # First try exact id match (most reliable)
        for candidate in candidates:
            if candidate.get("id") == target_id and target_id:
                return candidate

        # Then try tag + text match (good for unique text content)
        for candidate in candidates:
            if (candidate.get("tag") == target_tag and
                candidate.get("text", "")[:50] == target_text and target_text):
                return candidate

        # Then try tag + similar position (for elements without unique text)
        # This helps match elements that moved due to responsive layout
        for candidate in candidates:
            if candidate.get("tag") == target_tag:
                cand_bounds = candidate.get("bounds", {})
                # Check if X position is similar (within 50px) - Y will differ due to reflow
                if abs(cand_bounds.get("x", 0) - target_bounds.get("x", 0)) < 50:
                    return candidate

        # Fall back to just tag match (last resort)
        for candidate in candidates:
            if candidate.get("tag") == target_tag:
                return candidate

        return None
    
    def _extract_root_styles(
        self,
        viewport_data: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Dict[str, str]]:
        """Extract ALL root/body styles from each viewport - pass through everything."""
        root_styles = {}

        for viewport_name, data in viewport_data.items():
            styles = data.get("rootStyles", {})
            # Pass through ALL styles from JavaScript extraction
            # Don't filter - let page_agent decide what to use
            root_styles[viewport_name] = dict(styles)

        return root_styles
    
    def _extract_images(
        self, 
        desktop_data: Dict[str, Any],
        base_url: str
    ) -> List[ImageInfo]:
        """Extract all image URLs from the page."""
        images = []
        seen_urls = set()
        
        def extract_from_element(elem: Dict):
            # Check for image elements
            if elem.get("tag") == "img":
                url = elem.get("imageUrl") or elem.get("attributes", {}).get("src", "")
                if url and url not in seen_urls and not url.startswith("data:"):
                    # Resolve relative URLs
                    if not url.startswith(("http://", "https://")):
                        url = urljoin(base_url, url)
                    
                    # Decode Next.js image optimization URLs to direct paths
                    url = decode_nextjs_image_url(url, base_url)
                    
                    seen_urls.add(url)
                    images.append(ImageInfo(
                        url=url,
                        alt_text=elem.get("attributes", {}).get("alt", ""),
                        is_background=False
                    ))
            
            # Check for background images
            bg_image = elem.get("styles", {}).get("backgroundImage", "")
            if bg_image and bg_image != "none":
                urls = re.findall(r'url\(["\']?([^"\')\s]+)["\']?\)', bg_image)
                for url in urls:
                    if url and url not in seen_urls and not url.startswith("data:"):
                        if not url.startswith(("http://", "https://")):
                            url = urljoin(base_url, url)
                        
                        # Decode Next.js image optimization URLs to direct paths
                        url = decode_nextjs_image_url(url, base_url)
                        
                        seen_urls.add(url)
                        images.append(ImageInfo(url=url, is_background=True))
            
            # Recurse into children
            for child in elem.get("children", []):
                extract_from_element(child)
        
        for elem in desktop_data.get("elements", []):
            extract_from_element(elem)
        
        return images
    
    async def close(self):
        """Clean up browser resources."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()


# Singleton instance
_extractor: Optional[WebsiteExtractor] = None


def get_website_extractor() -> WebsiteExtractor:
    """Get or create the website extractor instance."""
    global _extractor
    if _extractor is None:
        from app.config import settings
        _extractor = WebsiteExtractor(
            screenshot_timeout=getattr(settings, 'SCREENSHOT_TIMEOUT', 60)
        )
    return _extractor
