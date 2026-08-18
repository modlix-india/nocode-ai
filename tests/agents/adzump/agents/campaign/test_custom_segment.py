"""Unit tests for custom segments
(google/audience/custom_segment.py, and the term/url limits in audience/models.py).
"""

# regression: validateOnly on customAudiences:mutate accepts an 11-word, 81-character keyword
# and zero members (probed live), so every published limit is ours to enforce before the call;
# and submit must be unreachable until a draft exists, so creating a real resource in the
# advertiser's account cannot happen on the turn the user merely asked about it.
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from pydantic import ValidationError

from app.agents.adzump.agents.campaign.google.audience import custom_segment as cs
from app.agents.adzump.agents.campaign.google.audience.models import (
    CustomSegmentTerm,
    CustomSegmentUrl,
)
from app.agents.adzump.agents.campaign.models import audience, set_audience

_CREATED = "customers/1/customAudiences/555"


def _ctx(**state):
    session_ctx: dict = {
        "campaign_spec": {
            "platform": "GOOGLE",
            "account": "1",
            "channel": "Demand Gen",
        },
        "aud_customer_id": "1",
        "aud_country": "IN",
        "aud_product_name": "Acme Homes",
        **state,
    }
    set_audience(
        session_ctx,
        {
            "signals": [
                {
                    "kind": "IN_MARKET",
                    "ref": "customers/1/userInterests/1",
                    "label": "Apartments",
                    "source": "TAXONOMY",
                    "rationale": "",
                    "path": [],
                    "negative": False,
                    "owned": False,
                    "metrics": None,
                }
            ],
            "demographics": {},
            "dimension_groups": [["customers/1/userInterests/1"]],
            "meta": {"country": "IN"},
        },
    )
    return {"session_context": session_ctx}


def _draft(ctx, ideas, suggestions=("home loan emi", "home loan rates")):
    """`ideas` is what the Planner returns - it EXPANDS beyond the seeds, so the tool must
    read its output rather than assume it gets back only what it sent."""

    async def fake_suggest(seeds, **kw):
        return [mock.Mock(keyword=s) for s in suggestions]

    async def fake_ideas(seeds, **kw):
        fake_ideas.seeds = seeds
        fake_ideas.url = kw.get("url")
        return ideas

    fake_ideas.seeds, fake_ideas.url = [], None
    ctx.setdefault("_probe", {})["ideas"] = fake_ideas

    with (
        mock.patch.object(cs.autosuggest, "fetch_suggestions", new=fake_suggest),
        mock.patch.object(cs.keyword_planner, "fetch_keyword_ideas", new=fake_ideas),
    ):
        return asyncio.run(
            cs._draft_custom_segment({"themes": ["home loans", "housing finance"]}, ctx)
        )


def _submit(ctx, params, existing=(), created=_CREATED):
    async def fake_list(**kw):
        return list(existing)

    async def fake_create(**kw):
        fake_create.calls.append(kw)
        return created

    fake_create.calls = []

    async def noop(*a, **kw):
        return None

    with (
        mock.patch.object(cs.custom_audience, "list_enabled", new=fake_list),
        mock.patch.object(cs.custom_audience, "create", new=fake_create),
        mock.patch(
            "app.agents.adzump.agents.campaign.tools.google.audience_update.emit_panel",
            new=noop,
        ),
    ):
        res = asyncio.run(cs._submit_custom_segment(params, ctx))
    return res, fake_create.calls


class TermLimitTests(unittest.TestCase):
    """The API will not reject these, so we must."""

    def test_over_ten_words_is_rejected(self):
        with self.assertRaises(ValidationError):
            CustomSegmentTerm(keyword=" ".join(["w"] * 11))

    def test_over_eighty_characters_is_rejected(self):
        with self.assertRaises(ValidationError):
            CustomSegmentTerm(keyword="a" * 81)

    def test_double_width_terms_use_the_halved_cap(self):
        # Google counts CJK against 40, not 80.
        CustomSegmentTerm(keyword="住" * 40)
        with self.assertRaises(ValidationError):
            CustomSegmentTerm(keyword="住" * 41)

    def test_whitespace_is_collapsed_before_measuring(self):
        self.assertEqual(
            CustomSegmentTerm(keyword="  home   loan  ").keyword, "home loan"
        )

    def test_a_url_must_carry_its_scheme(self):
        # proto: "An HTTP URL, protocol-included" - a bare domain is silently useless.
        with self.assertRaises(ValidationError):
            CustomSegmentUrl(url="example.com/loans")
        self.assertEqual(
            CustomSegmentUrl(url="https://example.com/loans").url,
            "https://example.com/loans",
        )


