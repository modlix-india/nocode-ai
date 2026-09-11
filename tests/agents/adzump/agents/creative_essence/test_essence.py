"""Unit: agents/creative_essence - the deterministic seams around the model.

Freezes the chain build-message -> [model] -> parse -> collect, plus extract()'s
orchestration (unique-by-hash, chunking, per-creative fallback, usage roll-up).
Below the model, no mocks: real functions, hand-built inputs. The one stub is
``_run_once`` - the model boundary itself.
"""
from __future__ import annotations

import asyncio
import io
import unittest

from app.agents.adzump.agents.creative_essence.agent import (
    EssenceAnalyst,
    MAX_CONCURRENT_CALLS,
    MAX_IMAGES_PER_CALL,
    _build_essence_message,
    _collect,
    _parse_batch,
    _shrink_for_vision,
)
from app.agents.adzump.agents.creative_essence.models import (
    CreativeImage,
    EssenceBatch,
    EssenceVerdict,
)
from app.agents.adzump.creative_intelligence.models import Creative

_VERDICT_JSON = (
    '{"verdicts": [{"idx": 0, "angle": "own a lakeside home",'
    ' "hook_type": "aspiration", "awareness_stage": "solution_aware",'
    ' "media_format": "static_image", "visual_style": "lifestyle"}]}'
)


def _png_bytes() -> bytes:
    """A real decodable 1x1 PNG - extract() drops bytes PIL can't verify."""
    from io import BytesIO
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    return buf.getvalue()


_PNG = _png_bytes()


def _ci(content_hash: str, media_type: str = "image", **creative_fields) -> CreativeImage:
    return CreativeImage(
        creative=Creative(creative_id=content_hash, media_type=media_type,
                          content_hash=content_hash, **creative_fields),
        data=_PNG if content_hash else b"",
    )


