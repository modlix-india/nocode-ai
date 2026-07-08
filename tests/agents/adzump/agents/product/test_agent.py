"""ProductAgent module-level helpers - the JSON-failure salvage path.

Regression for PR #91 B3: the salvage read looked up `search_results_merged`,
a key with zero writers, so the record promised "search evidence is attached"
and attached nothing. It must read `search_results` - the {query, candidates}
entries shortlist_competitors stashes in `_research_state`.
"""
import unittest

from app.agents.adzump.agents.product.agent import _build_minimal_result


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


if __name__ == "__main__":
    unittest.main()
