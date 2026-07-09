"""Shared fixtures for the adzump2 agent tests + offline eval harness.

Two things live here (both frozen, no network):

1. ``FAKE_PLAN`` — a fake CampaignPlan JSON matching the Java shape the
   adzump service persists (CONTRACT.md §1): extracted top-level columns
   (id, clientCode, status, name, productId, ...) + a JSON ``body`` holding
   the nested IR (objective, budget, schedule, adGroups, creatives, ...).
2. Completeness payloads mirroring ``GET /api/adzump/plans/{id}/completeness``
   for the two interesting states (missing slots vs complete).

The per-slot ``body`` payloads (``OBJECTIVE_PATCH`` etc.) are the RFC-7386
merge-patch bodies the eval scenarios send through ``update_plan`` — kept here
so the unit tests and ``scripts/adzump2/eval.py`` drive the exact same data.
"""

from __future__ import annotations

import copy
from typing import Any

# ── P0 required slots (mirror of the Java completeness rules) ─────────────
# "adGroups" is satisfied by body.adGroups OR body.assetGroups (PMax).
REQUIRED_SLOTS: list[str] = [
    "name",
    "productId",
    "objective",
    "budget",
    "schedule",
    "adGroups",
    "creatives",
]

# ── per-slot body payloads (the update_plan merge-patch building blocks) ──

OBJECTIVE = {
    "platformObjective": "LEADS",
    "targetMilestone": "SITE_VISIT",
    "conversionEvent": "lead_submit",
}

BUDGET = {
    "currency": "INR",
    "dailyBudget": {"amount": 3000, "currency": "INR"},
    "totalBudget": None,
}

SCHEDULE = {
    "startAt": "2026-07-15T00:00:00",
    "endAt": None,
    "timezone": "Asia/Calcutta",
    "optimizationCadence": "DAILY",
}

AD_GROUPS = [
    {
        "id": "ag_1",
        "name": "Whitefield end-users",
        "platform": "GOOGLE",
        "targeting": {
            "geo": {"type": "PLACES", "places": ["Whitefield", "Marathahalli"]},
            "languages": ["en"],
            "keywords": [{"text": "2 bhk whitefield", "matchType": "PHRASE"}],
            "negativeKeywords": [{"text": "rent", "matchType": "BROAD"}],
        },
        "ads": [{"id": "ad_1", "creativeIds": ["cr_1"]}],
    }
]

CREATIVES = [
    {
        "id": "cr_1",
        "type": "TEXT",
        "headlines": ["2 & 3 BHK in Whitefield", "Book a Site Visit"],
        "descriptions": ["RERA approved. Possession 2027."],
    }
]

# Merge-patch bodies (RFC 7386) as the update_plan tool would send them.
OBJECTIVE_PATCH: dict[str, Any] = {"body": {"objective": OBJECTIVE}}
BUDGET_SCHEDULE_PATCH: dict[str, Any] = {"body": {"budget": BUDGET, "schedule": SCHEDULE}}
BUDGET_PATCH: dict[str, Any] = {"body": {"budget": BUDGET}}
OBJECTIVE_SCHEDULE_PATCH: dict[str, Any] = {"body": {"objective": OBJECTIVE, "schedule": SCHEDULE}}
GROUPS_CREATIVES_PATCH: dict[str, Any] = {"body": {"adGroups": AD_GROUPS, "creatives": CREATIVES}}

# ── the fake plan (Java CampaignPlan row shape: columns + JSON body) ──────

FAKE_PLAN: dict[str, Any] = {
    "schemaVersion": "1.0",
    "id": "cp_01HTEST0001",
    "revision": 1,
    "clientCode": "SYSTEM",
    "status": "DRAFT",
    "name": "Whitefield Launch - Site Visits",
    "productId": "prd_5521",
    "productTemplateId": "tmpl_apartments_sales",
    "vertical": "real_estate",
    "platforms": ["GOOGLE"],
    "campaignTypes": {"GOOGLE": "SEARCH"},
    "body": {
        "objective": OBJECTIVE,
        "budget": BUDGET,
        "schedule": SCHEDULE,
        "compliance": {"specialAdCategory": "HOUSING", "disclaimers": []},
        "adGroups": AD_GROUPS,
        "assetGroups": [],
        "creatives": CREATIVES,
        "leadForm": None,
        "landingPage": None,
    },
    "links": {"google": {"adAccountId": None, "campaignId": None}},
}

# ── completeness payloads (GET /plans/{id}/completeness response shape) ───

COMPLETENESS_MISSING_BUDGET_CREATIVES: dict[str, Any] = {
    "complete": False,
    "missingRequired": ["budget", "creatives"],
    "filled": ["name", "productId", "objective", "schedule", "adGroups"],
    "requiredSlots": list(REQUIRED_SLOTS),
}

COMPLETENESS_COMPLETE: dict[str, Any] = {
    "complete": True,
    "missingRequired": [],
    "filled": list(REQUIRED_SLOTS),
    "requiredSlots": list(REQUIRED_SLOTS),
}


