"""Unit: _uploads._sniff_video_ctype - reject non-video bytes before rehosting.

The ad library serves a poster JPEG at some "video" URLs (poster-only ads);
saving those as .mp4 yields an unplayable file. Magic bytes beat the vendor's
content-type header, so an image is rejected even when the header lies.
"""
from __future__ import annotations

import unittest

from app.agents.adzump._uploads import _sniff_video_ctype

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


if __name__ == "__main__":
    unittest.main()
