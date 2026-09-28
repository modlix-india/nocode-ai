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

import asyncio
import unittest
from dataclasses import replace

from app.agents.adzump.core.journey import Status
from app.agents.adzump.workflow import NEW_CAMPAIGN, AdzumpContext
from app.agents.adzump.tools.launch import _launch_campaign
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

    # S0-5 → S2 · the review prescription is two tool calls; the card itself
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
                self.assertTrue(block.startswith("review & publish"))
                for tool in ("show_campaign_summary", "launch_campaign"):
                    self.assertIn(tool, block)

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
        self.assertTrue(missing[0].startswith("review & publish"))

    # S0-7 · offers are never load-bearing: required asks proceed past declines
    def test_offers_never_load_bearing(self):
        actx = make_actx(
            {"platform": "Google Ads", "duration": "30 days",
             "competitive_analysis_declined": "true"}, product=SAAS)
        missing = NEW_CAMPAIGN.walk(actx).missing
        self.assertIsNotNone(_entry(missing, "budget"))
        self.assertIsNone(_entry(missing, "competitive analysis"))

    def test_launch_required_set_excludes_offers(self):
        # Both offers declined + every required field set: the launch guard
        # stack must pass the completeness check (offers are not required) and
        # stop at the consent gate - proving declines never block launch.
        spec = {**META_DONE, "ig_page_declined": "true",
                "competitor_creatives_declined": "true"}
        session = make_session(last_user="what about targeting?",
                               spec=spec, product=SAAS)
        result = asyncio.run(_launch_campaign(
            {}, {"session_context": session.context, "_session": session}))
        self.assertFalse(result.success)
        self.assertNotIn("missing required fields", result.error)
        self.assertIn("launch confirmation", result.error)


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
    # location hook, behavior-tested in test_campaign_data).
    EXEMPT = {"product", "target_areas"}
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


if __name__ == "__main__":
    unittest.main()
