"""Unit tests for the shared audience mutation and the agent's manage tools
(tools/google/audience_update.py, google/audience/manage_tools.py).

The panel's click path and the agent's spoken path go through the SAME apply_edit, so these
cover both.
"""

# regression: an audience must never end up empty (grouped mode has no untargeted fallback);
# a ref must be re-resolved against the live catalogue rather than trusted from the caller;
# and the manage session must hand its edits back rather than aliasing the parent's dict.
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.campaign.google.audience import manage_tools
from app.agents.adzump.agents.campaign.google.audience.agent import (
    AUD_MANAGE_HISTORY_TURNS,
)
from app.agents.adzump.agents.campaign.models import audience, set_audience
from app.agents.adzump.agents.campaign.tools.google import audience_update as au

_APARTMENTS = "customers/1/userInterests/80071"
_VILLAS = "customers/1/userInterests/80072"
_LIST = "customers/1/userLists/9"

_CATALOGUE = [
    {
        "id": 80071,
        "ref": _APARTMENTS,
        "label": "Apartments",
        "kind": "IN_MARKET",
        "path": ["Real Estate", "Apartments"],
    },
    {
        "id": 80072,
        "ref": _VILLAS,
        "label": "Villas",
        "kind": "IN_MARKET",
        "path": ["Real Estate", "Villas"],
    },
]


def _signal(ref, label, kind="IN_MARKET", **kw):
    return {
        "kind": kind,
        "ref": ref,
        "label": label,
        "source": "TAXONOMY",
        "rationale": "",
        "path": [],
        "negative": False,
        "owned": False,
        "metrics": None,
        **kw,
    }


def _ctx(*signals, demographics=None):
    session_ctx = {
        "campaign_spec": {
            "platform": "GOOGLE",
            "account": "1",
            "channel": "Demand Gen",
        }
    }
    sigs = list(signals) or [_signal(_APARTMENTS, "Apartments")]
    set_audience(
        session_ctx,
        {
            "signals": sigs,
            "demographics": demographics or {},
            "dimension_groups": [[s["ref"] for s in sigs if not s["negative"]]],
            "meta": {"country": "IN", "craft_id": "campaign_x"},
        },
    )
    return {"session_context": session_ctx}


def _apply(params, ctx):
    async def fake_load(**kw):
        return list(_CATALOGUE)

    with mock.patch.object(au.catalogue, "load", new=fake_load):
        return asyncio.run(au.apply_edit(params, ctx))


def _signals(ctx):
    return audience(ctx["session_context"])["signals"]


class AddTests(unittest.TestCase):
    def test_a_catalogue_segment_is_added_with_its_ancestry(self):
        ctx = _ctx()
        ok, msg = _apply({"action": "add", "ref": _VILLAS}, ctx)
        self.assertTrue(ok, msg)
        added = next(s for s in _signals(ctx) if s["ref"] == _VILLAS)
        # label/kind/path come from the catalogue, never from the caller — a ref carries none
        self.assertEqual(added["label"], "Villas")
        self.assertEqual(added["path"], ["Real Estate", "Villas"])

    def test_an_unknown_ref_is_refused(self):
        ctx = _ctx()
        ok, msg = _apply(
            {"action": "add", "ref": "customers/1/userInterests/99999"}, ctx
        )
        self.assertFalse(ok)
        self.assertIn("not a segment that can serve", msg)
        self.assertEqual(len(_signals(ctx)), 1)

    def test_a_bare_id_resolves_like_a_resource_name(self):
        ctx = _ctx()
        ok, _ = _apply({"action": "add", "ref": "80072"}, ctx)
        self.assertTrue(ok)
        self.assertIn(_VILLAS, [s["ref"] for s in _signals(ctx)])

    def test_adding_the_same_segment_twice_is_refused(self):
        ctx = _ctx()
        ok, msg = _apply({"action": "add", "ref": _APARTMENTS}, ctx)
        self.assertFalse(ok)
        self.assertIn("already targeted", msg)

    def test_the_dimension_group_keeps_covering_every_positive(self):
        # Groups AND together; a positive outside every group is silently not targeted.
        ctx = _ctx()
        _apply({"action": "add", "ref": _VILLAS}, ctx)
        dump = audience(ctx["session_context"])
        self.assertEqual(
            sorted(dump["dimension_groups"][0]), sorted([_APARTMENTS, _VILLAS])
        )


