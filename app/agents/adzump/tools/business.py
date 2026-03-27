"""Business analysis tools — website scraping, competitor analysis, location search.

scrape_website runs the local ScrapeAgent pipeline (httpx → Playwright → LLM extraction).
analyze_competitors and search_locations call the ds service.
"""

from __future__ import annotations

import json
import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.tools._shared import get_ds_client, build_ds_headers

logger = logging.getLogger(__name__)


async def _scrape_website(params: dict, context: dict) -> ToolResult:
    """Scrape a website and extract business information using LLM."""
    url = params.get("url", "").strip()
    if not url:
        return ToolResult(success=False, error="URL is required.")

    # Add scheme if missing
    if not url.startswith("http"):
        url = f"https://{url}"

    try:
        from app.agents.adzump.agents.business.scrape_agent import get_scrape_agent

        agent = get_scrape_agent()
        profile = await agent.run(url)

        # Store business info in session context for other tools
        session_ctx = context.get("session_context", {})
        session_ctx["business_info"] = profile.model_dump()

        # Build a concise summary for the LLM
        summary_parts = [
            f"Business: {profile.brand_name}",
            f"Type: {profile.business_type}",
            f"Location: {profile.primary_location}",
        ]
        if profile.service_areas:
            summary_parts.append(f"Service Areas: {', '.join(profile.service_areas)}")
        if profile.unique_features:
            summary_parts.append(f"USPs: {', '.join(profile.unique_features[:5])}")
        if profile.products_services:
            summary_parts.append(f"Products/Services: {', '.join(profile.products_services[:10])}")
        if profile.contact:
            contact_parts = []
            if profile.contact.phone:
                contact_parts.append(f"Phone: {profile.contact.phone}")
            if profile.contact.email:
                contact_parts.append(f"Email: {profile.contact.email}")
            if contact_parts:
                summary_parts.append(f"Contact: {', '.join(contact_parts)}")

        summary_parts.append(f"\nFull Summary:\n{profile.summary}")

        return ToolResult(
            success=True,
            data=profile.model_dump(),
            summary="\n".join(summary_parts),
        )

    except RuntimeError as e:
        return ToolResult(success=False, error=str(e))
    except Exception as e:
        logger.exception("scrape_website failed: url=%s", url)
        return ToolResult(success=False, error=f"Scraping failed: {type(e).__name__}: {e}")


async def _analyze_competitors(params: dict, context: dict) -> ToolResult:
    """Analyze competitors in the same business space (calls ds service)."""
    business_description = params.get("business_description", "").strip()
    location = params.get("location", "").strip()

    if not business_description:
        return ToolResult(success=False, error="business_description is required.")

    client = get_ds_client()
    headers = build_ds_headers(context)

    result = await client.post(
        "/api/ds/competitor/analyze",
        headers=headers,
        json={
            "business_description": business_description,
            "location": location,
        },
    )

    if not result.success:
        return result

    return ToolResult(
        success=True,
        data=result.data,
        summary=f"Found competitors for: {business_description[:50]}",
    )


async def _search_locations(params: dict, context: dict) -> ToolResult:
    """Search for geographic locations for ad targeting (calls ds service)."""
    query = params.get("query", "").strip()
    if not query:
        return ToolResult(success=False, error="query is required.")

    client = get_ds_client()
    headers = build_ds_headers(context)

    result = await client.post(
        "/api/ds/maps/render",
        headers=headers,
        json={"query": query},
    )

    if not result.success:
        return result

    return ToolResult(
        success=True,
        data=result.data,
        summary=f"Found locations for: {query}",
    )


scrape_website = ToolDefinition(
    name="scrape_website",
    description="Scrape a website to extract business information (name, type, description, products/services, USPs, contact info, location). Use this to understand the business before creating a campaign.",
    display_name="Analyze Website",
    parameters=[
        ToolParameter(name="url", type="string", description="The website URL to analyze", required=True),
    ],
    execute=_scrape_website,
)

analyze_competitors = ToolDefinition(
    name="analyze_competitors",
    description="Find and analyze competitors in the same business space. Returns competitor domains and estimated ad spend.",
    display_name="Analyze Competitors",
    parameters=[
        ToolParameter(name="business_description", type="string", description="Description of the business to find competitors for", required=True),
        ToolParameter(name="location", type="string", description="Target location/market (e.g. 'Mumbai, India')", required=False),
    ],
    execute=_analyze_competitors,
)

search_locations = ToolDefinition(
    name="search_locations",
    description="Search for geographic locations for ad targeting. Returns location names with Google Place IDs.",
    display_name="Search Locations",
    parameters=[
        ToolParameter(name="query", type="string", description="Location search query (e.g. 'Mumbai', 'California')", required=True),
    ],
    execute=_search_locations,
)

BUSINESS_TOOLS = [scrape_website, analyze_competitors, search_locations]
