"""scrapecreators.com adapter - raw ad mapping, advertiser selection, search policy."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.creative_intelligence.sources import scrapecreators
from app.agents.adzump.creative_intelligence.sources.scrapecreators import (
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

    def test_video_ad_uses_video_and_poster(self):
        ad = _ad()
        ad["snapshot"]["display_format"] = "VIDEO"
        ad["snapshot"]["videos"] = [{
            "video_hd_url": "https://cdn/v.mp4",
            "video_preview_image_url": "https://cdn/v_poster.jpg",
        }]
        creative = _to_creative(ad)
        self.assertEqual(creative.media_type, "video")
        self.assertEqual(creative.source_asset_url, "https://cdn/v.mp4")
        self.assertEqual(creative.poster_source_url, "https://cdn/v_poster.jpg")

    def test_multi_images_is_carousel(self):
        ad = _ad()
        ad["snapshot"]["display_format"] = "MULTI_IMAGES"
        self.assertEqual(_to_creative(ad).media_type, "carousel")

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

    def test_video_in_card_maps_as_video(self):
        ad = _ad()
        ad["snapshot"].update({
            "display_format": "VIDEO", "images": [], "videos": [],
            "cards": [{"video_hd_url": "https://cdn/card_v.mp4",
                       "video_preview_image_url": "https://cdn/card_v_poster.jpg"}],
        })
        creative = _to_creative(ad)
        self.assertEqual(creative.source_asset_url, "https://cdn/card_v.mp4")
        self.assertEqual(creative.poster_source_url, "https://cdn/card_v_poster.jpg")

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

        async def fake_page(*, name, country, search_type, cursor):
            self.calls.append({"search_type": search_type, "country": country,
                               "cursor": cursor})
            return pages.pop(0)

        source._search_page = fake_page
        return source

    def test_exact_phrase_first_then_unordered_retry(self):
        source = self._source_with_pages([
            {"searchResults": [], "cursor": ""},
            {"searchResults": [_ad()], "cursor": ""},
        ])
        fetched = asyncio.run(source.fetch(
            domain="", name="Purva Sparkling Springs", country="IN"))
        self.assertEqual([c["search_type"] for c in self.calls],
                         ["keyword_exact_phrase", "keyword_unordered"])
        self.assertEqual(self.calls[0]["country"], "IN")
        self.assertEqual(len(fetched.creatives), 1)
        self.assertEqual(fetched.resolved_name, "Purva Sparkling Springs")
        self.assertEqual(fetched.logo_url, "https://cdn/logo.jpg")
        self.assertEqual(fetched.platform_ids, {"page_id": "p1"})

    def test_cursor_pagination_stops_without_cursor(self):
        source = self._source_with_pages([
            {"searchResults": [_ad()], "cursor": "next"},
            {"searchResults": [_ad()], "cursor": ""},
        ])
        fetched = asyncio.run(source.fetch(domain="", name="Purva Sparkling Springs"))
        self.assertEqual([c["cursor"] for c in self.calls], ["", "next"])
        self.assertEqual(len(fetched.creatives), 2)

    def test_missing_key_and_name_raise(self):
        with mock.patch.object(scrapecreators.settings, "SCRAPECREATORS_API_KEY", ""):
            with self.assertRaises(ScrapeCreatorsError):
                asyncio.run(ScrapeCreatorsSource().fetch(domain="", name="X"))
        with self.assertRaises(ScrapeCreatorsError):
            asyncio.run(ScrapeCreatorsSource().fetch(domain="d.com", name=""))


if __name__ == "__main__":
    unittest.main()
