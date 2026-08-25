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
from app.agents.adzump.agents.campaign.google.audience.constants import (
    BLUEPRINTS_KEY,
    pending_ref,
)
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


def _submit(ctx, params):
    """Submit no longer talks to Google - it records a blueprint publish creates at launch."""

    async def noop(*a, **kw):
        return None

    with mock.patch(
        "app.agents.adzump.agents.campaign.tools.google.audience_update.emit_panel",
        new=noop,
    ):
        res = asyncio.run(cs._submit_custom_segment(params, ctx))
    return res, (ctx["session_context"].get(BLUEPRINTS_KEY) or {})


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
        res, plans = _submit(ctx, {"terms": ["home loan emi"], "label": "Loan seekers"})
        self.assertFalse(res.success)
        self.assertIn("draft_custom_segment first", res.error)
        self.assertEqual(plans, {})

    def test_terms_outside_the_draft_are_refused(self):
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        res, plans = _submit(
            ctx, {"terms": ["something invented"], "label": "Loan seekers"}
        )
        self.assertFalse(res.success)
        self.assertIn("not in the draft", res.error)
        self.assertEqual(plans, {})

    def test_approval_records_a_blueprint_and_creates_nothing(self):
        # It used to create a real CustomAudience here - before the panel review, and even
        # on a dry run. publish materialises it at launch instead.
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        res, plans = _submit(ctx, {"terms": ["home loan emi"], "label": "Loan seekers"})
        self.assertTrue(res.success, res.error)

        ref = pending_ref("Loan seekers")
        self.assertEqual(plans[ref]["terms"][0]["keyword"], "home loan emi")
        created = next(
            s for s in audience(ctx["session_context"])["signals"] if s["ref"] == ref
        )
        self.assertEqual(created["kind"], "CUSTOM_AUDIENCE")
        self.assertEqual(created["source"], "GENERATED")
        self.assertTrue(created["owned"])

    def test_the_draft_is_spent_after_submitting(self):
        # Otherwise a second "yes" would silently record a duplicate.
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        _submit(ctx, {"terms": ["home loan emi"], "label": "Loan seekers"})
        res, _ = _submit(ctx, {"terms": ["home loan emi"], "label": "Loan seekers"})
        self.assertFalse(res.success)

    def test_a_url_is_passed_through_and_validated(self):
        ctx = _ctx()
        _draft(ctx, [{"keyword": "home loan emi", "volume": 900}])
        res, plans = _submit(
            ctx,
            {
                "terms": ["home loan emi"],
                "label": "Loan seekers",
                "url": "example.com",  # no scheme
            },
        )
        self.assertFalse(res.success)
        self.assertIn("http", res.error)
        self.assertEqual(plans, {})


class MemberEditTests(unittest.TestCase):
    """Panel edits to a segment that Google does not hold yet."""

    def _ctx_with_blueprint(self):
        ctx = _ctx()
        ref = pending_ref("Buyers")
        ctx["session_context"][BLUEPRINTS_KEY] = {
            ref: {
                "label": "Buyers",
                "terms": [{"keyword": "a", "volume": 10}],
                "urls": [],
                "apps": [],
            }
        }
        return ctx, ref

    def _edit(self, params, ctx):
        from app.agents.adzump.agents.campaign.tools.google import audience_update as au

        return asyncio.run(au.apply_member_edit(params, ctx))

    def test_each_member_type_can_be_added(self):
        for params, field, expected in [
            ({"action": "add_term", "keyword": "b", "volume": 50}, "terms", 2),
            ({"action": "add_url", "url": "https://x.com"}, "urls", 1),
            ({"action": "add_app", "app": "com.x.y"}, "apps", 1),
        ]:
            with self.subTest(action=params["action"]):
                ctx, ref = self._ctx_with_blueprint()
                ok, msg = self._edit({**params, "ref": ref}, ctx)
                self.assertTrue(ok, msg)
                plan = ctx["session_context"][BLUEPRINTS_KEY][ref]
                self.assertEqual(len(plan[field]), expected)

    def test_an_invalid_member_is_refused_with_googles_reason(self):
        ctx, ref = self._ctx_with_blueprint()
        ok, msg = self._edit(
            {"action": "add_app", "ref": ref, "app": "notapackage"}, ctx
        )
        self.assertFalse(ok)
        self.assertIn("Android package name", msg)

    def test_the_last_search_term_cannot_be_removed(self):
        # Keywords are the one member type a custom audience cannot be built without.
        ctx, ref = self._ctx_with_blueprint()
        ok, msg = self._edit({"action": "delete_term", "ref": ref, "keyword": "a"}, ctx)
        self.assertFalse(ok)
        self.assertIn("at least one search term", msg)

    def test_a_created_segment_is_not_editable(self):
        # Google would take an update, but that path is not built - AGENT.md 6.3 - so an
        # edit here would change our copy and nothing else.
        ctx, _ = self._ctx_with_blueprint()
        ok, msg = self._edit(
            {
                "action": "add_term",
                "ref": "customers/1/customAudiences/9",
                "keyword": "b",
            },
            ctx,
        )
        self.assertFalse(ok)
        self.assertIn("already in the account", msg)