def fake_plan(**overrides: Any) -> dict[str, Any]:
    """Deep copy of FAKE_PLAN with optional top-level overrides."""
    plan = copy.deepcopy(FAKE_PLAN)
    plan.update(overrides)
    return plan


# ══════════════════════════════════════════════════════════════════════════
# A5 diagnose (P3) — the seeded-underperformer read surface.
#
# Three frozen payloads mirroring the FIXED A5 contract endpoints on the
# adzump Java service (all under /api/adzump/plans/{planId}):
#   GET .../performance    → J10 PerformanceSnapshot   (DIAG_SNAPSHOT)
#   GET .../recommendations→ J12 ActionSet             (DIAG_ACTION_SET)
#   GET .../attribute-map  → J20 attribute map         (DIAG_ATTRIBUTE_MAP)
# plus DIAG_LLM_DIAGNOSIS — the canned _llm_json seam output the offline test
# monkeypatches in (a raw model Diagnosis, deliberately including a couple of
# gate/grounding VIOLATIONS the DiagnoseAgent must strip in code).
#
# The seeded story (real_estate): the investment_roi / NRI-investor angle WINS
# (books site visits, ~1.9x lift, low junk); the broad-keyword ad set
# (adset_broad) is the JUNK SOURCE (~45% budget-mismatch/junk); adset_new is a
# freshly-launched FAST_ONLY grain with spend but no matured CRM outcome yet.
# ══════════════════════════════════════════════════════════════════════════

# ── J10 PerformanceSnapshot ───────────────────────────────────────────────
DIAG_SNAPSHOT: dict[str, Any] = {
    "snapshotId": "snap_01HDIAG0001",
    "campaignPlanId": "cp_01HTEST0001",
    "clientCode": "SYSTEM",
    "vertical": "real_estate",
    "generatedAt": "2026-07-01T04:00:00",
    "window": {"from": "2026-06-01", "to": "2026-06-30", "timezone": "Asia/Calcutta"},
    "rows": [
        {
            "grain": "CAMPAIGN",
            "entityId": "camp_1",
            "blendedScore": 55.0,
            "signalMaturity": "MATURE",
            "platform": {"impressions": 240000, "clicks": 6100, "ctr": 0.0254,
                         "cpc": 24.0, "spend": 146000, "platformConversions": 320},
            "crm": {"leads": 220, "qualified": 52, "deals": 6, "junk": 49,
                    "costPerQualified": 2800, "costPerDeal": 24000,
                    "tags": [], "notes": ""},
        },
        {
            # THE WINNER — investor ROI angle, high blended score, low junk.
            "grain": "ADSET",
            "entityId": "adset_roi",
            "blendedScore": 78.0,
            "signalMaturity": "MATURE",
            "platform": {"impressions": 90000, "clicks": 2900, "ctr": 0.0322,
                         "cpc": 21.0, "spend": 61000, "platformConversions": 150},
            "crm": {"leads": 120, "qualified": 40, "deals": 6, "junk": 4,
                    "costPerQualified": 1525, "costPerDeal": 10166,
                    "tags": ["site-visit-booked", "investor-intent"],
                    "notes": "RM: NRI investors, strong intent, ROI-driven."},
        },
        {
            # THE JUNK SOURCE — broad keywords, low blended score, junk concentrates.
            "grain": "ADSET",
            "entityId": "adset_broad",
            "blendedScore": 28.0,
            "signalMaturity": "MATURE",
            "platform": {"impressions": 110000, "clicks": 2600, "ctr": 0.0236,
                         "cpc": 26.0, "spend": 67000, "platformConversions": 140},
            "crm": {"leads": 100, "qualified": 8, "deals": 0, "junk": 45,
                    "costPerQualified": 8375, "costPerDeal": None, "junkRate": 0.45,
                    "tags": ["budget-mismatch", "junk"],
                    "notes": "RM: leads want sub-60L homes, out of price band."},
        },
        {
            # THE THIN GRAIN — freshly launched, FAST_ONLY, no matured CRM outcome.
            "grain": "ADSET",
            "entityId": "adset_new",
            "blendedScore": 40.0,
            "signalMaturity": "FAST_ONLY",
            "platform": {"impressions": 18000, "clicks": 520, "ctr": 0.0289,
                         "cpc": 22.0, "spend": 11000, "platformConversions": 22},
            "crm": {"leads": 0, "qualified": 0, "deals": 0, "junk": 0,
                    "costPerQualified": None, "costPerDeal": None,
                    "tags": [], "notes": "Launched 3 days ago."},
        },
    ],
}

