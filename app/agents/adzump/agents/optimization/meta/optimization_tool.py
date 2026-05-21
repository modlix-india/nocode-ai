import asyncio
import logging
from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.agents.adzump.agents.optimization.meta.age_optimization_agent import (
    meta_age_optimization_agent,
)
from app.agents.adzump.agents.optimization.meta.services.campaign_mapping_service import (
    campaign_mapping_service,
)
from app.agents.adzump.agents.optimization.meta.services.recommendation_storage import (
    recommendation_storage_service,
)
from app.agents.adzump.agents.optimization.meta.models import (
    MetaOptimizationResponse,
)
from app.agents.adzump.adapters.meta.age import MetaAgeAdapter

logger = logging.getLogger(__name__)


def _format_markdown_response(
    response: MetaOptimizationResponse | dict,
    detailed_mapping: dict = None,
    from_cache: bool = False,
) -> str:
    source_prefix = "\n\n*(Sourced from storage cache)*\n\n" if from_cache else "\n\n"

    # Standardize response to MetaOptimizationResponse model at the boundary
    if isinstance(response, dict):
        try:
            opt_response = MetaOptimizationResponse.model_validate(response)
        except Exception as e:
            logger.warning(
                f"Failed to validate response dictionary into MetaOptimizationResponse: {e}"
            )
            opt_response = MetaOptimizationResponse(
                success=response.get("success", False),
                message=response.get("message", "Validation failed"),
                recommendations=[],
                errors=[{"error": f"Validation failed: {str(e)}"}],
            )
    elif isinstance(response, MetaOptimizationResponse):
        opt_response = response
    else:
        try:
            opt_response = MetaOptimizationResponse.model_validate(response)
        except Exception as e:
            logger.exception(f"Unexpected response object type {type(response)}: {e}")
            opt_response = MetaOptimizationResponse(
                success=False,
                message=f"Invalid response type: {type(response)}",
                recommendations=[],
                errors=[{"error": f"Invalid type: {str(e)}"}],
            )

    recommendations = opt_response.recommendations

    if not recommendations:
        return f"{source_prefix}### Meta Age Optimization\n\nNo age optimization recommendations were generated. Either targeting is already fully optimal or no eligible adsets were found."

    markdown = f"{source_prefix}### Meta Age Target Recommendations\n\n"
    for rec in recommendations:
        campaign_name = rec.campaign_name or "Unknown Campaign"
        campaign_id = rec.campaign_id

        # Get product name from detailed mappings if available
        product_name = "Unknown Product"
        if detailed_mapping and str(campaign_id) in detailed_mapping:
            product_name = detailed_mapping[str(campaign_id)].get(
                "product_name", "Unknown Product"
            )
        elif rec.product_id:
            product_name = rec.product_id

        markdown += f"**Product:** {product_name}\n"
        markdown += f"**Campaign:** {campaign_name} `({campaign_id})`\n"

        # Access fields using standardized Pydantic models
        age_recs = rec.fields.age or []
        if not age_recs:
            markdown += "  - No adset recommendations found for this campaign.\n\n"
            continue

        for age_rec in age_recs:
            adset_name = age_rec.adset_name or "Unknown Adset"
            current_min = age_rec.current_min
            current_max = age_rec.current_max
            rec_min = age_rec.recommended_min
            rec_max = age_rec.recommended_max
            reason = age_rec.reason or "No reason provided."

            markdown += f"##### Adset: *{adset_name}*\n"
            markdown += (
                f"- **Current Age Range:** `{current_min} - {current_max}` years\n"
            )
            markdown += f"- **Recommended Range:** `{rec_min} - {rec_max}` years\n"
            markdown += f"- **Reason for recommendation:** {reason}\n\n"
        markdown += "---\n\n"

    if opt_response.errors:
        markdown += "### Warnings & Errors:\n"
        for err in opt_response.errors:
            c_id = err.get("campaign_id", "")
            err_msg = err.get("error", "")
            markdown += f"- **Campaign ID {c_id}:** {err_msg}\n"

    return markdown


async def _emit_yes_no_prompt(event_stream, question: str, summary: str) -> ToolResult:
    await event_stream.emit_text(f"\n{question}\n")
    yes_no_suggestions = [
        {"label": "Yes", "value": "Yes"},
        {"label": "No", "value": "No"},
    ]
    await event_stream.emit_suggestions(options=yes_no_suggestions, mode="single")
    return ToolResult(success=True, summary=summary, data={"prompt_confirm": True})


