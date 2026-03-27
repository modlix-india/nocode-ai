"""LLM-based extraction service for business information.

Two-pass extraction:
  Pass 1 (extract_metadata): Cheap structured extraction — brand, type, location
  Pass 2 (extract_summary): Rich marketing summary with products, USPs, contact

Ported from ds/core/scraping/extraction_service.py — simplified to use
nocode-ai's OpenAI client configuration.
"""

import logging
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from app.agents.adzump.agents.business.models import (
    BusinessProfile,
    PageContent,
    WebsiteMetadata,
    WebsiteSummary,
)
from app.config import settings

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Models for extraction — use cheap/fast models
METADATA_MODEL = "gpt-4o-mini"
SUMMARY_MODEL = "gpt-4o-mini"


def _load_prompt(name: str, **kwargs: str) -> str:
    """Load a prompt template and format with kwargs."""
    path = PROMPTS_DIR / name
    template = path.read_text(encoding="utf-8")
    return template.format(**kwargs)


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a Pydantic JSON schema compatible with OpenAI strict mode.

    OpenAI requires:
    - additionalProperties: false on every object
    - No default values on properties
    - All properties in required array
    """
    schema = schema.copy()

    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        props = schema.get("properties", {})
        schema["required"] = list(props.keys())
        for key, prop in props.items():
            props[key] = _strict_schema(prop)
            props[key].pop("default", None)
            props[key].pop("title", None)

    if "items" in schema:
        schema["items"] = _strict_schema(schema["items"])

    if "$defs" in schema:
        for def_name, def_schema in schema["$defs"].items():
            schema["$defs"][def_name] = _strict_schema(def_schema)

    if "anyOf" in schema:
        schema["anyOf"] = [_strict_schema(s) for s in schema["anyOf"]]

    schema.pop("title", None)
    schema.pop("description", None)

    return schema


def _get_openai_client() -> AsyncOpenAI:
    """Get an AsyncOpenAI client using nocode-ai's configured API key."""
    return AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


class ExtractionService:
    """Extracts business information from web pages using LLM."""

    async def extract_metadata(self, pages: list[PageContent]) -> WebsiteMetadata:
        """Pass 1: cheap structured extraction — brand, type, location."""
        content = _prepare_content(pages)
        prompt = _load_prompt("metadata.txt", content=content)

        client = _get_openai_client()
        response = await client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=METADATA_MODEL,
            temperature=0.1,
            max_tokens=500,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "website_metadata",
                    "schema": _strict_schema(WebsiteMetadata.model_json_schema()),
                    "strict": True,
                },
            },
        )

        result = WebsiteMetadata.model_validate_json(
            response.choices[0].message.content
        )
        logger.info("metadata_extracted: brand=%s type=%s", result.brand_name, result.business_type)
        return result

    async def extract_summary(
        self, pages: list[PageContent], metadata: WebsiteMetadata
    ) -> WebsiteSummary:
        """Pass 2: rich marketing summary with context from Pass 1."""
        content = _prepare_content(pages)
        prompt = _load_prompt(
            "summary.txt",
            content=content,
            brand_name=metadata.brand_name,
            business_type=metadata.business_type,
            primary_location=metadata.primary_location,
            service_areas=", ".join(metadata.service_areas) or "Not provided",
        )

        client = _get_openai_client()
        response = await client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=SUMMARY_MODEL,
            temperature=0.2,
            max_tokens=2000,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "website_summary",
                    "schema": _strict_schema(WebsiteSummary.model_json_schema()),
                    "strict": True,
                },
            },
        )

        result = WebsiteSummary.model_validate_json(
            response.choices[0].message.content
        )
        logger.info("summary_extracted: usps=%d products=%d",
                     len(result.unique_selling_points), len(result.products_services))
        return result

    def build_profile(
        self,
        metadata: WebsiteMetadata,
        summary: WebsiteSummary,
        pages: list[PageContent],
    ) -> BusinessProfile:
        """Combine metadata + summary into aggregate profile."""
        return BusinessProfile(
            brand_name=metadata.brand_name,
            business_type=metadata.business_type,
            primary_location=metadata.primary_location,
            service_areas=metadata.service_areas,
            summary=summary.marketing_summary,
            unique_features=summary.unique_selling_points,
            products_services=summary.products_services,
            contact=summary.contact_info,
            pages_analyzed=[page.url for page in pages],
        )


def _prepare_content(pages: list[PageContent]) -> str:
    """Combine page titles, headings, and paragraphs into a single string."""
    sections = []
    for page in pages:
        parts = []
        if page.title:
            parts.append(f"Title: {page.title}")
        if page.meta_description:
            parts.append(f"Description: {page.meta_description}")
        if page.headings:
            parts.append("Headings: " + " | ".join(page.headings))
        if page.paragraphs:
            parts.append("\n".join(page.paragraphs))
        if parts:
            sections.append("\n".join(parts))
    return "\n\n---\n\n".join(sections)