class DraftTests(unittest.TestCase):
    def test_zero_volume_terms_are_dropped(self):
        # A term nobody searches reaches nobody.
        ctx = _ctx()
        res = _draft(
            ctx,
            [
                {"keyword": "home loan emi", "volume": 900},
                {"keyword": "home loan rates", "volume": 0},
            ],
        )
        self.assertTrue(res.success)
        self.assertEqual([t["keyword"] for t in res.data["terms"]], ["home loan emi"])

    def test_terms_that_break_the_limits_are_never_offered(self):
        ctx = _ctx()
        res = _draft(
            ctx,
            [
                {"keyword": "home loan emi", "volume": 900},
                {"keyword": " ".join(["w"] * 11), "volume": 5000},
            ],
        )
        self.assertEqual([t["keyword"] for t in res.data["terms"]], ["home loan emi"])

    def test_drafting_creates_nothing(self):
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        # only the pre-existing taxonomy signal
        self.assertEqual(len(audience(ctx["session_context"])["signals"]), 1)

    def test_the_planners_own_expansion_is_kept(self):
        # fetch_keyword_ideas returns terms BEYOND the seeds, and that expansion is the point
        # - scoring only the seed list would throw away everything the user did not think of.
        ctx = _ctx()
        res = _draft(
            ctx,
            [
                {"keyword": "home loan emi", "volume": 900},
                {"keyword": "cheapest housing finance 2026", "volume": 5000},
            ],
            suggestions=("home loan emi",),
        )
        self.assertIn(
            "cheapest housing finance 2026", [t["keyword"] for t in res.data["terms"]]
        )

    def test_candidates_are_capped(self):
        # The Planner expands far past the seeds; without a cap the agent is handed hundreds
        # of terms to choose ten from.
        ctx = _ctx()
        res = _draft(
            ctx,
            [{"keyword": f"term {i}", "volume": 1000 - i} for i in range(200)],
        )
        self.assertEqual(len(res.data["terms"]), cs._MAX_CANDIDATES)

    def test_the_cap_keeps_the_highest_volume(self):
        # fetch_keyword_ideas returns highest-volume first, so the cap must not reorder.
        ctx = _ctx()
        res = _draft(
            ctx, [{"keyword": f"term {i}", "volume": 1000 - i} for i in range(200)]
        )
        volumes = [t["volume"] for t in res.data["terms"]]
        self.assertEqual(volumes, sorted(volumes, reverse=True))
        self.assertEqual(volumes[0], 1000)

    def test_every_theme_the_agent_gave_reaches_the_planner(self):
        # The agent IS the seed generator - one phrase only explores one direction, so its
        # phrasings must survive to the expansion rather than being collapsed to the first.
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}], suggestions=())
        seeds = ctx["_probe"]["ideas"].seeds
        self.assertIn("home loans", seeds)
        self.assertIn("housing finance", seeds)

    def test_a_single_string_is_accepted(self):
        # Models send a bare string for an array parameter often enough to handle it.
        ctx = _ctx()

        async def fake_suggest(seeds, **kw):
            return []

        async def fake_ideas(seeds, **kw):
            fake_ideas.seeds = seeds
            return [{"keyword": "home loan emi", "volume": 900}]

        with (
            mock.patch.object(cs.autosuggest, "fetch_suggestions", new=fake_suggest),
            mock.patch.object(
                cs.keyword_planner, "fetch_keyword_ideas", new=fake_ideas
            ),
        ):
            res = asyncio.run(cs._draft_custom_segment({"themes": "home loans"}, ctx))
        self.assertTrue(res.success)
        self.assertIn("home loans", fake_ideas.seeds)

    def test_the_campaigns_geo_reaches_the_planner(self):
        # Unset, the Planner falls back to its India default - a US segment would then be
        # filtered and ranked on Indian search volume.
        ctx = _ctx(aud_geo=["geoTargetConstants/2840"])
        captured = {}

        async def fake_suggest(seeds, **kw):
            return []

        async def fake_ideas(seeds, **kw):
            captured.update(kw)
            return [{"keyword": "home loan emi", "volume": 900}]

        with (
            mock.patch.object(cs.autosuggest, "fetch_suggestions", new=fake_suggest),
            mock.patch.object(
                cs.keyword_planner, "fetch_keyword_ideas", new=fake_ideas
            ),
        ):
            asyncio.run(cs._draft_custom_segment({"themes": ["home loans"]}, ctx))
        self.assertEqual(captured["geo_target_constants"], ["geoTargetConstants/2840"])

    def test_the_business_url_rides_along(self):
        # keywordAndUrlSeed - Google reads the landing page for richer ideas.
        ctx = _ctx(aud_business_url="https://acme.example")
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        self.assertEqual(ctx["_probe"]["ideas"].url, "https://acme.example")

    def test_an_open_breaker_is_not_reported_as_no_demand(self):
        ctx = _ctx()

        async def fake_suggest(seeds, **kw):
            return []

        async def boom(seeds, **kw):
            raise cs.keyword_planner.PlannerUnavailable("breaker open")

        with (
            mock.patch.object(cs.autosuggest, "fetch_suggestions", new=fake_suggest),
            mock.patch.object(cs.keyword_planner, "fetch_keyword_ideas", new=boom),
        ):
            res = asyncio.run(cs._draft_custom_segment({"themes": ["x"]}, ctx))
        self.assertFalse(res.success)
        self.assertIn("temporarily unavailable", res.error)

    def test_no_demand_is_said_plainly_rather_than_invented(self):
        ctx = _ctx()
        res = _draft(ctx, [{"keyword": "x", "volume": 0}])
        self.assertTrue(res.success)
        self.assertEqual(res.data["terms"], [])
        self.assertIn("do not invent", res.summary)