class DeterministicSeamTests(unittest.TestCase):
    def test_insight_line_rows(self):
        from app.agents.adzump.agents.creative_essence.agent import _insight_line
        from app.agents.adzump.creative_intelligence.models import Essence

        for label, essence, expected in [
            ("full verdict",
             Essence(hook_type="offer", hook_text="Pay 10% now",
                     media_format="static_image"),
             "offer · “Pay 10% now” · static image"),
            ("no hook text falls back to angle",
             Essence(hook_type="aspiration", angle="own a lakeside home",
                     media_format="static_image"),
             "aspiration · “own a lakeside home” · static image"),
            ("nothing labeled",
             Essence(hook_type="other", media_format="static_image"),
             "other · static image"),
        ]:
            with self.subTest(label):
                self.assertEqual(_insight_line(essence), expected)

    def test_build_parse_collect_shrink(self):
        for name, text, want_hook in [
            ("fenced", f"```json\n{_VERDICT_JSON}\n```", "aspiration"),
            ("bare object", _VERDICT_JSON, "aspiration"),
            ("prose around bare object", f"Here you go: {_VERDICT_JSON} done.",
             "aspiration"),
            ("empty text", "", None),
            ("garbage", "not json at all", None),
            # Off-list enum values coerce to the field default (live
            # 2026-09-08: one invented word failed a whole 12-image batch and
            # forced 12 sequential single-image calls) - the batch PARSES.
            ("wrong enum coerces",
             '{"verdicts": [{"idx": 0, "hook_type": "clickbait"}]}', "other"),
            # Same leniency for the classification enums: an invented category
            # coerces to "unknown" - which the gate then rejects fail-closed.
            ("wrong category coerces to unknown",
             '{"verdicts": [{"idx": 0, "category": "real_estate_stuff"}]}',
             "other"),
        ]:
            with self.subTest(parse=name):
                batch = _parse_batch(text)
                if want_hook is None:
                    self.assertIsNone(batch)
                else:
                    self.assertEqual(len(batch.verdicts), 1)
                    self.assertEqual(batch.verdicts[0].hook_type, want_hook)
        with self.subTest("collect maps idx -> content_hash, drops out-of-range, None is a noop"):
            chunk = [_ci("aaa"), _ci("bbb")]
            essences: dict = {}
            _collect(EssenceBatch(verdicts=[
                EssenceVerdict(idx=1, angle="second image"),
                EssenceVerdict(idx=7, angle="out of range"),
            ]), chunk, essences)
            self.assertEqual(list(essences), ["bbb"])
            _collect(None, chunk, essences)
            self.assertEqual(list(essences), ["bbb"])
        with self.subTest("verdict -> Essence strips idx and dumps camelCase for the store"):
            essence = EssenceVerdict(idx=3, angle="a", hook_type="urgency").to_essence()
            self.assertEqual((essence.angle, essence.hook_type), ("a", "urgency"))
            self.assertNotIn("idx", essence.model_dump())
            self.assertEqual(essence.model_dump(by_alias=True)["hookType"], "urgency")
        with self.subTest("classification fields ride the verdict into the store shape"):
            verdict = _parse_batch(
                '{"verdicts": [{"idx": 0, "category": "residential_villa",'
                ' "market": "Bangalore / Whitefield", "offering_stage": "pre_launch",'
                ' "advertised_project": "Lakeside Villas",'
                ' "advertiser_role": "broker", "category_confidence": 0.93,'
                ' "category_evidence": "OCR: LAKESIDE VILLAS",'
                ' "category_method": "combined"}]}').verdicts[0]
            stored = verdict.to_essence().model_dump(by_alias=True)
            self.assertEqual(
                (stored["category"], stored["advertiserRole"],
                 stored["categoryConfidence"], stored["offeringStage"]),
                ("residential_villa", "broker", 0.93, "pre_launch"))
        with self.subTest("message: one block per image, in order, with copy metadata"):
            text, blocks = _build_essence_message([
                _ci("aaa", headline="Lakeside villas", cta="Book now",
                    landing_url="https://rival.in/villas"),
                _ci("bbb", media_type="video"),
            ], competitor_name="Prestige Tranquility")
            self.assertEqual(len(blocks), 2)
            for expected in ("[Image 0]", "'Lakeside villas'", "'Book now'",
                             "'https://rival.in/villas'", "[Image 1]",
                             "video ad - this is its poster still",
                             "(no ad copy captured)",
                             "'Prestige Tranquility'",  # advertiser_role reference
                             "never as a category signal"):
                self.assertIn(expected, text)
            for block in blocks:
                self.assertEqual((block["type"], block["source"]["type"]), ("image", "base64"))
        with self.subTest("shrink: undecodable/small pass through, big shrinks to jpeg"):
            from PIL import Image

            def png(size, mode="RGB", color="red"):
                buf = io.BytesIO()
                Image.new(mode, size, color).save(buf, format="PNG")
                return buf.getvalue()

            self.assertEqual(_shrink_for_vision(b"not an image", "image/png"),
                             (b"not an image", "image/png"))
            small = png((64, 64))
            self.assertEqual(_shrink_for_vision(small, "image/png"), (small, "image/png"))
            shrunk, content_type = _shrink_for_vision(png((2400, 1200)), "image/png")
            self.assertEqual(content_type, "image/jpeg")
            self.assertLessEqual(max(Image.open(io.BytesIO(shrunk)).size), 1024)
            # transparency composites onto white, not black
            shrunk, _ = _shrink_for_vision(png((2000, 2000), "RGBA", (255, 0, 0, 0)), "image/png")
            self.assertGreaterEqual(min(Image.open(io.BytesIO(shrunk)).getpixel((0, 0))), 250)


