"""Unit: app/agents/adzump2/product — the A2 product-study slice.

DETERMINISTIC: no network, no live LLM, no DB. The reused legacy ProductAgent
is monkeypatched at the ``study._resolve_product_agent`` seam so its canned
``AnalysisOutput`` drives the mapping; vertical deduction runs the REAL
deterministic heuristic (the study is called with no event stream / auth, which
is exactly the offline path ``VerticalDeducer.deduce`` falls back to). The J9
profile write is proven by patching ``SaasClient._request`` at the CLASS level.

Covers (A2 §5, §8):
  (a) result schema — profile / vertical / competitors / asset_gaps present
  (b) vertical deduction — clear real-estate → real_estate (no confirm);
      ambiguous → generic + a TAGGED present_options confirm (field="vertical")
  (c) competitor de-dup (by name AND by URL host) + source/confidence defaults
  (d) confirm_product_profile triggers the J9 PATCH /products/{id}/profile
      (with edits merge + vertical override), and errors cleanly with no study
      or no product_id
  (e) the pure heuristic + confidence policy directly

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump2.test_product_study -v
"""

from __future__ import annotations

import asyncio
import types
import unittest
from typing import Any
from unittest import mock

from app.config import settings

# Provider-key checks must never bite an offline unit test.
for _key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY"):
    if not getattr(settings, _key, ""):
        setattr(settings, _key, "offline-test-key")

from app.core.tools.base import ToolResult
from app.core.tools.http_client import SaasClient

from app.agents.adzump2.product import tools as product_tools
from app.agents.adzump2.product import study as study_module
from app.agents.adzump2.product.models import AssetGaps, ProductProfile, VerticalGuess
from app.agents.adzump2.product.tools import analyze_product, confirm_product_profile
from app.agents.adzump2.product.vertical import (
    LOW_CONFIDENCE_THRESHOLD,
    apply_confidence_policy,
    deduce_vertical_heuristic,
)

AUTH_HEADERS = {
    "Authorization": "Bearer offline-test",
    "clientCode": "SYSTEM",
    "appCode": "adzump",
}


# ── canned legacy AnalysisOutput fixtures ────────────────────────────────────

def _analysis(product: dict, competitive: dict, asset_gaps: AssetGaps | None = None,
              screenshot_url: str | None = None) -> types.SimpleNamespace:
    """A duck-typed stand-in for the legacy AnalysisOutput (attribute reads
    only: product / competitive / notes / screenshot_url / asset_gaps)."""
    return types.SimpleNamespace(
        product=product,
        competitive=competitive,
        notes=[],
        screenshot_url=screenshot_url,
        raw_text="",
        asset_gaps=asset_gaps or AssetGaps(),
    )


RE_PRODUCT = {
    "product_name": "Valmark Cityville",
    "business_type": "Residential real estate developer",
    "business_scale": "local",
    "location": "Whitefield, Bangalore",
    "suggested_locations": ["Marathahalli", "Sarjapur"],
    "summary": "2 & 3 BHK RERA-approved apartments in Whitefield with assured ROI. Possession 2027.",
    "unique_features": ["Assured ROI", "Walk to metro", "RERA approved"],
    "products_services": ["2 BHK apartments", "3 BHK apartments"],
    "pricing": "₹80L - ₹1.2Cr",
    "contact": {"phone": "080-123", "email": "", "address": "Whitefield"},
    "pages_analyzed": ["https://valmark.example/cityville"],
}

RE_COMPETITIVE = {
    "competitors": [
        {"name": "Prestige Group", "url": "https://prestige.example",
         "why_competitor": "same-city apartments"},
        {"name": "prestige group", "url": "https://prestige.example/projects"},  # dup name+host
        {"name": "Sobha Ltd", "url": "https://www.sobha.example"},
        {"name": "Sobha Limited", "url": "https://sobha.example/whitefield"},     # dup host
        {"name": "Brigade Enterprises", "url": None},                             # LLM (no url)
        {"name": "Brigade Enterprises", "url": None},                             # dup name
    ],
    "our_usps": ["RERA approved", "Metro connectivity"],
}

