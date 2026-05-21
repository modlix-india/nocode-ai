import asyncio
import json
import logging
from typing import Any
from app.core.agent import BaseAgent
from app.core.session import BaseSession, AuthContext
from app.core.streaming import AgentEventStream
from app.core.context import BaseContext
from app.agents.adzump.agents.optimization.meta.tools.meta_optimization_tools import (
    META_OPTIMIZATION_TOOLS,
    list_meta_business_accounts,
    list_meta_ad_accounts,
    fetch_meta_age_metrics,
)
from app.agents.adzump.agents.optimization.meta.models import (
    MetaOptimizationResponse,
    MetaCampaignRecommendation,
    MetaOptimizationFields,
    MetaAgeFieldRecommendation,
    MetaAgeAIResponse,
)
from app.agents.adzump.tools._shared import extract_json
from app.agents.adzump.agents.optimization.meta.meta_age_prompt import (
    META_AGE_SYSTEM_PROMPT,
)
from app.config import settings

logger = logging.getLogger(__name__)


class _SilentQueue(asyncio.Queue):
    """A queue that silently discards all items put into it."""

    async def put(self, item: Any) -> None:
        pass


class SilentStream(AgentEventStream):
    """A high-performance dummy stream that silently discards all events."""

    def __init__(self) -> None:
        super().__init__()
        self._queue = _SilentQueue()


class _PassthroughEventStream(AgentEventStream):
    """Forwards sub-agent progress to the parent stream using dynamic delegation."""

    def __init__(self, parent: AgentEventStream, parent_tool_use_id: str) -> None:
        super().__init__()
        self._parent = parent
        self._parent_tool_use_id = parent_tool_use_id

    @property
    def is_cancelled(self) -> bool:
        return getattr(self._parent, "is_cancelled", False)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._parent, name)


