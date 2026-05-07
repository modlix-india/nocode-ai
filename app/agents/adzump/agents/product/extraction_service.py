  """LLM-based extraction service for business information.

Three-stage extraction:
  1. extract_metadata: Cheap structured extraction — product name, type, location
  2. extract_structured_data: USPs, products, contact as JSON
  3. stream_summary: Marketing summary streamed as plain text
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from app.agents.adzump.agents.product.models import (
    BusinessProfile,
    PageContent,
    WebsiteMetadata,
)
from app.config import settings

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
METADATA_MODEL = "gpt-4o-mini"
SUMMARY_MODEL = "gpt-4o-mini"


def _load_prompt(name: str, **kwargs: str) -> str:
    """Load a prompt template and format with kwargs."""
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8").format(**kwargs)


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a Pydantic JSON schema compatible with OpenAI strict mode."""
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
    return AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


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


class ExtractionService:
    """Extracts business information from web pages using LLM."""

    async def extract_metadata(self, pages: list[PageContent]) -> WebsiteMetadata:
        """Stage 1: cheap structured extraction — product name, type, location."""
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

        result = WebsiteMetadata.model_validate_json(response.choices[0].message.content)
        logger.info("metadata_extracted: brand=%s type=%s", result.product_name, result.business_type)
        return result

    async def stream_summary(
        self, pages: list[PageContent], metadata: WebsiteMetadata,
    ) -> AsyncIterator[str]:
        """Stage 3: stream marketing summary as plain text tokens."""
        content = _prepare_content(pages)

        prompt = _load_prompt(
            "summary_text.txt",
            content=content,
            product_name=metadata.product_name,
            business_type=metadata.business_type,
            location=metadata.location.location or "Unknown",
            suggested_locations=", ".join(metadata.location.suggested_locations) or "Not provided",
        )

        client = _get_openai_client()
        stream = await client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=SUMMARY_MODEL,
            temperature=0.2,
            max_tokens=1500,
            stream=True,
        )

        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def build_profile(
        self,
        metadata: WebsiteMetadata,
        summary_text: str,
        pages: list[PageContent],
    ) -> BusinessProfile:
        """Combine metadata + streamed summary into profile."""
        return BusinessProfile(
            product_name=metadata.product_name,
            business_type=metadata.business_type,
            location=metadata.location,
            summary=summary_text,
            pages_analyzed=[page.url for page in pages],
        )
