"""The journey engine's invariant suite (S0 rows + S3).

The S0 rows were written against the retired if-chain and passed UNCHANGED
across the slice-3 conversion - they are the equivalence referee (D6:
membership, order, tool named - never prose wording). S0-6 goes through the
REAL ``from_session`` lenient path with raw legacy dicts, not the ``make_actx``
fixture shortcut. S3 adds the engine's one deliberate semantic: waiting (an
ask in flight) blocks review. The registry lint is ``Journey``'s own
construction check (core/test_journey.py).
"""
from __future__ import annotations

import unittest
from dataclasses import replace

from app.agents.adzump.agents.campaign.models import (
    Channel,
    build_review_items,
    set_audience,
)
from app.agents.adzump.core.journey import Status
from app.agents.adzump.workflow import CAMPAIGN_DETAILS, NEW_CAMPAIGN, AdzumpContext
from app.agents.adzump.models import OfferResolution
from app.agents.adzump.tools.campaign_data import CREATIVES_REVIEW_ASK
from tests.agents.adzump._fixtures import RE, SAAS, make_actx, make_session


def _entry(missing: list[str], prefix: str) -> str | None:
    """First missing-list entry for a field, matched by its stable prefix."""
    return next((m for m in missing if m.startswith(prefix)), None)


GOOGLE_DONE = {
    "platform": "Google Ads",
    "duration": "30 days",
    "budget": "$50/day",
    "parent_account": "111-222",
    "account": "333-444",
}
META_DONE = {**GOOGLE_DONE, "platform": "Meta", "fb_page": "pg-9"}


