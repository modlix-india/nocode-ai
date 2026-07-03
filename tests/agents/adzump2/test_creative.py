"""Unit: app/agents/adzump2/creative (A4) — offline, no network, no LLM, no DB.

The single LLM seam (``CreativeAgent._llm_json``) is monkeypatched with canned,
purpose-keyed outputs, so the whole strategy → best-of-N copy → image brief →
attribute-tag → critic/repair pipeline + the lead-form builder are provable with
no live model.

Covers (A4-creative.md §8):
  (a) Creative schema: id/format/copy(pools)/attributes/asset_refs/predict_score
  (b) attribute tags are within the J5 taxonomy axes (keys ⊆ axes; closed values ∈ vocab)
  (c) RSA slot-count pools satisfied (>=3 headlines, 2-4 descriptions), no shortfall
  (d) predict_score is None + a TODO(J20) is present (creative + result-level)
  (e) the critic loop is BOUNDED (initial critique + <=MAX_CRITIC_REPAIR repairs)
      and a persistently-weak creative is flagged EXPLORE, never crashes / never
      dropped on predict (predict is stubbed in P1)
  (f) lead-form retry on malformed structure, then vertical fallback
  (g) validate_attributes keeps a novel value but flags it (CONTRACT §6 rule 9)
  (h) the generate_creatives tool runs offline with an inline profile

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump2.test_creative -v
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest import mock

from app.config import settings

# Provider-key checks must never bite an offline unit test (set before import).
for _key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY"):
    if not getattr(settings, _key, ""):
        setattr(settings, _key, "offline-test-key")

from app.agents.adzump2.creative.creative import (
    CRITIC_THRESHOLD,
    MAX_CRITIC_REPAIR,
    MAX_LLM_CALLS,
    CreativeAgent,
    get_creative_agent,
)
from app.agents.adzump2.creative.leadform import build_lead_form, fallback_lead_form
from app.agents.adzump2.creative.models import EXPLORE, LAUNCH, Copy, Creative, CreativeSet, LeadForm
from app.agents.adzump2.creative.taxonomy import (
    FORMAT_SLOTS,
    get_taxonomy,
    normalize_pools,
    validate_attributes,
)
from app.agents.adzump2.creative.tools import generate_creatives


# ── canned model outputs (a real-estate product) ────────────────────────────

PROFILE: dict[str, Any] = {
    "name": "Valmark Cityville",
    "vertical": "real_estate",
    "pitch": "Premium 2 & 3 BHK apartments in Whitefield with assured rental ROI.",
    "value_props": ["Assured 12% ROI", "Walk to metro", "RERA approved", "Ready 2027"],
    "offerings": ["2 BHK", "3 BHK"],
    "price_band": "80L-1.2Cr",
    "geo": ["Whitefield", "Marathahalli"],
    "tone": "premium, aspirational",
    "assets": [{"url": "https://cdn.example/hero.jpg", "role": "hero"}],
    "brand_url": "https://cityville.example",
}

STRATEGY_OUT = [
    {
        "angle": "investment_roi",
        "rationale": "Assured 12% ROI is the strongest differentiator.",
        "attributes": {
            "visualSubject": "interior_render",
            "offer": "pre_launch_price",
            "cta": "book_now",
            "audiencePairing": "nri_investors",
            "copyAttributes": ["number_led", "urgency"],
        },
    },
    {
        "angle": "location",
        "rationale": "Walk-to-metro convenience for end users.",
        "attributes": {
            "visualSubject": "location_map",
            "offer": "launch_offer",
            "cta": "schedule_visit",
            "audiencePairing": "end_users",
            "copyAttributes": ["location_led"],
        },
    },
    {
        "angle": "amenities",
        "rationale": "Clubhouse + amenities for upgraders.",
        "attributes": {
            "visualSubject": "amenity",
            "offer": "limited_units",
            "cta": "enquire_now",
            "audiencePairing": "upgraders",
            "copyAttributes": ["benefit_led"],
        },
    },
]

# One copy variant, short enough to satisfy BOTH RSA (<=30 headline / <=90 desc)
# and Meta/IMAGE (<=40 headline / <=30 desc / <=125 primary) slot specs.
_ONE_VARIANT = {
    "headlines": [
        "2 & 3 BHK in Whitefield",   # 23
        "Assured 12% ROI Homes",     # 22 (number-led → hook)
        "Walk to Metro",             # 13
        "RERA Approved Homes",       # 19
        "Book a Site Visit",         # 17
    ],
    "primary_texts": [
        "Own a premium 2-3 BHK in Whitefield with assured ROI. RERA-approved.",
        "Site visits open this weekend, book your slot now.",
    ],
    "descriptions": [
        "RERA-approved. Visits open.",   # 27
        "Ready 2027. Book now.",         # 21
        "Assured 12% ROI homes.",        # 22
    ],
    "cta": "book_now",
}
COPY_OUT = {"variants": [dict(_ONE_VARIANT), dict(_ONE_VARIANT), dict(_ONE_VARIANT)]}

CRITIQUE_PASS = {"scores": [{"index": 0, "score": 0.85, "by_axis": {"clarity": 0.9}, "issues": []}]}
CRITIQUE_FAIL = {"scores": [{"index": 0, "score": 0.30, "by_axis": {"clarity": 0.4},
                             "issues": ["vague hook", "no proof point"]}]}

LEADFORM_OUT = {
    "fields": [
        "FULL_NAME",
        "PHONE",
        "EMAIL",
        {"key": "budget", "type": "CHOICE", "label": "Budget", "options": ["<80L", "80L-1.2Cr"]},
    ],
    "privacyUrl": "https://cityville.example/privacy",
    "thankyou": "Thanks! We'll call to schedule a visit.",
}
LEADFORM_MALFORMED = {"foo": "bar"}  # no fields → MalformedLeadForm


def _scripted_llm(scripts: dict[str, Any], counter: dict[str, int]):
    """Build a fake ``_llm_json`` seam. ``scripts[purpose]`` is a payload, a list
    (consumed in order, last repeats), or a callable(call_index)->payload."""

    async def fake(task: str, *, purpose: str, auth=None, event_stream=None):
        counter[purpose] = counter.get(purpose, 0) + 1
        spec = scripts.get(purpose)
        if callable(spec):
            return spec(counter[purpose] - 1)
        if isinstance(spec, list):
            i = min(counter[purpose] - 1, len(spec) - 1)
            return spec[i]
        return spec

    return fake


def _fresh_agent(scripts: dict[str, Any], counter: dict[str, int]) -> CreativeAgent:
    """A non-singleton CreativeAgent with the LLM seam scripted (dies per test)."""
    agent = CreativeAgent()
    agent._llm_json = _scripted_llm(scripts, counter)  # type: ignore[assignment]
    return agent


# ── taxonomy / pool primitives ───────────────────────────────────────────────


class TaxonomyTests(unittest.TestCase):
    def test_validate_attributes_keys_subset_and_novel_value_warned(self) -> None:
        tax = get_taxonomy("real_estate")
        attrs = {
            "angle": "investment_roi",         # valid closed value
            "offer": "moon_pricing",            # NOVEL closed value → kept + warned
            "hook": "12% assured ROI",          # free text
            "copyAttributes": ["number_led", "made_up"],  # one valid, one novel
            "not_an_axis": "x",                 # unknown axis → dropped
        }
        clean, warnings = validate_attributes(attrs, tax)
        # keys strictly within the taxonomy axes
        self.assertTrue(set(clean).issubset(set(tax.axes)))
        self.assertNotIn("not_an_axis", clean)
        # novel value kept (exploration), not hard-failed
        self.assertEqual(clean["offer"], "moon_pricing")
        self.assertIn("made_up", clean["copyAttributes"])
        self.assertIn("number_led", clean["copyAttributes"])
        # and each was flagged
        self.assertTrue(any("moon_pricing" in w for w in warnings))
        self.assertTrue(any("made_up" in w for w in warnings))
        self.assertTrue(any("not_an_axis" in w for w in warnings))

    def test_rsa_pool_normalization(self) -> None:
        pools, shortfalls = normalize_pools(dict(_ONE_VARIANT), "RSA")
        specs = {s.field: s for s in FORMAT_SLOTS["RSA"]}
        self.assertGreaterEqual(len(pools["headlines"]), specs["headlines"].min)
        self.assertLessEqual(len(pools["headlines"]), specs["headlines"].max)
        self.assertGreaterEqual(len(pools["descriptions"]), specs["descriptions"].min)
        self.assertLessEqual(len(pools["descriptions"]), specs["descriptions"].max)
        self.assertEqual(pools["primary_texts"], [])  # RSA has no primary text
        self.assertEqual(shortfalls, [])

    def test_pool_shortfall_reported_not_fabricated(self) -> None:
        thin = {"headlines": ["only one"], "descriptions": [], "primary_texts": [], "cta": ""}
        pools, shortfalls = normalize_pools(thin, "RSA")
        self.assertEqual(len(pools["headlines"]), 1)  # never padded with invented copy
        self.assertTrue(any("headlines" in s for s in shortfalls))
        self.assertTrue(any("descriptions" in s for s in shortfalls))


# ── the full generate() pipeline ─────────────────────────────────────────────


class GeneratePipelineTests(unittest.TestCase):
    def _run_happy(self) -> tuple[CreativeSet, dict[str, int]]:
        counter: dict[str, int] = {}
        agent = _fresh_agent(
            {
                "strategy": STRATEGY_OUT,
                "copy": COPY_OUT,
                "critique": CRITIQUE_PASS,
                "leadform": LEADFORM_OUT,
            },
            counter,
        )
        result = asyncio.run(
            agent.generate(profile=PROFILE, vertical="real_estate", formats=["RSA", "IMAGE"])
        )
        return result, counter

    def test_creative_schema_and_predict_stub(self) -> None:
        result, _ = self._run_happy()
        self.assertIsInstance(result, CreativeSet)
        self.assertEqual(result.vertical, "real_estate")
        # 3 angles x 2 formats = 6 creatives
        self.assertEqual(len(result.creatives), 6)

        for c in result.creatives:
            self.assertIsInstance(c, Creative)
            self.assertTrue(c.id)
            self.assertIn(c.format, {"RSA", "IMAGE"})
            self.assertIsInstance(c.copy, Copy)
            self.assertIsInstance(c.copy.headlines, list)
            self.assertIsInstance(c.copy.descriptions, list)
            self.assertIsInstance(c.copy.primary_texts, list)
            self.assertIsInstance(c.attributes, dict)
            self.assertIsInstance(c.asset_refs, list)
            # predict is STUBBED in P1 — None + a clear TODO(J20)
            self.assertIsNone(c.predict_score)
            self.assertIn("TODO(J20)", c.predict_note)

        # result-level predict stub
        self.assertEqual(result.predict["status"], "STUBBED")
        self.assertFalse(result.predict["scored"])
        self.assertIsNone(result.predict["floor"])
        self.assertIn("TODO(J20)", result.predict["todo"])

    def test_attributes_within_taxonomy_axes(self) -> None:
        result, _ = self._run_happy()
        tax = get_taxonomy("real_estate")
        for c in result.creatives:
            # every attribute KEY is a known taxonomy axis
            self.assertTrue(set(c.attributes).issubset(set(tax.axes)), c.attributes)
            self.assertIn("angle", c.attributes)
            self.assertIn(c.attributes["angle"], tax.axis("angle").values)
            # closed single-value axes carry vocab values (canned data is valid)
            for axis_name in ("visualSubject", "offer", "cta", "audiencePairing"):
                if axis_name in c.attributes:
                    self.assertIn(c.attributes[axis_name], tax.axis(axis_name).values)
            if "copyAttributes" in c.attributes:
                for v in c.attributes["copyAttributes"]:
                    self.assertIn(v, tax.axis("copyAttributes").values)
            # hook is a free-text axis, present from the number-led headline
            self.assertIn("hook", c.attributes)

    def test_rsa_slot_pools_satisfied(self) -> None:
        result, _ = self._run_happy()
        rsa = [c for c in result.creatives if c.format == "RSA"]
        self.assertTrue(rsa)
        for c in rsa:
            self.assertGreaterEqual(len(c.copy.headlines), 3)
            self.assertLessEqual(len(c.copy.headlines), 15)
            self.assertGreaterEqual(len(c.copy.descriptions), 2)
            self.assertLessEqual(len(c.copy.descriptions), 4)
            self.assertEqual(c.pool_shortfalls, [])
            self.assertEqual(c.disposition, LAUNCH)

    def test_image_creatives_have_brief_and_assets(self) -> None:
        result, _ = self._run_happy()
        images = [c for c in result.creatives if c.format == "IMAGE"]
        self.assertTrue(images)
        for c in images:
            self.assertIsNotNone(c.image_brief)
            self.assertIn("TODO(J16", c.image_brief.todo)
            self.assertTrue(c.asset_refs)  # existing profile asset or IMG_TODO placeholder
            self.assertGreaterEqual(len(c.copy.primary_texts), 1)

    def test_lead_form_generated_in_pipeline(self) -> None:
        result, counter = self._run_happy()
        self.assertIsInstance(result.lead_form, LeadForm)
        self.assertEqual(result.lead_form.source, "GENERATED")
        types = {f.type for f in result.lead_form.fields}
        self.assertIn("FULL_NAME", types)
        self.assertIn("PHONE", types)
        self.assertEqual(counter.get("leadform"), 1)

    def test_plan_serialization_shape(self) -> None:
        result, _ = self._run_happy()
        c0 = result.creatives[0].to_plan_creative()
        self.assertIn("copy", c0)
        self.assertIn("headlines", c0["copy"])
        self.assertIsNone(c0["predictScore"])  # None on the wire in P1
        self.assertEqual(c0["source"], "GENERATED")
        lf = result.lead_form.to_plan_lead_form()
        self.assertIn("fields", lf)
        self.assertIn("privacyPolicyUrl", lf)


class CriticGateTests(unittest.TestCase):
    def test_bounded_critic_loop_and_explore_flag(self) -> None:
        # critic always fails → bounded repair, then flag EXPLORE (never dropped).
        counter: dict[str, int] = {}
        agent = _fresh_agent(
            {
                "strategy": [STRATEGY_OUT[0]],   # single angle
                "copy": COPY_OUT,
                "critique": CRITIQUE_FAIL,       # always below threshold
                "leadform": LEADFORM_OUT,
            },
            counter,
        )
        result = asyncio.run(
            agent.generate(profile=PROFILE, vertical="real_estate", formats=["RSA"],
                           n_angles=1, best_of_n=2)
        )
        self.assertEqual(len(result.creatives), 1)
        c = result.creatives[0]
        # critic score is below the floor → EXPLORE candidate, not launchable
        self.assertLess(c.critic_score, CRITIC_THRESHOLD)
        self.assertEqual(c.disposition, EXPLORE)
        self.assertEqual(result.launchable, [])
        self.assertEqual(len(result.explore), 1)
        # predict never hard-drops in P1
        self.assertIsNone(c.predict_score)
        # BOUNDED: 1 initial critique + MAX_CRITIC_REPAIR repair critiques
        self.assertEqual(counter["critique"], 1 + MAX_CRITIC_REPAIR)
        # copy: 1 initial best-of-N call + MAX_CRITIC_REPAIR repair calls
        self.assertEqual(counter["copy"], 1 + MAX_CRITIC_REPAIR)
        # total LLM calls stayed within the per-generate budget
        self.assertLessEqual(result.llm_calls, MAX_LLM_CALLS)

    def test_llm_budget_never_exceeded(self) -> None:
        counter: dict[str, int] = {}
        agent = _fresh_agent(
            {"strategy": STRATEGY_OUT, "copy": COPY_OUT,
             "critique": CRITIQUE_FAIL, "leadform": LEADFORM_OUT},
            counter,
        )
        result = asyncio.run(
            agent.generate(profile=PROFILE, vertical="real_estate",
                           formats=["RSA", "IMAGE"], n_angles=6, best_of_n=5)
        )
        self.assertLessEqual(result.llm_calls, MAX_LLM_CALLS)


# ── lead-form retry / fallback ───────────────────────────────────────────────


class LeadFormTests(unittest.TestCase):
    def test_retry_on_malformed_then_recover(self) -> None:
        counter: dict[str, int] = {}
        agent = _fresh_agent({"leadform": [LEADFORM_MALFORMED, LEADFORM_OUT]}, counter)
        tax = get_taxonomy("real_estate")
        form = asyncio.run(build_lead_form(agent, PROFILE, tax, retries=2))
        self.assertIsInstance(form, LeadForm)
        self.assertEqual(form.source, "GENERATED")
        self.assertEqual(counter["leadform"], 2)  # 1 malformed + 1 good
        keys = {f.type for f in form.fields}
        self.assertIn("FULL_NAME", keys)
        self.assertTrue(any(f.type == "CHOICE" and f.options for f in form.fields))

    def test_falls_back_after_exhausting_retries(self) -> None:
        counter: dict[str, int] = {}
        agent = _fresh_agent({"leadform": lambda _i: LEADFORM_MALFORMED}, counter)
        tax = get_taxonomy("real_estate")
        form = asyncio.run(build_lead_form(agent, PROFILE, tax, retries=2))
        self.assertEqual(form.source, "FALLBACK")
        self.assertEqual(counter["leadform"], 3)  # initial + 2 retries
        # fallback mirrors the vertical defaults
        expected = fallback_lead_form(PROFILE, tax)
        self.assertEqual(
            [f.type for f in form.fields], [f.type for f in expected.fields]
        )
        self.assertIn("FULL_NAME", {f.type for f in form.fields})

    def test_malformed_choice_without_options_is_retried(self) -> None:
        counter: dict[str, int] = {}
        bad_choice = {"fields": [{"key": "budget", "type": "CHOICE"}], "thankyou": "hi"}
        agent = _fresh_agent({"leadform": [bad_choice, LEADFORM_OUT]}, counter)
        tax = get_taxonomy("real_estate")
        form = asyncio.run(build_lead_form(agent, PROFILE, tax, retries=2))
        self.assertEqual(form.source, "GENERATED")
        self.assertEqual(counter["leadform"], 2)


# ── the generate_creatives tool (offline, inline profile) ────────────────────


class ToolTests(unittest.TestCase):
    def test_generate_creatives_tool_offline(self) -> None:
        counter: dict[str, int] = {}
        singleton = get_creative_agent()
        fake = _scripted_llm(
            {"strategy": STRATEGY_OUT, "copy": COPY_OUT,
             "critique": CRITIQUE_PASS, "leadform": LEADFORM_OUT},
            counter,
        )
        params = {
            "product_profile": PROFILE,
            "formats": ["RSA", "IMAGE"],
            "write_to_plan": False,  # no plan in this session → no backend call
        }
        context: dict[str, Any] = {"session_context": {}}
        with mock.patch.object(singleton, "_llm_json", fake):
            result = asyncio.run(generate_creatives.execute(params, context))
        self.assertTrue(result.success, result.error)
        self.assertIn("Generated", result.summary)
        data = result.data
        self.assertEqual(data["vertical"], "real_estate")
        self.assertEqual(data["counts"]["total"], 6)
        self.assertFalse(data["writtenToPlan"])
        self.assertIn("TODO(J20)", data["predict"]["todo"])
        # every creative on the wire carries a null predict score
        for c in data["creatives"]:
            self.assertIsNone(c["predict_score"])

    def test_tool_requires_profile(self) -> None:
        context: dict[str, Any] = {"session_context": {}}
        result = asyncio.run(generate_creatives.execute({"write_to_plan": False}, context))
        self.assertFalse(result.success)
        self.assertIn("profile", result.error.lower())


if __name__ == "__main__":
    unittest.main()
