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


class PageContent(BaseModel):
    """Parsed content from a single web page."""
    url: str
    title: str
    meta_description: str = ""
    headings: list[str] = []
    paragraphs: list[str] = []
    links: list[SiteLink] = []
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


class ScrapeResult(BaseModel):
    """Result from any scraping adapter."""
    success: bool
    content: PageContent | None = None
    screenshot: str | None = None  # base64 encoded PNG
    error: str | None = None


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