SAAS_PRODUCT = {
    "product_name": "Flowdesk Analytics",
    "business_type": "B2B SaaS analytics platform",
    "business_scale": "national",
    "location": "Remote",
    "suggested_locations": [],
    "summary": "Dashboards and reporting for product teams. Starts at $49/mo.",
    "unique_features": ["Real-time dashboards", "Slack alerts"],
    "products_services": ["Dashboards", "Reports", "Alerts"],
    "pricing": "$49/mo",
    "contact": {},
    "pages_analyzed": [],
}

SAAS_COMPETITIVE = {"competitors": []}


def _patch_product_agent(analysis: types.SimpleNamespace):
    """Return a mock.patch replacing the legacy-ProductAgent seam with a stub
    whose async ``analyze(**kwargs)`` yields the canned analysis."""
    class _StubProductAgent:
        async def analyze(self, **kwargs: Any) -> types.SimpleNamespace:
            return analysis

    stub = _StubProductAgent()
    return mock.patch.object(study_module, "_resolve_product_agent", lambda: stub)


def _context(session_context: dict | None = None) -> dict[str, Any]:
    """Offline tool context — NO event_stream/auth, so deduction uses the
    deterministic heuristic (the documented offline path)."""
    return {
        "session_id": "SYSTEM_test0001",
        "headers": dict(AUTH_HEADERS),
        "session_context": session_context if session_context is not None else {},
    }


def _run(coro):
    return asyncio.run(coro)


# ── (a) + (b clear) result schema + clear-RE deduction ───────────────────────
class StudySchemaAndClearVerticalTests(unittest.TestCase):
    def test_clear_real_estate_schema_and_no_confirm(self):
        ctx = _context()
        with _patch_product_agent(_analysis(RE_PRODUCT, RE_COMPETITIVE,
                                            screenshot_url="https://cdn.example/shot.jpg")):
            result = _run(analyze_product.execute({"url": "https://valmark.example"}, ctx))

        self.assertTrue(result.success, result.error)
        study = result.data["study"]

        # schema: all four artifact sections present
        for key in ("profile", "vertical", "competitors", "asset_gaps", "needs_vertical_confirm"):
            self.assertIn(key, study)
        profile = study["profile"]
        for slot in ("name", "pitch", "value_props", "offerings", "geo",
                     "price_band", "brand", "tone", "assets", "attributes"):
            self.assertIn(slot, profile, f"profile missing slot {slot}")

        # profile mapping from the legacy business dict
        self.assertEqual(profile["name"], "Valmark Cityville")
        self.assertEqual(profile["price_band"], "₹80L - ₹1.2Cr")
        self.assertEqual(profile["offerings"], ["2 BHK apartments", "3 BHK apartments"])
        self.assertIn("Whitefield, Bangalore", profile["geo"])
        self.assertIn("Marathahalli", profile["geo"])
        self.assertIn("https://cdn.example/shot.jpg", profile["assets"])
        # our_usps folded into value_props (deduped)
        self.assertIn("Assured ROI", profile["value_props"])
        self.assertIn("Metro connectivity", profile["value_props"])
        self.assertEqual(profile["attributes"]["business_type"], "Residential real estate developer")

        # clear real estate → real_estate at/above threshold, no confirm
        self.assertEqual(study["vertical"]["code"], "real_estate")
        self.assertGreaterEqual(study["vertical"]["confidence"], LOW_CONFIDENCE_THRESHOLD)
        self.assertFalse(study["needs_vertical_confirm"])
        self.assertNotEqual(result.data.get("elicited"), True)

        # stash for confirm + J19
        self.assertIn("_product_study", ctx["session_context"])
        self.assertIn("_product_competitors", ctx["session_context"])


# ── (b ambiguous) generic + tagged confirm ───────────────────────────────────
class AmbiguousVerticalConfirmTests(unittest.TestCase):
    def test_ambiguous_maps_to_generic_and_raises_tagged_confirm(self):
        ctx = _context()
        with _patch_product_agent(_analysis(SAAS_PRODUCT, SAAS_COMPETITIVE)):
            result = _run(analyze_product.execute({"url": "https://flowdesk.example"}, ctx))

        self.assertTrue(result.success, result.error)
        study = result.data["study"]
        self.assertEqual(study["vertical"]["code"], "generic")
        self.assertTrue(study["needs_vertical_confirm"])

        # runtime elicitation signalled to the run loop, tagged for capture
        self.assertTrue(result.data.get("elicited"))
        self.assertEqual(result.data.get("elicit_field"), "vertical")
        self.assertEqual(result.data.get("elicit_expects"), "single")

        # the reused present_options fired: tagged suggestions stashed with the
        # vertical options (real_estate + generic), single-select
        sugg = ctx["session_context"].get("_pending_suggestions")
        self.assertIsInstance(sugg, dict)
        self.assertEqual(sugg.get("mode"), "single")
        values = {o["value"] for o in sugg.get("options", [])}
        self.assertIn("real_estate", values)
        self.assertIn("generic", values)
        # per-option capture answers wired
        answers = result.data.get("elicit_answers") or {}
        self.assertEqual(answers.get("real_estate"), "real_estate")


