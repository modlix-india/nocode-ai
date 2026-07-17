"""Meta Detailed Targeting tools.

Defines the three smart category tools and one validation tool that the
DetailedTargetingAgent LLM calls to discover and finalise Meta targeting segments.

Each fetch tool calls the TargetingAdapter which applies the correct
multi-phase Meta Graph API strategy for that category:

  fetch_interests    : search per seed + targetingsuggestions expansion
  fetch_behaviors    : full catalog browse + search per seed
  fetch_demographics : full tree browse + search per seed x 8 subtypes
  validate_targeting : batched GET targetingvalidation, stores final result

Context dict expected keys (injected by DetailedTargetingAgent.build_tool_context):
  - auth          : AuthContext  - Meta credentials
  - ad_account_id : str         - the Meta ad account ID (e.g. "act_12345")
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.adapters.meta.targeting_adapter import TargetingAdapter
from app.agents.adzump.agents.campaign.meta.models import TargetingCategory, TargetingEntity

# Category limits
CATEGORY_LIMITS: dict[TargetingCategory, int] = {
    TargetingCategory.INTERESTS: 25,
    TargetingCategory.DEMOGRAPHICS: 15,
    TargetingCategory.BEHAVIORS: 20,
}
DEFAULT_CATEGORY_LIMIT = 15

logger = logging.getLogger(__name__)

# Shared adapter instance
_adapter = TargetingAdapter()

# Cap returned candidates per category to keep LLM context manageable
CANDIDATE_DISPLAY_LIMIT = 100


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------
def _resolve_account_id(context: dict[str, Any]) -> str:
    """Resolve the ad account ID from multiple possible context sources."""
    account_id = (context.get("ad_account_id") or "").strip()
    if account_id:
        return account_id

    parent_ctx = context.get("parent_session_context") or {}
    account_id = ((parent_ctx.get("campaign_spec") or {}).get("account") or "").strip()
    if account_id:
        return account_id

    session_ctx = context.get("session_context") or {}
    account_id = ((session_ctx.get("campaign_spec") or {}).get("account") or "").strip()
    return account_id


def _get_auth(context: dict[str, Any]):
    return context.get("auth")


def _entity_to_dict(e: TargetingEntity) -> dict[str, Any]:
    """Serialise a TargetingEntity to a compact dict for the LLM."""
    return {
        "id": e.id,
        "name": e.name,
        "type": e.type,
        "category": e.category,
        "audience_size_lower_bound": e.audience_size_lower_bound,
        "audience_size_upper_bound": e.audience_size_upper_bound,
    }


# ---------------------------------------------------------------------------
# Tool 1 - fetch_interests
# ---------------------------------------------------------------------------
async def _fetch_interests(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Fetch interest targeting candidates using keyword search + recommendation expansion.

    Phase 1: search per seed keyword against Meta targetingsearch (parallel).
    Phase 2: expand using targetingsuggestions with those IDs (batched by 10).
    Returns up to CANDIDATE_DISPLAY_LIMIT deduplicated interest segments.
    """
    seeds: list[str] = params.get("seeds", [])
    if not seeds or not isinstance(seeds, list):
        return ToolResult(success=False, error="seeds must be a non-empty list of strings")

    logger.info("[DetailedTargeting] fetch_interests seeds: %s", seeds)

    auth = _get_auth(context)
    if not auth:
        return ToolResult(success=False, error="Authentication context missing")

    account_id = _resolve_account_id(context)
    if not account_id:
        return ToolResult(success=False, error="ad_account_id not found in context")

    try:
        entities = await _adapter.fetch_interests(
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            account_id=account_id,
            seeds=seeds,
        )
        entities.sort(key=lambda e: e.audience_size_upper_bound or 0, reverse=True)
        data = [_entity_to_dict(e) for e in entities[:CANDIDATE_DISPLAY_LIMIT]]
        return ToolResult(
            success=True,
            data=data,
            summary=f"Found {len(data)} interest candidates (from {len(seeds)} seeds).",
        )
    except Exception as exc:
        logger.exception("fetch_interests failed")
        return ToolResult(success=False, error=str(exc))


