"""scrapecreators.com adapter - raw ad mapping, advertiser selection, search policy."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.creative_intelligence import scrapecreators
from app.agents.adzump.creative_intelligence.scrapecreators import (
    ScrapeCreatorsError,
    ScrapeCreatorsSource,
    _ads_of_the_advertiser,
    _to_creative,
)


def _ad(*, page_id="p1", page_name="Purva Sparkling Springs",
        link_url=None, **overrides) -> dict:
    ad = {
        "ad_archive_id": "a1",
        "page_id": page_id,
        "page_name": page_name,
        "is_active": True,
        "start_date": 1740729600,
        "end_date": 1741334400,
        "publisher_platform": ["FACEBOOK", "INSTAGRAM"],
        "spend": None,
        "snapshot": {
            "body": {"text": "Lakefront living"},
            "title": "Sparkling Springs",
            "cta_text": "Learn More",
            "link_url": link_url,
            "display_format": "IMAGE",
            "images": [{"original_image_url": "https://cdn/x.jpg",
                        "resized_image_url": "https://cdn/x_small.jpg"}],
            "videos": [],
            "page_profile_picture_url": "https://cdn/logo.jpg",
        },
    }
    ad.update(overrides)
    return ad


class ToCreativeTests(unittest.TestCase):
    def test_image_ad_maps_fully(self):
        creative = _to_creative(_ad())
        self.assertEqual(creative.creative_id, "a1")
        self.assertEqual(creative.media_type, "image")
        self.assertEqual(creative.source_asset_url, "https://cdn/x.jpg")
        self.assertEqual(creative.headline, "Sparkling Springs")
        self.assertEqual(creative.primary_text, "Lakefront living")
        self.assertEqual(creative.cta, "Learn More")
        self.assertTrue(creative.is_active)
        self.assertEqual(creative.days_running, 7)
        self.assertEqual(creative.publisher_platforms, ["FACEBOOK", "INSTAGRAM"])
        self.assertTrue(creative.first_seen.startswith("2025-02-28"))

    def test_media_rows(self):
        video = {"video_hd_url": "https://cdn/v.mp4",
                 "video_preview_image_url": "https://cdn/v_poster.jpg"}
        for label, snapshot_extra, media, asset, poster in [
            ("video ad uses video + poster", {"display_format": "VIDEO", "videos": [video]},
             "video", "https://cdn/v.mp4", "https://cdn/v_poster.jpg"),
            ("video in a card maps as video",
             {"display_format": "VIDEO", "images": [], "videos": [], "cards": [video]},
             "video", "https://cdn/v.mp4", "https://cdn/v_poster.jpg"),
            ("multi images is a carousel", {"display_format": "MULTI_IMAGES"},
             "carousel", "https://cdn/x.jpg", ""),
        ]:
            with self.subTest(label):
                ad = _ad()
                ad["snapshot"].update(snapshot_extra)
                creative = _to_creative(ad)
                self.assertEqual((creative.media_type, creative.source_asset_url,
                                  creative.poster_source_url), (media, asset, poster))

    def test_carousel_pulls_media_and_copy_from_cards(self):
        # Live shape (verified 2026-09-01): carousel ads have empty top-level
        # images/videos; each card carries its own media and copy.
        ad = _ad()
        ad["snapshot"].update({
            "display_format": "MULTI_IMAGES",
            "title": None, "cta_text": None, "link_url": None,
            "body": None, "images": [], "videos": [],
            "cards": [{
                "title": "Villas Around a Waterfall",
                "body": "Lakefront villas",
                "cta_text": "Learn More",
                "link_url": "https://purvasparklingspring.com",
                "original_image_url": "https://cdn/card1.jpg",
                "resized_image_url": "https://cdn/card1_small.jpg",
                "video_hd_url": None,
            }],
        })
        creative = _to_creative(ad)
        self.assertEqual(creative.media_type, "carousel")
        self.assertEqual(creative.source_asset_url, "https://cdn/card1.jpg")
        self.assertEqual(creative.headline, "Villas Around a Waterfall")
        self.assertEqual(creative.primary_text, "Lakefront villas")
        self.assertEqual(creative.landing_url, "https://purvasparklingspring.com")

    def test_null_fields_survive(self):
        ad = _ad()
        ad["snapshot"].update({"title": None, "cta_text": None, "body": None,
                               "link_url": None, "images": []})
        ad.update({"start_date": None, "end_date": None, "is_active": None})
        creative = _to_creative(ad)
        self.assertEqual(creative.headline, "")
        self.assertFalse(creative.is_active)
        self.assertEqual(creative.days_running, 0)


class AdvertiserSelectionTests(unittest.TestCase):
    """Keyword search mixes advertisers; exactly one page's ads may enter the
    shared library, chosen by domain link, else name match, else nobody."""

    def test_domain_link_beats_name_and_size(self):
        ads = ([_ad(page_id="agg", page_name="99acres Deals")] * 5
               + [_ad(page_id="dev", page_name="Puravankara",
                      link_url="https://purvasparklingspring.com/offer")])
        chosen = _ads_of_the_advertiser(
            ads, domain="purvasparklingspring.com", name="Purva Sparkling Springs")
        self.assertEqual({a["page_id"] for a in chosen}, {"dev"})

    def test_leadgen_domain_match_via_extra_links(self):
        # Live shape (2026-09-01): lead ads carry link_url=fb.me; the real
        # site rides in snapshot.extra_links.
        ad = _ad(page_id="dev", page_name="Some Renamed Page",
                 link_url="http://fb.me/")
        ad["snapshot"]["extra_links"] = ["https://fincity.com/privacypolicy2",
                                         "https://purvasparklingspring.com/"]
        chosen = _ads_of_the_advertiser(
            [ad], domain="purvasparklingspring.com", name="Purva Sparkling Springs")
        self.assertEqual({a["page_id"] for a in chosen}, {"dev"})

    def test_name_match_without_domain(self):
        ads = [_ad(page_id="junk", page_name="Springs Salon")] * 3 \
            + [_ad(page_id="own", page_name="Purva Sparkling Springs")]
        chosen = _ads_of_the_advertiser(ads, domain="", name="Purva Sparkling Springs")
        self.assertEqual({a["page_id"] for a in chosen}, {"own"})

    def test_no_match_returns_nothing(self):
        ads = [_ad(page_id="junk", page_name="Springs Salon")]
        self.assertEqual(
            _ads_of_the_advertiser(ads, domain="lodhagroup.com", name="Lodha Azur"),
            [])


class SearchPolicyTests(unittest.TestCase):
    def _source_with_pages(self, pages: list[dict]):
        source = ScrapeCreatorsSource()
        self.calls: list[dict] = []

        async def fake_page(*, name, country, cursor):
            self.calls.append({"country": country, "cursor": cursor})
            return pages.pop(0)

        source._search_page = fake_page
        return source

    def test_one_default_search_attributes_the_advertiser(self):
        # No search_type is ever sent (Kailash 2026-09-04): the API default
        # casts the widest net; exact_phrase missed word-order variants.
        source = self._source_with_pages([
            {"searchResults": [_ad()], "cursor": ""},
        ])
        fetched = asyncio.run(source.fetch(
            domain="", name="Purva Sparkling Springs", country="IN"))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["country"], "IN")
        self.assertEqual(len(fetched.creatives), 1)
        self.assertEqual(fetched.logo_url, "https://cdn/logo.jpg")

    def test_no_search_type_param_reaches_the_api(self):
        captured: dict = {}

        class _Resp:
            status_code = 200
            def json(self):
                return {"searchResults": [], "cursor": ""}

        class _Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, url, headers=None, params=None):
                captured.update(params or {})
                return _Resp()

        with mock.patch.object(scrapecreators.settings,
                               "SCRAPECREATORS_API_KEY", "k"), \
             mock.patch.object(scrapecreators.httpx, "AsyncClient",
                               return_value=_Client()):
            asyncio.run(ScrapeCreatorsSource().fetch(domain="", name="X"))
        self.assertNotIn("search_type", captured)
        self.assertEqual(captured.get("query"), "X")

    def test_mention_tier_requires_a_brand_mention(self):
        # No attributable page anywhere (all broker ads): only ads whose OWN
        # text names the brand ship (capped, no page identity claimed). Ads
        # that merely matched locality keywords are attribution mismatches
        # and never enter the library (Rule 5, live 2026-09-10).
        mentioning = [_ad(page_id=f"b{i}", page_name=f"Broker {i}")
                      for i in range(scrapecreators.MENTION_ADS_CAP + 5)]
        for ad in mentioning:
            ad["snapshot"]["body"] = {"text": "New launch: Nambiar Villas X!"}
        unrelated = [_ad(page_id=f"u{i}", page_name=f"Other {i}")
                     for i in range(4)]
        source = self._source_with_pages([
            {"searchResults": unrelated + mentioning, "cursor": ""},
        ])
        fetched = asyncio.run(source.fetch(domain="", name="Nambiar Villas X"))
        self.assertEqual(len(fetched.creatives), scrapecreators.MENTION_ADS_CAP)
        self.assertEqual(fetched.logo_url, "")
        with self.subTest("no ad names the brand -> nothing ships"):
            source = self._source_with_pages([
                {"searchResults": unrelated, "cursor": ""},
            ])
            fetched = asyncio.run(source.fetch(domain="",
                                               name="Nambiar Villas X"))
            self.assertEqual(fetched.creatives, [])

    def test_mentions_brand_rows(self):
        # Landing urls (live 2026-09-20) and distinctive tokens (live 2026-09-21).
        cases = [
            ("brand in snapshot link_url", "Godrej Platinum",
             {"link_url": "https://godrej-platinum.in/offer"}, True),
            ("brand in extra_links", "Godrej Platinum",
             {"extra_links": ["https://fb.me/x", "https://godrejplatinum.in"]}, True),
            ("brand in a card link_url", "Godrej Platinum",
             {"cards": [{"title": "2BHK", "body": {"text": "Book now"},
                         "link_url": "https://www.godrej-platinum.in/book"}]}, True),
            ("unrelated link does not match", "Godrej Platinum",
             {"link_url": "https://prestige-lakeside.in"}, False),
            ("distinctive token in body", "Nambiar Villas",
             {"body": {"text": "Nambiar District 25 - book your site visit"}},
             True),
            ("distinctive token in link", "Nambiar Villas",
             {"link_url": "https://nambiar-district25.in"}, True),
            ("no distinctive token anywhere", "Nambiar Villas",
             {"body": {"text": "Luxury villas in South Bangalore"}}, False),
            ("all tokens required, one missing", "Godrej Platinum",
             {"body": {"text": "Godrej Woodland plots now open"}}, False),
            ("all-generic name never attributes", "Pre Launch Property",
             {"body": {"text": "Pre launch property offers!"}}, False),
            ("tokens scattered across pieces never attribute", "Godrej United",
             {"title": "Godrej & Boyce",
              "body": {"text": "United by design"}}, False),
            ("all tokens in one piece attributes", "Godrej United",
             {"body": {"text": "Godrej United - 3 BHK in Whitefield"}}, True),
        ]
        for label, name, snapshot_extra, expected in cases:
            with self.subTest(label):
                ad = _ad(page_name="Some Broker")
                ad["snapshot"]["body"] = {"text": "Great offers"}
                ad["snapshot"]["title"] = "New Launch"
                ad["snapshot"].update(snapshot_extra)
                self.assertIs(scrapecreators._mentions_brand(ad, name), expected)

    def test_carousel_expands_one_creative_per_card(self):
        # Rule 4: N cards -> N creatives, each with its OWN asset and link -
        # collapsing a carousel into one fileUrl is what produced mixed
        # imagery under one ad.
        ad = _ad()
        ad["snapshot"]["display_format"] = "CAROUSEL"
        ad["snapshot"]["images"] = []
        ad["snapshot"]["cards"] = [
            {"original_image_url": f"https://cdn/c{i}.jpg",
             "title": f"Card {i}", "link_url": f"https://x.com/c{i}",
             "body": {"text": f"card body {i}"}}
            for i in range(3)
        ]
        creatives = scrapecreators._to_creatives(ad)
        self.assertEqual([c.creative_id for c in creatives],
                         ["a1:0", "a1:1", "a1:2"])
        self.assertEqual([c.source_asset_url for c in creatives],
                         [f"https://cdn/c{i}.jpg" for i in range(3)])
        self.assertEqual([c.landing_url for c in creatives],
                         [f"https://x.com/c{i}" for i in range(3)])
        self.assertEqual(creatives[1].headline, "Card 1")
        with self.subTest("single-asset ad stays one creative, id unchanged"):
            creatives = scrapecreators._to_creatives(_ad())
            self.assertEqual([c.creative_id for c in creatives], ["a1"])

    def test_cursor_pagination_rows(self):
        # Each page is a metered credit: paging stops at the last cursor or PAGE_LIMIT.
        endless = [{"searchResults": [_ad()], "cursor": "next"}] * (
            scrapecreators.PAGE_LIMIT + 2)
        for label, pages, want_calls in [
            ("stops when the cursor runs out",
             [{"searchResults": [_ad()], "cursor": "next"},
              {"searchResults": [_ad()], "cursor": ""}], 2),
            ("stops at PAGE_LIMIT while the vendor keeps paging",
             list(endless), scrapecreators.PAGE_LIMIT),
        ]:
            with self.subTest(label):
                source = self._source_with_pages(pages)
                fetched = asyncio.run(source.fetch(domain="", name="Purva Sparkling Springs"))
                self.assertEqual(len(self.calls), want_calls)
                self.assertEqual([c["cursor"] for c in self.calls][:2], ["", "next"])
                self.assertEqual(len(fetched.creatives), want_calls)

    def test_missing_key_and_name_raise(self):
        with mock.patch.object(scrapecreators.settings, "SCRAPECREATORS_API_KEY", ""):
            with self.assertRaises(ScrapeCreatorsError):
                asyncio.run(ScrapeCreatorsSource().fetch(domain="", name="X"))
        with self.assertRaises(ScrapeCreatorsError):
            asyncio.run(ScrapeCreatorsSource().fetch(domain="d.com", name=""))


if __name__ == "__main__":
    unittest.main()