# ── (c) competitor de-dup ────────────────────────────────────────────────────
class CompetitorDedupTests(unittest.TestCase):
    def test_dedup_by_name_and_host_with_defaults(self):
        ctx = _context()
        with _patch_product_agent(_analysis(RE_PRODUCT, RE_COMPETITIVE)):
            result = _run(analyze_product.execute({"url": "https://valmark.example"}, ctx))

        competitors = result.data["study"]["competitors"]
        names = [c["name"] for c in competitors]
        self.assertEqual(names, ["Prestige Group", "Sobha Ltd", "Brigade Enterprises"],
                         f"expected 3 deduped competitors, got {names}")

        by_name = {c["name"]: c for c in competitors}
        # url-backed → WEB + 0.7; no url → LLM + 0.5
        self.assertEqual(by_name["Prestige Group"]["source"], "WEB")
        self.assertEqual(by_name["Prestige Group"]["confidence"], 0.7)
        self.assertEqual(by_name["Brigade Enterprises"]["source"], "LLM")
        self.assertIsNone(by_name["Brigade Enterprises"]["url"])
        self.assertEqual(by_name["Brigade Enterprises"]["confidence"], 0.5)


# ── asset-gap elicitation branch ─────────────────────────────────────────────
class AssetGapElicitationTests(unittest.TestCase):
    def test_open_asset_gaps_defer_for_upload(self):
        ctx = _context()
        gaps = AssetGaps(logo_missing=True, missing_categories=["hero"], verdict="needs_upload")
        with _patch_product_agent(_analysis(RE_PRODUCT, RE_COMPETITIVE, asset_gaps=gaps)):
            result = _run(analyze_product.execute({"url": "https://valmark.example"}, ctx))

        self.assertTrue(result.success)
        # RE is confident → no vertical confirm, so the asset-gap branch fires
        self.assertFalse(result.data["study"]["needs_vertical_confirm"])
        self.assertTrue(result.data.get("elicited"))
        self.assertEqual(result.data.get("elicit_expects"), "multi")
        self.assertEqual(result.data.get("elicit_payload", {}).get("logo_missing"), True)
        self.assertEqual(result.audience, "user")


