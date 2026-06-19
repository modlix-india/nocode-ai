"""Conversion-health remediation: auto-apply mutation payloads + manual fix guides.

Pure (no I/O) — unit-testable on its own. A fix is auto-applyable only when the API
exposes a mutable field (verified against the proto field_behavior annotations); the
owning check attaches the per-entity mutation as ``auto_fix`` and build_mutation_payload
surfaces it. Everything the API can't do unattended (deploying a tag, choosing a
value, accumulating volume) falls back to a manual guide whose steps describe the
action, not brittle UI menu paths.

Sources (verified 2026-06-17):
  Counting (ecommerce "Every" / leads "One"): https://support.google.com/google-ads/answer/3438531
  Data-driven attribution available to all conversion actions, no min-volume
  threshold: https://support.google.com/google-ads/answer/6394265
"""

from __future__ import annotations

from app.agents.adzump.recommendations.models import (
    ConversionFixGuide,
    ConversionFixStep,
    ConversionHealthCheck,
)


# Manual remediation guides keyed by check_id — the fallback for fixes the API
# can't perform unattended. code_step_index marks which step gets the tag snippet.
_FIX_GUIDES: dict[str, dict] = {
    "no_active_conversion_actions": {
        "title": "Set up conversion tracking",
        "summary": "Smart Bidding needs at least one active conversion action to optimise toward.",
        "estimated_time": "20-30 min",
        "steps": [
            "Open the Conversions page in Google Ads (under Goals).",
            "Create a new conversion action and choose its source (Website, App, Phone, or Import).",
            "Define the action (category, value, counting) and save.",
            "Deploy the generated Google tag and event snippet, then verify they fire.",
        ],
    },
    "primary_goal_not_biddable": {
        "title": "Set the primary conversion goal",
        "summary": "No active conversion action is set as the primary goal for Smart Bidding to optimise toward.",
        "estimated_time": "5-10 min",
        "steps": [
            "Open the Conversions / Goals page in Google Ads.",
            "Choose the conversion action that best represents your business objective.",
            "Set it as the account-default (primary) goal.",
        ],
    },
    "tag_snippets_missing": {
        "title": "Deploy the conversion tag",
        "summary": "Active website conversion actions have no tag on the site, so conversions can't be recorded.",
        "estimated_time": "15-20 min",
        "steps": [
            "Open the affected conversion action and view its tag setup.",
            "Copy the global site tag and event snippet shown here.",
            "Install the global site tag on every page and the event snippet on the conversion page.",
            "Confirm with Google Tag Assistant that the tag fires on conversion.",
        ],
        "code_step_index": 1,
    },
    "conversion_drop_detected": {
        "title": "Investigate the conversion drop",
        "summary": "The campaign is serving but recorded zero conversions in 30 days — usually a broken tag or upload.",
        "estimated_time": "15-30 min",
        "steps": [
            "Confirm recent site or checkout changes didn't remove the conversion tag.",
            "For tag-based tracking, verify the tag fires with Google Tag Assistant.",
            "For API or offline uploads, check the upload job is running and GCLIDs are captured.",
            "Cross-check analytics to confirm conversions actually stopped, not just recording.",
        ],
    },
    "no_conversion_value": {
        "title": "Add a conversion value",
        "summary": "Revenue actions have no value set, so Target ROAS bidding has no revenue signal.",
        "estimated_time": "5-10 min",
        "steps": [
            "Open the affected conversion action's value settings.",
            "Choose dynamic per-conversion values, or set a default value that represents the action's worth.",
            "For dynamic values, pass value and currency parameters in the event snippet.",
        ],
    },
    "insufficient_signal": {
        "title": "Build conversion volume",
        "summary": "The bidding strategy has fewer conversions than it needs and is in extended learning.",
        "estimated_time": "Ongoing",
        "steps": [
            "Avoid large budget or target changes while the strategy is learning.",
            "Broaden targeting or add a higher-volume secondary conversion to add signal.",
            "If volume stays low, consider Maximize Clicks or Manual CPC until conversions accumulate.",
        ],
    },
}


def build_mutation_payload(check: ConversionHealthCheck) -> dict | None:
    """Return the check's ``auto_fix`` mutation only if every target has a
    resource_name; incomplete → None so the caller falls back to a manual guide."""
    fix = (check.metadata or {}).get("auto_fix")
    if not isinstance(fix, dict):
        return None
    updates = fix.get("updates") or []
    if not updates or any(not u.get("resource_name") for u in updates):
        return None
    return fix


def build_implementation_guide(
    check: ConversionHealthCheck,
    tag_snippet: dict | None,
) -> ConversionFixGuide | None:
    """Step-by-step manual remediation for checks the API can't auto-apply."""
    spec = _FIX_GUIDES.get(check.check_id)
    if not spec:
        return None

    snippet_code = None
    if tag_snippet:
        snippet_code = (
            "\n\n".join(
                part
                for part in (
                    tag_snippet.get("global_site_tag", ""),
                    tag_snippet.get("event_snippet", ""),
                )
                if part
            )
            or None
        )
    code_step = spec.get("code_step_index")

    steps = [
        ConversionFixStep(
            step_number=idx + 1,
            instruction=instruction,
            code=snippet_code if (idx == code_step and snippet_code) else None,
        )
        for idx, instruction in enumerate(spec["steps"])
    ]
    return ConversionFixGuide(
        title=spec["title"],
        summary=spec["summary"],
        steps=steps,
        estimated_time=spec.get("estimated_time", ""),
        can_auto_apply=False,
    )
