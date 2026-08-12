"""Unit tests for the keyword agent's post-generation tools
(app/agents/adzump/agents/campaign/google/keyword/manage_tools.py).

These are the agent's hands and eyes: lookup_keyword answers from the record, edit_keywords
mutates through the SAME engine the panel's click path uses.
"""

# regression: an edit must never go through the submit tools (they replace a set wholesale,
# which fabricates provenance on rows the model never re-derived and clobbers panel clicks).
from __future__ import annotations

import asyncio
import unittest

from app.agents.adzump.agents.campaign.google.keyword import manage_tools, tools
from app.agents.adzump.agents.campaign.models import keyword_research


def _row(keyword: str, **extra) -> dict:
    return {"keyword": keyword, "volume": 100, "match_type": "PHRASE", **extra}


def _ctx(**over) -> dict:
    dump = {
        "themes": {
            "brand": {
                "theme": "brand",
                "label": "Brand",
                "positives": [
                    _row(
                        "nike air",
                        rationale="the brand itself",
                        admitted_by="brand name — mandatory",
                        source="google",
                        source_seed="nike",
                        volume_at_pick=9000,
                    )
                ],
                "negatives": [_row("nike air scam", reason="distrust — not a buyer")],
                "rejections": [
                    {
                        "keyword": "cheap nike air",
                        "rule": "not_selected",
                        "volume_at_eval": 4400,
                        "reason": "price-shopper — we sell premium",
                    },
                    {
                        "keyword": "nike air glue",
                        "rule": "zero_volume",
                        "volume_at_eval": 0,
                        "reason": "",
                    },
                ],
            },
            "generic": {
                "theme": "generic",
                "label": "Generic",
                "positives": [_row("running shoes")],
                "negatives": [],
                "rejections": [],
            },
        },
        "meta": {"geo": {}},
    }
    ctx = {
        "session_context": {
            "keyword_research": dump,
            "product_data": {"product_name": "Nike"},
        }
    }
    ctx["session_context"].update(over)
    return ctx


def _built(ctx: dict) -> dict:
    """The saved keyword set, read the way production reads it. The fixture seeds the
    pre-envelope key, so this also covers the one-way migration on first write."""
    return keyword_research(ctx["session_context"]) or {}


def _lookup(kw: str, ctx: dict):
    return asyncio.run(manage_tools._lookup_keyword({"keyword": kw}, ctx))


def _edit(edits: list[dict], ctx: dict):
    return asyncio.run(manage_tools._edit_keywords({"edits": edits}, ctx))


class _CaptureStream:
    """Captures emit_craft so we can assert the panel is re-emitted after an agent edit."""

    def __init__(self):
        self.crafts: list[dict] = []

    async def emit_craft(self, craft_id, title, blocks, append=False):
        self.crafts.append({"craft_id": craft_id, "blocks": blocks, "append": append})


class LookupTests(unittest.TestCase):
    """'Why is X here?' / 'Why isn't Y?' — answered from what was recorded."""

    def test_a_kept_keyword_returns_its_full_record(self):
        res = _lookup("nike air", _ctx())
        self.assertTrue(res.success)
        self.assertIn("IS in the brand ad group", res.summary)
        self.assertIn("the brand itself", res.summary)  # rationale
        self.assertIn("brand name — mandatory", res.summary)  # admitted_by
        self.assertIn("nike", res.summary)  # source_seed
        self.assertIn("9000", res.summary)  # volume_at_pick

    def test_a_negative_says_why_it_is_excluded(self):
        res = _lookup("nike air scam", _ctx())
        self.assertIn("negatives", res.summary)
        self.assertIn("distrust — not a buyer", res.summary)

    def test_a_passed_over_keyword_answers_from_the_ledger(self):
        res = _lookup("cheap nike air", _ctx())
        self.assertIn("NOT in the brand ad group", res.summary)
        self.assertIn("scored but not selected", res.summary)
        self.assertIn("4400", res.summary)  # volume when scored
        self.assertIn("price-shopper", res.summary)  # the recorded reason

    def test_a_zero_volume_drop_explains_itself(self):
        res = _lookup("nike air glue", _ctx())
        self.assertIn("no Google search volume", res.summary)

    def test_an_unseen_keyword_admits_there_is_no_record(self):
        # The honest case: we cannot record why we never thought of something.
        res = _lookup("blue suede shoes", _ctx())
        self.assertTrue(res.success)
        self.assertIn("No record", res.summary)
        self.assertIn("never a candidate", res.summary)
        self.assertIn("keyword_metrics", res.summary)  # steers to a fresh check
        self.assertIn("fresh check", res.summary)  # ...and to say so