class JourneyInvariantTests(unittest.TestCase):
    """S0 - the behaviors every journey rework must preserve."""

    # S0-1 · offer-once: a settled offer never re-enters the missing-list
    def test_settled_offer_absent_from_missing(self):
        rows = [
            ("analysis accepted", make_actx(
                {"platform": "Google Ads"}, product=SAAS, attempted=True),
                "competitive analysis"),
            ("analysis declined", make_actx(
                {"platform": "Google Ads", "competitive_analysis_declined": "true"},
                product=SAAS), "competitive analysis"),
            ("creatives settled", make_actx(
                {"platform": "Meta"}, product=SAAS, creatives_resolved=True),
                "competitor creatives"),
            ("creatives never offered on Google", make_actx(
                {"platform": "Google Ads"}, product=SAAS,
                competitor_names=["Rival"], attempted=True),
                "competitor creatives"),
        ]
        for label, actx, prefix in rows:
            with self.subTest(label):
                self.assertIsNone(_entry(NEW_CAMPAIGN.walk(actx).missing, prefix))

    # S0-2 · decline honored through the REAL from_session/predicate wiring
    def test_decline_honored_from_raw_session(self):
        rows = [
            ("analysis declined",
             {"platform": "Google Ads", "competitive_analysis_declined": "true"},
             {}, "competitive analysis"),
            ("creatives declined",
             {"platform": "Meta", "competitor_creatives_declined": "true"},
             {}, "competitor creatives"),
            ("empty fetch completed (every competitor covered)",
             {"platform": "Meta"},
             {"competitor_analysis": {"competitors": [
                 {"name": "R", "url": "https://r.com", "creatives": []}]}},
             "competitor creatives"),
        ]
        for label, spec, extra, prefix in rows:
            with self.subTest(label):
                session = make_session(spec=spec, product=SAAS, **extra)
                actx = AdzumpContext.from_session(session)
                self.assertIsNone(_entry(NEW_CAMPAIGN.walk(actx).missing, prefix))

    # S0-4 · an accepted creatives offer starts research, never a blind fetch:
    # known rivals get the list-review checkpoint first (Kailash 2026-09-09)
    def test_accepted_offer_unlocks_fetch(self):
        rows = [
            ("rivals known", ["Lodha"], "fetch_competitor_creatives"),
            ("rivals unknown", [], "analyze_competitors"),
        ]
        for label, names, tool in rows:
            with self.subTest(label):
                actx = make_actx(
                    {"platform": "Meta", "competitor_creatives": "accepted"},
                    product=SAAS, competitor_names=names, last_user="Yes")
                line = _entry(NEW_CAMPAIGN.walk(actx).missing, "competitor creatives")
                self.assertIsNotNone(line)
                self.assertIn(tool, line)
                self.assertNotIn('field "competitor_creatives"', line)  # not a re-ask
                if names:
                    self.assertIn(CREATIVES_REVIEW_ASK, line)
                else:
                    self.assertNotIn("fetch_competitor_creatives", line)

    # S0-5 → S2 · the summary prescription is two tool calls; the card itself
    # is CODE-rendered (tools/summary.py) - no VERBATIM template remains.
    def test_review_block_shape(self):
        rows = [
            ("google", make_actx(GOOGLE_DONE, product=SAAS, attempted=True)),
            ("meta+ig", make_actx({**META_DONE, "ig_page": "ig-7"},
                                  product=SAAS, creatives_resolved=True)),
            ("fb-only", make_actx({**META_DONE, "ig_page_declined": "true"},
                                  product=SAAS, creatives_resolved=True)),
        ]
        for label, actx in rows:
            with self.subTest(label):
                missing = NEW_CAMPAIGN.walk(actx).missing
                self.assertEqual(len(missing), 1)
                block = missing[0]
                self.assertTrue(block.startswith("review the summary"))
                self.assertIn("show_campaign_summary", block)
                self.assertIn('field "summary_confirmed"', block)
                # F20: the model echoed copyable call syntax into the launch bubble
                self.assertNotIn("present_options(", block)

    # S0-6 · in-flight legacy session resumes sanely through from_session
    def test_legacy_session_resume(self):
        session = make_session(
            last_user="hello again",
            spec={**META_DONE, "ig_page_declined": "true",
                  "competitor_creatives_declined": "true"},
            product=SAAS,
            _ig_offered=True,  # dead legacy marker rides along, ignored
            competitor_analysis={"competitors": [{"name": "Lodha"}]},
        )
        actx = AdzumpContext.from_session(session)
        self.assertIs(actx.competitor_creatives_resolution,
                      OfferResolution.DECLINED)
        missing = NEW_CAMPAIGN.walk(actx).missing
        self.assertEqual(len(missing), 1)
        self.assertTrue(missing[0].startswith("review the summary"))


class JourneyEngineTests(unittest.TestCase):
    """S3 · the engine semantics the if-chain could not express."""

    def test_waiting_ask_blocks_review(self):
        # Everything set except the creatives offer, whose ask is ON SCREEN:
        # the step is waiting - never re-prescribed, but the journey is NOT
        # complete, so review cannot fire over an unanswered ask (the retired
        # if-chain prescribed review here). The missing-section prompt must
        # not claim review-readiness either.
        actx = make_actx({**META_DONE, "ig_page": "ig-7"}, product=SAAS,
                         pending_ask="competitor_creatives")
        progress = NEW_CAMPAIGN.walk(actx)
        self.assertEqual(progress.missing, ())
        self.assertFalse(progress.complete)
        section = progress.missing_section()
        self.assertNotIn("review", section)
        self.assertIn("pending on screen", section)

    def test_accepted_answer_beats_the_open_rail(self):
        # A capture can store ACCEPTED while the rail is still open (actx is
        # built before the answered rail is reaped): the fetch is owed NOW -
        # the waiting gate must not swallow the said-YES prescription (the
        # retired chain checked ACCEPTED before the rail, in that order).
        actx = make_actx(
            {**META_DONE, "ig_page": "ig-7", "competitor_creatives": "accepted"},
            product=SAAS, competitor_names=["Rival"],
            pending_ask="competitor_creatives")
        missing = NEW_CAMPAIGN.walk(actx).missing
        self.assertEqual(len(missing), 1)
        self.assertIn("fetch_competitor_creatives", missing[0])

    def test_upstream_ask_hides_dependents(self):
        # No product: every later step requires it, so the URL ask is the
        # ONLY line (the old early-return, now expressed as dependencies).
        missing = NEW_CAMPAIGN.walk(make_actx({}, product={})).missing
        self.assertEqual(len(missing), 1)
        self.assertIn("analyze_product", missing[0])