class DeleteTests(unittest.TestCase):
    def test_a_segment_is_removed(self):
        ctx = _ctx(_signal(_APARTMENTS, "Apartments"), _signal(_VILLAS, "Villas"))
        ok, msg = _apply({"action": "delete", "ref": _VILLAS}, ctx)
        self.assertTrue(ok, msg)
        self.assertEqual([s["ref"] for s in _signals(ctx)], [_APARTMENTS])

    def test_deleting_a_pending_custom_segment_drops_its_blueprint(self):
        # Nothing exists in the account yet, so the terms go with the signal. Left behind,
        # the panel would keep offering terms for a segment the user already removed.
        from app.agents.adzump.agents.campaign.google.audience.constants import (
            BLUEPRINTS_KEY,
            pending_ref,
        )

        ref = pending_ref("Villa buyers")
        ctx = _ctx(
            _signal(_APARTMENTS, "Apartments"),
            _signal(ref, "Villa buyers", kind="CUSTOM_AUDIENCE", owned=True),
        )
        ctx["session_context"][BLUEPRINTS_KEY] = {ref: {"label": "Villa buyers"}}

        ok, msg = _apply({"action": "delete", "ref": ref}, ctx)
        self.assertTrue(ok, msg)
        self.assertEqual(ctx["session_context"][BLUEPRINTS_KEY], {})

    def test_deleting_a_real_segment_leaves_other_blueprints_alone(self):
        from app.agents.adzump.agents.campaign.google.audience.constants import (
            BLUEPRINTS_KEY,
            pending_ref,
        )

        ref = pending_ref("Villa buyers")
        ctx = _ctx(_signal(_APARTMENTS, "Apartments"), _signal(_VILLAS, "Villas"))
        ctx["session_context"][BLUEPRINTS_KEY] = {ref: {"label": "Villa buyers"}}

        _apply({"action": "delete", "ref": _VILLAS}, ctx)
        self.assertIn(ref, ctx["session_context"][BLUEPRINTS_KEY])

    def test_the_last_segment_cannot_be_removed(self):
        # An ad group with no positive segment cannot run, and grouped mode has no
        # untargeted fallback to land in.
        ctx = _ctx()
        ok, msg = _apply({"action": "delete", "ref": _APARTMENTS}, ctx)
        self.assertFalse(ok)
        self.assertIn("cannot run with no audience", msg)
        self.assertEqual(len(_signals(ctx)), 1)

    def test_an_exclusion_does_not_count_as_the_remaining_audience(self):
        # Only user lists can be excluded; an exclusion targets nobody, so deleting the last
        # positive still empties the campaign.
        ctx = _ctx(
            _signal(_APARTMENTS, "Apartments"),
            _signal(_LIST, "Existing customers", kind="USER_LIST", negative=True),
        )
        ok, msg = _apply({"action": "delete", "ref": _APARTMENTS}, ctx)
        self.assertFalse(ok)
        self.assertIn("cannot run with no audience", msg)

    def test_deleting_something_untargeted_is_refused(self):
        ctx = _ctx()
        ok, msg = _apply({"action": "delete", "ref": _VILLAS}, ctx)
        self.assertFalse(ok)
        self.assertIn("not in this audience", msg)


class DemographicsTests(unittest.TestCase):
    def test_valid_narrowing_is_stored(self):
        ctx = _ctx()
        ok, _ = _apply(
            {
                "action": "set_demographics",
                "age_ranges": [{"min_age": 25, "max_age": 54}],
                "genders": ["FEMALE"],
            },
            ctx,
        )
        self.assertTrue(ok)
        demo = audience(ctx["session_context"])["demographics"]
        self.assertEqual(demo["genders"], ["FEMALE"])

    def test_an_invalid_age_band_is_refused(self):
        # AgeSegment takes Google's own endpoints, not arbitrary integers.
        ctx = _ctx()
        ok, msg = _apply(
            {"action": "set_demographics", "age_ranges": [{"min_age": 30}]}, ctx
        )
        self.assertFalse(ok)
        self.assertIn("Invalid demographics", msg)

    def test_overlapping_bands_are_refused(self):
        ctx = _ctx()
        ok, msg = _apply(
            {
                "action": "set_demographics",
                "age_ranges": [
                    {"min_age": 18, "max_age": 34},
                    {"min_age": 25, "max_age": 44},
                ],
            },
            ctx,
        )
        self.assertFalse(ok)
        self.assertIn("Invalid demographics", msg)

    def test_sending_nothing_clears_the_narrowing(self):
        ctx = _ctx(demographics={"genders": ["FEMALE"]})
        ok, msg = _apply({"action": "set_demographics"}, ctx)
        self.assertTrue(ok)
        self.assertIn("every age and gender", msg)
        self.assertEqual(
            audience(ctx["session_context"])["demographics"]["genders"], []
        )