class EditTests(unittest.TestCase):
    """Edits go through the panel's own engine, so they can't break its invariants."""

    def test_add_goes_through_the_shared_engine_and_persists(self):
        ctx = _ctx()
        res = _edit(
            [
                {
                    "action": "add",
                    "keyword_type": "generic",
                    "section": "positives",
                    "keyword": "trail running shoes",
                    "match_type": "PHRASE",
                }
            ],
            ctx,
        )
        self.assertTrue(res.success)
        rows = _built(ctx)["themes"]["generic"]["positives"]
        self.assertIn("trail running shoes", [r["keyword"] for r in rows])

    def test_batched_edits_apply_in_one_call(self):
        ctx = _ctx()
        res = _edit(
            [
                {
                    "action": "add",
                    "keyword_type": "generic",
                    "section": "positives",
                    "keyword": "trail shoes",
                },
                {
                    "action": "delete",
                    "keyword_type": "generic",
                    "section": "positives",
                    "keyword": "running shoes",
                },
            ],
            ctx,
        )
        self.assertTrue(res.success)
        self.assertEqual(res.data["applied"], 2)
        rows = [r["keyword"] for r in _built(ctx)["themes"]["generic"]["positives"]]
        self.assertEqual(rows, ["trail shoes"])

    def test_the_engines_invariants_still_bind_the_agent(self):
        # A positive can't live in two ad groups — the same rule a panel click hits.
        ctx = _ctx()
        res = _edit(
            [
                {
                    "action": "add",
                    "keyword_type": "generic",
                    "section": "positives",
                    "keyword": "nike air",
                }
            ],
            ctx,
        )
        self.assertFalse(res.success)
        self.assertIn("two ad groups", res.error)

    def test_a_negative_can_never_be_exact_even_from_the_agent(self):
        ctx = _ctx()
        _edit(
            [
                {
                    "action": "add",
                    "keyword_type": "generic",
                    "section": "negatives",
                    "keyword": "free shoes",
                    "match_type": "EXACT",
                }
            ],
            ctx,
        )
        rows = _built(ctx)["themes"]["generic"]["negatives"]
        self.assertEqual(rows[0]["match_type"], "PHRASE")

    def test_partial_failure_reports_but_still_applies_the_rest(self):
        ctx = _ctx()
        res = _edit(
            [
                {
                    "action": "add",
                    "keyword_type": "generic",
                    "section": "positives",
                    "keyword": "trail shoes",
                },
                {
                    "action": "delete",
                    "keyword_type": "generic",
                    "section": "positives",
                    "keyword": "ghost",
                },
            ],
            ctx,
        )
        self.assertTrue(res.success)  # the good one landed
        self.assertEqual(res.data["applied"], 1)
        self.assertIn("rejected", res.summary)

    def test_edits_must_be_a_non_empty_list(self):
        self.assertFalse(_edit([], _ctx()).success)


class SubmitToolsAreStructurallyBuildOnlyTests(unittest.TestCase):
    """The submit tools rebuild a set wholesale — right for a build, destructive for an edit.
    Manage is a SEPARATE configured instance that never exposes them, so an edit can't reach
    them at all — stronger than the old runtime guard, which this replaces."""

    def test_manage_instance_exposes_edit_not_submit(self):
        from app.agents.adzump.agents.campaign.google.keyword.agent import (
            get_keyword_manage_agent,
            get_keyword_research_agent,
        )

        manage = set(get_keyword_manage_agent().tools)
        research = set(get_keyword_research_agent().tools)
        # The manage agent can edit but can never wholesale-replace a saved set.
        self.assertIn("edit_keywords", manage)
        self.assertNotIn("submit_positive_keywords", manage)
        self.assertNotIn("submit_negative_keywords", manage)
        # The build agent submits; it has no edit tool (that path is manage-only).
        self.assertIn("submit_positive_keywords", research)
        self.assertIn("submit_negative_keywords", research)
        self.assertNotIn("edit_keywords", research)

    def test_submit_still_works_during_a_real_run(self):
        state = {
            "kw_type": "generic",
            "kw_candidates": [{"keyword": "running shoes", "volume": 500}],
        }
        res = asyncio.run(
            tools._submit_positive_keywords(
                {"keywords": [{"keyword": "running shoes", "match_type": "phrase"}]},
                {"session_context": state},
            )
        )
        self.assertTrue(res.success)


