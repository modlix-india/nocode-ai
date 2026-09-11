"""Unit: creative_intelligence/taxonomy.py - Stage A classification + Stage C gate.

Stage A normalizes the Product Analyst's own text into the controlled
vocabulary (signals in priority order, override wins, idempotent per taxonomy
vintage). Stage C fails closed: unknown / other_industry / mismatch /
sub-threshold confidence never pass, an empty ad market never rejects.
"""
from __future__ import annotations

import unittest

from app.agents.adzump.creative_intelligence import taxonomy
from app.agents.adzump.creative_intelligence.models import Essence


class ClassifyTextTests(unittest.TestCase):
    def test_keyword_rows(self):
        for text, want in [
            ("Pre-launch high-rise apartments, Whitefield Bangalore",
             "residential_apartment"),
            ("2 & 3 BHK flats near ITPL", "residential_apartment"),
            ("Luxury villas with private gardens", "residential_villa"),
            ("2 & 3 BHK villaments and row houses", "residential_villa"),
            ("Plotted development, DTCP approved plots", "residential_plot"),
            ("Integrated township across 100 acres", "residential_township"),
            ("Grade A office space, 4500 sqft carpet area", "commercial_office"),
            ("Managed co-working desks in Koramangala", "commercial_coworking"),
            ("Warehouse and logistics park leasing", "commercial_industrial"),
            ("Senior living community with assisted care", "senior_living"),
            ("Beachside resort bookings", "hospitality"),
            ("Premium property advisory", "other_real_estate"),
            ("Artisanal soy candles", "unknown"),
            ("", "unknown"),
        ]:
            with self.subTest(text=text or "(empty)"):
                got, _evidence = taxonomy.classify_text(text)
                self.assertEqual(got, want)

    def test_offering_stage_rows(self):
        for text, want in [
            ("Pre-launch offer, EOI open", "pre_launch"),
            ("Ready to move 3BHK", "ready_to_move"),
            ("Under construction, possession Dec 2027", "under_construction"),
            ("no stage words here", ""),
        ]:
            with self.subTest(text=text):
                self.assertEqual(taxonomy.classify_offering_stage(text), want)


class EnsureProductClassifiedTests(unittest.TestCase):
    def test_signal_priority_business_type_first(self):
        product = {
            "business_type": "Pre-launch high-rise apartments",
            "product_name": "Green Villas",  # would say villa - must lose
            "place": {"display_name": "Whitefield, Bangalore"},
        }
        category = taxonomy.ensure_product_classified(product)
        self.assertEqual(category, "residential_apartment")
        self.assertEqual(product["product_category_source"], "businessType")
        self.assertEqual(product["product_market"], "Whitefield, Bangalore")
        self.assertEqual(product["product_offering_stage"], "pre_launch")
        self.assertEqual(product["taxonomy_version"], taxonomy.TAXONOMY_VERSION)

    def test_falls_through_signals_then_unknown(self):
        with self.subTest("name decides when businessType is silent"):
            product = {"business_type": "premium living",
                       "product_name": "Nambiar Villas"}
            self.assertEqual(taxonomy.ensure_product_classified(product),
                             "residential_villa")
            self.assertEqual(product["product_category_source"], "productName")
        with self.subTest("site links are the last resort"):
            product = {"site_links": [
                {"href": "/villas-in-whitefield", "text": "Our projects"}]}
            self.assertEqual(taxonomy.ensure_product_classified(product),
                             "residential_villa")
            self.assertEqual(product["product_category_source"], "siteLinks")
        with self.subTest("nothing matches -> unknown, stamped anyway"):
            product = {"business_type": "artisanal candles"}
            self.assertEqual(taxonomy.ensure_product_classified(product), "unknown")
            self.assertEqual(product["product_category_source"], "")

    def test_stored_classification_is_reused_until_version_bump(self):
        product = {"business_type": "villas",
                   "product_category": "residential_apartment",  # human-visible: stale
                   "taxonomy_version": taxonomy.TAXONOMY_VERSION}
        # current vintage -> trusted as-is, NOT re-derived (once per record)
        self.assertEqual(taxonomy.ensure_product_classified(product),
                         "residential_apartment")
        product["taxonomy_version"] = "0"  # bump -> re-derives
        self.assertEqual(taxonomy.ensure_product_classified(product),
                         "residential_villa")

    def test_override_wins_and_skips_derivation(self):
        product = {"business_type": "Pre-launch high-rise apartments",
                   "product_category_override": "residential_villa"}
        self.assertEqual(taxonomy.ensure_product_classified(product),
                         "residential_villa")
        self.assertNotIn("product_category", product)  # Stage A never ran


class MarketMatchTests(unittest.TestCase):
    def test_rows(self):
        for ad, product, want in [
            ("Bangalore / Whitefield", "Whitefield, Bangalore, Karnataka", True),
            ("Bengaluru / HSR", "Whitefield, Bangalore", True),  # alias
            ("Mumbai / Andheri", "Whitefield, Bangalore", False),
            ("", "Bangalore", True),          # no ad evidence -> pass
            ("Bangalore", "", True),          # no product market -> pass
            ("Gurugram / Sector 62", "Gurgaon, Haryana", True),
        ]:
            with self.subTest(ad=ad or "(empty)", product=product or "(empty)"):
                self.assertEqual(taxonomy.market_matches(ad, product), want)


class GateCreativeTests(unittest.TestCase):
    def test_verdict_rows(self):
        rows = [
            ("match accepted",
             Essence(category="residential_apartment", category_confidence=0.9),
             True, ""),
            ("no essence fails closed", None, False, taxonomy.UNKNOWN_CATEGORY),
            ("unknown never passes even confident",
             Essence(category="unknown", category_confidence=0.99),
             False, taxonomy.UNKNOWN_CATEGORY),
            ("other_industry",
             Essence(category="other_industry", category_confidence=0.9),
             False, taxonomy.NON_REAL_ESTATE),
            ("different category",
             Essence(category="residential_plot", category_confidence=0.9),
             False, taxonomy.CATEGORY_MISMATCH),
            ("exactly at threshold passes",
             Essence(category="residential_apartment",
                     category_confidence=taxonomy.ACCEPT_THRESHOLD), True, ""),
            ("below threshold",
             Essence(category="residential_apartment", category_confidence=0.74),
             False, taxonomy.LOW_CONFIDENCE),
            ("wrong city",
             Essence(category="residential_apartment", category_confidence=0.9,
                     market="Mumbai / Andheri"),
             False, taxonomy.MARKET_MISMATCH),
        ]
        for label, essence, want_ok, want_reason in rows:
            with self.subTest(label):
                ok, reason = taxonomy.gate_creative(
                    "residential_apartment", "Whitefield, Bangalore", essence)
                self.assertEqual((ok, reason), (want_ok, want_reason))


if __name__ == "__main__":
    unittest.main()
