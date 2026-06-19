"""verify_conversion_health tool for campaign conversion tracking diagnostics."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.recommendations.models import (
    ConversionFixCard,
    ConversionHealthCheck,
    ConversionHealthReport,
    HealthLabel,
    CheckSeverity,
    ConversionSignal,
)
from app.agents.adzump.agents.optimization.tools.google._conversion_fixes import (
    build_implementation_guide,
    build_mutation_payload,
)

from app.agents.adzump.adapters.google.conversion_metrics import (
    conversion_metrics_adapter,
)
from app.agents.adzump.adapters.google.conversion_enums import (
    AttributionModel,
    ConversionActionCountingType,
    ConversionActionStatus,
    ConversionTrackingStatus,
    CONVERSION_DEPENDENT_STRATEGIES,
    CONVERSION_TRACKED_STATUSES,
    DEPRECATED_ATTRIBUTION_MODELS,
    ECOMMERCE_CATEGORIES,
    LEAD_CATEGORIES,
    SMART_BIDDING_MIN_CONVERSIONS,
    VALUE_REQUIRED_CATEGORIES,
    WEBSITE_TAG_TYPES,
)


logger = logging.getLogger(__name__)


def _auto_fix(operation: str, updates: list[dict]) -> dict:
    """A per-entity mutation an auto-applyable check attaches to its metadata as
    ``auto_fix``; build_mutation_payload validates and surfaces it as the payload."""
    return {"operation": operation, "updates": updates}


def _scope_ok(resource_name: str, customer_id: str, campaign_id: str) -> bool:
    """In scope iff a customer/campaign segment the resource name carries matches
    ours (names without the segment pass). Guards both the displayed ids and the
    mutation write targets — never surface or mutate another customer's resource."""
    rn = str(resource_name or "")
    if customer_id and "customers/" in rn and f"customers/{customer_id}" not in rn:
        return False
    if campaign_id and "campaigns/" in rn and f"campaigns/{campaign_id}" not in rn:
        return False
    return True


# Estimated Optimisation Score delta per failing check.
# These are internal estimates — not from Google's API.
# Used only to sort fix cards by impact for the frontend.
_OPTISCORE_DELTA: dict[str, float] = {
    "no_active_conversion_actions": 20.0,
    "primary_goal_not_biddable": 15.0,
    "tag_snippets_missing": 15.0,
    "conversion_drop_detected": 10.0,
    "insufficient_signal": 8.0,
    "deprecated_attribution_model": 5.0,
    "wrong_counting_type": 5.0,
    "no_conversion_value": 3.0,
}