class EditPanelReEmitTests(unittest.TestCase):
    """A spoken edit must refresh the panel — otherwise the UI shows the pre-edit set."""

    def test_successful_edit_reemits_the_keyword_block_in_place(self):
        stream = _CaptureStream()
        ctx = _ctx()
        ctx["session_context"]["campaign_craft_id"] = "campaign_abc"
        ctx["event_stream"] = stream
        res = _edit(
            [
                {
                    "action": "add",
                    "keyword_type": "generic",
                    "section": "positives",
                    "keyword": "trail shoes",
                }
            ],
            ctx,
        )
        self.assertTrue(res.success)
        self.assertEqual(len(stream.crafts), 1)  # panel refreshed exactly once
        craft = stream.crafts[0]
        self.assertEqual(craft["craft_id"], "campaign_abc")
        self.assertTrue(craft["append"])  # keyed upsert (no flash), not a rebuild
        self.assertEqual(craft["blocks"][0]["id"], "keyword_review")

    def test_a_rejected_edit_does_not_touch_the_panel(self):
        stream = _CaptureStream()
        ctx = _ctx()
        ctx["event_stream"] = stream
        res = _edit(
            [
                {
                    "action": "delete",
                    "keyword_type": "generic",
                    "section": "positives",
                    "keyword": "ghost",
                }
            ],
            ctx,
        )  # not present
        self.assertFalse(res.success)
        self.assertEqual(stream.crafts, [])  # nothing changed → no re-emit

    def test_reemit_keys_off_the_crafts_own_meta_not_a_session_key(self):
        # The panel was drawn under the craft_id research recorded in the dump's meta; an
        # edit must upsert into THAT container even when a session key holds a stale value —
        # so the re-emit works by construction, not by the two happening to coincide.
        stream = _CaptureStream()
        ctx = _ctx()
        ctx["session_context"]["keyword_research"]["meta"]["craft_id"] = "campaign_real"
        ctx["session_context"]["campaign_craft_id"] = "campaign_stale"  # must NOT win
        ctx["event_stream"] = stream
        res = _edit(
            [
                {
                    "action": "add",
                    "keyword_type": "generic",
                    "section": "positives",
                    "keyword": "trail shoes",
                }
            ],
            ctx,
        )
        self.assertTrue(res.success)
        self.assertEqual(stream.crafts[0]["craft_id"], "campaign_real")


class EditWrapperTests(unittest.TestCase):
    """The batching wrapper around the shared _apply_edit engine."""

    def test_too_many_edits_in_one_call_are_rejected(self):
        edits = [
            {
                "action": "add",
                "keyword_type": "generic",
                "section": "positives",
                "keyword": f"trail shoes {i}",
            }
            for i in range(manage_tools._MAX_EDITS + 1)
        ]
        res = _edit(edits, _ctx())
        self.assertFalse(res.success)
        self.assertIn("Too many", res.error)

    def test_non_dict_edits_are_skipped_not_fatal(self):
        res = _edit(
            [
                "nonsense",
                None,
                {
                    "action": "add",
                    "keyword_type": "generic",
                    "section": "positives",
                    "keyword": "trail shoes",
                },
            ],
            _ctx(),
        )
        self.assertTrue(res.success)  # the one valid edit lands; junk is skipped
        self.assertEqual(res.data["applied"], 1)

    def test_edits_must_be_a_list(self):
        res = asyncio.run(
            manage_tools._edit_keywords({"edits": "add trail shoes"}, _ctx())
        )
        self.assertFalse(res.success)


