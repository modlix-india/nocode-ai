"""Product analysis tools — website scraping and product profiling.

``analyze_product`` is the primary tool — it runs the ProductAnalyst sub-agent
for enhanced scraping, and falls back to the deterministic ``ScrapePipeline``
pipeline if the sub-agent fails.

``scrape_website`` remains exported as a lower-level fallback.
"""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.tools._shared import emit_progress, upload_screenshot

logger = logging.getLogger(__name__)


# ── Tool implementations (bottom-up: fallback first, primary second) ──

async def _scrape_website(params: dict, context: dict) -> ToolResult:
    """Scrape a website and extract business information using LLM."""
    url = params.get("url", "").strip()
    if not url:
        return ToolResult(success=False, error="URL is required.")

    if not url.startswith("http"):
        url = f"https://{url}"

    stream = context.get("event_stream")
    tool_use_id = context.get("tool_use_id", "")

    async def progress(message: str) -> None:
        if stream and tool_use_id:
            await stream.emit_tool_update(tool_use_id, message)

    try:
        from app.agents.adzump.agents.product.scrape_pipeline import get_scrape_pipeline

        session_ctx = context.get("session_context", {})
        craft_id = session_ctx.get("craft_id", "")

        craft_title = ""

        async def on_craft(stage: str, data, _unused) -> None:
            nonlocal craft_title
            if not stream:
                return

            if stage == "screenshot" and data:
                import base64
                screenshot_bytes = base64.b64decode(data)
                screenshot_url = await upload_screenshot(
                    screenshot_bytes, f"{craft_id}.jpg", context,
                )
                if screenshot_url:
                    await stream.emit_craft(craft_id, url, [
                        {"type": "image", "url": screenshot_url},
                        {"type": "callout", "text": "Analyzing website...", "variant": "info"},
                    ])
                    logger.info("Craft screenshot: id=%s url=%s", craft_id, screenshot_url[:80])

            elif stage == "metadata" and data:
                craft_title = data.product_name
                kv_items = [{"key": "Website", "value": url}]
                if data.location.location:
                    kv_items.append({"key": "Location", "value": data.location.location})
                if data.location.suggested_locations:
                    kv_items.append({"key": "Target Areas", "value": ", ".join(data.location.suggested_locations)})
                blocks = [
                    {"type": "badge", "label": data.business_type},
                    {"type": "key_value", "items": kv_items},
                    {"type": "divider"},
                    {"type": "callout", "text": "Generating marketing summary...", "variant": "info"},
                ]
                await stream.emit_craft(craft_id, craft_title, blocks, append=True)
                logger.info("Craft metadata: id=%s", craft_id)

            elif stage == "summary_delta" and data:
                await stream.emit_craft_text(craft_id, data)

            elif stage == "complete":
                logger.info("Craft complete: id=%s", craft_id)

        agent = get_scrape_pipeline()
        profile = await agent.run(url, progress_callback=progress, craft_callback=on_craft)

        # Store business info in session context
        session_ctx = context.get("session_context", {})
        session_ctx["product_data"] = profile.model_dump()
        session_ctx["_craft_id"] = craft_id

        # Build a concise summary for the LLM
        summary_parts = [
            f"Business: {profile.product_name}",
            f"Type: {profile.business_type}",
        ]
        if profile.location.location:
            summary_parts.append(f"Location: {profile.location.location}")
        if profile.location.suggested_locations:
            summary_parts.append(f"Suggested Ad Locations: {', '.join(profile.location.suggested_locations)}")
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