async def _run_campaign_optimization(
    campaign_id: str,
    client_code: str,
    context: dict,
    event_stream,
    tool_use_id: str,
) -> MetaOptimizationResponse:
    """
    Helper to resolve account details, analyze age targeting, and persist results to DB.
    Returns a MetaOptimizationResponse object.
    """
    meta_adapter = MetaAgeAdapter()
    try:
        auth_headers = context.get("headers") or context.get("auth_headers", {})
        resolved = await meta_adapter.resolve_account_details(
            campaign_id=campaign_id,
            client_code=client_code,
            auth_headers=auth_headers,
        )
        ad_account_id = resolved["ad_account_id"]
        business_id = resolved["business_id"]
    except Exception as e:
        logger.exception(
            f"Failed to resolve account details for campaign {campaign_id}"
        )
        return MetaOptimizationResponse(
            success=False,
            message=f"Could not resolve account details for campaign: {str(e)}",
            recommendations=[],
            errors=[
                {
                    "campaign_id": campaign_id,
                    "error": f"Failed to resolve account details: {str(e)}",
                }
            ],
        )

    optimization_response = await meta_age_optimization_agent.analyze(
        context=context,
        parent_event_stream=event_stream,
        parent_tool_use_id=tool_use_id,
        campaign_id=campaign_id,
        ad_account_id=ad_account_id,
        business_id=business_id,
    )

    # Save generated recommendations to storage
    if optimization_response.success:
        for recommendation in optimization_response.recommendations:
            try:
                await recommendation_storage_service.store(
                    recommendation=recommendation,
                    client_code=client_code,
                    context=context,
                )
            except Exception as e:
                logger.exception(f"Failed to persist recommendation: {e}")

    return optimization_response