# Factory helper — eliminates repeated ConversionHealthCheck construction
def _check(
    check_id: str,
    passed: bool,
    severity: str,
    title: str,
    description: str = "",
    affected_entity_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> ConversionHealthCheck:
    """Build a ConversionHealthCheck with consistent defaults."""
    return ConversionHealthCheck(
        check_id=check_id,
        passed=passed,
        severity=severity,
        title=title,
        description=description,
        affected_entity_ids=affected_entity_ids or [],
        metadata=metadata or {},
    )


# Check 1: No active conversion actions
def _check_no_active_actions(data: dict) -> ConversionHealthCheck:
    """Account has at least one ENABLED conversion action."""
    check_id = "no_active_conversion_actions"
    if data.get("tracking_status") == ConversionTrackingStatus.NOT_CONVERSION_TRACKED.value:
        return _check(
            check_id,
            False,
            CheckSeverity.CRITICAL,
            "No conversion tracking configured",
            "This account has no conversion tracking set up. "
            "Smart Bidding recommendations are unreliable without conversion data.",
        )

    # Tracking is present — now check for at least one enabled action.
    # We only proceed here when tracking_status is in CONVERSION_TRACKED_STATUSES
    # OR is UNKNOWN (data gap). Either way, check for enabled actions.
    tracking_confirmed = data.get("tracking_status") in CONVERSION_TRACKED_STATUSES
    enabled = [
        action
        for action in data["actions"]
        if action["status"] == ConversionActionStatus.ENABLED.value
    ]
    if not enabled:
        return _check(
            check_id,
            False,
            CheckSeverity.CRITICAL,
            "No active conversion actions",
            "Conversion tracking is configured but no actions are currently active. "
            "Smart Bidding has no signal to optimise against."
            + (
                " Note: tracking ownership could not be confirmed — verify the "
                "account is not using cross-account conversion tracking."
                if not tracking_confirmed
                else ""
            ),
        )
    details = (
        data["tracking_status"]
        .replace("CONVERSION_TRACKING_MANAGED_BY_", "")
        .replace("_", " ")
        .title()
    )
    return _check(
        check_id,
        True,
        CheckSeverity.CRITICAL,
        "Active conversion actions found",
        f"{len(enabled)} active conversion action(s) found."
        + (
            f"Tracking managed by: {details}."
            if tracking_confirmed
            else " Tracking ownership could not be confirmed."
        ),
        affected_entity_ids=[action["id"] for action in enabled],
    )


# Check 2: Primary goal not biddable
def _check_primary_not_biddable(data: dict) -> ConversionHealthCheck:
    """Primary conversion action is included in Smart Bidding signal."""
    check_id = "primary_goal_not_biddable"
    primary = [
        action
        for action in data["actions"]
        if action["primary_for_goal"]
        and action["status"] == ConversionActionStatus.ENABLED.value
    ]
    if not primary:
        return _check(
            check_id,
            False,
            CheckSeverity.CRITICAL,
            "No primary conversion action set",
            "No active conversion action is marked as the primary goal.",
        )

    excluded = [
        action for action in primary if not action["include_in_conversions_metric"]
    ]
    if excluded:
        return _check(
            check_id,
            False,
            CheckSeverity.CRITICAL,
            "Primary conversion action excluded from Smart Bidding",
            f"'{excluded[0]['name']}' is set as the primary goal but is excluded "
            "from the conversions metric. Smart Bidding is optimising toward nothing.",
            affected_entity_ids=[action["id"] for action in excluded],
            metadata={
                # include_in_conversions_metric is mutable — re-include each action.
                "auto_fix": _auto_fix(
                    "MutateConversionActions",
                    [
                        {
                            "resource_name": action["resource_name"],
                            "field": "include_in_conversions_metric",
                            "value": True,
                        }
                        for action in excluded
                    ],
                ),
            },
        )

    primary_cats = {action["category"] for action in primary}
    non_biddable = [
        goal
        for goal in data["goals"]
        if goal["category"] in primary_cats and not goal["biddable"]
    ]
    if non_biddable:
        return _check(
            check_id,
            False,
            CheckSeverity.CRITICAL,
            "Primary conversion goal not biddable for this campaign",
            "The primary conversion goal is not set as biddable for this campaign. "
            "Smart Bidding is active but has no goal to optimise toward.",
            affected_entity_ids=[goal["resource_name"] for goal in non_biddable],
            metadata={
                # campaign_conversion_goal.biddable is mutable — turn it on.
                "auto_fix": _auto_fix(
                    "MutateCampaignConversionGoals",
                    [
                        {
                            "resource_name": goal["resource_name"],
                            "field": "biddable",
                            "value": True,
                        }
                        for goal in non_biddable
                    ],
                ),
            },
        )

    return _check(
        check_id,
        True,
        CheckSeverity.CRITICAL,
        "Primary conversion action is biddable",
        "The primary conversion action is active and included in Smart Bidding signal.",
    )


# Check 3: Tag snippets missing
def _check_tag_missing(data: dict) -> ConversionHealthCheck:
    """Check whether website conversion tags exist and cover landing pages."""
    check_id = "tag_snippets_missing"

    # Signal A: tag snippet never generated at all
    missing = [
        action
        for action in data["actions"]
        if action["status"] == ConversionActionStatus.ENABLED.value
        and action["type"] in WEBSITE_TAG_TYPES
        and not action["tag_snippets"]
    ]

    # Signal B: tag exists but Google detected untagged landing pages
    tag_coverage_recs = data.get("tag_coverage_recs", [])
    has_coverage_gap = len(tag_coverage_recs) > 0

    # Signal A is critical — zero tracking anywhere
    if missing:
        names = ", ".join(action["name"] for action in missing)
        return _check(
            check_id,
            False,
            CheckSeverity.CRITICAL,
            "Conversion tag snippets missing",
            f"{len(missing)} website conversion action(s) have no tag deployed: {names}.",
            affected_entity_ids=[action["id"] for action in missing],
            metadata={"missing_action_names": [action["name"] for action in missing]},
        )

    # Signal B is warning — partial coverage, some pages untracked
    if has_coverage_gap:
        untagged_pages = []
        for rec in tag_coverage_recs:
            untagged_pages.extend(rec.get("details", {}).get("untagged_urls", []))

        return _check(
            check_id,
            False,
            CheckSeverity.WARNING,
            "Google Tag missing from some landing pages",
            "Your Google tag is deployed but missing from some pages that your ads "
            "link to. This reduces conversion measurement accuracy.",
            metadata={
                "recommendation_resource_names": [
                    r["resource_name"] for r in tag_coverage_recs
                ],
                "untagged_pages": untagged_pages,
            },
        )

    return _check(
        check_id,
        True,
        CheckSeverity.CRITICAL,
        "Tag deployed and covering landing pages",
        "All active website conversion actions have tags deployed. "
        "No coverage gaps detected.",
    )


# check 4: Possible conversion drop detected
def _check_conversion_drop_detected(
    data: dict,
    campaign_context: dict | None,
) -> ConversionHealthCheck:
    """Detect zero-conversion anomalies for active Smart Bidding campaigns."""
    check_id = "conversion_drop_detected"
    if not campaign_context:
        return _check(
            check_id,
            True,
            CheckSeverity.WARNING,
            "Conversion drop check skipped",
            "No campaign context available to run this check.",
        )

    freshness = campaign_context.get("metric_freshness", {})
    if not freshness.get("is_fresh", True):
        age = freshness.get("age_days", "unknown")
        return _check(
            check_id,
            True,
            CheckSeverity.WARNING,
            "Conversion drop check skipped",
            f"Impression share data is {age} day(s) old. "
            "Check skipped to avoid false positives on stale data.",
        )

    conversions = float(campaign_context.get("conversions", 0.0))
    bidding = campaign_context.get("bidding_strategy_type", "")
    search_is = campaign_context.get("search_impression_share")

    on_conversion_strategy = bidding in CONVERSION_DEPENDENT_STRATEGIES

    # has_impressions: any non-None IS value means the campaign served.
    # None = zero impressions (campaign paused, never served, or budget exhausted).
    # ~0.1 = Google's sentinel for IS < 10% (precise values below 10% not reported).
    # Removing the old > 0.05 threshold so campaigns with < 10% IS are not excluded.
    has_impressions = search_is is not None  # any none-None means campaign served.
    zero_conversions = conversions == 0.0

    if on_conversion_strategy and has_impressions and zero_conversions:
        # Tailor description based on how conversions are tracked on this campaign.
        # For tag-based campaigns the cause is likely a broken/missing website tag.
        # For API-upload campaigns the cause is likely a broken upload pipeline.
        # When both types are present we flag both possibilities.
        has_tag_actions = any(
            action.get("requires_tag")
            and action["status"] == ConversionActionStatus.ENABLED.value
            for action in data["actions"]
        )

        has_api_actions = any(
            not action.get("requires_tag")
            and action["status"] == ConversionActionStatus.ENABLED.value
            for action in data["actions"]
        )
        if has_api_actions and has_tag_actions:
            cause = (
                "Check both your website tag deployment and your "
                "conversion upload pipeline for recent failures."
            )

        elif has_api_actions:
            cause = (
                "Your conversions are tracked via API upload. "
                "Check your upload pipeline — the job may have stopped "
                "running, or GCLID capture may have failed."
            )
        else:
            cause = (
                "Check for recent changes to your website tag deployment. "
                "A broken or missing tag is the most likely cause."
            )

        return _check(
            check_id,
            False,
            CheckSeverity.WARNING,
            "Zero conversions detected despite active impressions",
            f"This campaign is receiving impressions but recorded zero conversions "
            f"in the last 30 days. {cause}",
            affected_entity_ids=[campaign_context.get("campaign_id", "")],
            metadata={
                "conversions_30d": conversions,
                "bidding_strategy": bidding,
                "search_impression_share": search_is,
                "has_tag_actions": has_tag_actions,
                "has_api_actions": has_api_actions,
                "cause": cause,
            },
        )

    return _check(
        check_id,
        True,
        CheckSeverity.WARNING,
        "No conversion drop detected",
        "Conversion trend looks normal.",
    )


# check 5: Deprecated attribution model in use
def _check_deprecated_attribution(data: dict) -> ConversionHealthCheck:
    """No active conversion actions use deprecated attribution models."""
    check_id = "deprecated_attribution_model"
    affected = [
        action
        for action in data["actions"]
        if action["status"] == ConversionActionStatus.ENABLED.value
        and action["attribution_model"] in DEPRECATED_ATTRIBUTION_MODELS
    ]
    if affected:
        count = len(affected)
        models = sorted({action["attribution_model"] for action in affected})
        # convert enums to the readable names for the description
        readable_models = [
            m.replace("GOOGLE_SEARCH_ATTRIBUTION_", "").replace("_", " ").title()
            for m in models
        ]
        return _check(
            check_id,
            False,
            CheckSeverity.WARNING,
            "Deprecated attribution model in use",
            f"{count} active conversion action(s) use deprecated attribution models: {', '.join(readable_models)}. Consider switching to data-driven or last-click attribution models for better performance and support.",
            affected_entity_ids=[action["id"] for action in affected],
            metadata={
                "deprecated_models_in_use": models,
                # attribution_model is mutable — switch each to data-driven.
                "auto_fix": _auto_fix(
                    "MutateConversionActions",
                    [
                        {
                            "resource_name": action["resource_name"],
                            "field": "attribution_model_settings.attribution_model",
                            "value": AttributionModel.GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN.value,
                        }
                        for action in affected
                    ],
                ),
            },
        )
    return _check(
        check_id,
        True,
        CheckSeverity.WARNING,
        "Attribution models are current",
        "All active conversion actions use supported attribution models.",
    )


# check 6: Wrong conversion action counting type for conversion action category
def _check_wrong_counting(data: dict) -> ConversionHealthCheck:
    """Validate counting type settings for ecommerce and lead conversion actions."""
    check_id = "wrong_counting_type"
    issues = []
    for action in data["actions"]:
        if action["status"] != ConversionActionStatus.ENABLED.value:
            continue
        if not action.get("requires_tag", True):
            continue
        if (
            action.get("category") in ECOMMERCE_CATEGORIES
            and action["counting_type"]
            == ConversionActionCountingType.ONE_PER_CLICK.value
        ):
            issues.append(
                (action, "should count every conversion event, not just one per click")
            )
        elif (
            action.get("category") in LEAD_CATEGORIES
            and action["counting_type"]
            == ConversionActionCountingType.MANY_PER_CLICK.value
        ):
            issues.append(
                (action, "should count one conversion per click, not every event")
            )
    if issues:
        details = " | ".join(
            f"'{action['name']}' -{reason}" for action, reason in issues
        )
        return _check(
            check_id,
            False,
            CheckSeverity.WARNING,
            "Inconsistent counting types detected",
            details,
            affected_entity_ids=[action["id"] for action, _ in issues],
            metadata={
                # counting_type is mutable — ecommerce counts every event, leads one.
                "auto_fix": _auto_fix(
                    "MutateConversionActions",
                    [
                        {
                            "resource_name": action["resource_name"],
                            "field": "counting_type",
                            "value": (
                                ConversionActionCountingType.MANY_PER_CLICK.value
                                if action.get("category") in ECOMMERCE_CATEGORIES
                                else ConversionActionCountingType.ONE_PER_CLICK.value
                            ),
                        }
                        for action, _ in issues
                    ],
                ),
            },
        )
    return _check(
        check_id,
        True,
        CheckSeverity.WARNING,
        "Conversion counting types are consistent with categories",
        "All conversion actions have counting types consistent with their categories.",
    )


# check 7: No conversion value set for revenue actions
def _check_no_value(data: dict) -> ConversionHealthCheck:
    """Ensure revenue actions report a conversion value for ROAS optimization."""
    check_id = "no_conversion_value"
    no_value = [
        action
        for action in data["actions"]
        if action["status"] == ConversionActionStatus.ENABLED.value
        and action["category"] in VALUE_REQUIRED_CATEGORIES
        and action["always_use_default_value"]
        and action["default_value"] == 0.0
    ]
    if no_value:
        names = ", ".join(action["name"] for action in no_value)
        return _check(
            check_id,
            False,
            CheckSeverity.INFO,
            "No conversion value set for revenue actions",
            f"{len(no_value)} active revenue conversion action(s) have no value set: {names}. Target ROAS bidding has no revenue signal to optimise toward.",
            affected_entity_ids=[action["id"] for action in no_value],
            metadata={"no_value_actions": [action["id"] for action in no_value]},
        )
    return _check(
        check_id,
        True,
        CheckSeverity.INFO,
        "Conversion values are set for revenue actions",
        "All active revenue conversion actions have conversion values set.",
    )


# check 8: Insufficient conversion signal for smart bidding strategy
def _check_insufficient_signal(campaign_context: dict | None) -> ConversionHealthCheck:
    """Warn if conversion-dependent bidding needs more than 30 monthly conversions."""
    check_id = "insufficient_signal"
    if not campaign_context:
        return _check(
            check_id,
            True,
            CheckSeverity.WARNING,
            "Conversion signal check skipped",
            "No campaign context available to run this check.",
        )
    conversions = float(campaign_context.get("conversions", 0.0))
    bidding = campaign_context.get("bidding_strategy_type", "")
    if (
        bidding in CONVERSION_DEPENDENT_STRATEGIES
        and conversions < SMART_BIDDING_MIN_CONVERSIONS
    ):
        return _check(
            check_id,
            False,
            CheckSeverity.WARNING,
            "Insufficient conversion volume for current bidding strategy",
            (
                f"Campaign is on {bidding} with only {conversions:.0f} conversions "
                f"in 30 days (minimum: {SMART_BIDDING_MIN_CONVERSIONS}). "
                "The bidding model is in extended learning mode."
            ),
            metadata={
                "bidding_strategy": bidding,
                "conversions_30d": conversions,
                "threshold_used": SMART_BIDDING_MIN_CONVERSIONS,
            },
        )
    return _check(
        check_id,
        True,
        CheckSeverity.WARNING,
        "Conversion signal is sufficient",
        f"{conversions:.0f} conversions recorded in the last 30 days.",
    )


def _compute_health(checks: list[ConversionHealthCheck]) -> tuple[int, str]:
    failing = [check for check in checks if not check.passed]
    score = 100
    for check in failing:
        if check.severity == CheckSeverity.CRITICAL:
            score -= 40
        elif check.severity == CheckSeverity.WARNING:
            score -= 15
        elif check.severity == CheckSeverity.INFO:
            score -= 5
    score = max(0, min(100, score))
    if score == 0:
        label = HealthLabel.CRITICAL
    elif score < 50:
        label = HealthLabel.WARNING
    elif score < 80:
        label = HealthLabel.DEGRADED
    else:
        label = HealthLabel.HEALTHY
    return score, label.value


def _build_fix_cards(
    checks: list[ConversionHealthCheck],
    campaign_id: str,
    actions: list[dict],
    customer_id: str = "",
) -> list[ConversionFixCard]:
    tag_snippets_by_action = {}
    for action in actions:
        # Provide all tag snippets for this action
        tag_snippets = action.get("tag_snippets")
        tag_snippet = (
            tag_snippets[0]
            if tag_snippets and isinstance(tag_snippets, list) and isinstance(tag_snippets[0], dict)
            else None
        )
        tag_snippets_by_action[action["id"]] = {
            "action_name": action["name"],
            "global_site_tag": tag_snippet.get("globalSiteTag", "")
            if tag_snippet
            else "",
            "event_snippet": tag_snippet.get("eventSnippet", "") if tag_snippet else "",
        }

    fix_type_map: dict[str, str] = {
        "no_active_conversion_actions": "tag",
        "primary_goal_not_biddable": "goal",
        "tag_snippets_missing": "tag",
        "conversion_drop_detected": "tag",
        "deprecated_attribution_model": "config",
        "wrong_counting_type": "config",
        "no_conversion_value": "config",
        "insufficient_signal": "signal",
    }

    cards: list[ConversionFixCard] = []
    for check in checks:
        if check.passed:
            continue

        # Defensively drop displayed entity ids outside this customer/campaign.
        if check.affected_entity_ids:
            kept_ids = []
            for eid in check.affected_entity_ids:
                if _scope_ok(eid, customer_id, campaign_id):
                    kept_ids.append(eid)
                else:
                    logger.warning(
                        "verify_conversion_health: filtering out-of-scope entity id "
                        "%s (customer=%s campaign=%s)",
                        eid,
                        customer_id,
                        campaign_id,
                    )
            check.affected_entity_ids = kept_ids

        # Same guard on the mutation WRITE targets — the higher-stakes path. Any
        # out-of-scope target is dropped; if that empties the update set,
        # build_mutation_payload returns None and the card falls back to a guide.
        auto_fix = check.metadata.get("auto_fix")
        if isinstance(auto_fix, dict) and customer_id:
            kept_updates = []
            for upd in auto_fix.get("updates", []):
                if _scope_ok(upd.get("resource_name", ""), customer_id, campaign_id):
                    kept_updates.append(upd)
                else:
                    logger.warning(
                        "verify_conversion_health: dropping out-of-scope mutation "
                        "target %s",
                        upd.get("resource_name", ""),
                    )
            auto_fix["updates"] = kept_updates

        tag_snippet = None
        if check.check_id == "tag_snippets_missing" and tag_snippets_by_action:
            # Use the snippet for the specific affected conversion action if available,
            # otherwise fall back to the first snippet in the dict.
            affected_ids = getattr(check, "affected_entity_ids", None) or []
            tag_snippet = None
            for eid in affected_ids:
                eid_key = str(eid).split("/")[-1] if "/" in str(eid) else str(eid)
                if eid_key in tag_snippets_by_action:
                    tag_snippet = tag_snippets_by_action[eid_key]
                    break
            if tag_snippet is None:
                tag_snippet = next(iter(tag_snippets_by_action.values()))

        # A check is auto-applyable iff we can build a complete mutation payload;
        # everything else gets a manual step-by-step guide. Single source — the
        # flag can never claim "auto" for a fix we can't actually execute.
        mutation_payload = build_mutation_payload(check)
        can_auto_apply = mutation_payload is not None
        implementation_guide = (
            None if can_auto_apply else build_implementation_guide(check, tag_snippet)
        )

        cards.append(
            ConversionFixCard(
                check_id=check.check_id,
                campaign_id=campaign_id,
                title=check.title,
                rationale=check.description,
                severity=check.severity,
                fix_type=fix_type_map.get(check.check_id, "config"),
                optiscore_delta=_OPTISCORE_DELTA.get(check.check_id, 0.0),
                can_auto_apply=can_auto_apply,
                tag_snippet=tag_snippet,
                mutation_payload=mutation_payload,
                implementation_guide=implementation_guide,
            )
        )

    cards.sort(key=lambda c: c.optiscore_delta, reverse=True)
    return cards


# Tool execute function
async def _verify_conversion_health(params: dict, context: dict) -> ToolResult:
    """Run 8 conversion tracking health checks for a campaign."""
    campaign_id = str(params.get("campaign_id") or "").strip()
    logger.info("verify_conversion_health: START campaign=%s", campaign_id)

    from app.agents.adzump.agents.optimization.tools.google._channel import (
        prepare_google_campaign_tool,
    )

    prep = await prepare_google_campaign_tool(
        context, campaign_id, section="conversion_health"
    )
    if isinstance(prep, ToolResult):
        return prep

    headers = context.get("headers", {})
    client_code = context.get("client_code", "")
    session_ctx = context.get("session_context", {})
    customer_id = prep.account_id
    login_customer_id = prep.login_customer_id

    try:
        data = await conversion_metrics_adapter.fetch_conversion_health(
            customer_id=customer_id,
            login_customer_id=login_customer_id,
            client_code=client_code,
            auth_headers=headers,
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error(
            "verify_conversion_health: data_collection FAILED campaign=%s "
            "account=%s error=%s",
            campaign_id,
            customer_id,
            str(e),
            exc_info=True,
        )
        return ToolResult(
            success=False,
            error=(
                f"Couldn't retrieve conversion data for campaign {campaign_id}. "
                "The Google Ads API may be temporarily unavailable. "
                "Please try again shortly."
            ),
        )

    campaign_context: dict | None = None
    for c in session_ctx.get("_campaign_contexts", []):
        if c.get("campaign_id") == campaign_id:
            campaign_context = c
            break

    if not campaign_context:
        try:
            from app.agents.adzump.adapters.google.campaign_metrics import (
                campaign_metrics_adapter,
            )

            contexts = await campaign_metrics_adapter.fetch_campaign_contexts(
                customer_id=customer_id,
                login_customer_id=login_customer_id,
                client_code=client_code,
                auth_headers=headers,
                campaign_ids=[campaign_id],
            )
            if contexts:
                campaign_context = contexts[0]
        except Exception as exc:
            logger.warning(
                "verify_conversion_health: failed to fetch campaign context: %s",
                exc,
            )

    checks = [
        _check_no_active_actions(data),
        _check_primary_not_biddable(data),
        _check_tag_missing(data),
        _check_conversion_drop_detected(data, campaign_context),
        _check_deprecated_attribution(data),
        _check_wrong_counting(data),
        _check_no_value(data),
        _check_insufficient_signal(campaign_context),
    ]

    score, label = _compute_health(checks)
    fix_cards = _build_fix_cards(checks, campaign_id, data["actions"], customer_id=customer_id)

    signal_raw = session_ctx.get("conversion_signal")
    conversion_signal = None
    if signal_raw:
        try:
            conversion_signal = ConversionSignal(**signal_raw)
        except Exception:
            logger.info("conversion_signal parse failed", exc_info=True)

    report = ConversionHealthReport(
        campaign_id=campaign_id,
        health_label=label,
        health_score=score,
        conversion_signal=conversion_signal,
        checks=checks,
        fix_cards=fix_cards,
        evaluated_at=datetime.now(timezone.utc).isoformat(),
    )

    # Stash in session context so optimize.py can assemble the full CampaignRecommendation
    session_ctx["_last_conversion_health"] = report.model_dump()
    session_ctx.setdefault("_fresh_recommendations", {}).setdefault(campaign_id, {})[
        "conversion_health"
    ] = report.model_dump()

    failing = [check for check in checks if not check.passed]
    auto_apply_ids = {card.check_id for card in fix_cards if card.can_auto_apply}
    summary_lines = [
        f"Conversion Health: {label} ({score}/100)",
        f"Checks: {len(checks) - len(failing)} passed, {len(failing)} failing",
        "",
    ]
    for check in failing:
        summary_lines.append(f"[{check.severity.upper()}] {check.title}")
        summary_lines.append(f"  {check.description}")
        if check.check_id in auto_apply_ids:
            summary_lines.append("Can be auto-applied via API")
        summary_lines.append("")

    if fix_cards:
        summary_lines.append(f"{len(fix_cards)} fix card(s) generated.")
        auto = [fc for fc in fix_cards if fc.can_auto_apply]
        if auto:
            summary_lines.append(
                f"{len(auto)} can be auto-applied: "
                + ", ".join(fc.check_id for fc in auto)
            )

    logger.info(
        "verify_conversion_health: campaign=%s score=%d label=%s failing=%d",
        campaign_id,
        score,
        label,
        len(failing),
    )

    return ToolResult(
        success=True,
        data={"report": report.model_dump()},
        summary="\n".join(summary_lines),
    )


# Tool definition exposed to the agent
verify_conversion_health = ToolDefinition(
    name="verify_conversion_health",
    description=(
        "Run 8 diagnostic checks on a campaign's conversion tracking setup. "
        "Call this when the conversion_signal status is 'dropping' or 'untracked', "
        "or when you are about to recommend TARGET_CPA, TARGET_ROAS, or "
        "MAXIMIZE_CONVERSIONS, or when the user asks about conversion tracking health. "
        "Returns a health report with fix cards for any failing checks."
    ),
    display_name="Verify Conversion Health",
    parameters=[
        ToolParameter(
            name="campaign_id",
            type="string",
            description="Google Ads campaign ID to run health checks against.",
            required=True,
        ),
    ],
    execute=_verify_conversion_health,
)