# ── (d) confirm → J9 PATCH ───────────────────────────────────────────────────
class _RecordingSaas:
    """Class-level SaasClient._request replacement recording every call and
    returning a canned success.

    Patched as a class attribute, a callable OBJECT is not a descriptor, so
    ``client._request(method, path, ...)`` calls ``__call__`` WITHOUT the client
    instance — the signature starts at ``method`` on purpose (see the same note
    on ``FakeSaasRequests`` in test_agent.py)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, method: str, path: str,
                       headers: dict | None = None, json: Any = None,
                       params: dict | None = None) -> ToolResult:
        self.calls.append({"method": method, "path": path, "json": json})
        return ToolResult(success=True, data={"updated": True},
                          summary=f"{method} {path} → 200")


class ConfirmProfileWriteTests(unittest.TestCase):
    def _seed_study_ctx(self, product_id_seed: str | None = None) -> dict[str, Any]:
        """Run analyze_product to populate the stash, then return the context."""
        ctx = _context()
        params: dict[str, Any] = {"url": "https://valmark.example"}
        if product_id_seed:
            params["product_id"] = product_id_seed
        with _patch_product_agent(_analysis(RE_PRODUCT, RE_COMPETITIVE)):
            res = _run(analyze_product.execute(params, ctx))
        self.assertTrue(res.success)
        return ctx

    def test_confirm_patches_product_profile_endpoint(self):
        ctx = self._seed_study_ctx()
        rec = _RecordingSaas()
        with mock.patch.object(SaasClient, "_request", rec):
            result = _run(confirm_product_profile.execute({"product_id": "prd_5521"}, ctx))

        self.assertTrue(result.success, result.error)
        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        self.assertEqual(call["method"], "PATCH")
        self.assertEqual(call["path"], "/api/adzump/products/prd_5521/profile")
        # J9 body carries the drafted profile + the deduced vertical
        self.assertIn("profile", call["json"])
        self.assertEqual(call["json"]["profile"]["name"], "Valmark Cityville")
        self.assertEqual(call["json"]["vertical"], "real_estate")
        # competitors returned for J19
        self.assertEqual(result.data["productId"], "prd_5521")
        self.assertEqual(len(result.data["competitors"]), 3)

    def test_confirm_uses_seeded_product_id_and_applies_edits_and_vertical(self):
        ctx = self._seed_study_ctx(product_id_seed="prd_777")
        rec = _RecordingSaas()
        edits = {"pitch": "Handcrafted homes", "attributes": {"tone": "premium"}}
        with mock.patch.object(SaasClient, "_request", rec):
            result = _run(confirm_product_profile.execute(
                {"vertical": "generic", "edits": edits}, ctx))

        self.assertTrue(result.success, result.error)
        call = rec.calls[0]
        self.assertEqual(call["path"], "/api/adzump/products/prd_777/profile")  # from stash
        prof = call["json"]["profile"]
        self.assertEqual(prof["pitch"], "Handcrafted homes")            # edit applied
        self.assertEqual(prof["attributes"]["tone"], "premium")          # attributes merged
        self.assertEqual(prof["attributes"]["business_type"],
                         "Residential real estate developer")            # drafted attr kept
        self.assertEqual(call["json"]["vertical"], "generic")            # override applied

    def test_confirm_without_study_errors(self):
        result = _run(confirm_product_profile.execute({"product_id": "prd_1"}, _context()))
        self.assertFalse(result.success)
        self.assertIn("analyze_product", result.error)

    def test_confirm_without_product_id_errors(self):
        ctx = self._seed_study_ctx()  # seeded via url, no product id stashed
        rec = _RecordingSaas()
        with mock.patch.object(SaasClient, "_request", rec):
            result = _run(confirm_product_profile.execute({}, ctx))
        self.assertFalse(result.success)
        self.assertIn("product_id", result.error)
        self.assertEqual(rec.calls, [], "must not call J9 without a product id")


# ── (e) pure heuristic + confidence policy ───────────────────────────────────
class HeuristicAndPolicyTests(unittest.TestCase):
    def test_heuristic_clear_real_estate(self):
        profile = ProductProfile(
            name="Valmark Cityville",
            pitch="2 & 3 BHK RERA-approved apartments; possession 2027",
            offerings=["2 BHK apartments"],
            attributes={"business_type": "Residential real estate developer"},
        )
        guess = deduce_vertical_heuristic(profile)
        self.assertEqual(guess.code, "real_estate")
        self.assertGreaterEqual(guess.confidence, LOW_CONFIDENCE_THRESHOLD)

    def test_heuristic_ambiguous_is_generic_low_confidence(self):
        profile = ProductProfile(
            name="Flowdesk Analytics",
            pitch="Dashboards and reporting for product teams",
            offerings=["Dashboards", "Reports"],
            attributes={"business_type": "B2B SaaS analytics platform"},
        )
        guess = deduce_vertical_heuristic(profile)
        self.assertEqual(guess.code, "generic")
        self.assertLess(guess.confidence, LOW_CONFIDENCE_THRESHOLD)

    def test_policy_downgrades_low_confidence_specific_to_generic_confirm(self):
        low = VerticalGuess(code="real_estate", confidence=0.4, rationale="weak")
        eff, needs = apply_confidence_policy(low)
        self.assertEqual(eff.code, "generic")
        self.assertTrue(needs)

    def test_policy_keeps_confident_specific(self):
        high = VerticalGuess(code="real_estate", confidence=0.85, rationale="strong")
        eff, needs = apply_confidence_policy(high)
        self.assertEqual(eff.code, "real_estate")
        self.assertFalse(needs)

    def test_policy_generic_always_confirms(self):
        g = VerticalGuess(code="generic", confidence=0.9, rationale="")
        eff, needs = apply_confidence_policy(g)
        self.assertEqual(eff.code, "generic")
        self.assertTrue(needs)


if __name__ == "__main__":
    unittest.main()