async def _analyze_product(params: dict, context: dict) -> ToolResult:
    """Spawn the Product Analyst agent to scrape + generate product profile.

    Thin bridge: cache check → spawn agent → persist → return.
    Falls back to _scrape_website if auth is missing or agent fails.
    """
    import time as _time
    _run_start = _time.monotonic()

    url = (params.get("url") or "").strip()
    if not url:
        return ToolResult(success=False, error="url is required.")
    # Force https:// at the input boundary so storage records, complete-event
    # payloads, and downstream API calls are all consistent. The previous
    # `not url.startswith("http")` check accepted `http://` as-is, leaving
    # records keyed under both schemes for the same business.
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    elif not url.startswith("https://"):
        url = f"https://{url}"

    stream = context.get("event_stream")
    tool_use_id = context.get("tool_use_id", "")
    auth = context.get("auth")
    session_ctx = context.get("session_context", {}) or {}

    # Return cached results if already analyzed.
    parent_session = context.get("_session")
    existing_business = (
        (parent_session.context.get("product_data") if parent_session else None)
        or session_ctx.get("product_data")
    )
    if existing_business:
        name = existing_business.get("product_name", "product")
        return ToolResult(
            success=True,
            data={"business": existing_business},
            summary=f"Already analyzed: {name}. No need to re-analyze.",
        )

    # Cross-session storage cache: same URL was analyzed in a previous session
    # (by adzump or ds/chatv2). Hydrate session.context and skip the scrape.
    try:
        from app.agents.adzump.services.business_storage import hydrate_from_storage
        from app.agents.adzump.tools.competitor import _emit_final_craft
        target_ctx = parent_session.context if parent_session else session_ctx
        hit = await hydrate_from_storage(url, target_ctx, context)
        if hit:
            business = target_ctx.get("product_data") or {}
            name = business.get("product_name", "product")
            await emit_progress(context, f"Reused stored analysis for {name}")

            # Re-render the craft panel with the hydrated data so the user
            # sees the same screenshot + summary + competitors UI they'd
            # get on a fresh scrape.
            craft_id = target_ctx.get("craft_id", "")
            if stream and craft_id:
                competitive = target_ctx.get("competitor_analysis") or {"competitors": []}
                try:
                    await _emit_final_craft(
                        stream, craft_id, url, business, competitive,
                        screenshot_url=(
                            business.get("primary_screenshot_url")
                            or business.get("screenshot_url")
                        ),
                        baked_summary=business.get("summary", ""),
                    )
                except Exception as e:
                    logger.warning("storage_hydrate_craft_failed: %s: %s",
                                   type(e).__name__, str(e)[:200])

            return ToolResult(
                success=True,
                data={"business": business, "from_storage": True},
                summary=(
                    f"Reused prior analysis for {name} from storage. "
                    f"Type: {business.get('business_type', '')}. "
                    f"Location: {business.get('location', '')}. "
                    "Tell the user we're picking up where things left off."
                ),
            )
    except Exception as e:
        logger.warning("storage_hydrate_skipped: %s: %s", type(e).__name__, str(e)[:200])

    if auth is None:
        logger.warning("analyze_product: no auth in context, falling back to scrape_website")
        return await _scrape_website(params, context)

    try:
        from app.agents.adzump.agents.product.agent import get_product_agent

        await emit_progress(context, "Starting product analysis…")
        output = await get_product_agent().analyze(
            url=url,
            parent_event_stream=stream,
            parent_tool_use_id=tool_use_id,
            auth=auth,
            parent_session_context=session_ctx,
            user_message=(
                f"Analyze this business website: {url}\n\n"
                "SCOPE: Scrape the homepage ONCE and generate a product profile. "
                "Do NOT scrape sub-pages — one scrape_url call only. "
                "Do NOT call web_search or web_fetch. "
                "After scraping, write the final JSON with the 'business' section filled "
                "and an empty 'competitive' section."
            ),
        )

        if not output.business:
            raise RuntimeError("Agent produced no usable result")

        business = output.business
        if output.screenshot_url:
            business["screenshot_url"] = output.screenshot_url

        # Persist to parent session.
        #
        # Ownership:
        #   product_data       — shared between this tool and the scrape sub-agent.
        #                        The sub-agent populates runtime artifacts during
        #                        scraping (site_links, primary_screenshot_url,
        #                        scrape_count, scraped_urls); this tool merges the
        #                        analyst's structured JSON on top. Keys in the JSON
        #                        (product_name, summary, business_type, etc.) win
        #                        on conflict; runtime-only keys are preserved. Do
        #                        NOT replace product_data wholesale — that wipes
        #                        the sub-agent's artifacts (site_links was empty
        #                        in storage records because of this).
        #   product_profile    — owned by the scrape sub-agent. Wholesale-set in
        #                        agents/product/tools/scrape.py:_generate_business_profile
        #                        with a rich, streamed GPT-4o marketing summary.
        #                        We only refresh url + title from the analysis
        #                        output; summary stays under the sub-agent's
        #                        ownership and must not be overwritten here.
        #   competitor_analysis — owned by this tool when competitive output exists.
        parent_session = context.get("_session")
        target_ctx = parent_session.context if parent_session else session_ctx
        target_ctx["product_data"] = {**(target_ctx.get("product_data") or {}), **business}
        profile = target_ctx.setdefault("product_profile", {})
        profile["url"] = url
        profile["title"] = business.get("product_name", "") or profile.get("title", "")
        # Seed summary only if the sub-agent didn't run (defensive fallback for
        # legacy callers that bypass the sub-agent). The storage save's own
        # fallback chain `profile.summary or product.summary` covers the same
        # case at a different layer; keeping this here too means downstream
        # readers of product_profile see something useful regardless of layer.
        if not profile.get("summary"):
            profile["summary"] = business.get("summary", "")
        if output.competitive and output.competitive.get("competitors"):
            target_ctx["competitor_analysis"] = output.competitive

        # Build summary for the LLM.
        summary_lines: list[str] = []
        if business.get("product_name"):
            summary_lines.append(f"Product: {business['product_name']}")
        if business.get("business_type"):
            summary_lines.append(f"Type: {business['business_type']}")
        loc = business.get("location") or ""
        if isinstance(loc, dict):
            loc = loc.get("location", "")
        if loc:
            summary_lines.append(f"Location: {loc}")
        if business.get("summary"):
            summary_lines.append(f"\n{business['summary']}")

        duration_ms = int((_time.monotonic() - _run_start) * 1000)
        if stream:
            try:
                await stream.emit_agent_finished(
                    agent_id="product_analyst",
                    status="success",
                    duration_ms=duration_ms,
                    summary=f"Analyzed {business.get('product_name', 'product')}",
                )
            except Exception:
                pass

        return ToolResult(
            success=True,
            data={"business": business},
            summary="\n".join(summary_lines) or "Product analysis complete.",
        )

    except Exception as e:
        logger.warning("analyze_product failed: %s: %s",
                       type(e).__name__, str(e)[:200])
        if stream:
            try:
                await stream.emit_agent_finished(
                    agent_id="product_analyst",
                    status="error",
                    summary=str(e)[:100],
                )
            except Exception:
                pass
        await emit_progress(context, "Using simpler pipeline…")
        return await _scrape_website(params, context)


# ── Tool definitions ──────────────────────────────────────────────────

analyze_business = ToolDefinition(
    name="analyze_product",
    description=(
        "Scrape a business website and generate a product profile: what they sell "
        "(all variants), location, pricing, USPs, target customer. This is the FIRST "
        "step when the user gives a URL. Does NOT do competitor research — that "
        "happens separately later. Returns a structured product profile."
    ),
    display_name="Analyze Product",
    parameters=[
        ToolParameter(name="url", type="string", description="The business website URL to analyze.", required=True),
    ],
    execute=_analyze_product,
)

scrape_website = ToolDefinition(
    name="scrape_website",
    description="Scrape a website to extract basic business information (name, type, description, products/services, USPs, contact info, location). Lower-level than analyze_business — prefer analyze_business unless you only need raw site content.",
    display_name="Scrape Website",
    parameters=[
        ToolParameter(name="url", type="string", description="The website URL to analyze", required=True),
    ],
    execute=_scrape_website,
)

BUSINESS_TOOLS = [analyze_business, scrape_website]