class MetaAgeOptimizationAgent(BaseAgent):
    """Agent for analyzing Meta Ads age performance and suggesting targeting improvements."""

    display_name = "Meta Age Optimizer"

    def __init__(self) -> None:
        agent_context = BaseContext(doc_paths=[], static_prefix=META_AGE_SYSTEM_PROMPT)
        agent_context._cached_static_text = META_AGE_SYSTEM_PROMPT

        provider = getattr(settings, "ADZUMP_PROVIDER", settings.LLM_PROVIDER)

        super().__init__(
            name="meta_age_optimizer",
            tools=META_OPTIMIZATION_TOOLS,
            context_builder=agent_context,
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=settings.MAX_AGENT_TURNS,
            provider=provider,
        )

    async def analyze(
        self,
        context: dict,
        parent_event_stream: AgentEventStream,
        parent_tool_use_id: str = "root",
        campaign_id: str | None = None,
        ad_account_id: str | None = None,
        business_id: str | None = None,
    ) -> MetaOptimizationResponse:
        """Run the data gathering and analysis loop."""
        client_code = context.get("client_code")
        authentication_headers = context.get("headers") or context.get(
            "auth_headers", {}
        )
        authorization_token = (
            authentication_headers.get("Authorization", "")
            .replace("Bearer ", "")
            .strip()
        )

        authentication = AuthContext(
            token=authorization_token,
            client_code=client_code,
            client_id=context.get("client_id", 0),
            user_id=context.get("user_id", 0),
            app_code=context.get("app_code", "marketingai"),
        )

        sub_session = BaseSession(agent_name=self.name)
        await sub_session.get_or_create(None, authentication)

        # Build clean session context
        skipped_keys = {"event_stream", "headers", "auth_headers"}
        sub_session.context = {
            key: value
            for key, value in context.items()
            if key not in skipped_keys
            and isinstance(value, (str, int, float, bool, list, dict, type(None)))
        }

        wrapped_stream = _PassthroughEventStream(
            parent_event_stream, parent_tool_use_id
        )

        # --- DATA GATHERING ---
        status_message = (
            f"Gathering metrics for campaign {campaign_id}..."
            if campaign_id
            else "Gathering Meta ad accounts and performance metrics..."
        )
        await wrapped_stream.emit_thinking(status_message)

        collected_adset_data = []
        try:
            if ad_account_id and business_id:
                # Bypass global scan, query specific ad account directly!
                metrics_result = await fetch_meta_age_metrics(
                    {"ad_account_id": ad_account_id}, sub_session.context
                )
                if metrics_result.success and metrics_result.data:
                    current_adsets = metrics_result.data.get("adsets", [])

                    if campaign_id:
                        current_adsets = [
                            adset
                            for adset in current_adsets
                            if str(adset.get("campaign_id")) == str(campaign_id)
                        ]

                    for adset in current_adsets:
                        adset["parent_account_id"] = business_id
                        adset["account_id"] = ad_account_id
                    collected_adset_data.extend(current_adsets)
            else:
                # 1. Get Business Accounts
                business_result = await list_meta_business_accounts(
                    {}, sub_session.context
                )
                if not business_result.success:
                    return MetaOptimizationResponse(
                        success=False,
                        message=f"Failed to list businesses: {business_result.error}",
                    )

                business_accounts = business_result.data or []
                for business_account in business_accounts:
                    business_id = business_account.get("id")
                    # 2. Get Ad Accounts
                    ad_account_result = await list_meta_ad_accounts(
                        {"business_id": business_id}, sub_session.context
                    )
                    if not ad_account_result.success:
                        continue

                    ad_accounts = ad_account_result.data or []
                    for ad_account in ad_accounts:
                        ad_account_id = ad_account.get("id")
                        # 3. Get Age Metrics
                        metrics_result = await fetch_meta_age_metrics(
                            {"ad_account_id": ad_account_id}, sub_session.context
                        )
                        if metrics_result.success and metrics_result.data:
                            current_adsets = metrics_result.data.get("adsets", [])

                            if campaign_id:
                                current_adsets = [
                                    adset
                                    for adset in current_adsets
                                    if str(adset.get("campaign_id")) == str(campaign_id)
                                ]

                            for adset in current_adsets:
                                adset["parent_account_id"] = business_id
                                adset["account_id"] = ad_account_id
                            collected_adset_data.extend(current_adsets)

        except Exception as exception:
            logger.exception("Error during data gathering")
            return MetaOptimizationResponse(
                success=False, message=f"Error gathering metrics: {str(exception)}"
            )

        if not collected_adset_data:
            target_label = f"campaign {campaign_id}" if campaign_id else "any campaigns"
            return MetaOptimizationResponse(
                success=True,
                message=f"No adsets with sufficient age performance data were found for {target_label}.",
                recommendations=[],
            )

        # --- AI ANALYSIS (BATCH BY CAMPAIGN) ---
        campaign_batches = {}
        for adset in collected_adset_data:
            c_id = adset.get("campaign_id", "unknown")
            if c_id not in campaign_batches:
                campaign_batches[c_id] = []
            campaign_batches[c_id].append(adset)

        await wrapped_stream.emit_thinking(
            f"Analyzing {len(collected_adset_data)} adsets across {len(campaign_batches)} campaigns..."
        )

        semaphore = asyncio.Semaphore(5)

        async def analyze_campaign_batch(
            batch_campaign_id: str, batch_adsets: list
        ) -> str | None:
            schema_instructions = """
Return your results STRICTLY in the following JSON format:
{
  "recommendations": [
    {
      "adset_id": "string",
      "recommended_age_min": integer,
      "recommended_age_max": integer,
      "reason": "string"
    }
  ]
}
"""
            analysis_prompt = (
                f"Analyze the {len(batch_adsets)} adsets for campaign {batch_campaign_id}.\n\n"
                f"DATA:\n{json.dumps(batch_adsets, indent=2)}\n\n"
                f"{schema_instructions}"
            )

            async with semaphore:
                batch_session = BaseSession(agent_name=self.name)
                await batch_session.get_or_create(None, authentication)
                batch_session.context = dict(sub_session.context)

                try:
                    await self.run(
                        user_message=analysis_prompt,
                        session=batch_session,
                        event_stream=SilentStream(),
                        parent_tool_use_id=parent_tool_use_id,
                    )
                except Exception as e:
                    logger.error(
                        f"Error during campaign {batch_campaign_id} analysis: {e}"
                    )
                    return None

            for message in reversed(batch_session.get_messages()):
                if message.get("role") == "assistant":
                    content = message.get("content")
                    if isinstance(content, str):
                        logger.info(
                            f"LLM Output for campaign {batch_campaign_id}: {content}"
                        )
                        return content
                    elif isinstance(content, list):
                        text = "\n".join(
                            item.get("text", "")
                            for item in content
                            if item.get("type") == "text"
                        )
                        logger.info(
                            f"LLM Output for campaign {batch_campaign_id}: {text}"
                        )
                        return text

            logger.warning(
                f"No assistant message found for campaign {batch_campaign_id}"
            )
            return None

        # Execute all batches concurrently
        tasks = [
            analyze_campaign_batch(c_id, adsets)
            for c_id, adsets in campaign_batches.items()
        ]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        logger.info(f"Batch results: {batch_results}")

        return self._map_ai_results_to_campaigns(batch_results, collected_adset_data)

    def _map_ai_results_to_campaigns(
        self, batch_results: list, original_adsets: list[dict]
    ) -> MetaOptimizationResponse:
        """Map simplified AI responses back to the original dictionary structure."""
        adset_map = {str(adset.get("adset_id")): adset for adset in original_adsets}
        campaign_groups: dict[str, dict] = {}

        for result in batch_results:
            if not result or isinstance(result, Exception):
                continue

            parsed_data = extract_json(result)
            if not parsed_data:
                continue

            try:
                ai_response = MetaAgeAIResponse.model_validate(parsed_data)
            except Exception as e:
                logger.warning(f"AI response failed schema validation: {e}")
                continue

            for rec in ai_response.recommendations:
                adset_id = str(rec.adset_id).strip()
                if not adset_id or adset_id not in adset_map:
                    continue

                original_data = adset_map[adset_id]
                campaign_id = str(original_data.get("campaign_id", ""))

                if campaign_id not in campaign_groups:
                    campaign_groups[campaign_id] = {
                        "campaign_id": campaign_id,
                        "campaign_name": original_data.get("campaign_name", ""),
                        "campaign_type": original_data.get(
                            "campaign_objective", "UNKNOWN"
                        ),
                        "account_id": original_data.get("account_id", ""),
                        "parent_account_id": original_data.get("parent_account_id", ""),
                        "product_id": original_data.get("product_id"),
                        "adsets": [],
                    }

                try:
                    recommended_min = int(rec.recommended_age_min)
                    recommended_max = int(rec.recommended_age_max)

                    orig_min = original_data.get("current_min")
                    current_min = int(orig_min) if orig_min is not None else 18
                    orig_max = original_data.get("current_max")
                    current_max = int(orig_max) if orig_max is not None else 65

                    if recommended_min >= recommended_max or (
                        recommended_min == current_min
                        and recommended_max == current_max
                    ):
                        continue

                    age_recommendation = MetaAgeFieldRecommendation(
                        adset_id=adset_id,
                        adset_name=original_data.get("adset_name", ""),
                        current_min=current_min,
                        current_max=current_max,
                        recommended_min=recommended_min,
                        recommended_max=recommended_max,
                        reason=rec.reason or "No reason provided.",
                        applied=False,
                    )
                    campaign_groups[campaign_id]["adsets"].append(age_recommendation)
                except Exception as e:
                    logger.error(
                        f"Failed to parse mapped recommendation row for {adset_id}: {e}"
                    )

        final_recommendations: list[MetaCampaignRecommendation] = []
        for group_data in campaign_groups.values():
            if not group_data["adsets"]:
                continue
            try:
                campaign_recommendation = MetaCampaignRecommendation(
                    platform="META",
                    parent_account_id=group_data["parent_account_id"],
                    account_id=group_data["account_id"],
                    product_id=group_data["product_id"],
                    campaign_id=group_data["campaign_id"],
                    campaign_name=group_data["campaign_name"],
                    campaign_type=group_data["campaign_type"],
                    completed=False,
                    fields=MetaOptimizationFields(age=group_data["adsets"]),
                )
                final_recommendations.append(campaign_recommendation)
            except Exception as exception:
                logger.error(f"Failed to build campaign recommendation: {exception}")

        return MetaOptimizationResponse(
            success=True,
            message=f"Generated {len(final_recommendations)} Meta age optimization recommendations.",
            recommendations=final_recommendations,
        )


meta_age_optimization_agent = MetaAgeOptimizationAgent()
