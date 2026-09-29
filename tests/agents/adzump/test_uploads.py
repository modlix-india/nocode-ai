"""Unit: _uploads - what counts as a real image or video before rehosting.

The ad library serves a poster JPEG at some "video" URLs (poster-only ads);
saving those as .mp4 yields an unplayable file. Magic bytes beat the vendor's
content-type header, so an image is rejected even when the header lies. Some
CDNs serve image bytes with no Content-Type; the URL's extension then decides.
"""
from __future__ import annotations

import unittest

from app.agents.adzump._uploads import _sniff_video_ctype, looks_like_image_response

_MP4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00moov"
_WEBM = b"\x1aE\xdf\xa3\x01\x00\x00\x00rest-of-ebml"
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIFmore"
_PNG = b"\x89PNG\r\n\x1a\nIHDR"
_GIF = b"GIF89a\x01\x00"


class SniffVideoCtypeTests(unittest.TestCase):
    def test_only_real_video_bytes_survive(self):
        for name, data, header, expected in [
            ("mp4 ftyp box -> video/mp4", _MP4, "video/mp4", "video/mp4"),
            ("webm EBML header -> video/webm", _WEBM, "application/octet-stream", "video/webm"),
            ("mp4 bytes even under a wrong header", _MP4, "application/octet-stream", "video/mp4"),
            # The actual bug: a poster JPEG mislabeled as video is rejected.
            ("jpeg under a video/mp4 header -> reject", _JPEG, "video/mp4", None),
            ("png -> reject", _PNG, "image/png", None),
            ("gif -> reject", _GIF, "video/mp4", None),
            # No detectable magic: trust a video/* header, distrust anything else.
            ("no magic + video header -> trust header", b"random-bytes-no-magic", "video/mp4", "video/mp4"),
            ("no magic + non-video header -> reject", b"random-bytes-no-magic", "text/html", None),
        ]:
            with self.subTest(case=name):
                self.assertEqual(_sniff_video_ctype(data, header), expected)


class LooksLikeImageResponseTests(unittest.TestCase):
    def test_rows(self):
        for label, ctype, url, expected in [
            ("an image content-type", "image/jpeg", "https://x/y.jpg", True),
            ("with parameters", "image/webp; charset=binary", "https://x/y.webp", True),
            ("any case", "IMAGE/PNG", "https://x/y.png", True),
            # HTTP 200 with no Content-Type: the URL's extension decides
            ("no type, .jpg", "", "https://cdn.x/A01-8K-Entrance-copy.jpg", True),
            ("no type, encoded name", "", "https://cdn.x/Banner-Image%201.jpg", True),
            ("no type (None), .webp", None, "https://cdn.x/photo.webp", True),
            ("no type, .PNG with a query", "", "https://cdn.x/p.PNG?cache=1", True),
            # an explicit non-image type wins over a misleading extension
            ("html under .jpg", "text/html", "https://x/y.jpg", False),
            ("json under .png", "application/json", "https://x/y.png", False),
            ("no type, no image extension", "", "https://x/some-page", False),
            ("no type, a tracking url", "", "https://x/track?id=1", False),
        ]:
            with self.subTest(label):
                self.assertEqual(looks_like_image_response(ctype, url), expected)


if __name__ == "__main__":
    unittest.main()
