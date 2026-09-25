"""Unit: creative_intelligence/verify.py - served-URL asset verification.

The acceptance injections (spec 2026-09-10): 404, html-with-200, 0-byte,
1x1 placeholder, undecodable bytes -> never pass; a real image passes with
width/height/aspectRatio stamped; a video keeps its creative but drops an
unverifiable poster. All through a mock transport - no network.
"""
from __future__ import annotations

import asyncio
import io
import unittest
from unittest import mock

import httpx

from app.agents.adzump.creative_intelligence import verify
from app.agents.adzump.creative_intelligence.models import Creative


def _png(size=(10, 8), pad_to=3000) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format="PNG")
    data = buf.getvalue()
    # PNG readers ignore trailing bytes - pad tiny fixtures past the size gate
    # so the DIMENSION checks (not size) are what the test exercises.
    return data + b"\0" * max(0, pad_to - len(data))


def _mp4(pad_to=3000) -> bytes:
    data = b"\x00\x00\x00\x18ftypmp42" + b"\0" * 16
    return data + b"\0" * max(0, pad_to - len(data))


_RealAsyncClient = httpx.AsyncClient  # bound BEFORE patching (the patch is global)


def _client_patch(handler):
    def make_client(**kw):
        kw.pop("transport", None)
        return _RealAsyncClient(transport=httpx.MockTransport(handler), **kw)

    return mock.patch.object(verify.httpx, "AsyncClient", make_client)


def _transport(status: int, body: bytes, ctype: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body,
                              headers={"content-type": ctype})

    return _client_patch(handler)


def _verify(path="/api/files/static/file/X/competitor-creatives/a.png",
            expect="image", *, status=200, body=b"", ctype="image/png"):
    with _transport(status, body, ctype):
        return asyncio.run(verify.verify_served_asset(path, expect=expect))


class VerifyServedAssetTests(unittest.TestCase):
    def test_rejection_rows(self):
        for label, kwargs, want_reason in [
            ("404", dict(status=404, body=_png()), verify.FETCH_FAILED),
            ("200 with an html error page",
             dict(body=b"<html>not found</html>" + b"\0" * 3000,
                  ctype="text/html"), verify.BAD_CONTENT_TYPE),
            ("json body", dict(body=b"{}" + b"\0" * 3000,
                               ctype="application/json"),
             verify.BAD_CONTENT_TYPE),
            ("0-byte", dict(body=b""), verify.TOO_SMALL),
            ("under the size floor", dict(body=b"x" * 100), verify.TOO_SMALL),
            ("1x1 placeholder pixel", dict(body=_png(size=(1, 1))),
             verify.TOO_SMALL),
            ("image bytes that don't decode",
             dict(body=b"\xff\xd8\xffgarbage" + b"\0" * 3000),
             verify.UNDECODABLE),
            ("empty path", dict(body=_png()), verify.EMPTY_FILE_URL),
        ]:
            with self.subTest(label):
                path = "" if label == "empty path" else \
                    "/api/files/static/file/X/competitor-creatives/a.png"
                media = _verify(path, **kwargs)
                self.assertFalse(media.ok)
                self.assertEqual(media.reason, want_reason)

    def test_valid_image_stamps_dimensions(self):
        media = _verify(body=_png(size=(1080, 1920)))
        self.assertTrue(media.ok)
        self.assertEqual((media.width, media.height), (1080, 1920))

    def test_video_container_gate(self):
        with self.subTest("mp4 magic passes (ffprobe absent -> sniff gates)"):
            with mock.patch.object(verify, "_ffprobe_duration",
                                   new=mock.AsyncMock(return_value=None)):
                media = _verify(expect="video", body=_mp4(),
                                ctype="video/mp4")
            self.assertTrue(media.ok)
        with self.subTest("image bytes at a video url fail"):
            media = _verify(expect="video", body=_png(), ctype="video/mp4")
            self.assertFalse(media.ok)
            self.assertEqual(media.reason, verify.UNDECODABLE)
        with self.subTest("ffprobe zero duration fails"):
            with mock.patch.object(verify, "_ffprobe_duration",
                                   new=mock.AsyncMock(return_value=0.0)):
                media = _verify(expect="video", body=_mp4(),
                                ctype="video/mp4")
            self.assertFalse(media.ok)


class VerifyCreativeTests(unittest.TestCase):
    def test_empty_file_url_is_dropped_before_any_fetch(self):
        keep, reason = asyncio.run(
            verify.verify_creative(Creative(creative_id="a", file_url="  ")))
        self.assertFalse(keep)
        self.assertEqual(reason, verify.EMPTY_FILE_URL)

    def test_verified_image_stamps_rule6_facts(self):
        c = Creative(creative_id="a", media_type="image",
                     file_url="/api/files/static/file/X/y/a.png")
        with _transport(200, _png(size=(1000, 500)), "image/png"):
            keep, reason = asyncio.run(verify.verify_creative(c))
        self.assertTrue(keep)
        self.assertEqual((c.width, c.height, c.aspect_ratio), (1000, 500, 2.0))
        self.assertTrue(c.verified_at)

    def test_video_with_bad_poster_keeps_creative_drops_poster(self):
        c = Creative(creative_id="v", media_type="video",
                     file_url="/api/files/static/file/X/y/v.mp4",
                     poster_url="/api/files/static/file/X/y/p.jpg")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(".mp4"):
                return httpx.Response(200, content=_mp4(),
                                      headers={"content-type": "video/mp4"})
            return httpx.Response(404, content=b"")

        with _client_patch(handler), \
             mock.patch.object(verify, "_ffprobe_duration",
                               new=mock.AsyncMock(return_value=12.5)):
            keep, _ = asyncio.run(verify.verify_creative(c))
        self.assertTrue(keep)
        self.assertEqual(c.poster_url, "")
        self.assertEqual(c.duration_seconds, 12.5)


if __name__ == "__main__":
    unittest.main()
