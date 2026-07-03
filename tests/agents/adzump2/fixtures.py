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