# ── J12 ActionSet (already significance-gated; NO action on the thin grain) ──
DIAG_ACTION_SET: dict[str, Any] = {
    "campaignPlanId": "cp_01HTEST0001",
    "snapshotId": "snap_01HDIAG0001",
    "generatedAt": "2026-07-01T04:05:00",
    "objectiveBefore": 55.0,
    "objectiveProjectedAfter": 60.8,
    "actions": [
        {
            "type": "SHIFT_BUDGET",
            "targetId": "adset_broad",
            "change": {"fromId": "adset_broad", "toId": "adset_roi", "shiftPct": 30},
            "rationale": "adset_broad blendedScore 28 vs adset_roi 78; reallocate within caps.",
            "expectedDelta": 4.2,
            "confidence": 0.82,
            "significanceVerdict": "SIGNIFICANT",
            "risk": "MED",
            "requiresApproval": True,
        },
        {
            "type": "ADD_NEGATIVE_KEYWORD",
            "targetId": "adset_broad",
            "change": {"keywords": ["cheap flats", "budget homes", "low price 2bhk"]},
            "rationale": "Broad terms drive budget-mismatch junk (junkRate 0.45).",
            "expectedDelta": 1.6,
            "confidence": 0.90,
            "significanceVerdict": "SIGNIFICANT",
            "risk": "LOW",
            "requiresApproval": True,
        },
    ],
}

# ── J20 attribute map (tenant-private; per axis+value) ─────────────────────
DIAG_ATTRIBUTE_MAP: dict[str, Any] = {
    "clientCode": "SYSTEM",
    "vertical": "real_estate",
    "attributes": [
        # WINNERS (exploit — high lift, enough volume + confidence): NOT gaps.
        {"axis": "angle", "value": "investment_roi", "outcomeLift": 1.9,
         "volume": 320, "confidence": 0.86, "junkCorrelation": 0.05},
        {"axis": "audiencePairing", "value": "nri_investors", "outcomeLift": 2.1,
         "volume": 210, "confidence": 0.80, "junkCorrelation": 0.04},
        # GAPS (explore — under-explored and/or junk-concentrated).
        {"axis": "angle", "value": "location", "outcomeLift": 0.9,
         "volume": 45, "confidence": 0.35, "junkCorrelation": 0.40},
        {"axis": "angle", "value": "possession_ready", "outcomeLift": None,
         "volume": 6, "confidence": 0.10, "junkCorrelation": 0.00},
        {"axis": "audiencePairing", "value": "end_users", "outcomeLift": 0.8,
         "volume": 14, "confidence": 0.20, "junkCorrelation": 0.30},
        {"axis": "offer", "value": "low_price_band", "outcomeLift": 0.5,
         "volume": 8, "confidence": 0.15, "junkCorrelation": 0.55},
    ],
}

# The gap axis+value set the engine must derive from DIAG_ATTRIBUTE_MAP.
DIAG_ATTRIBUTE_GAPS: set[tuple[str, str]] = {
    ("angle", "location"),
    ("angle", "possession_ready"),
    ("audiencePairing", "end_users"),
    ("offer", "low_price_band"),
}

# ── the canned _llm_json seam output (raw model Diagnosis) ─────────────────
# Deliberately contains: (a) a PAUSE_ENTITY on the thin grain adset_new that the
# engine did NOT gate → must be dropped from ranked_actions (thin → watchlist);
# (b) a "double down on investment_roi" test proposal grounded on a WINNER, not
# a gap → must be dropped. The two real J12 actions carry model why/priority.
DIAG_LLM_DIAGNOSIS: dict[str, Any] = {
    "narrative": (
        "The investment_roi angle paired with nri_investors is the clear winner — it books "
        "site visits and qualifies at ~1.9x baseline with almost no junk. The junk concentrates "
        "on the broad-keyword ad set (adset_broad): ~45% of its leads are tagged "
        "budget-mismatch/junk, wanting sub-60L homes outside your band. Shift budget off "
        "adset_broad to the ROI ad set and negative-keyword the wasteful terms. adset_new is "
        "only 3 days old — too early to judge."
    ),
    "ranked_actions": [
        {"target_id": "adset_broad", "type": "SHIFT_BUDGET", "priority": 1,
         "why": "Stop funding the junk-heavy broad ad set; feed the proven ROI winner."},
        {"target_id": "adset_broad", "type": "ADD_NEGATIVE_KEYWORD", "priority": 2,
         "why": "Cheapest, safest cut — block the budget-mismatch search terms."},
        {"target_id": "adset_new", "type": "PAUSE_ENTITY", "priority": 3,
         "why": "(model over-reach on a FAST_ONLY grain — the engine must strip this)"},
    ],
    "test_proposals": [
        {"hypothesis": "Test a possession-ready angle for end-users to convert the sub-60L "
                       "demand into in-band interest",
         "angle": "possession_ready", "audience": "end_users", "route": "A4",
         "grounds_on": {"axis": "angle", "value": "possession_ready"},
         "rationale": "possession_ready is unexplored (volume 6); end_users under-served."},
        {"hypothesis": "Test a lower-price-band hook where budget-mismatch junk is high",
         "angle": "location", "audience": "", "route": "A4",
         "grounds_on": {"axis": "offer", "value": "low_price_band"},
         "rationale": "offer=low_price_band is junk-concentrated (0.55)."},
        {"hypothesis": "Double down on the investor ROI angle",
         "angle": "investment_roi", "route": "A4",
         "grounds_on": {"axis": "angle", "value": "investment_roi"},
         "rationale": "It already wins — (not a gap; should be dropped)."},
    ],
    "watchlist": [
        {"target_id": "adset_new",
         "reason": "Launched 3 days ago; spend but zero CRM outcomes — wait for maturity."},
    ],
}