class StateRowTests(unittest.TestCase):
    """What each step shows in State - one walk with Missing, so the two
    sections can never disagree about a step."""

    @staticmethod
    def _step_states(actx: AdzumpContext) -> dict:
        progress = NEW_CAMPAIGN.walk(actx, actx.set_at, actx.current_turn)
        return {state.label: state for state in progress.step_states}

    def test_rows(self):
        rows = [
            # (case, actx, row label, value, status)
            ("no product: later steps wait behind the URL ask",
             make_actx({}, product={}), "Platform", None, Status.BLOCKED),
            ("location is off for a non-real-estate product",
             make_actx({"platform": "Meta"}, product=SAAS), "Location", None, Status.OFF),
            ("unmapped target areas are still owed",
             make_actx({"location": "Whitefield", "platform": "Google Ads"},
                       product={**RE, "target_areas": [{"name": "Whitefield"}]}),
             "Target Areas", "Whitefield", Status.OPEN),
            ("an on-screen creatives ask is waiting",
             make_actx(dict(META_DONE), product=SAAS, pending_ask="competitor_creatives"),
             "Competitor ads", None, Status.WAITING),
            ("a Facebook-only decline settles instagram",
             make_actx({**META_DONE, "instagram": "declined"}, product=SAAS),
             "Instagram Account", "not linked (Facebook only)", Status.DONE),
            ("Meta still shows the competitor list",
             make_actx({"platform": "Meta"}, product=SAAS, competitor_names=["Rival"],
                       attempted=True),
             "Competitors", "Rival", Status.OFF),
            ("a declined analysis says so",
             make_actx({"platform": "Google Ads", "competitive_analysis": "declined"},
                       product=SAAS),
             "Competitors", "declined", Status.DONE),
        ]
        for case, actx, label, value, status in rows:
            with self.subTest(case):
                state = self._step_states(actx)[label]
                self.assertEqual((state.value, state.status), (value, status))

    def test_every_written_field_has_an_age(self):
        actx = replace(make_actx(dict(GOOGLE_DONE), product=SAAS),
                       set_at={"account": 3}, current_turn=5)
        states = self._step_states(actx)
        self.assertEqual(states["Ad Account"].turns_ago, 2)
        self.assertIsNone(states["Duration"].turns_ago)  # never stamped