class ChatEditTests(unittest.TestCase):
    """Spoken edits. People ask in lists ("add these five"), and one member per call spent a
    turn each against MAX_TURNS - a long list stopped half-applied with no sign which half."""

    def _ctx_with_blueprint(self):
        ctx = _ctx()
        ref = pending_ref("Buyers")
        ctx["session_context"][BLUEPRINTS_KEY] = {
            ref: {
                "label": "Buyers",
                "terms": [{"keyword": "a", "volume": 10}],
                "urls": [],
                "apps": [],
            }
        }
        return ctx, ref

    def _edit(self, params, ctx):
        async def volumes(keywords, **_):
            return [{"keyword": k, "volume": 100} for k in keywords]

        with (
            mock.patch(
                "app.agents.adzump.adapters.google.keyword_planner"
                ".fetch_keyword_historical_metrics",
                side_effect=volumes,
            ) as planner,
            mock.patch(
                "app.agents.adzump.agents.campaign.tools.google"
                ".audience_update.emit_panel",
                new=mock.AsyncMock(),
            ) as emit,
        ):
            result = asyncio.run(cs._edit_custom_segment(params, ctx))
        return result, planner, emit

    def test_every_value_in_one_call_costs_one_lookup_and_one_redraw(self):
        ctx, ref = self._ctx_with_blueprint()
        result, planner, emit = self._edit(
            {"action": "add_term", "terms": ["b", "c", "d", "e", "f"]}, ctx
        )
        self.assertTrue(result.success, result.error)
        plan = ctx["session_context"][BLUEPRINTS_KEY][ref]
        self.assertEqual(len(plan["terms"]), 6)
        self.assertEqual(planner.call_count, 1)  # not one per term
        self.assertEqual(emit.call_count, 1)  # the panel must not flash five times
        self.assertEqual(len(planner.call_args[0][0]), 5)

    def test_urls_and_apps_take_lists_too(self):
        for action, field, values in [
            ("add_url", "urls", ["https://x.com", "https://y.com"]),
            ("add_app", "apps", ["com.x.y", "com.a.b"]),
        ]:
            with self.subTest(action=action):
                ctx, ref = self._ctx_with_blueprint()
                result, _, _ = self._edit({"action": action, field: values}, ctx)
                self.assertTrue(result.success, result.error)
                plan = ctx["session_context"][BLUEPRINTS_KEY][ref]
                self.assertEqual(len(plan[field]), 2)

    def test_the_good_values_land_and_the_refused_ones_are_named(self):
        # Partial is the common case - one duplicate in a list of five must not cost the
        # other four, and the user has to be told which one did not take.
        ctx, ref = self._ctx_with_blueprint()
        result, _, _ = self._edit({"action": "add_term", "terms": ["b", "a", "c"]}, ctx)
        self.assertTrue(result.success)
        self.assertEqual(result.data["applied"], 2)
        self.assertIn("'a'", result.summary)
        self.assertIn("already in this segment", result.summary)
        plan = ctx["session_context"][BLUEPRINTS_KEY][ref]
        self.assertEqual(len(plan["terms"]), 3)

    def test_nothing_landing_is_a_failure_not_a_quiet_success(self):
        ctx, _ = self._ctx_with_blueprint()
        result, _, emit = self._edit({"action": "add_term", "terms": ["a"]}, ctx)
        self.assertFalse(result.success)
        self.assertIn("already in this segment", result.error)
        self.assertEqual(emit.call_count, 0)  # nothing changed, so nothing to redraw

    def test_an_action_with_no_values_says_which_one_it_wanted(self):
        ctx, _ = self._ctx_with_blueprint()
        result, _, _ = self._edit({"action": "add_url", "urls": []}, ctx)
        self.assertFalse(result.success)
        self.assertIn("urls", result.error)


