"""Data models for the scraping pipeline.

Ported from ds/core/models/scraping.py — simplified to remove geo/location
models not needed in nocode-ai.
"""

from pydantic import BaseModel


class ContactInfo(BaseModel):
    phone: str | None = None
    email: str | None = None
    address: str | None = None


class PageContent(BaseModel):
    """Parsed content from a single web page."""
    url: str
    title: str
    meta_description: str = ""
    headings: list[str] = []
    paragraphs: list[str] = []
    links: list[str] = []
    structured_data: dict | None = None


class WebsiteMetadata(BaseModel):
    """Pass 1 LLM extraction — cheap structured extraction."""
    brand_name: str
    business_type: str
    primary_location: str
    service_areas: list[str] = []


class WebsiteSummary(BaseModel):
    """Pass 2 LLM extraction — rich marketing summary."""
    marketing_summary: str
    unique_selling_points: list[str] = []
    products_services: list[str] = []
    contact_info: ContactInfo | None = None


class BusinessProfile(BaseModel):
    """Aggregate — full extracted business data."""
    brand_name: str
    business_type: str
    primary_location: str
    service_areas: list[str] = []
    summary: str
    unique_features: list[str] = []
    products_services: list[str] = []
    contact: ContactInfo | None = None
    pages_analyzed: list[str] = []


class ScrapeResult(BaseModel):
    """Result from any scraping adapter."""
    success: bool
    content: PageContent | None = None
    error: str | None = None