class _StubbedAnalyst(EssenceAnalyst):
    """Real extract(), stubbed model boundary: _run_once pops queued batches."""

    def __init__(self, batches):
        self.name = "creative_essence"  # skip BaseAgent init - no LLM plumbing
        self.batches = list(batches)
        self.calls: list[list[str]] = []

    async def _run_once(self, chunk, stream, auth, parent_session_context, competitor_name=""):
        self.calls.append([ci.creative.content_hash for ci in chunk])
        return self.batches.pop(0), 100, 10


class _ParentStream:
    is_cancelled = False

    def __init__(self):
        self.finished: list[dict] = []

    async def emit_agent_finished(self, **kw):
        self.finished.append(kw)


class ExtractTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_orchestration(self):
        with self.subTest("dedups by hash, skips unhashed, empty input makes no call"):
            agent = _StubbedAnalyst([EssenceBatch(verdicts=[EssenceVerdict(idx=0, angle="x")])])
            essences = await agent.extract([_ci("aaa"), _ci("aaa"), _ci("")],
                                           _ParentStream(), auth=None)
            self.assertEqual(agent.calls, [["aaa"]])
            self.assertEqual(list(essences), ["aaa"])
            idle = _StubbedAnalyst([])
            self.assertEqual(await idle.extract([], _ParentStream(), auth=None), {})
            self.assertEqual(idle.calls, [])
        with self.subTest("unparseable batch falls back per-creative; absent stays absent"):
            agent = _StubbedAnalyst([
                None,  # the 2-image batch fails to parse
                EssenceBatch(verdicts=[EssenceVerdict(idx=0, angle="solo a")]),
                None,  # second single fails too - absent from result, not invented
            ])
            essences = await agent.extract([_ci("aaa"), _ci("bbb")], _ParentStream(), auth=None)
            self.assertEqual(agent.calls, [["aaa", "bbb"], ["aaa"], ["bbb"]])
            self.assertEqual(list(essences), ["aaa"])
        with self.subTest("chunks past the per-call cap"):
            agent = _StubbedAnalyst([EssenceBatch(), EssenceBatch(verdicts=[])])
            await agent.extract([_ci(f"h{i}") for i in range(MAX_IMAGES_PER_CALL + 1)],
                                _ParentStream(), auth=None)
            self.assertEqual([len(c) for c in agent.calls], [MAX_IMAGES_PER_CALL, 1])
        with self.subTest("chunks run concurrently, bounded by MAX_CONCURRENT_CALLS"):
            class _Probe(EssenceAnalyst):
                def __init__(self):
                    self.name = "creative_essence"
                    self.in_flight = 0
                    self.max_in_flight = 0

                async def _run_once(self, chunk, stream, auth, parent_session_context, competitor_name=""):
                    self.in_flight += 1
                    self.max_in_flight = max(self.max_in_flight, self.in_flight)
                    await asyncio.sleep(0.01)
                    self.in_flight -= 1
                    return EssenceBatch(verdicts=[]), 0, 0

            probe = _Probe()
            await probe.extract([_ci(f"h{i}") for i in range(MAX_IMAGES_PER_CALL * 4)],
                                _ParentStream(), auth=None)
            self.assertGreater(probe.max_in_flight, 1)
            self.assertLessEqual(probe.max_in_flight, MAX_CONCURRENT_CALLS)
        with self.subTest("agent_finished carries summed usage"):
            parent = _ParentStream()
            agent = _StubbedAnalyst([
                None,
                EssenceBatch(verdicts=[EssenceVerdict(idx=0)]),
                EssenceBatch(verdicts=[EssenceVerdict(idx=0)]),
            ])
            await agent.extract([_ci("aaa"), _ci("bbb")], parent, auth=None)
            self.assertEqual(len(parent.finished), 1)
            finished = parent.finished[0]
            self.assertEqual(finished["agent_id"], "creative_essence")
            self.assertEqual(finished["tokens_in"], 300)  # 3 stubbed calls x 100
            self.assertEqual(finished["summary"], "essence for 2/2 creatives")


if __name__ == "__main__":
    unittest.main()
