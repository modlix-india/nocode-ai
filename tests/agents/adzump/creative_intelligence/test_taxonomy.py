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


class ClassifyProductTests(unittest.TestCase):
    """The fields to stamp - what the creatives tool saves on the product row."""

    def test_stamps(self):
        current = {"category": "real_estate", "taxonomy_version": taxonomy.TAXONOMY_VERSION}
        rows = [  # (case, product, stamped keys)
            ("already classified", current, set()),
            ("override without a market", {"category_override": "residential_villa",
                                           "place": {"address": "Hebbal"}}, {"market"}),
            ("override with a market", {"category_override": "residential_villa",
                                        "market": "Hebbal"}, set()),
            ("unclassified", {"business_type": "villas"},
             {"category", "subcategory", "market", "offering_stage", "category_source",
              "category_confidence", "taxonomy_version"}),
        ]
        for case, product, keys in rows:
            with self.subTest(case):
                self.assertEqual(set(taxonomy.classify_product(dict(product))), keys)


class EnsureProductClassifiedTests(unittest.TestCase):
    def test_signal_priority_business_type_first(self):
        product = {
            "business_type": "Pre-launch high-rise apartments",
            "product_name": "Green Villas",  # would say villa - must lose
            "place": {"display_name": "Whitefield, Bangalore"},
        }
        category = taxonomy.ensure_product_classified(product)
        # Top-level category, the kind as subcategory (Kailash 2026-09-25).
        self.assertEqual((category, product["subcategory"]), ("real_estate", "apartment"))
        self.assertEqual(product["category_source"], "businessType")
        self.assertEqual(product["market"], "Whitefield, Bangalore")
        self.assertEqual(product["offering_stage"], "pre_launch")
        self.assertEqual(product["taxonomy_version"], taxonomy.TAXONOMY_VERSION)

    def test_falls_through_signals_then_unknown(self):
        with self.subTest("name decides when businessType is silent"):
            product = {"business_type": "premium living",
                       "product_name": "Nambiar Villas"}
            self.assertEqual(taxonomy.ensure_product_classified(product), "real_estate")
            self.assertEqual(product["subcategory"], "villa")
            self.assertEqual(product["category_source"], "productName")
        with self.subTest("site links are the last resort"):
            product = {"site_links": [
                {"href": "/villas-in-whitefield", "text": "Our projects"}]}
            self.assertEqual(taxonomy.ensure_product_classified(product), "real_estate")
            self.assertEqual(product["category_source"], "siteLinks")
        with self.subTest("nothing matches -> unknown, stamped anyway"):
            product = {"business_type": "artisanal candles"}
            self.assertEqual(taxonomy.ensure_product_classified(product), "unknown")
            self.assertEqual((product["category_source"], product["subcategory"]), ("", ""))

    def test_stored_classification_is_reused_until_version_bump(self):
        product = {"business_type": "villas", "category": "real_estate",
                   "subcategory": "apartment",  # human-visible: stale
                   "taxonomy_version": taxonomy.TAXONOMY_VERSION}
        # current vintage -> trusted as-is, NOT re-derived (once per record)
        self.assertEqual(taxonomy.ensure_product_classified(product), "real_estate")
        self.assertEqual(product["subcategory"], "apartment")
        product["taxonomy_version"] = "0"  # bump -> re-derives
        taxonomy.ensure_product_classified(product)
        self.assertEqual(product["subcategory"], "villa")
        # A record stored in the old leaf shape re-derives into the new one.
        old = {"business_type": "villas", "category": "residential_villa",
               "taxonomy_version": taxonomy.TAXONOMY_VERSION}
        self.assertEqual(taxonomy.ensure_product_classified(old), "real_estate")
        self.assertEqual(old["subcategory"], "villa")

    def test_override_wins_and_skips_derivation(self):
        product = {"business_type": "Pre-launch high-rise apartments",
                   "category_override": "other_industry"}
        self.assertEqual(taxonomy.ensure_product_classified(product), "other_industry")
        self.assertNotIn("category", product)  # Stage A never ran


class MarketMatchTests(unittest.TestCase):
    def test_rows(self):
        for ad, product, want in [
            ("Bangalore / Whitefield", "Whitefield, Bangalore, Karnataka", True),
            ("Bengaluru / HSR", "Whitefield, Bangalore", True),  # alias
            ("Mumbai / Andheri", "Whitefield, Bangalore", False),
            ("", "Bangalore", True),          # no ad evidence -> pass
            ("Bangalore", "", True),          # no product market -> pass
            ("Gurugram / Sector 62", "Gurgaon, Haryana", True),
            # Locality-token fallback: display_name may lack the city entirely
            # (live 2026-09-21 - every Bangalore ad got market_mismatch).
            ("Bangalore / Bannerghatta Road",
             "Valmark Cityville, Bannerghatta Rd, Karnataka, India", True),
            ("Mumbai / Bandra West",
             "Valmark Cityville, Bannerghatta Rd, Karnataka, India", False),
            # Whole-token overlap only - "pura" must not match "puravankara".
            ("Chennai / Pura", "Puravankara Towers, Bangalore", False),
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
            # A villa project's competitors sell apartments and plots too:
            # only the top-level category has to match (live 2026-09-25:
            # Godrej's and Lodha's apartment ads were all dropped).
            ("another kind of real estate",
             Essence(category="residential_plot", category_confidence=0.9),
             True, ""),
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
                    "real_estate", "Whitefield, Bangalore", essence)
                self.assertEqual((ok, reason), (want_ok, want_reason))

    def test_top_level_and_subcategory(self):
        rows = [("residential_villa", "real_estate", "villa"),
                ("commercial_office", "real_estate", "office"),
                ("hospitality", "real_estate", "hospitality"),
                ("real_estate", "real_estate", ""),
                ("other_industry", "other_industry", ""),
                ("unknown", "unknown", ""), ("", "unknown", "")]
        for leaf, top, sub in rows:
            with self.subTest(leaf or "(empty)"):
                self.assertEqual((taxonomy.top_level(leaf), taxonomy.subcategory(leaf)),
                                 (top, sub))
        # A real-estate ad never matches a product of another industry.
        ok, reason = taxonomy.gate_creative(
            "other_industry", "", Essence(category="residential_villa",
                                          category_confidence=0.9))
        self.assertEqual((ok, reason), (False, taxonomy.CATEGORY_MISMATCH))


if __name__ == "__main__":
    unittest.main()