class DependencyMirrorTests(unittest.TestCase):
    """Slice 6 · the forward ask-order graph (Step.requires) and the backward
    invalidation graph (_FIELD_DEPENDENTS) are ONE graph read in two
    directions, maintained by hand in two files. The drift that matters:
    step B is asked after A because B's answer assumes A - so a change to A
    must clear B, or a stale B ships (the wrong-city-launch bug class, R11)."""

    # Each step's spec field(s) are its Step.fields. Steps outside the spec
    # cascade write none: product is the whole session (a new URL restarts
    # everything), target_areas lives in product_data (its invalidation is the
    # location hook, behavior-tested in test_campaign_data). tool_question is a
    # one-turn routing of the reply, and the build lives in campaign_build
    # (prepare_campaign_review refuses to rebuild over the user's review).
    EXEMPT = {"product", "target_areas", "tool_question", "build"}
    STEP_FIELDS = {step.name: step.fields for step in NEW_CAMPAIGN.steps}

    @staticmethod
    def _invalidated_by(field: str) -> set[str]:
        """Transitive closure of _FIELD_DEPENDENTS from one changed field."""
        from app.agents.adzump.tools.campaign_data import _FIELD_DEPENDENTS
        cleared: set[str] = set()
        frontier = [field]
        while frontier:
            for dep in _FIELD_DEPENDENTS.get(frontier.pop(), ()):
                if dep not in cleared:
                    cleared.add(dep)
                    frontier.append(dep)
        return cleared

    def test_every_step_is_mapped_or_exempt(self):
        # A NEW step must be placed in this mirror deliberately - either
        # declaring its spec field(s) or exempted with a reason above.
        from app.agents.adzump.models import CampaignSpec
        for name, fields in self.STEP_FIELDS.items():
            with self.subTest(step=name):
                self.assertEqual(not fields, name in self.EXEMPT)
                self.assertLessEqual(set(fields), set(CampaignSpec.model_fields))
        self.assertTrue(self.EXEMPT <= set(self.STEP_FIELDS))

    def test_requires_edges_have_invalidation_mirrors(self):
        for step in NEW_CAMPAIGN.steps:
            fields = self.STEP_FIELDS.get(step.name, ())
            for required_step in step.requires:
                for changed in self.STEP_FIELDS.get(required_step, ()):
                    cleared = self._invalidated_by(changed)
                    for field in fields:
                        with self.subTest(step=step.name,
                                          after=required_step, field=field):
                            self.assertIn(
                                field, cleared,
                                f"'{step.name}' is asked after "
                                f"'{required_step}', so its answer assumes it "
                                f"- but changing '{changed}' never clears "
                                f"'{field}': a stale value would ship.",
                            )

    def test_dependents_name_real_spec_fields(self):
        # Typo guard: every key and value in the invalidation map must be a
        # real CampaignSpec field or a known legacy marker - a misspelled
        # entry silently clears nothing.
        from app.agents.adzump.models import LEGACY_DECLINED_KEYS, CampaignSpec
        from app.agents.adzump.tools.campaign_data import _FIELD_DEPENDENTS
        known = set(CampaignSpec.model_fields) | set(LEGACY_DECLINED_KEYS.values())
        for changed, dependents in _FIELD_DEPENDENTS.items():
            self.assertIn(changed, known)
            for dep in dependents:
                with self.subTest(changed=changed, dependent=dep):
                    self.assertIn(dep, known)


