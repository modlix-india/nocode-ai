"""Unit tests for offering-taxonomy fail-soft behavior
(app/agents/adzump/agents/campaign/google/keyword/taxonomy.py).
"""
# regression: a transient derivation failure must return complete=False so the
# caller does NOT cache (and permanently poison) the degraded taxonomy; a response
# with usage populated must not crash the one-shot billing hook.
from __future__ import annotations

import asyncio
import types
import unittest
from unittest import mock

from app.agents.adzump.agents.campaign.google.keyword.taxonomy import (
    derive_offering_taxonomy,
)


def _derive(product: dict):
    return asyncio.run(derive_offering_taxonomy(product))


def _fake_openai(*, content=None, usage=None, raise_exc=None):
    """Patch openai.AsyncOpenAI (imported inside the function at call time) with a
    client whose chat.completions.create yields the given response, or raises."""
    create = mock.AsyncMock()
    if raise_exc is not None:
        create.side_effect = raise_exc
    else:
        create.return_value = types.SimpleNamespace(
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content=content))],
            usage=usage,
        )
    client = mock.MagicMock()
    client.chat.completions.create = create
    return mock.patch("openai.AsyncOpenAI", return_value=client)


_VALID_JSON = (
    '{"primary_offering": "eyewear", "core_terms": ["eyeglasses", "sunglasses"], '
    '"sibling_categories": ["contact lenses"], "is_location_specific": false, '
    '"sells_physical_products": true, "includes_informational_funnel": false}'
)


class TaxonomyFailSoftTests(unittest.TestCase):
    def test_empty_brief_returns_complete_deterministic_fallback(self):
        # No product fields -> no LLM call -> a deterministic fallback that IS cacheable.
        tax = _derive({})
        self.assertTrue(tax.complete)
        self.assertEqual(tax.core_terms, [])

    def test_transient_failure_marks_incomplete(self):
        with _fake_openai(raise_exc=RuntimeError("boom")):
            tax = _derive({"business_type": "eyewear brand"})
        self.assertFalse(tax.complete)  # caller must NOT cache this
        self.assertEqual(tax.primary_offering, "eyewear brand")
        self.assertEqual(tax.core_terms, ["eyewear brand"])

    def test_unparseable_json_marks_incomplete(self):
        with _fake_openai(content="not json at all", usage=None):
            tax = _derive({"business_type": "eyewear brand"})
        self.assertFalse(tax.complete)

    def test_valid_response_is_complete(self):
        with _fake_openai(content=_VALID_JSON, usage=None):
            tax = _derive({"business_type": "eyewear brand",
                           "products_services": ["eyeglasses"]})
        self.assertTrue(tax.complete)
        self.assertEqual(tax.primary_offering, "eyewear")
        self.assertIn("eyeglasses", tax.core_terms)
        self.assertTrue(tax.sells_physical_products)

    def test_usage_present_does_not_crash_billing(self):
        # resp.usage populated + no active session -> record_oneshot_usage no-ops safely.
        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        with _fake_openai(content=_VALID_JSON, usage=usage):
            tax = _derive({"business_type": "eyewear brand"})
        self.assertTrue(tax.complete)


if __name__ == "__main__":
    unittest.main()
