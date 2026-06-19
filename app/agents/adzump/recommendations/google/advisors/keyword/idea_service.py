"""Keyword Idea Service.

Orchestrates the LLM-powered generation, selection, and scoring of new
keyword recommendations for a campaign. Combines Google Ads Keyword Planner
suggestions, Native Recommendations API suggestions, and LLM reasoning.
"""

from typing import Any, Dict, List, Set

import logging

from app.core.session import BaseSession

from app.agents.adzump.adapters.google.planner import keyword_planner_adapter
from app.agents.adzump.adapters.google.recommendations import (
    google_recommendations_adapter,
    RecommendationType,
)
from app.agents.adzump.recommendations.models import GoogleKeywordRecommendation
from app.agents.adzump.recommendations.google.advisors._utils import clean_and_load_json
from app.agents.adzump.recommendations.google.advisors.keyword.seed_expander import (
    KeywordSeedExpander,
)
from app.agents.adzump.recommendations.google.advisors.keyword.scorer import (
    assign_ad_groups,
    calculate_semantic_scores,
    score_and_rank_keywords,
)
from app.services.llm_provider import get_llm_provider
from app.agents.adzump.utils.prompt_loader import load_prompt

logger = logging.getLogger(__name__)


class KeywordIdeaService:
    # Semantic scoring
    DEFAULT_SEMANTIC_SCORE: float = 50.0
    MAX_ANCHOR_KEYWORDS: int = 20

    # Prompt formatting
    MAX_SUGGESTIONS_IN_PROMPT: int = 50
    MAX_ENTRIES_FOR_AD_GROUP_FORMAT: int = 50

    # Keyword cap
    MIN_KEYWORDS_PER_CAMPAIGN: int = 15
    KEYWORDS_PER_AD_GROUP: int = 5

    # Native recommendation impact score
    IMPACT_UPLIFT_CAP: float = 50.0
    IMPACT_SCORE_DECIMALS: int = 1

    # Fallback defaults for unhydrated native recs
    FALLBACK_VOLUME: int = 0
    FALLBACK_COMPETITION: str = "UNKNOWN"
    FALLBACK_COMPETITION_INDEX: float = 0.0
    FALLBACK_MATCH_TYPE: str = "BROAD"

    def __init__(self):
        self.seed_expander = KeywordSeedExpander()
        self.keyword_planner = keyword_planner_adapter

    async def suggest_keywords(
        self,
        campaign_details: Dict[str, Any],
        account_id: str,
        parent_id: str,
        client_code: str,
        context: Dict[str, Any],
        session: BaseSession | None = None,
    ) -> List[GoogleKeywordRecommendation]:
        """Generate and select new keywords using multiple sources."""
        keywords = campaign_details.get("entries", [])
        good_kws = [
            e["keyword"] for e in keywords if e.get("strength") in ("good", "top")
        ]
        if not good_kws:
            return []

        top_kws = [e["keyword"] for e in keywords if e.get("strength") == "top"]
        brand_name = context.get("brand_name", "")
        business_type = context.get("business_type", "")
        primary_location = context.get("primary_location", "")
        features_context = ", ".join(context.get("unique_features", []))

        expanded_seeds = await self.seed_expander.expand_seeds(
            good_keywords=good_kws,
            business_type=business_type,
            primary_location=primary_location,
            features_context=features_context,
            brand_name=brand_name,
            session=session,
        )

        auth_headers = context.get("headers", {})

        google_suggestions = (
            await self.keyword_planner.generate_keyword_ideas(
                customer_id=account_id,
                login_customer_id=parent_id,
                client_code=client_code,
                seed_keywords=expanded_seeds,
                url=context.get("url"),
                auth_headers=auth_headers,
            )
            or []
        )

        # Fetch native recommendations & hydrate
        hydrated_native = await self._fetch_and_hydrate_native_recommendations(
            account_id=account_id,
            parent_id=parent_id,
            client_code=client_code,
            campaign_id=campaign_details.get("campaign_id", ""),
            auth_headers=auth_headers,
        )

        # Combine suggestions
        all_suggestions = google_suggestions + hydrated_native
        if not all_suggestions:
            return []

        # Deduplicate by keyword text — keep highest-quality entry:
        # prefer native recommendation (has impact_score) over Planner, then higher volume.
        best_by_text: dict[str, dict] = {}
        for s in all_suggestions:
            text = s["keyword"].lower()
            if text not in best_by_text:
                best_by_text[text] = s
            else:
                existing = best_by_text[text]
                s_has_impact = s.get("impact_score") is not None
                ex_has_impact = existing.get("impact_score") is not None
                if s_has_impact and not ex_has_impact:
                    best_by_text[text] = s
                elif not s_has_impact and not ex_has_impact:
                    if (s.get("volume") or 0) > (existing.get("volume") or 0):
                        best_by_text[text] = s
        unique_suggestions = list(best_by_text.values())

        suggestion_keywords = [s["keyword"].strip().lower() for s in unique_suggestions]
        semantic_scores = await calculate_semantic_scores(
            [s["keyword"] for s in unique_suggestions],
            top_kws or good_kws[: self.MAX_ANCHOR_KEYWORDS],
        )
        for s, normalized_key in zip(unique_suggestions, suggestion_keywords):
            s["semantic_score"] = semantic_scores.get(normalized_key, self.DEFAULT_SEMANTIC_SCORE)

        llm_selected = await self._llm_select_keywords(
            unique_suggestions,
            campaign_details,
            context,
            session=session,
        )
        if not llm_selected:
            return []

        ad_group_map = await assign_ad_groups(
            [kw.get("keyword", "") for kw in llm_selected], keywords
        )
        for kw in llm_selected:
            ag = ad_group_map.get(kw.get("keyword", "").lower(), {})
            kw["ad_group_id"] = ag.get("ad_group_id")
            kw["ad_group_name"] = ag.get("ad_group_name")

        ad_group_ids = {e.get("ad_group_id") for e in keywords if e.get("ad_group_id")}
        ad_group_count = max(len(ad_group_ids), 1)

        return self._build_recommendations(
            llm_selected,
            unique_suggestions,
            existing_kws={e["keyword"].lower() for e in keywords},
            ad_group_count=ad_group_count,
        )

    async def _fetch_and_hydrate_native_recommendations(
        self,
        account_id: str,
        parent_id: str,
        client_code: str,
        campaign_id: str,
        auth_headers: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """Fetch KEYWORD recommendations from Google Ads and hydrate metrics."""
        native_suggestions = await google_recommendations_adapter.fetch_recommendations(
            customer_id=account_id,
            login_customer_id=parent_id,
            client_code=client_code,
            auth_headers=auth_headers,
            recommendation_type=RecommendationType.KEYWORD,
        )
        if campaign_id:
            campaign_resource = f"customers/{account_id}/campaigns/{campaign_id}"
            native_suggestions = [
                rec for rec in native_suggestions
                if rec.get("campaign") == campaign_resource
            ]

        hydrated_native = []
        if native_suggestions:
            for rec in native_suggestions:
                details = rec.get("details", {})
                kw_info = details.get("keyword", {})
                text = kw_info.get("text")
                if not text:
                    continue

                # Fetch historical metrics for this specific keyword
                metrics = (
                    await self.keyword_planner.generate_historical_metrics(
                        customer_id=account_id,
                        login_customer_id=parent_id,
                        client_code=client_code,
                        keywords=[text],
                        auth_headers=auth_headers,
                    )
                    or []
                )

                volume = self.FALLBACK_VOLUME
                competition = self.FALLBACK_COMPETITION
                competition_index = self.FALLBACK_COMPETITION_INDEX
                if metrics:
                    m = metrics[0]
                    volume = m.get("volume", self.FALLBACK_VOLUME)
                    competition = m.get("competition", self.FALLBACK_COMPETITION)
                    competition_index = m.get(
                        "competitionIndex", self.FALLBACK_COMPETITION_INDEX
                    )

                # Compute impact_score from Google's predicted conversion uplift
                impact = rec.get("impact", {})
                base_conv = impact.get("base", {}).get("conversions") or 0
                pot_conv = impact.get("potential", {}).get("conversions") or 0
                uplift = pot_conv - base_conv
                impact_score = (
                    round(
                        min(uplift / self.IMPACT_UPLIFT_CAP * 100, 100),
                        self.IMPACT_SCORE_DECIMALS,
                    )
                    if uplift > 0
                    else None
                )

                hydrated_native.append(
                    {
                        "keyword": text,
                        "volume": volume,
                        "competition": competition,
                        "competitionIndex": competition_index,
                        "match_type": kw_info.get(
                            "match_type", self.FALLBACK_MATCH_TYPE
                        ),
                        "is_native_recommendation": True,
                        "impact": impact,
                        "impact_score": impact_score,
                        "resource_name": rec.get("resource_name", ""),
                    }
                )
        return hydrated_native

    def _build_recommendations(
        self,
        llm_selected: List[Dict[str, Any]],
        suggestions: List[Dict[str, Any]],
        existing_kws: Set[str],
        ad_group_count: int = 1,
    ) -> List[GoogleKeywordRecommendation]:
        suggestion_map = {s["keyword"].lower(): s for s in suggestions}
        finalized = []
        for kw in llm_selected:
            text = kw.get("keyword", "").strip().lower()
            google_data = suggestion_map.get(text)
            if not text or text in existing_kws or not google_data:
                continue
            finalized.append({**kw, **google_data})

        max_keywords = max(
            self.MIN_KEYWORDS_PER_CAMPAIGN, ad_group_count * self.KEYWORDS_PER_AD_GROUP
        )

        return [
            GoogleKeywordRecommendation(
                text=kw["keyword"],
                match_type=kw.get("match_type", "PHRASE"),
                ad_group_id=kw.get("ad_group_id"),
                ad_group_name=kw.get("ad_group_name"),
                recommendation="ADD",
                reason=kw.get("reason", "High-potential keyword"),
                origin="KEYWORD",
                metrics={
                    "volume": kw.get("volume", 0),
                    "competition": kw.get("competition", ""),
                    "competitionIndex": kw.get("competitionIndex", 0),
                    "semantic_score": kw.get("semantic_score", 0),
                },
                score=kw.get("final_score"),
            )
            for kw in score_and_rank_keywords(finalized)[:max_keywords]
        ]

    async def _llm_select_keywords(
        self,
        suggestions: List[Dict[str, Any]],
        campaign_details: Dict[str, Any],
        context: Dict[str, Any],
        session: BaseSession | None = None,
    ) -> List[Dict[str, Any]]:
        prompt = self._format_selection_prompt(suggestions, campaign_details, context)

        try:
            provider = get_llm_provider("openai")
            response = await provider.create_completion(
                system_prompt="You are a Google Ads keyword analyst. Return JSON only.",
                messages=[{"role": "user", "content": prompt}],
                model_tier="balanced",
                max_tokens=4000,
            )

            from app.agents.adzump.recommendations.google.advisors._utils import (
                track_advisor_llm_call,
            )

            await track_advisor_llm_call(
                session=session,
                response=response,
                prefix="kw_select",
                provider_name=provider.name.lower()
                if hasattr(provider, "name")
                else "openai",
            )

            parsed = clean_and_load_json(response.get("content", ""))
            selected = parsed.get("keywords") or parsed.get("selected_keywords") or []
            return selected if isinstance(selected, list) else []
        except Exception:
            logger.error("llm_selection_failed", exc_info=True)
            return []

    @staticmethod
    def _format_ad_group_keywords(entries: List[Dict[str, Any]]) -> str:
        groups: Dict[str, List[str]] = {}
        for e in entries[: KeywordIdeaService.MAX_ENTRIES_FOR_AD_GROUP_FORMAT]:
            key = f"{e.get('ad_group_name', 'Unknown')} (id:{e.get('ad_group_id', '')})"
            groups.setdefault(key, []).append(e["keyword"])
        return "\n".join(f"- {name}: {', '.join(kws)}" for name, kws in groups.items())

    def _format_selection_prompt(
        self,
        suggestions: List[Dict[str, Any]],
        campaign_details: Dict[str, Any],
        context: Dict[str, Any],
    ) -> str:
        entries = campaign_details.get("entries", [])

        match_types: Dict[str, int] = {}
        for entry in entries:
            mt = entry.get("match_type", "phrase")
            match_types[mt] = match_types.get(mt, 0) + 1

        prompt_template = load_prompt("recommendations/keyword_suggestion_prompt.txt")
        if not prompt_template:
            prompt_template = """
            Select the best keywords for this campaign.
            Campaign: {campaign_name}
            Business: {brand_name} ({business_type})
            Location: {service_areas}
            
            Candidates:
            {suggestions_list}
            
            Return JSON {"keywords": [{"keyword": "...", "reason": "..."}]}
            """

        return prompt_template.format(
            brand_name=context.get("brand_name", ""),
            business_type=context.get("business_type", ""),
            service_areas=", ".join(context.get("service_areas", [])),
            url=context.get("url", ""),
            unique_features=", ".join(context.get("unique_features", [])),
            business_summary=context.get("summary", ""),
            campaign_name=campaign_details.get("name", ""),
            ad_group_keywords=self._format_ad_group_keywords(entries),
            suggestions_count=min(len(suggestions), self.MAX_SUGGESTIONS_IN_PROMPT),
            suggestions_list=self._format_suggestions_list(
                suggestions[: self.MAX_SUGGESTIONS_IN_PROMPT]
            ),
            anchor_summary=", ".join(
                f"{mt}: {c}"
                for mt, c in sorted(match_types.items(), key=lambda x: -x[1])
            ),
        )

    @staticmethod
    def _format_suggestions_list(suggestions: List[Dict[str, Any]]) -> str:
        """Format suggestion lines with [G] tag and ROI column for native recs."""
        lines = []
        for s in suggestions:
            native_tag = " [G]" if s.get("is_native_recommendation") else ""
            impact_score = s.get("impact_score")
            roi_col = f"{impact_score:.0f}" if impact_score is not None else "\u2014"
            lines.append(
                f"- {s['keyword']}{native_tag} | Vol: {s.get('volume', 0)} "
                f"| Comp: {s.get('competition', 'UNKNOWN')} "
                f"| CompIdx: {s.get('competitionIndex', 0):.2f} "
                f"| ROI: {roi_col} "
                f"| Semantic: {s.get('semantic_score', 50):.0f}"
            )
        return "\n".join(lines)


idea_service = KeywordIdeaService()