class BuildStageTests(unittest.TestCase):
    """After the details: the user okays the summary, then on Google picks the
    campaign type (and, for Search, the ad groups), the build runs, and launch
    comes last. Meta goes from the okayed summary straight to launch."""

    CONFIRMED = {"summary_confirmed": True}

    def _walk(self, spec=GOOGLE_DONE, **kw):
        return NEW_CAMPAIGN.walk(make_actx(dict(spec), product=SAAS, attempted=True, **kw))

    def test_each_stage_prescribes_the_next_step(self):
        search = {**GOOGLE_DONE, "channel": "SEARCH"}
        rows = [  # (case, spec, context, first entry's prefix, text it must carry)
            ("the summary first", GOOGLE_DONE, {}, "review the summary",
             'field "summary_confirmed"'),
            ("then the campaign type", GOOGLE_DONE, self.CONFIRMED, "channel",
             'field "channel"'),
            ("search picks its ad groups", search, self.CONFIRMED, "ad groups",
             '"answer": "brand,generic"'),
            ("demand gen builds the audience", {**GOOGLE_DONE, "channel": "DEMAND_GEN"},
             self.CONFIRMED, "build the campaign", "audience targeting"),
            ("search builds the keywords", {**search, "ad_groups": "brand"}, self.CONFIRMED,
             "build the campaign", "do NOT ask either question again"),
            ("a build owing work is finished, not rebuilt", GOOGLE_DONE,
             {**self.CONFIRMED, "build_gaps": ("unfinished ad groups - call `manage_keywords`",)},
             "unfinished ad groups", "manage_keywords"),
            ("built: launch", {**search, "ad_groups": "brand"},
             {**self.CONFIRMED, "build_done": True}, "launch", "launch_campaign tool"),
        ]
        for case, spec, kw, prefix, carries in rows:
            with self.subTest(case):
                first = self._walk(spec, **kw).missing[0]
                self.assertTrue(first.startswith(prefix), first)
                self.assertIn(carries, first)
                # F20: prose that names the tool, never copyable call syntax
                for call in ("present_options(", "prepare_campaign_review(", "launch_campaign("):
                    self.assertNotIn(call, first)

    def test_every_channel_is_offered(self):
        first = self._walk(**self.CONFIRMED).missing[0]
        for channel in Channel:
            self.assertIn(f'"answer": "{channel.value}"', first)
            self.assertIn(channel.chip_label, first)

    def test_meta_never_builds(self):
        meta = {**META_DONE, "ig_page": "ig-7"}
        for kw in ({}, self.CONFIRMED):
            with self.subTest(kw=kw):
                progress = NEW_CAMPAIGN.walk(make_actx(
                    meta, product=SAAS, creatives_resolved=True, **kw))
                joined = " ".join(progress.missing)
                self.assertNotIn("prepare_campaign_review", joined)
                self.assertNotIn("ad_groups", joined)
                self.assertNotIn("Review panel", progress.state_section())
        self.assertTrue(progress.missing[0].startswith("launch"))

    def test_the_review_panel_row_names_what_the_channel_declared(self):
        progress = self._walk({**GOOGLE_DONE, "channel": "DEMAND_GEN"}, **self.CONFIRMED,
                              build_done=True,
                              review_items=("the audience targeting", "where the ads will show"))
        self.assertIn("- Review panel: the audience targeting, where the ads will show ✓",
                      progress.state_section())

    def test_a_slot_that_never_ran_is_not_promised(self):
        # DemandGenBuild declares creative, but no tool fills it yet.
        ctx = {"campaign_spec": {"platform": "GOOGLE", "account": "1",
                                 "channel": "Demand Gen"}}
        set_audience(ctx, {"signals": [], "demographics": {},
                           "dimension_groups": [], "meta": {}})
        self.assertEqual(build_review_items(ctx), ("the audience targeting",))

    def test_the_summary_card_waits_only_for_the_details(self):
        # The card renders off CAMPAIGN_DETAILS: the steps after it never hold it back.
        for kw in ({}, self.CONFIRMED, {**self.CONFIRMED, "build_done": True}):
            with self.subTest(kw=kw):
                actx = make_actx(GOOGLE_DONE, product=SAAS, attempted=True, **kw)
                self.assertTrue(CAMPAIGN_DETAILS.walk(actx).complete)

    def test_the_summary_okay_reads_either_answer_shape(self):
        for stored, confirmed in [("true", True), ("Yes, proceed", True),
                                  ("false", False), ("No, make changes", False), ("", False)]:
            with self.subTest(stored=stored):
                session = make_session(spec={**GOOGLE_DONE, "summary_confirmed": stored},
                                       product=SAAS)
                self.assertIs(AdzumpContext.from_session(session).summary_confirmed, confirmed)


class HelperQuestionTests(unittest.TestCase):
    """A helper agent asked the user something. It holds the record the answer
    refers to, so the reply goes back to it - prescribing the next campaign step
    instead reads as leave to move on (live: "yes add them" was answered with
    "should we launch?", and the next "yes" launched the campaign)."""

    def _missing(self, **kw):
        spec = {**GOOGLE_DONE, "channel": "DEMAND_GEN"}
        return NEW_CAMPAIGN.walk(make_actx(
            spec, product=SAAS, attempted=True, summary_confirmed=True, build_done=True,
            review_items=("the audience targeting",), **kw)).missing

    def test_the_reply_goes_back_to_whichever_tool_asked(self):
        for tool in ("manage_audience", "manage_keywords"):
            with self.subTest(tool):
                missing = self._missing(awaiting_tool=tool)
                self.assertIn(f"{tool}(user_message=", missing[0])
                self.assertIn("Do NOT act on it yourself", missing[0])
                self.assertFalse(any(m.startswith("launch") for m in missing))
        self.assertTrue(self._missing()[0].startswith("launch"))


if __name__ == "__main__":
    unittest.main()
