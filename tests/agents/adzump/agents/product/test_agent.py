"""ProductAgent module-level helpers: the JSON-failure salvage path
(_build_minimal_result - regression for PR #91 B3, which read a key with zero
writers) and the discovery output contract (_discovery_violations /
_scrub_unverified - the code teeth behind 'every competitor cites
fetch_candidates evidence')."""
import asyncio
import unittest

from app.agents.adzump.agents.product.agent import (
    _PassthroughEventStream,
    _build_minimal_result,
    _discovery_violations,
    _scrub_unverified,
)


def _ctx(search_results=None):
    ctx = {"product_data": {}}
    if search_results is not None:
        ctx["_research_state"] = {"search_results": search_results}
    return ctx


class BuildMinimalResultTests(unittest.TestCase):
    def test_no_session_context_returns_none(self):
        self.assertIsNone(_build_minimal_result("https://acme.com", {}))

    def test_host_becomes_product_name(self):
        result = _build_minimal_result("https://www.acme.com/x", _ctx())
        self.assertEqual(result["business"]["product_name"], "acme.com")

    def test_search_evidence_variants(self):
        variants = [
            ("stashed hits become a note",
             [{"query": "acme rivals",
               "candidates": [{"name": "Rival Co", "url": "https://rival.co"}]}],
             ["acme rivals", "Rival Co", "https://rival.co"]),
            ("nameless candidates skipped",
             [{"query": "q", "candidates": [{"url": "https://noname.io"}]}],
             None),
            ("no research state, no evidence note", None, None),
        ]
        for label, search_results, expected_fragments in variants:
            with self.subTest(label):
                result = _build_minimal_result("https://acme.com", _ctx(search_results))
                evidence = [n for n in result["notes"] if "search evidence" in n]
                if expected_fragments is None:
                    self.assertEqual(evidence, [])
                else:
                    self.assertEqual(len(evidence), 1)
                    for fragment in expected_fragments:
                        self.assertIn(fragment, evidence[0])


_RESEARCHED = {"candidate_pool": {"C1": {}},
               "verified_competitors": [{"cid": "C1", "name": "Sobha Magnus"}]}


def _payload(*competitors: dict) -> dict:
    return {"competitive": {"competitors": list(competitors)}}


class DiscoveryContractTests(unittest.TestCase):
    """Live 2026-09-08: the analyst wrote its final JSON straight from search
    content, skipping the pipeline - hand-typed URLs, no evidence, and an
    'empty' list that had never looked. The contract is now checked in code."""

    def test_violation_rows(self):
        rows = [
            ("verified citation passes",
             _payload({"name": "Sobha Magnus", "competitor_id": "C1",
                       "url": None}), _RESEARCHED, 0),
            ("name-only entry tolerated (nothing to poison)",
             _payload({"name": "Sobha Magnus", "url": None}), _RESEARCHED, 0),
            ("model-typed url on unverified entry",
             _payload({"name": "Godrej", "url": "https://godrej-typo.com"}),
             _RESEARCHED, 1),
            ("unknown evidence id",
             _payload({"name": "Ghost", "competitor_id": "C9", "url": None}),
             _RESEARCHED, 1),
            ("empty list after a real pipeline run is honest",
             _payload(), {"candidate_pool": {}}, 0),
            ("empty list without ever looking",
             _payload(), {}, 1),
            ("no competitive block at all",
             {}, {}, 1),
        ]
        for label, payload, research_state, expected in rows:
            with self.subTest(label):
                self.assertEqual(
                    len(_discovery_violations(payload, research_state)),
                    expected)

    def test_scrub_strips_only_unverified(self):
        verified = {"name": "Sobha Magnus", "competitor_id": "C1", "url": None}
        invented = {"name": "Godrej", "url": "https://godrej-typo.com"}
        bad_cite = {"name": "Ghost", "competitor_id": "C9",
                    "url": "https://ghost.example"}
        competitive = {"competitors": [verified, invented, bad_cite]}
        scrubbed = _scrub_unverified(competitive, _RESEARCHED)
        self.assertEqual(scrubbed, ["Godrej", "Ghost"])
        self.assertEqual(verified["competitor_id"], "C1")  # untouched
        for comp in (invented, bad_cite):
            self.assertIsNone(comp["url"])
            self.assertNotIn("competitor_id", comp)


class ThinkingStreamTests(unittest.TestCase):
    """The card's thinking names candidate IDs, even one cut across deltas."""

    def test_an_id_split_across_deltas_is_named_whole(self):
        class Parent:
            def __init__(self):
                self.thinking, self.order = [], []

            async def emit_thinking(self, text):
                self.thinking.append(text)
                self.order.append("thinking")

            async def emit_tool_start(self, *args, **kwargs):
                self.order.append("tool")

        state = {"candidate_pool": {
            "C16": {"name": "Valmark Cityville - Villas", "host": "valmarkcityville.com"}}}
        parent = Parent()
        stream = _PassthroughEventStream(parent, "tu1", state)

        async def run():
            for delta in ["Keeping C1", "6 and C", "99. Next C1"]:
                await stream.emit_thinking(delta)
            await stream.emit_tool_start("fetch_candidates", {})
            await stream.flush_thinking()

        asyncio.run(run())
        self.assertEqual("".join(parent.thinking),
                         "Keeping Valmark Cityville (valmarkcityville.com) and C99. Next C1")
        self.assertEqual(parent.order[-2:], ["thinking", "tool"])  # held piece before the row


if __name__ == "__main__":
    unittest.main()