class ManageMemoryTests(unittest.TestCase):
    """handle() runs a throwaway session per call, so a follow-up ("yes, add that one") only
    resolves if the recent exchanges are replayed — and the orchestrator must not narrate the
    outcome it was never told."""

    def _handle(self, parent_ctx: dict, user_message: str, reply: str):
        from unittest import mock
        from app.agents.adzump.agents.campaign.google.keyword import agent as kw_agent
        from app.core.session import BaseSession

        agent = kw_agent.get_keyword_manage_agent()
        seen: dict = {}

        async def fake_run(
            user_message, session, event_stream
        ):  # no self — patched on instance
            seen["seeded"] = list(
                session.messages
            )  # what the agent sees before it speaks
            session.append_user_message(user_message)
            session.append_assistant_message([{"type": "text", "text": reply}])

        async def fake_goc(self, session_id, auth):
            self.session_id = "throwaway"
            return "throwaway"

        with (
            mock.patch.object(agent, "run", new=fake_run),
            mock.patch.object(BaseSession, "get_or_create", new=fake_goc),
        ):
            ctx = {
                "session_context": parent_ctx,
                "auth": mock.MagicMock(),
                "event_stream": None,
            }
            res = asyncio.run(agent.handle(user_message, ctx))
        return res, seen

    def _parent(self, conversation=None):
        return {
            "keyword_research": {
                "themes": {
                    "brand": {
                        "theme": "brand",
                        "label": "Brand",
                        "positives": [],
                        "negatives": [],
                    }
                },
                "meta": {},
            },
            "product_data": {"product_name": "Kajaria"},
            "kw_conversation": conversation or [],
        }

    def test_prior_exchange_is_replayed_into_the_run(self):
        prior = [
            {
                "user": "why no staircase keyword?",
                "reply": "It never came up — the one with demand is 'kajaria staircase tiles'.",
            }
        ]
        _res, seen = self._handle(
            self._parent(prior), "yes add that one", "Done — added."
        )
        replayed = " ".join(str(m.get("content")) for m in seen["seeded"])
        self.assertIn(
            "kajaria staircase tiles", replayed
        )  # the referent is now resolvable

    def test_exchange_is_recorded_and_window_is_bounded(self):
        from app.agents.adzump.agents.campaign.google.keyword.agent import (
            KW_MANAGE_HISTORY_TURNS,
        )

        prior = [
            {"user": f"q{i}", "reply": f"a{i}"}
            for i in range(KW_MANAGE_HISTORY_TURNS + 2)
        ]
        parent = self._parent(prior)
        self._handle(parent, "add trail shoes", "Added trail shoes.")
        conv = parent["kw_conversation"]
        self.assertLessEqual(len(conv), KW_MANAGE_HISTORY_TURNS)  # bounded
        self.assertEqual(
            conv[-1], {"user": "add trail shoes", "reply": "Added trail shoes."}
        )  # newest kept

    def test_return_tells_the_orchestrator_not_to_narrate(self):
        res, _ = self._handle(self._parent(), "add trail shoes", "Added trail shoes.")
        self.assertTrue(res.success)
        self.assertIn("do not restate", res.summary.lower())
        self.assertNotIn(
            "trail shoes", res.summary
        )  # the outcome is NOT handed to the orchestrator


class ManageReminderRendersFullyTests(unittest.TestCase):
    """The manage turn-prompt nests each ad group's selection bar, whose text carries
    $target_count / $max_seeds. A single substitution pass would leave those literal —
    guard against that leak through the REAL build_turn_reminder path."""

    def _reminder(self) -> str:
        import re  # noqa: F401 — used in the assertions below
        from app.agents.adzump.agents.campaign.google.keyword.agent import (
            get_keyword_manage_agent,
        )
        from app.core.session import BaseSession

        session = BaseSession(agent_name="keyword_research")
        session.context = {
            "kw_mode": "manage",
            "kw_type": "brand",
            "kw_user_message": "add location keywords",
            "keyword_research": {
                "themes": {
                    "brand": {
                        "label": "Brand",
                        "positives": [],
                        "negatives": [],
                        "rejections": [],
                    },
                    "generic": {
                        "label": "Generic",
                        "positives": [],
                        "negatives": [],
                        "rejections": [],
                    },
                }
            },
        }
        return asyncio.run(get_keyword_manage_agent().build_turn_reminder(session, 1))

    def test_no_unresolved_template_vars(self):
        import re

        leftover = re.findall(r"\$\{?[A-Za-z_]\w*", self._reminder())
        self.assertEqual(
            leftover, [], f"unresolved template vars in manage prompt: {leftover}"
        )

    def test_carries_both_bars_and_the_verbatim_ask(self):
        rem = self._reminder()
        self.assertIn("Brand ad group", rem)
        self.assertIn("Generic ad group", rem)
        self.assertIn("add location keywords", rem)  # the user's words reach the model


if __name__ == "__main__":
    unittest.main()
