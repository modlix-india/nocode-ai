"""Meta Detailed Targeting tools.

Defines the 5 category search and validation tools that the
DetailedTargetingAgent LLM calls to discover and finalise Meta targeting segments.

Each fetch tool calls the TargetingAdapter which applies the correct
multi-phase Meta Graph API strategy for that category:

  fetch_interests                : search per seed + targetingsuggestions expansion
  fetch_behaviors                : full catalog browse + search per seed
  fetch_demographics             : browse 5 fixed subtypes (no seeds), returns full catalog
  search_professional_demographics : targetingsearch per seed across work/education subtypes
  validate_targeting             : batched GET targetingvalidation, stores final result

Context dict expected keys (injected by DetailedTargetingAgent.build_tool_context):
  - auth          : AuthContext  - Meta credentials
  - ad_account_id : str         - the Meta ad account ID (e.g. "act_12345")
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.adapters.meta.targeting_adapter import TargetingAdapter
from app.agents.adzump.agents.meta_detailed_targeting.models import (
    TargetingEntity,
    resolve_ad_account_id,
)


TOTAL_TARGETING_LIMIT = 60

logger = logging.getLogger(__name__)

# Shared adapter instance
_adapter = TargetingAdapter()

CANDIDATE_DISPLAY_LIMIT = 30


# Context helpers
def _get_auth(context: dict[str, Any]):
    return context.get("auth")


def _entity_to_dict(e: TargetingEntity) -> dict[str, Any]:
    """Serialise a TargetingEntity to a compact dict for the LLM."""
    d: dict[str, Any] = {
        "id": e.id,
        "name": e.name,
        "type": e.type,
    }
    if e.audience_size_lower_bound:
        d["size"] = e.audience_size_lower_bound
    return d


def _stash_candidates(context: dict[str, Any], entities: list[TargetingEntity]) -> None:
    """Stash fetched candidate entities in context memory as serializable dicts indexed by entity ID."""
    session_ctx = context.get("session_context")
    target = session_ctx if session_ctx is not None else context
    pool = target.setdefault("_candidate_pool", {})
    for e in entities:
        if e and e.id:
            pool[str(e.id)] = e.model_dump()


# Tool 1 - fetch_interests
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

    account_id = resolve_ad_account_id(context)
    if not account_id:
        return ToolResult(success=False, error="ad_account_id not found in context")

    try:
        entities = await _adapter.fetch_interests(
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            account_id=account_id,
            seeds=seeds,
        )
        _stash_candidates(context, entities)
        data = [_entity_to_dict(e) for e in entities[:CANDIDATE_DISPLAY_LIMIT]]
        logger.info("[DetailedTargeting] Fetched %d interests candidates", len(data))
        return ToolResult(
            success=True,
            data=data,
        )
    except Exception as exc:
        logger.exception("fetch_interests failed")
        return ToolResult(success=False, error=str(exc))


# Tool 2 - fetch_behaviors
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

    account_id = resolve_ad_account_id(context)
    if not account_id:
        return ToolResult(success=False, error="ad_account_id not found in context")

    try:
        entities = await _adapter.fetch_behaviors(
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            account_id=account_id,
            seeds=seeds,
        )
        _stash_candidates(context, entities)
        data = [_entity_to_dict(e) for e in entities[:CANDIDATE_DISPLAY_LIMIT]]
        logger.info("[DetailedTargeting] Fetched %d behaviors candidates", len(data))
        return ToolResult(
            success=True,
            data=data,
        )
    except Exception as exc:
        logger.exception("fetch_behaviors failed")
        return ToolResult(success=False, error=str(exc))


# Tool 3 - fetch_demographics  (fixed catalog — no seeds)
async def _fetch_demographics(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Fetch the complete fixed demographic catalog (life_events, family, income, industries, education_statuses).

    No seeds required. Calls targetingbrowse per fixed subtype in parallel.
    Returns the full ~99 entry fixed catalog, untruncated.
    """
    auth = _get_auth(context)
    if not auth:
        return ToolResult(success=False, error="Authentication context missing")

    account_id = resolve_ad_account_id(context)
    if not account_id:
        return ToolResult(success=False, error="ad_account_id not found in context")

    try:
        entities = await _adapter.fetch_demographics(
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            account_id=account_id,
        )
        _stash_candidates(context, entities)
        data = [_entity_to_dict(e) for e in entities]
        logger.info("[DetailedTargeting] Fetched %d fixed demographic candidates", len(data))
        return ToolResult(
            success=True,
            data=data,
        )
    except Exception as exc:
        logger.exception("fetch_demographics failed")
        return ToolResult(success=False, error=str(exc))