async def optimize_meta_age(parameters: dict, context: dict) -> ToolResult:
    """
    Run an agentic analysis of Meta Ads age performance and generate recommendations.
    Supports case-insensitive campaign name or product name filters.
    Bypasses costly scans using cached lookup or prompts for user confirmation.
    """
    event_stream = context.get("event_stream")
    tool_use_id = context.get("tool_use_id", "root")
    client_code = context.get("client_code")

    campaign_name = parameters.get("campaign_name")
    product_name = parameters.get("product_name")
    confirm_generate = parameters.get("confirm_generate")
    force_refresh = bool(parameters.get("force_refresh", False))

    if not client_code:
        logger.error("Missing client_code in authentication context")
        return ToolResult(
            success=False, error="Missing client_code in authentication context"
        )

    if not event_stream:
        logger.error("No event stream found in context")
        return ToolResult(success=False, error="No event stream found in context")

    try:
        # --- 1. Load Campaign Mappings for Resolution ---
        detailed_mapping = (
            await campaign_mapping_service.get_campaign_mapping_with_summary(
                client_code, context
            )
        )

        if detailed_mapping:
            detailed_mapping = {
                c_id: mapping
                for c_id, mapping in detailed_mapping.items()
                if str(mapping.get("platform", "META")).upper() == "META"
            }

        if not detailed_mapping:
            msg = "No active Meta campaigns were found linked to your account. Please ensure your campaigns are connected first."
            await event_stream.emit_text(f"\n{msg}\n")
            return ToolResult(success=False, error=msg)

        confirm_yes = bool(
            confirm_generate and str(confirm_generate).strip().lower() == "yes"
        )
        confirm_no = bool(
            confirm_generate and str(confirm_generate).strip().lower() == "no"
        )

        # --- 2. Check for Specific Target Request ---
        is_specific_request = bool(campaign_name or product_name)

        # --- PATH A: General Request (No campaign or product filter) ---
        if not is_specific_request:
            stored_recs = (
                None
                if force_refresh
                else (
                    await recommendation_storage_service.fetch_all_active_recommendations(
                        client_code, context
                    )
                )
            )

            if stored_recs and not force_refresh:
                optimization_response = MetaOptimizationResponse(
                    success=True,
                    message="Retrieved active age recommendations directly from storage.",
                    recommendations=stored_recs,
                )
                markdown_content = _format_markdown_response(
                    optimization_response,
                    detailed_mapping=detailed_mapping,
                    from_cache=True,
                )
                await event_stream.emit_text(markdown_content)
                return ToolResult(
                    success=True,
                    summary=f"Retrieved {len(stored_recs)} active age recommendations directly from storage dashboard.",
                    data=optimization_response.model_dump(),
                )
            else:
                if confirm_yes or force_refresh:
                    logger.info(
                        "Executing global account-level targeting optimization analysis"
                    )
                    await event_stream.emit_thinking(
                        "Analyzing all campaigns and adsets across your ad account..."
                    )

                    optimization_response = await meta_age_optimization_agent.analyze(
                        context=context,
                        parent_event_stream=event_stream,
                        parent_tool_use_id=tool_use_id,
                        campaign_id=None,
                    )

                    # Save generated recommendations to storage
                    if optimization_response.success:
                        for recommendation in optimization_response.recommendations:
                            try:
                                await recommendation_storage_service.store(
                                    recommendation=recommendation,
                                    client_code=client_code,
                                    context=context,
                                )
                            except Exception as e:
                                logger.exception(
                                    f"Failed to persist recommendation: {e}"
                                )

                    markdown_content = _format_markdown_response(
                        optimization_response,
                        detailed_mapping=detailed_mapping,
                        from_cache=False,
                    )
                    await event_stream.emit_text(markdown_content)

                    return ToolResult(
                        success=optimization_response.success,
                        summary=optimization_response.message,
                        data=optimization_response.model_dump(),
                    )
                elif confirm_no:
                    msg = "Okay, I will not generate recommendations at this time."
                    await event_stream.emit_text(f"\n{msg}\n")
                    return ToolResult(success=True, summary=msg)
                else:
                    msg = "No age optimization recommendations found in storage. Would you like to generate them now?"
                    return await _emit_yes_no_prompt(
                        event_stream=event_stream,
                        question=msg,
                        summary="No active recommendations in storage. Prompted user with Yes/No to generate.",
                    )

        # --- PATH B: Specific Campaign Request ---
        elif campaign_name:
            campaign_name_stripped = str(campaign_name).strip().lower()
            exact_campaign_id = None
            exact_campaign_name = None

            # 1. Exact match search
            for c_id, mapping in detailed_mapping.items():
                mapped_name = str(mapping.get("campaign_name", "")).strip().lower()
                if mapped_name == campaign_name_stripped:
                    exact_campaign_id = c_id
                    exact_campaign_name = mapping.get("campaign_name", "")
                    break

            # 2. Substring matching search
            if not exact_campaign_id:
                matched_campaigns = []
                for c_id, mapping in detailed_mapping.items():
                    mapped_name = str(mapping.get("campaign_name", "")).strip().lower()
                    if campaign_name_stripped in mapped_name:
                        matched_campaigns.append(
                            (c_id, mapping.get("campaign_name", ""))
                        )

                if len(matched_campaigns) == 1:
                    exact_campaign_id, exact_campaign_name = matched_campaigns[0]
                elif len(matched_campaigns) > 1:
                    options = [
                        {"label": name, "value": name} for _, name in matched_campaigns
                    ]
                    msg = f"I found multiple campaigns matching '{campaign_name}'. Please select the one you meant:"
                    await event_stream.emit_text(f"\n{msg}\n")
                    await event_stream.emit_suggestions(options=options, mode="single")
                    return ToolResult(
                        success=True,
                        summary="Multiple campaign matches found. Presented interactive choices to the user.",
                        data={"suggestions": options},
                    )
                else:
                    msg = f"No campaigns matching '{campaign_name}' were found."
                    await event_stream.emit_text(f"\n{msg}\n")
                    return ToolResult(
                        success=False,
                        summary=f"No campaign matches found for '{campaign_name}'.",
                        error=msg,
                    )

            # Resolved uniquely to exact_campaign_id and exact_campaign_name!
            cached_rec = (
                None
                if force_refresh
                else (
                    await recommendation_storage_service.fetch_active_recommendation(
                        campaign_id=exact_campaign_id,
                        client_code=client_code,
                        context=context,
                    )
                )
            )

            if cached_rec and not force_refresh:
                optimization_response = MetaOptimizationResponse(
                    success=True,
                    message=f"Retrieved active age recommendation for '{exact_campaign_name}' directly from storage.",
                    recommendations=[cached_rec],
                )
                markdown_content = _format_markdown_response(
                    optimization_response,
                    detailed_mapping=detailed_mapping,
                    from_cache=True,
                )
                await event_stream.emit_text(markdown_content)
                return ToolResult(
                    success=True,
                    summary=f"Loaded active recommendation for campaign '{exact_campaign_name}' from storage.",
                    data=optimization_response.model_dump(),
                )
            else:
                if confirm_yes or force_refresh:
                    await event_stream.emit_thinking(
                        f"Resolving account details and running targeted analysis for '{exact_campaign_name}'..."
                    )

                    optimization_response = await _run_campaign_optimization(
                        campaign_id=exact_campaign_id,
                        client_code=client_code,
                        context=context,
                        event_stream=event_stream,
                        tool_use_id=tool_use_id,
                    )

                    markdown_content = _format_markdown_response(
                        optimization_response,
                        detailed_mapping=detailed_mapping,
                        from_cache=False,
                    )
                    await event_stream.emit_text(markdown_content)

                    return ToolResult(
                        success=optimization_response.success,
                        summary=optimization_response.message,
                        data=optimization_response.model_dump(),
                    )
                elif confirm_no:
                    msg = f"Okay, I will not generate recommendation for '{exact_campaign_name}' at this time."
                    await event_stream.emit_text(f"\n{msg}\n")
                    return ToolResult(success=True, summary=msg)
                else:
                    msg = f"No recommendation found in storage for '{exact_campaign_name}'. Would you like to generate one?"
                    return await _emit_yes_no_prompt(
                        event_stream=event_stream,
                        question=msg,
                        summary="No active recommendation in storage. Prompted user with Yes/No to generate.",
                    )

        # --- PATH C: Specific Product Request ---
        elif product_name:
            product_name_stripped = str(product_name).strip().lower()
            exact_product_name = None
            matched_campaigns_for_product = []

            # 1. Exact match check
            for c_id, mapping in detailed_mapping.items():
                mapped_prod_name = str(mapping.get("product_name", "")).strip().lower()
                if mapped_prod_name == product_name_stripped:
                    exact_product_name = mapping.get("product_name", "")
                    matched_campaigns_for_product.append((c_id, mapping))

            # 2. Substring matching check
            if not exact_product_name:
                matched_products = set()
                for c_id, mapping in detailed_mapping.items():
                    mapped_prod_name = (
                        str(mapping.get("product_name", "")).strip().lower()
                    )
                    if product_name_stripped in mapped_prod_name:
                        matched_products.add(mapping.get("product_name", ""))

                matched_products = list(matched_products)
                if len(matched_products) == 1:
                    exact_product_name = matched_products[0]
                    for c_id, mapping in detailed_mapping.items():
                        if (
                            str(mapping.get("product_name", "")).strip().lower()
                            == exact_product_name.strip().lower()
                        ):
                            matched_campaigns_for_product.append((c_id, mapping))
                elif len(matched_products) > 1:
                    options = [
                        {"label": prod, "value": prod} for prod in matched_products
                    ]
                    msg = f"I found multiple products matching '{product_name}'. Please select the one you meant:"
                    await event_stream.emit_text(f"\n{msg}\n")
                    await event_stream.emit_suggestions(options=options, mode="single")
                    return ToolResult(
                        success=True,
                        summary="Multiple product matches found. Presented interactive choices to the user.",
                        data={"suggestions": options},
                    )
                else:
                    msg = f"No products matching '{product_name}' were found."
                    await event_stream.emit_text(f"\n{msg}\n")
                    return ToolResult(
                        success=False,
                        summary=f"No product matches found for '{product_name}'.",
                        error=msg,
                    )

            # Resolved uniquely to exact_product_name and matched_campaigns_for_product!
            cached_recommendations = []
            missing_campaign_ids = []
            missing_campaign_names = []

            for c_id, mapping in matched_campaigns_for_product:
                cached_rec = (
                    None
                    if force_refresh
                    else (
                        await recommendation_storage_service.fetch_active_recommendation(
                            campaign_id=c_id, client_code=client_code, context=context
                        )
                    )
                )
                if cached_rec and not force_refresh:
                    cached_recommendations.append(cached_rec)
                else:
                    missing_campaign_ids.append(c_id)
                    missing_campaign_names.append(mapping.get("campaign_name", c_id))

            if not missing_campaign_ids and not force_refresh:
                optimization_response = MetaOptimizationResponse(
                    success=True,
                    message=f"Retrieved active age recommendations for product '{exact_product_name}' directly from storage.",
                    recommendations=cached_recommendations,
                )
                markdown_content = _format_markdown_response(
                    optimization_response,
                    detailed_mapping=detailed_mapping,
                    from_cache=True,
                )
                await event_stream.emit_text(markdown_content)
                return ToolResult(
                    success=True,
                    summary=f"Loaded all active recommendations for product '{exact_product_name}' from storage.",
                    data=optimization_response.model_dump(),
                )
            else:
                if confirm_yes or force_refresh:
                    await event_stream.emit_thinking(
                        f"Resolving account details and running targeted analysis for {len(missing_campaign_ids)} campaign(s) linked to '{exact_product_name}'..."
                    )

                    all_new_recommendations = []
                    all_errors = []

                    tasks = [
                        _run_campaign_optimization(
                            campaign_id=c_id,
                            client_code=client_code,
                            context=context,
                            event_stream=event_stream,
                            tool_use_id=tool_use_id,
                        )
                        for c_id in missing_campaign_ids
                    ]
                    results = await asyncio.gather(*tasks)

                    for res, c_id in zip(results, missing_campaign_ids):
                        if res and res.success:
                            all_new_recommendations.extend(res.recommendations)
                        else:
                            err_msg = (
                                res.message
                                if res
                                else "Failed during targeted execution."
                            )
                            all_errors.append({"campaign_id": c_id, "error": err_msg})

                    combined_response = MetaOptimizationResponse(
                        success=True,
                        message=f"Age optimization complete for product '{exact_product_name}'.",
                        recommendations=cached_recommendations
                        + all_new_recommendations,
                        errors=all_errors,
                    )

                    markdown_content = _format_markdown_response(
                        combined_response,
                        detailed_mapping=detailed_mapping,
                        from_cache=False,
                    )
                    await event_stream.emit_text(markdown_content)

                    return ToolResult(
                        success=True,
                        summary=f"Processed product '{exact_product_name}' recommendations (Cached: {len(cached_recommendations)}, New: {len(all_new_recommendations)}).",
                        data=combined_response.model_dump(),
                    )

                elif confirm_no:
                    if cached_recommendations:
                        optimization_response = MetaOptimizationResponse(
                            success=True,
                            message=f"Retrieved active recommendations for product '{exact_product_name}' from storage.",
                            recommendations=cached_recommendations,
                        )
                        markdown_content = _format_markdown_response(
                            optimization_response,
                            detailed_mapping=detailed_mapping,
                            from_cache=True,
                        )
                        await event_stream.emit_text(markdown_content)

                    msg = f"Okay, I will not generate the remaining {len(missing_campaign_ids)} missing recommendations for product '{exact_product_name}' at this time."
                    await event_stream.emit_text(f"\n{msg}\n")
                    return ToolResult(success=True, summary=msg)

                else:
                    if cached_recommendations:
                        optimization_response = MetaOptimizationResponse(
                            success=True,
                            message=f"Retrieved active recommendations for product '{exact_product_name}' from storage.",
                            recommendations=cached_recommendations,
                        )
                        markdown_content = _format_markdown_response(
                            optimization_response,
                            detailed_mapping=detailed_mapping,
                            from_cache=True,
                        )
                        await event_stream.emit_text(markdown_content)

                    msg = f"Recommendations missing for {len(missing_campaign_ids)} campaign(s) linked to '{exact_product_name}'. Would you like to generate them?"
                    return await _emit_yes_no_prompt(
                        event_stream=event_stream,
                        question=msg,
                        summary=f"Missing recommendations for {len(missing_campaign_ids)} campaigns under product. Prompted user with Yes/No to generate.",
                    )

    except Exception as exception:
        logger.exception("Unexpected error during meta age optimization")
        return ToolResult(success=False, error=f"Optimization failed: {str(exception)}")


OPTIMIZATION_TOOLS = [
    ToolDefinition(
        name="optimize_meta_age",
        description="Analyzes Meta Ads age breakdown performance and suggests optimizations. Supports case-insensitive campaign name or product name filters.",
        parameters=[
            ToolParameter(
                name="campaign_name",
                type="string",
                description="Optional: Specific Meta Campaign Name to optimize (resolved case-insensitively).",
                required=False,
            ),
            ToolParameter(
                name="product_name",
                type="string",
                description="Optional: Specific internal Product Name to resolve linked campaigns.",
                required=False,
            ),
            ToolParameter(
                name="confirm_generate",
                type="string",
                description="Optional: Interactive user response ('Yes' or 'No') to confirm dynamic generation.",
                required=False,
            ),
            ToolParameter(
                name="force_refresh",
                type="boolean",
                description="Optional: Force refresh to bypass cache and run optimization directly.",
                required=False,
            ),
        ],
        execute=optimize_meta_age,
    )
]