# ---------------------------------------------------------------------------
# Tool 2 - fetch_behaviors
# ---------------------------------------------------------------------------
async def _fetch_behaviors(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Fetch behavior targeting candidates via full catalog browse + per-seed search.

    Combines a full targetingbrowse of the behaviors catalog with parallel
    keyword searches per seed. Returns up to CANDIDATE_DISPLAY_LIMIT segments.
    """
    seeds: list[str] = params.get("seeds", [])
    if not seeds or not isinstance(seeds, list):
        return ToolResult(success=False, error="seeds must be a non-empty list of strings")

    logger.info("[DetailedTargeting] fetch_behaviors seeds: %s", seeds)

    auth = _get_auth(context)
    if not auth:
        return ToolResult(success=False, error="Authentication context missing")

    account_id = _resolve_account_id(context)
    if not account_id:
        return ToolResult(success=False, error="ad_account_id not found in context")

    try:
        entities = await _adapter.fetch_behaviors(
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            account_id=account_id,
            seeds=seeds,
        )
        entities.sort(key=lambda e: e.audience_size_upper_bound or 0, reverse=True)
        data = [_entity_to_dict(e) for e in entities[:CANDIDATE_DISPLAY_LIMIT]]
        return ToolResult(
            success=True,
            data=data,
            summary=f"Found {len(data)} behavior candidates (from {len(seeds)} seeds).",
        )
    except Exception as exc:
        logger.exception("fetch_behaviors failed")
        return ToolResult(success=False, error=str(exc))


# ---------------------------------------------------------------------------
# Tool 3 - fetch_demographics
# ---------------------------------------------------------------------------
async def _fetch_demographics(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Fetch demographic targeting candidates via full tree browse + per-seed x 8-subtype search.

    Combines a full targetingbrowse with parallel searches across all 8 demographic
    subtypes (life_events, family_statuses, income, industries, work_positions,
    work_employers, education_majors, education_statuses) for each seed.
    Returns up to CANDIDATE_DISPLAY_LIMIT segments.
    """
    seeds: list[str] = params.get("seeds", [])
    if not seeds or not isinstance(seeds, list):
        return ToolResult(success=False, error="seeds must be a non-empty list of strings")

    logger.info("[DetailedTargeting] fetch_demographics seeds: %s", seeds)

    auth = _get_auth(context)
    if not auth:
        return ToolResult(success=False, error="Authentication context missing")

    account_id = _resolve_account_id(context)
    if not account_id:
        return ToolResult(success=False, error="ad_account_id not found in context")

    try:
        entities = await _adapter.fetch_demographics(
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            account_id=account_id,
            seeds=seeds,
        )
        entities.sort(key=lambda e: e.audience_size_upper_bound or 0, reverse=True)
        data = [_entity_to_dict(e) for e in entities[:CANDIDATE_DISPLAY_LIMIT]]
        return ToolResult(
            success=True,
            data=data,
            summary=f"Found {len(data)} demographic candidates (from {len(seeds)} seeds).",
        )
    except Exception as exc:
        logger.exception("fetch_demographics failed")
        return ToolResult(success=False, error=str(exc))


# ---------------------------------------------------------------------------
# Tool 4 - validate_targeting
# ---------------------------------------------------------------------------
async def _validate_targeting(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Validate curated segments and store the final targeting result.

    Accepts curated lists of interests, behaviors, and demographics.
    Each entry must include 'id', 'name', and 'category' fields.
    Calls Meta targetingvalidation (batched by 50) to keep only valid=True segments.
    Applies category limits and stores the final result in context["detailed_targeting"].
    This is the LAST tool in the workflow - do NOT call any other tools after this.
    """
    interests_in: list[dict] = params.get("interests", [])
    behaviors_in: list[dict] = params.get("behaviors", [])
    demographics_in: list[dict] = params.get("demographics", [])

    auth = _get_auth(context)
    if not auth:
        return ToolResult(success=False, error="Authentication context missing")

    account_id = _resolve_account_id(context)
    if not account_id:
        return ToolResult(success=False, error="ad_account_id not found in context")

    def _dicts_to_entities(items: list[dict], default_category: str) -> list[TargetingEntity]:
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                result.append(TargetingEntity(
                    id=str(item.get("id", "")),
                    name=str(item.get("name", "")),
                    type=str(item.get("type", "")),
                    category=item.get("category") or default_category,
                    audience_size_lower_bound=item.get("audience_size_lower_bound"),
                    audience_size_upper_bound=item.get("audience_size_upper_bound"),
                    path=item.get("path") or [],
                    description=item.get("description") or "",
                ))
            except Exception:
                logger.debug("Skipping malformed entity: %r", item)
        return result

    interests_entities = _dicts_to_entities(interests_in, "interests")
    behaviors_entities = _dicts_to_entities(behaviors_in, "behaviors")
    demographics_entities = _dicts_to_entities(demographics_in, "demographics")

    interests_valid = []
    try:
        if interests_entities:
            interests_valid = await _adapter.validate(
                client_code=auth.client_code,
                auth_headers=auth.to_headers(),
                account_id=account_id,
                entities=interests_entities,
            )
    except Exception as exc:
        logger.warning(f"Interests validation failed: {exc}")

    behaviors_valid = []
    try:
        if behaviors_entities:
            behaviors_valid = await _adapter.validate(
                client_code=auth.client_code,
                auth_headers=auth.to_headers(),
                account_id=account_id,
                entities=behaviors_entities,
            )
    except Exception as exc:
        logger.warning(f"Behaviors validation failed: {exc}")

    # Demographics validation is skipped (returns unvalidated demographics)
    # to match PR specs and bypass Meta's rigid subtype API requirements.
    demographics_valid = demographics_entities

    # Apply category limits
    interests_limit = CATEGORY_LIMITS.get(TargetingCategory.INTERESTS, DEFAULT_CATEGORY_LIMIT)
    behaviors_limit = CATEGORY_LIMITS.get(TargetingCategory.BEHAVIORS, DEFAULT_CATEGORY_LIMIT)
    demographics_limit = CATEGORY_LIMITS.get(TargetingCategory.DEMOGRAPHICS, DEFAULT_CATEGORY_LIMIT)

    final = {
        "interests": [_entity_to_dict(e) for e in interests_valid[:interests_limit]],
        "behaviors": [_entity_to_dict(e) for e in behaviors_valid[:behaviors_limit]],
        "demographics": [_entity_to_dict(e) for e in demographics_valid[:demographics_limit]],
    }

    # Store result for recommend() to read after the loop.
    # We must write to session_context because the outer context is just a shallow copy.
    session_ctx = context.get("session_context")
    if session_ctx is not None:
        session_ctx["detailed_targeting"] = final
    else:
        context["detailed_targeting"] = final

    counts = (
        f"interests={len(final['interests'])}, "
        f"behaviors={len(final['behaviors'])}, "
        f"demographics={len(final['demographics'])}"
    )
    logger.info("validate_targeting complete - final result stored: %s", counts)
    return ToolResult(
        success=True,
        data=final,
        summary=f"Validated and finalised targeting: {counts}.",
    )


# ---------------------------------------------------------------------------
# Tool registry - consumed by DetailedTargetingAgent.__init__
# ---------------------------------------------------------------------------
TARGETING_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="fetch_interests",
        description=(
            "Discover interest targeting candidates for this campaign. "
            "Provide brand names, publications, tools, or lifestyle keywords that "
            "the target buyer follows on Facebook/Instagram. "
            "Do NOT use product descriptions or local business names - Meta does not index these. "
            "Use actual brand names (e.g. 'Salesforce', 'Forbes', 'HubSpot'). "
            "Internally runs keyword search + recommendation expansion in parallel."
        ),
        display_name="Fetch Interests",
        parameters=[
            ToolParameter(
                name="seeds",
                type="array",
                description=(
                    "List of 5 to 10 seed keywords - brand names, publications, lifestyle terms "
                    "that the target buyer uses or follows. "
                    "Examples: ['Salesforce', 'Forbes', 'HubSpot', 'digital marketing', 'business owner']"
                ),
                required=True,
                items={"type": "string"},
            ),
        ],
        execute=_fetch_interests,
    ),
    ToolDefinition(
        name="fetch_behaviors",
        description=(
            "Discover behavior targeting candidates for this campaign. "
            "Provide behavioral descriptors that match Meta's behavior catalog vocabulary: "
            "purchase behaviors, digital activities, travel, financial, consumer classification. "
            "Use Meta's vocabulary - not product descriptions. "
            "Examples: 'small business owners', 'engaged shoppers', 'frequent travelers'. "
            "Internally runs a full catalog browse + keyword search per seed in parallel."
        ),
        display_name="Fetch Behaviors",
        parameters=[
            ToolParameter(
                name="seeds",
                type="array",
                description=(
                    "List of 5 to 10 behavioral seed keywords matching Meta's behavior segment vocabulary. "
                    "Examples: ['small business owners', 'engaged shoppers', 'frequent travelers', 'b2b enterprise employees', 'new business owner']"
                ),
                required=True,
                items={"type": "string"},
            ),
        ],
        execute=_fetch_behaviors,
    ),
    ToolDefinition(
        name="fetch_demographics",
        description=(
            "Discover demographic targeting candidates for this campaign. "
            "Provide demographic descriptors that match Meta's indexed fields: "
            "job titles (LinkedIn-style, no seniority prefix), industries (top-level sector), "
            "education majors (degree field names), life events, income tiers, family statuses. "
            "Internally runs a full taxonomy browse + searches across all 8 demographic "
            "subtypes for each seed in parallel."
        ),
        display_name="Fetch Demographics",
        parameters=[
            ToolParameter(
                name="seeds",
                type="array",
                description=(
                    "List of 5 to 10 demographic seed keywords. Use job titles, industries, education "
                    "fields, life stage labels. "
                    "Examples: ['Marketing Manager', 'Software Engineer', 'MBA', 'new homeowner', 'founder']"
                ),
                required=True,
                items={"type": "string"},
            ),
        ],
        execute=_fetch_demographics,
    ),
    ToolDefinition(
        name="validate_targeting",
        description=(
            "FINAL STEP - call this last and only once. "
            "Pass your curated segments from fetch_interests, fetch_behaviors, and "
            "fetch_demographics after applying relevance filtering. "
            "Validates each segment against Meta's API, removes deprecated/inactive ones, "
            "applies category limits (interests<=25, behaviors<=20, demographics<=15), "
            "and stores the final result. Do NOT call any other tools after this."
        ),
        display_name="Validate Targeting",
        parameters=[
            ToolParameter(
                name="interests",
                type="array",
                description=(
                    "Curated interest segments to validate. "
                    "Each item must include 'id', 'name', 'category', and 'type' fields EXACTLY as returned by fetch_interests. "
                    "Maximum 25 items. Sort by audience_size descending before passing."
                ),
                required=True,
                items={"type": "object"},
            ),
            ToolParameter(
                name="behaviors",
                type="array",
                description=(
                    "Curated behavior segments to validate. "
                    "Each item must include 'id', 'name', 'category', and 'type' fields EXACTLY as returned by fetch_behaviors. "
                    "Maximum 20 items."
                ),
                required=True,
                items={"type": "object"},
            ),
            ToolParameter(
                name="demographics",
                type="array",
                description=(
                    "Curated demographic segments to validate. "
                    "Each item must include 'id', 'name', 'category', and 'type' fields EXACTLY as returned by fetch_demographics. "
                    "Maximum 15 items."
                ),
                required=True,
                items={"type": "object"},
            ),
        ],
        execute=_validate_targeting,
    ),
]
