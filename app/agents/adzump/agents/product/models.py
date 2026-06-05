"""Data models for the scraping pipeline.

Ported from ds/core/models/scraping.py — simplified to remove geo/location
models not needed in nocode-ai.
"""

from dataclasses import dataclass, field

from pydantic import BaseModel


class ContactInfo(BaseModel):
    phone: str | None = None
    email: str | None = None
    address: str | None = None


class SiteLink(BaseModel):
    """A single anchor extracted from a page (text + href)."""
    text: str = ""
    href: str


class SiteImage(BaseModel):
    """A single image candidate extracted from a page. The parser only
    gathers evidence; the LLM in `product_assets.py` makes the selection."""
    src: str
    alt: str = ""
    title: str = ""
    width: int | None = None
    height: int | None = None
    # Where the URL came from: "img" | "picture" | "og" | "link" | "jsonld"
    source: str = "img"
    in_header: bool = False
    in_footer: bool = False
    in_nav: bool = False
    class_attr: str = ""
    id_attr: str = ""
    # Rendered position + dimensions captured by Playwright (top-of-page).
    # Lets us recognize header logos on framework sites that don't use
    # semantic <header>/<nav> tags — the only reliable signal on Wix/Webflow.
    rendered_top: int | None = None
    rendered_left: int | None = None
    rendered_width: int | None = None
    rendered_height: int | None = None


class LogoPick(BaseModel):
    """One brand-logo pick from the LLM. Sites with parent/sub-brand
    structure (real estate developer + project, franchise, etc.) return
    more than one; most sites return one."""
    url: str                 # source URL the LLM picked
    source: str = ""         # which candidate source signal (img/og/jsonld/…)
    role: str = ""           # free-text role label from the LLM (e.g. "developer", "project", "main")
    reasoning: str = ""      # 1-line LLM explanation
    format: str = ""         # "svg" | "png" | "jpg" | "webp" | … — derived from content-type at fetch.
                             # Lets downstream consumers (creative-gen, etc.) branch on format
                             # without sniffing bytes. SVG needs rasterization before image-gen APIs.
    background: str = ""     # "light" | "dark" | "" — UI tile contrast hint from the vision LLM.
                             # A white wordmark designed for a dark header needs a dark backing tile
                             # to read against; the LLM looks at the thumbnail and decides. Empty
                             # = no hint = UI renders on its neutral default tile.


class CreativeRole(BaseModel):
    """Per-candidate creative-role assignment from the vision LLM.

    Shift 2 (post-v7 redesign · 2026-05-21). Mirrors `LogoPick` shape so
    downstream consumers can map URL → role (hero / amenity / floor_plan)
    without re-classifying. Populated only when the picker prompt asks for
    role labels; absent for legacy v6/v7 callers that still use
    `creative_image_urls`.
    """
    url: str
    role: str = ""                       # 'hero' | 'amenity' | 'floor_plan' | 'unused'
    reasoning: str = ""                  # one-line note from the model


class CreativeCompleteness(BaseModel):
    """Derived aggregate of `creatives_with_role` — code-computed, not model-
    emitted. Per Kiran's Q3 pick: model labels each candidate (perception),
    code applies the launch-readiness policy (derivation).
    """
    hero_found: bool = False
    amenities_count: int = 0
    floor_plan_found: bool = False
    # 'complete' = hero_found AND amenities_count >= 1
    # 'partial'  = hero_found (only) OR (amenities only, no hero)
    # 'needs_upload' = !hero_found
    verdict: str = "needs_upload"
    missing_categories: list[str] = []


class ProductAssets(BaseModel):
    """LLM-selected product logo(s) + ad-creative image candidates."""
    logos: list[LogoPick] = []          # 0..3 brand logos, primary first
    creative_image_urls: list[str] = []  # ranked best first (back-compat)
    creatives_with_role: list[CreativeRole] = []  # Shift 2: per-candidate role
    creative_completeness: CreativeCompleteness = CreativeCompleteness()  # derived
    confidence: float = 0.0              # 0..1, LLM self-assessment on logo picks
    note: str = ""                       # LLM's refusal/rejection rationale when a
                                         # logo-looking candidate was deliberately
                                         # rejected, or when no logo qualifies.
                                         # Load-bearing for regression triage —
                                         # without it, every miss looks identical.

    # Convenience accessors for callers that only need the primary logo.
    @property
    def logo_url(self) -> str | None:
        return self.logos[0].url if self.logos else None

    @property
    def logo_source(self) -> str:
        return self.logos[0].source if self.logos else ""

    @property
    def logo_reasoning(self) -> str:
        return self.logos[0].reasoning if self.logos else ""


