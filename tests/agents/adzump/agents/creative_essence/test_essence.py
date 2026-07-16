"""Unit: agents/creative_essence - the deterministic seams around the model.

Freezes the chain build-message -> [model] -> parse -> collect, plus extract()'s
orchestration (unique-by-hash, chunking, per-creative fallback, usage roll-up).
Below the model, no mocks: real functions, hand-built inputs. The one stub is
``_run_once`` - the model boundary itself.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \
        tests.agents.adzump.agents.creative_essence.test_essence -v
"""

from __future__ import annotations

import io
import unittest

from app.agents.adzump.agents.creative_essence.agent import (
    EssenceAnalyst,
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


def _ci(content_hash: str, media_type: str = "image", **creative_fields) -> CreativeImage:
    return CreativeImage(
        creative=Creative(creative_id=content_hash, media_type=media_type,
                          content_hash=content_hash, **creative_fields),
        data=b"PIXELS-" + content_hash.encode(),
    )


class ParseBatchTests(unittest.TestCase):
    def test_parse_variants(self):
        cases = [
            ("fenced", f"```json\n{_VERDICT_JSON}\n```", 1),
            ("bare object", _VERDICT_JSON, 1),
            ("prose around bare object", f"Here you go: {_VERDICT_JSON} done.", 1),
            ("empty text", "", None),
            ("garbage", "not json at all", None),
            ("wrong enum", '{"verdicts": [{"idx": 0, "hook_type": "clickbait"}]}', None),
        ]
        for name, text, want in cases:
            with self.subTest(case=name):
                batch = _parse_batch(text)
                if want is None:
                    self.assertIsNone(batch)
                else:
                    self.assertEqual(len(batch.verdicts), want)

    def test_parsed_enums_reach_the_typed_verdict(self):
        batch = _parse_batch(f"```json\n{_VERDICT_JSON}\n```")
        self.assertEqual(batch.verdicts[0].hook_type, "aspiration")
        self.assertEqual(batch.verdicts[0].awareness_stage, "solution_aware")


class CollectTests(unittest.TestCase):
    def test_maps_idx_to_content_hash_and_drops_oob(self):
        chunk = [_ci("aaa"), _ci("bbb")]
        batch = EssenceBatch(verdicts=[
            EssenceVerdict(idx=1, angle="second image"),
            EssenceVerdict(idx=7, angle="out of range"),
        ])
        essences: dict = {}
        _collect(batch, chunk, essences)
        self.assertEqual(list(essences), ["bbb"])
        self.assertEqual(essences["bbb"].angle, "second image")

    def test_none_batch_is_a_noop(self):
        essences: dict = {}
        _collect(None, [_ci("aaa")], essences)
        self.assertEqual(essences, {})


class VerdictTests(unittest.TestCase):
    def test_to_essence_strips_idx_and_keeps_fields(self):
        essence = EssenceVerdict(idx=3, angle="a", hook_type="urgency").to_essence()
        self.assertEqual((essence.angle, essence.hook_type), ("a", "urgency"))
        self.assertNotIn("idx", essence.model_dump())

    def test_essence_dumps_camelcase_for_the_store(self):
        d = EssenceVerdict(idx=0, hook_type="urgency").to_essence().model_dump(by_alias=True)
        self.assertEqual(d["hookType"], "urgency")


class BuildMessageTests(unittest.TestCase):
    def test_one_block_per_image_in_order_with_copy_metadata(self):
        chunk = [
            _ci("aaa", headline="Lakeside villas", cta="Book now"),
            _ci("bbb", media_type="video"),
        ]
        text, blocks = _build_essence_message(chunk)
        self.assertEqual(len(blocks), 2)
        self.assertIn("[Image 0]", text)
        self.assertIn("'Lakeside villas'", text)
        self.assertIn("'Book now'", text)
        self.assertIn("[Image 1]", text)
        self.assertIn("video ad - this is its poster still", text)
        self.assertIn("(no ad copy captured)", text)
        for block in blocks:
            self.assertEqual(block["type"], "image")
            self.assertEqual(block["source"]["type"], "base64")


class ShrinkTests(unittest.TestCase):
    def test_undecodable_bytes_pass_through(self):
        self.assertEqual(_shrink_for_vision(b"not an image", "image/png"),
                         (b"not an image", "image/png"))

    def test_small_image_passes_through_big_image_shrinks(self):
        from PIL import Image

        def png(size):
            buf = io.BytesIO()
            Image.new("RGB", size, "red").save(buf, format="PNG")
            return buf.getvalue()

        small = png((64, 64))
        self.assertEqual(_shrink_for_vision(small, "image/png"), (small, "image/png"))

        shrunk, content_type = _shrink_for_vision(png((2400, 1200)), "image/png")
        self.assertEqual(content_type, "image/jpeg")
        self.assertLessEqual(max(Image.open(io.BytesIO(shrunk)).size), 1024)

    def test_transparent_pixels_composite_onto_white_not_black(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (2000, 2000), (255, 0, 0, 0)).save(buf, format="PNG")
        shrunk, _ = _shrink_for_vision(buf.getvalue(), "image/png")
        pixel = Image.open(io.BytesIO(shrunk)).getpixel((0, 0))
        self.assertGreaterEqual(min(pixel), 250)  # white-ish, JPEG tolerance


class _StubbedAnalyst(EssenceAnalyst):
    """Real extract(), stubbed model boundary: _run_once pops queued batches."""

    def __init__(self, batches):
        self.name = "creative_essence"  # skip BaseAgent init - no LLM plumbing
        self.batches = list(batches)
        self.calls: list[list[str]] = []

    async def _run_once(self, chunk, stream, auth, parent_session_context):
        self.calls.append([ci.creative.content_hash for ci in chunk])
        return self.batches.pop(0), 100, 10


class _ParentStream:
    is_cancelled = False

    def __init__(self):
        self.finished: list[dict] = []

    async def emit_agent_finished(self, **kw):
        self.finished.append(kw)


class ExtractTests(unittest.IsolatedAsyncioTestCase):
    async def test_dedups_by_hash_and_skips_unhashed(self):
        agent = _StubbedAnalyst([EssenceBatch(verdicts=[EssenceVerdict(idx=0, angle="x")])])
        images = [_ci("aaa"), _ci("aaa"), _ci("")]  # dup hash + hashless
        essences = await agent.extract(images, _ParentStream(), auth=None)
        self.assertEqual(agent.calls, [["aaa"]])
        self.assertEqual(list(essences), ["aaa"])

    async def test_empty_input_returns_empty_without_a_call(self):
        agent = _StubbedAnalyst([])
        self.assertEqual(await agent.extract([], _ParentStream(), auth=None), {})
        self.assertEqual(agent.calls, [])

    async def test_unparseable_batch_falls_back_per_creative(self):
        agent = _StubbedAnalyst([
            None,  # the 2-image batch fails to parse
            EssenceBatch(verdicts=[EssenceVerdict(idx=0, angle="solo a")]),
            None,  # second single fails too - absent from result, not invented
        ])
        essences = await agent.extract([_ci("aaa"), _ci("bbb")], _ParentStream(), auth=None)
        self.assertEqual(agent.calls, [["aaa", "bbb"], ["aaa"], ["bbb"]])
        self.assertEqual(list(essences), ["aaa"])
        self.assertEqual(essences["aaa"].angle, "solo a")

    async def test_chunks_past_the_per_call_cap(self):
        n = MAX_IMAGES_PER_CALL + 1
        agent = _StubbedAnalyst([EssenceBatch(), EssenceBatch(verdicts=[])])
        await agent.extract([_ci(f"h{i}") for i in range(n)], _ParentStream(), auth=None)
        self.assertEqual([len(c) for c in agent.calls], [MAX_IMAGES_PER_CALL, 1])

    async def test_agent_finished_carries_summed_usage(self):
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