class PendingQuestionTests(unittest.TestCase):
    """One ask per turn. The audience agent's question is prose, so without declaring it the
    orchestrator stacked "Ready to launch the campaign?" underneath and offered only
    "Yes, launch" - a yes meant for the segment would have launched the campaign."""

    @staticmethod
    def _log_entry(result):
        """What core builds from a ToolResult before testing it - app/core/agent.py:961."""
        return {
            "tool": "manage_audience",
            "kind": "tool",
            "elicit_mode": "deferred",
            "elicited": bool(
                isinstance(result.data, dict) and result.data.get("elicited")
            ),
        }

    def test_a_pending_draft_is_declared_so_the_turn_yields(self):
        from app.core.agent import BaseAgent
        from app.core.tools.base import ToolResult

        asked = ToolResult(success=True, summary="", data={"elicited": True})
        self.assertTrue(BaseAgent._is_deferred_elicitation(self._log_entry(asked)))

    def test_an_ordinary_reply_does_not_stop_the_turn(self):
        from app.core.agent import BaseAgent
        from app.core.tools.base import ToolResult

        replied = ToolResult(success=True, summary="", data=None)
        self.assertFalse(BaseAgent._is_deferred_elicitation(self._log_entry(replied)))


class DraftExpiryTests(unittest.TestCase):
    """submit_custom_segment creates a REAL segment in the advertiser's account, so a draft
    they declined must not still be sitting there turns later."""

    def test_a_draft_survives_exactly_the_turn_it_was_offered_for(self):
        from app.agents.adzump.agents.campaign.google.audience.agent import carry_draft

        parent = {
            "aud_custom_candidates": [{"keyword": "villa"}],
            "aud_custom_theme": "villas",
        }
        self.assertTrue(carry_draft(parent))  # the approval turn sees it
        self.assertFalse(carry_draft(parent))  # declined - gone
        self.assertNotIn("aud_custom_candidates", parent)

    def test_nothing_to_carry_is_not_an_error(self):
        from app.agents.adzump.agents.campaign.google.audience.agent import carry_draft

        self.assertEqual(carry_draft({}), {})

    def test_a_redraft_gets_its_own_turn_rather_than_the_previous_draft_s(self):
        """The logged failure: the run holding a draft drafted again, the new list inherited
        the old one's expiry, and the user's "yes" landed on nothing - so the model drafted a
        third time and created a segment from terms they had never been shown."""
        from app.agents.adzump.agents.campaign.google.audience.agent import carry_draft

        parent = {
            "aud_custom_candidates": [{"keyword": "shown to the user"}],
            "aud_custom_theme": "villas",
            cs.DRAFT_ID_KEY: "first",
        }
        self.assertTrue(carry_draft(parent))
        # Same turn, the run drafts again: the copy-back replaces the list AND its id.
        parent["aud_custom_candidates"] = [{"keyword": "the newer list"}]
        parent[cs.DRAFT_ID_KEY] = "second"

        carried = carry_draft(parent)  # the turn the user says "add them please"
        self.assertEqual(
            carried["aud_custom_candidates"], [{"keyword": "the newer list"}]
        )
        self.assertFalse(carry_draft(parent))  # and it still expires one turn later


class NameCollisionTests(unittest.TestCase):
    """name must be unique per customer, so collisions are resolved rather than hit."""

    def test_a_free_name_is_used_as_is(self):
        self.assertEqual(cs.resolve_name([], "Loan seekers"), "Loan seekers")

    def test_a_taken_name_gets_the_first_free_suffix(self):
        # Ours or someone else's - reusing a live segment would attach whatever terms it
        # already had while the reply claims the ones the user just approved.
        existing = [{"name": "Loan seekers"}, {"name": "Loan seekers (2)"}]
        self.assertEqual(cs.resolve_name(existing, "Loan seekers"), "Loan seekers (3)")

    def test_a_long_label_keeps_room_for_the_suffix(self):
        long = "x" * 100
        self.assertEqual(len(cs.resolve_name([], long)), 80)
        taken = [{"name": cs.resolve_name([], long)}]
        self.assertTrue(cs.resolve_name(taken, long).endswith(" (2)"))
        self.assertLessEqual(len(cs.resolve_name(taken, long)), 80)


if __name__ == "__main__":
    unittest.main()