class ManageToolTests(unittest.TestCase):
    """The agent's hands and eyes go through the same engine the panel does."""

    def _edit(self, edits, ctx):
        async def fake_load(**kw):
            return list(_CATALOGUE)

        async def noop_emit(*a, **kw):
            return None

        with (
            mock.patch.object(au.catalogue, "load", new=fake_load),
            mock.patch.object(au, "emit_panel", new=noop_emit),
        ):
            return asyncio.run(manage_tools._edit_audience({"edits": edits}, ctx))

    def test_batched_edits_apply_in_one_call(self):
        ctx = _ctx(_signal(_APARTMENTS, "Apartments"))
        res = self._edit(
            [
                {"action": "add", "ref": _VILLAS},
                {"action": "delete", "ref": _APARTMENTS},
            ],
            ctx,
        )
        self.assertTrue(res.success)
        self.assertEqual(res.data["applied"], 2)
        self.assertEqual([s["ref"] for s in _signals(ctx)], [_VILLAS])

    def test_the_engines_invariants_still_bind_the_agent(self):
        ctx = _ctx()
        res = self._edit([{"action": "delete", "ref": _APARTMENTS}], ctx)
        self.assertFalse(res.success)
        self.assertIn("cannot run with no audience", res.error)

    def test_lookup_answers_from_the_record(self):
        ctx = _ctx(
            _signal(
                _APARTMENTS,
                "Apartments",
                rationale="ready to buy",
                path=["Real Estate", "Apartments"],
            )
        )
        res = asyncio.run(manage_tools._lookup_segment({"segment": "Apartments"}, ctx))
        self.assertTrue(res.success)
        self.assertIn("IS targeted", res.summary)
        self.assertIn("ready to buy", res.summary)
        self.assertIn("Real Estate", res.summary)  # ancestry, not just the leaf name

    def test_lookup_admits_when_there_is_no_record(self):
        # Never invent a past reason — the catalogue is large and "we didn't pick it" is
        # usually not a recorded judgement.
        ctx = _ctx()
        res = asyncio.run(manage_tools._lookup_segment({"segment": "Villas"}, ctx))
        self.assertTrue(res.success)
        self.assertIn("NOT targeted", res.summary)
        self.assertIn("no record", res.summary)

    def test_lookup_finds_a_segment_by_the_words_the_user_used(self):
        ctx = _ctx(_signal(_APARTMENTS, "Finance & Banking"))
        res = asyncio.run(manage_tools._lookup_segment({"segment": "finance"}, ctx))
        self.assertIn("IS targeted", res.summary)