class SubmitGateTests(unittest.TestCase):
    def test_submit_without_a_draft_is_refused(self):
        # The confirmation sits between draft and submit structurally, not by good behaviour.
        ctx = _ctx()
        res, calls = _submit(ctx, {"terms": ["home loan emi"], "label": "Loan seekers"})
        self.assertFalse(res.success)
        self.assertIn("draft_custom_segment first", res.error)
        self.assertEqual(calls, [])

    def test_terms_outside_the_draft_are_refused(self):
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        res, calls = _submit(
            ctx, {"terms": ["something invented"], "label": "Loan seekers"}
        )
        self.assertFalse(res.success)
        self.assertIn("not in the draft", res.error)
        self.assertEqual(calls, [])

    def test_a_drafted_term_creates_and_targets(self):
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        res, calls = _submit(ctx, {"terms": ["home loan emi"], "label": "Loan seekers"})
        self.assertTrue(res.success, res.error)
        self.assertEqual(calls[0]["keywords"], ["home loan emi"])
        signals = audience(ctx["session_context"])["signals"]
        created = next(s for s in signals if s["ref"] == _CREATED)
        self.assertEqual(created["kind"], "CUSTOM_AUDIENCE")
        self.assertEqual(created["source"], "GENERATED")
        self.assertTrue(created["owned"])

    def test_the_draft_is_spent_after_submitting(self):
        # Otherwise a second "yes" would silently create a duplicate.
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        _submit(ctx, {"terms": ["home loan emi"], "label": "Loan seekers"})
        res, calls = _submit(ctx, {"terms": ["home loan emi"], "label": "Loan seekers"})
        self.assertFalse(res.success)
        self.assertEqual(calls, [])

    def test_a_url_is_passed_through_and_validated(self):
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        res, calls = _submit(
            ctx,
            {
                "terms": ["home loan emi"],
                "label": "Loan seekers",
                "url": "example.com",  # no scheme
            },
        )
        self.assertFalse(res.success)
        self.assertIn("http", res.error)
        self.assertEqual(calls, [])


class DraftExpiryTests(unittest.TestCase):
    """submit_custom_segment creates a REAL segment in the advertiser's account, so a draft
    they declined must not still be sitting there turns later."""

    def test_a_draft_survives_exactly_the_turn_it_was_offered_for(self):
        from app.agents.adzump.agents.campaign.google.audience.agent import carry_draft

        parent = {"aud_custom_candidates": [{"keyword": "villa"}], "aud_custom_theme": "villas"}
        self.assertTrue(carry_draft(parent))  # the approval turn sees it
        self.assertFalse(carry_draft(parent))  # declined - gone
        self.assertNotIn("aud_custom_candidates", parent)

    def test_nothing_to_carry_is_not_an_error(self):
        from app.agents.adzump.agents.campaign.google.audience.agent import carry_draft

        self.assertEqual(carry_draft({}), {})


class NameCollisionTests(unittest.TestCase):
    """name must be unique per customer, so collisions are resolved rather than hit."""

    def test_a_colliding_name_of_ours_still_creates_with_the_approved_terms(self):
        # Reusing the existing segment would attach whatever terms it already had while the
        # reply claims the ones the user just approved. The name gets a suffix; the terms are
        # always written.
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        mine = {
            "resource_name": "customers/1/customAudiences/9",
            "name": "Loan seekers",
            "description": "adzump:v1:product=Acme Homes",
        }
        res, calls = _submit(
            ctx, {"terms": ["home loan emi"], "label": "Loan seekers"}, existing=[mine]
        )
        self.assertTrue(res.success, res.error)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "Loan seekers (2)")
        self.assertEqual(calls[0]["keywords"], ["home loan emi"])
        refs = [s["ref"] for s in audience(ctx["session_context"])["signals"]]
        self.assertIn(_CREATED, refs)
        self.assertNotIn(mine["resource_name"], refs)

    def test_someone_elses_name_forces_a_suffix(self):
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        theirs = {
            "resource_name": "customers/1/customAudiences/9",
            "name": "Loan seekers",
            "description": "built by hand in the UI",
        }
        res, calls = _submit(
            ctx,
            {"terms": ["home loan emi"], "label": "Loan seekers"},
            existing=[theirs],
        )
        self.assertTrue(res.success, res.error)
        self.assertEqual(calls[0]["name"], "Loan seekers (2)")

    def test_a_free_name_is_used_as_is(self):
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        _, calls = _submit(ctx, {"terms": ["home loan emi"], "label": "Loan seekers"})
        self.assertEqual(calls[0]["name"], "Loan seekers")


if __name__ == "__main__":
    unittest.main()