class PageContent(BaseModel):
    """Parsed content from a single web page."""
    url: str
    title: str
    meta_description: str = ""
    headings: list[str] = []
    paragraphs: list[str] = []
    links: list[SiteLink] = []
    images: list[SiteImage] = []
    structured_data: dict | None = None


class LocationInfo(BaseModel):
    """Business location with suggested ad targeting areas."""
    location: str = ""
    suggested_locations: list[str] = []


class WebsiteMetadata(BaseModel):
    """Pass 1 LLM extraction — cheap structured extraction."""
    product_name: str
    business_type: str
    location: LocationInfo = LocationInfo()


class BusinessProfile(BaseModel):
    """Aggregate — full extracted business data."""
    product_name: str
    business_type: str
    location: LocationInfo = LocationInfo()
    summary: str
    unique_features: list[str] = []
    products_services: list[str] = []
    contact: ContactInfo | None = None
    pages_analyzed: list[str] = []


class ScrapeTimings(BaseModel):
    """Per-stage scrape timing in ms, populated by the Playwright adapter.

    Eval/observability only — the production scrape path ignores this field.
    A stage left ``None`` did not run (distinct from a measured ``0.0``).
    ``intrinsic_ms = total_ms - sem_wait_ms`` is the honest "scrape cost",
    free of the inner browser-semaphore queue wait; never report raw work_s.
    """
    sem_wait_ms: float | None = None         # blocked on the cap-3 _browser_semaphore (pre-acquire)
    launch_ms: float | None = None           # playwright start + chromium launch + new_page + listener wiring
    goto_ms: float | None = None             # page.goto(domcontentloaded)
    networkidle_ms: float | None = None      # top-level networkidle settle (<=5s)
    cloudflare_ms: float | None = None       # challenge wait (<=6s; ~0 when not detected)
    cookie_ms: float | None = None           # consent-banner dismissal
    early_ms: float | None = None            # early screenshot + early-html callbacks (~0 when unused)
    scroll_ms: float | None = None           # full-page scroll (dwell + load-wait), the suspected dominant
    scroll_dwell_ms: float | None = None     # fixed dwell budget (steps x DWELL_MS) — the knob we own
    scroll_loadwait_ms: float | None = None  # network-dependent load-wait inside scroll
    positions_ms: float | None = None        # image bounding-box JS eval
    screenshot_ms: float | None = None       # final full-page screenshot + downscale
    parse_ms: float | None = None            # html parse (CPU, outside the browser)
    intrinsic_ms: float | None = None        # total_ms - sem_wait_ms (post-acquire work)
    total_ms: float | None = None            # scrape_page wall-clock incl. sem_wait + parse
    cold_start: bool = False                 # first scrape in this process (cold Chromium launch)
    step_count: int | None = None            # scroll viewport steps
    scroll_height_px: int | None = None      # document scrollHeight at scroll time


class ScrapeResult(BaseModel):
    """Result from any scraping adapter."""
    success: bool
    content: PageContent | None = None
    screenshot: str | None = None  # base64 encoded PNG
    error: str | None = None
    # Optional per-stage timing (Playwright adapter only); None on adapters that
    # don't track it. Read by the eval harness; ignored by the production caller.
    timings: ScrapeTimings | None = None


class CompetitorProfile(BaseModel):
    """A single competitor discovered and analyzed by the BusinessAnalyst."""
    name: str
    url: str
    business_type: str = ""
    location: str = ""
    pricing: str | None = None
    key_usps: list[str] = []         # top differentiators
    trust_signals: list[str] = []    # e.g. "500+ reviews", "Since 2005"
    weakness: str | None = None      # inferred gap, optional


class CompetitiveAnalysis(BaseModel):
    """Aggregate competitive landscape output of the BusinessAnalyst agent."""
    competitors: list[CompetitorProfile] = []
    our_usps: list[str] = []              # things we do that competitors don't
    competitive_threats: list[str] = []   # things competitors do better
    keyword_opportunities: list[str] = [] # keyword angles worth targeting
    positioning: str = ""                 # one-line positioning statement


class BusinessAnalysisResult(BaseModel):
    """Top-level result returned by the BusinessAnalyst agent."""
    business: BusinessProfile
    competitive: CompetitiveAnalysis = CompetitiveAnalysis()
    notes: list[str] = []  # agent-visible caveats (e.g. "skipped reviews: API error")


@dataclass
class AnalysisOutput:
    """Structured return type of ProductAgent.analyze().

    Uses raw dicts (not Pydantic models) because downstream code reads
    arbitrary keys like positioning_narrative, segments, etc. that aren't
    fully modeled yet. Strict validation is a future concern.
    """
    business: dict | None
    competitive: dict | None
    notes: list[str] = field(default_factory=list)
    screenshot_url: str | None = None
    raw_text: str = ""