class ManageMemoryTests(unittest.TestCase):
    """handle() runs a throwaway session per call, so a follow-up ("yes, add that one") only
    resolves if the recent exchanges are replayed — and the orchestrator must not narrate an
    outcome it was never told."""

    def _handle(self, parent_ctx: dict, user_message: str, reply: str):
        from app.agents.adzump.agents.campaign.google.audience import agent as aud_agent
        from app.core.session import BaseSession

        agent = aud_agent.get_audience_manage_agent()
        seen: dict = {}

        async def fake_run(user_message, session, event_stream):  # patched on instance
            seen["seeded"] = list(session.messages)  # what it sees before it speaks
            session.append_user_message(user_message)
            session.append_assistant_message([{"type": "text", "text": reply}])

        async def fake_goc(self, session_id, auth):
            self.session_id = "throwaway"
            return "throwaway"

        async def fake_load(**kw):
            return list(_CATALOGUE)

        with (
            mock.patch.object(agent, "run", new=fake_run),
            mock.patch.object(BaseSession, "get_or_create", new=fake_goc),
            mock.patch.object(aud_agent.catalogue, "load", new=fake_load),
        ):
            ctx = {
                "session_context": parent_ctx,
                "auth": mock.MagicMock(),
                "event_stream": None,
            }
            res = asyncio.run(agent.handle(user_message, ctx))
        return res, seen

    def _parent(self, conversation=None):
        parent = {
            "campaign_spec": {"account": "1", "channel": "Demand Gen"},
            "aud_conversation": conversation or [],
        }
        set_audience(
            parent,
            {
                "signals": [_signal(_APARTMENTS, "Apartments")],
                "demographics": {},
                "dimension_groups": [[_APARTMENTS]],
                "meta": {"country": "IN"},
            },
        )
        return parent

    def test_prior_exchange_is_replayed_into_the_run(self):
        prior = [
            {
                "user": "anything for people moving house?",
                "reply": "Yes — 'Residential Relocation' under Life Events.",
            }
        ]
        _res, seen = self._handle(
            self._parent(prior), "yes add that one", "Done — added."
        )
        replayed = " ".join(str(m.get("content")) for m in seen["seeded"])
        # the referent ("that one") is now resolvable
        self.assertIn("Residential Relocation", replayed)

    def test_the_window_is_bounded_and_keeps_the_newest(self):
        prior = [{"user": f"q{i}", "reply": f"a{i}"} for i in range(6)]
        parent = self._parent(prior)
        self._handle(parent, "one more", "sure")
        kept = parent["aud_conversation"]
        self.assertEqual(len(kept), AUD_MANAGE_HISTORY_TURNS)
        self.assertEqual(kept[-1]["user"], "one more")

    def test_the_orchestrator_is_told_not_to_claim_an_outcome(self):
        res, _ = self._handle(self._parent(), "drop the luxury one", "Removed it.")
        self.assertTrue(res.success)
        self.assertIn("do not restate", res.summary.lower())

    def test_the_catalogue_is_seeded_so_search_can_run(self):
        # The MANAGE step tells the model to search before adding, and manage mode never
        # calls fetch — without seeding, every "add something for X" would dead-end.
        from app.agents.adzump.agents.campaign.google.audience import agent as aud_agent
        from app.core.session import BaseSession

        agent = aud_agent.get_audience_manage_agent()
        captured: dict = {}

        async def fake_run(user_message, session, event_stream):
            captured["candidates"] = session.context.get("aud_candidates")

        async def fake_goc(self, session_id, auth):
            self.session_id = "throwaway"
            return "throwaway"

        async def fake_load(**kw):
            return list(_CATALOGUE)

        with (
            mock.patch.object(agent, "run", new=fake_run),
            mock.patch.object(BaseSession, "get_or_create", new=fake_goc),
            mock.patch.object(aud_agent.catalogue, "load", new=fake_load),
        ):
            asyncio.run(
                agent.handle(
                    "add something for movers",
                    {
                        "session_context": self._parent(),
                        "auth": mock.MagicMock(),
                        "event_stream": None,
                    },
                )
            )
        self.assertEqual(len(captured["candidates"]), len(_CATALOGUE))


class ManageContextTests(unittest.TestCase):
    """The MANAGE step says "the audience below is already built" — so it has to be below.

    Without the listing, "drop the finance ones" gives the model no labels to work from and
    it can only guess at names to look up.
    """

    def _context(self, dump: dict) -> str:
        from app.agents.adzump.agents.campaign.google.audience.agent import (
            get_audience_manage_agent,
        )
        from app.core.session import BaseSession

        session = BaseSession(agent_name="audience_targeting")
        session.context = {"aud_mode": "manage", "aud_business_text": "b"}
        set_audience(session.context, dump)
        return asyncio.run(get_audience_manage_agent().build_dynamic_context(session))

    def test_the_saved_audience_is_listed_with_its_ancestry(self):
        ctx = self._context(
            {
                "signals": [
                    _signal(
                        _APARTMENTS,
                        "Apartments",
                        rationale="ready to buy",
                        path=["Real Estate", "Apartments"],
                    )
                ],
                "demographics": {"genders": ["FEMALE"]},
                "dimension_groups": [[_APARTMENTS]],
                "meta": {},
            }
        )
        self.assertIn("CURRENT AUDIENCE", ctx)
        self.assertIn("Apartments [IN_MARKET]", ctx)
        self.assertIn("under Real Estate", ctx)
        self.assertIn("ready to buy", ctx)
        self.assertIn("gender FEMALE", ctx)

    def test_no_narrowing_is_stated_rather_than_omitted(self):
        # Silence would read as "unknown"; the model needs to know it is deliberate.
        ctx = self._context(
            {
                "signals": [_signal(_APARTMENTS, "Apartments")],
                "demographics": {},
                "dimension_groups": [[_APARTMENTS]],
                "meta": {},
            }
        )
        self.assertIn("DEMOGRAPHICS: none", ctx)

    def test_an_exclusion_is_marked_as_one(self):
        ctx = self._context(
            {
                "signals": [
                    _signal(_APARTMENTS, "Apartments"),
                    _signal(_LIST, "Past buyers", kind="USER_LIST", negative=True),
                ],
                "demographics": {},
                "dimension_groups": [[_APARTMENTS]],
                "meta": {},
            }
        )
        self.assertIn("EXCLUDED Past buyers", ctx)


if __name__ == "__main__":
    unittest.main()