# Tool 3b - search_professional_demographics  (open subtypes — needs seeds)
async def _search_professional_demographics(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Search open demographic databases for job titles, employers, and education majors.

    These subtypes (work_positions, work_employers, education_majors) cannot be
    browsed — they require keyword seeds just like fetch_interests.
    Only call this when the business specifically targets by profession or education.
    """
    seeds: list[str] = params.get("seeds", [])
    if not seeds or not isinstance(seeds, list):
        return ToolResult(success=False, error="seeds must be a non-empty list of strings")

    logger.info("[DetailedTargeting] search_professional_demographics seeds: %s", seeds)

    auth = _get_auth(context)
    if not auth:
        return ToolResult(success=False, error="Authentication context missing")

    account_id = resolve_ad_account_id(context)
    if not account_id:
        return ToolResult(success=False, error="ad_account_id not found in context")

    try:
        entities = await _adapter.search_open_demographics(
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            account_id=account_id,
            seeds=seeds,
        )
        _stash_candidates(context, entities)
        data = [_entity_to_dict(e) for e in entities[:CANDIDATE_DISPLAY_LIMIT]]
        logger.info("[DetailedTargeting] Fetched %d professional demographic candidates", len(data))
        return ToolResult(
            success=True,
            data=data,
        )
    except Exception as exc:
        logger.exception("search_professional_demographics failed")
        return ToolResult(success=False, error=str(exc))


# Tool 4 - validate_targeting
async def _validate_targeting(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Validate curated segments and store the final targeting result.

    Accepts selected_ids (a list of string IDs selected by the LLM from candidate tools).
    Looks up each ID from the stashed _candidate_pool context memory.
    Calls Meta targetingvalidation (batched by 50) to keep only valid=True segments.
    Applies global limits and stores the final result in context["detailed_targeting"].
    This is the LAST tool in the workflow - do NOT call any other tools after this.
    """
    selected_ids: list[str] = params.get("selected_ids", [])

    auth = _get_auth(context)
    if not auth:
        return ToolResult(success=False, error="Authentication context missing")

    account_id = resolve_ad_account_id(context)
    if not account_id:
        return ToolResult(success=False, error="ad_account_id not found in context")

    session_ctx = context.get("session_context")
    target = session_ctx if session_ctx is not None else context
    pool: dict[str, Any] = target.get("_candidate_pool") or {}

    entities_to_validate: list[TargetingEntity] = []

    if selected_ids and isinstance(selected_ids, list):
        for sid in selected_ids:
            sid_str = str(sid).strip()
            if sid_str in pool:
                item = pool[sid_str]
                if isinstance(item, TargetingEntity):
                    entities_to_validate.append(item)
                elif isinstance(item, dict):
                    parsed = TargetingEntity.from_meta(item)
                    if parsed:
                        entities_to_validate.append(parsed)
            elif sid_str:
                entities_to_validate.append(TargetingEntity(id=sid_str, name=sid_str, type="interests"))
    else:
        logger.warning(
            "[DetailedTargeting] validate_targeting called with empty selected_ids — "
            "no segments to validate. The model must pass selected_ids."
        )

    logger.info(
        "[DetailedTargeting] Model selected %d candidates for validation.",
        len(entities_to_validate)
    )

    valid_entities = []
    try:
        if entities_to_validate:
            valid_entities = await _adapter.validate(
                client_code=auth.client_code,
                auth_headers=auth.to_headers(),
                account_id=account_id,
                entities=entities_to_validate,
            )
    except Exception as exc:
        logger.warning(f"Targeting validation failed: {exc}")

    # 2. Limit and format the final result
    final_entities = valid_entities[:TOTAL_TARGETING_LIMIT]

    # Store full model_dump so audience_size, path, description survive round-trip to UI
    final = {
        "entities": [e.model_dump() for e in final_entities],
    }

    # Store result in unseeded _validated_targeting key for recommend() to read after loop
    if session_ctx is not None:
        session_ctx["_validated_targeting"] = final
    else:
        context["_validated_targeting"] = final

    counts = f"total_segments={len(final['entities'])}"
    logger.info("validate_targeting complete - final result stored: %s", counts)
    logger.info("[DetailedTargeting] Final validated entities count: %d", len(final_entities))
    return ToolResult(
        success=True,
        data=final,
        summary=f"Validated and finalised targeting: {counts}.",
        model_summary=f"Successfully validated {len(final_entities)} targeting segments.",
        audience="user",
    )


# Inner-loop tools — consumed by DetailedTargetingAgent.__init__
INNER_TARGETING_TOOLS: list[ToolDefinition] = [
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
            "Fetch the complete Meta fixed demographic catalog. "
            "Covers life events (Newlywed, Recently moved, New job, etc.), "
            "family statuses (Parents with toddlers, etc.), "
            "income tiers, industries, and education statuses. "
            "No arguments needed — returns the full catalog (~99 entries) for you to curate. "
            "Do NOT use this for job titles, employers, or college majors; use search_professional_demographics for those."
        ),
        display_name="Fetch Demographics",
        parameters=[],
        execute=_fetch_demographics,
    ),
    ToolDefinition(
        name="search_professional_demographics",
        description=(
            "Search Meta's open demographic databases for specific job titles, employers, and education majors. "
            "ONLY call this if the business specifically targets professionals by role, company, or degree — "
            "for example: B2B SaaS, HR tech, recruitment, medical/legal services, or education platforms. "
            "Do NOT call this for consumer products, real estate, luxury goods, or lifestyle businesses. "
            "Internally runs targetingsearch per seed across work_positions, work_employers, education_majors."
        ),
        display_name="Search Professional Demographics",
        parameters=[
            ToolParameter(
                name="seeds",
                type="array",
                description=(
                    "List of 5 to 10 job title, employer name, or college major seeds. "
                    "Examples: ['Software Engineer', 'McKinsey', 'MBA', 'Marketing Manager', 'Stanford University']"
                ),
                required=True,
                items={"type": "string"},
            ),
        ],
        execute=_search_professional_demographics,
    ),
    ToolDefinition(
        name="validate_targeting",
        description=(
            "FINAL STEP - call this last and only once. "
            "Pass the complete list of string IDs representing the desired active targeting selection. "
            "IMPORTANT: When expanding or adding to existing targeting (when CURRENT TARGETING is present), "
            "include both the existing segment IDs from CURRENT TARGETING and your newly curated candidate IDs. "
            "Validates each segment ID against Meta's API, removes deprecated/inactive ones, "
            "applies a global limit (maximum 60 segments total), "
            "and stores the final result. Do NOT call any other tools after this."
        ),
        display_name="Validate Targeting",
        parameters=[
            ToolParameter(
                name="selected_ids",
                type="array",
                description=(
                    "Complete list of active Meta targeting segment string IDs to validate and keep. "
                    "Include existing IDs from CURRENT TARGETING (for additive requests) plus newly fetched IDs. "
                    "Example: ['6003139266661', '6003208573211', '6008123456789']"
                ),
                required=True,
                items={"type": "string"},
            ),
        ],
        execute=_validate_targeting,
    ),
]
