"""Slice 2 — the judge-each capability of VisionJudge, below the model.

We never call the LLM: we hand-build the judge's JSON output (what the model
WOULD return) and assert the parse, and we assert the message builder turns N
images into N image blocks. Judgment quality (did it call #0 a logo?) is the
manual eval/smoke, never a unit test.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \
        tests.agents.adzump.agents.asset_picker.test_vision_judge -v
"""

from __future__ import annotations

import base64
import unittest

from app.agents.adzump.agents.asset_picker.agent import (
    _build_judge_message, _parse_judge,
)


class ParseJudgeTests(unittest.TestCase):
    """_parse_judge: judge-each final JSON → JudgeResult (empty on failure)."""

    def test_parses_one_verdict_per_image(self):
        text = (
            "```json\n"
            '{"verdicts": ['
            '{"idx":0,"role":"logo","relevant":true,"confidence":0.95,'
            '"needs_user":false,"question":"","reasoning":"wordmark"},'
            '{"idx":1,"role":"unknown","relevant":true,"confidence":0.4,'
            '"needs_user":true,"question":"floor plan or site map?","reasoning":"ambiguous"}'
            "]}\n```"
        )
        res = _parse_judge(text)
        self.assertEqual([v.idx for v in res.verdicts], [0, 1])
        self.assertEqual(res.verdicts[0].role, "logo")
        self.assertTrue(res.verdicts[0].relevant)
        # the ambiguous one flags the user instead of guessing
        self.assertTrue(res.verdicts[1].needs_user)
        self.assertEqual(res.verdicts[1].question, "floor plan or site map?")

    def test_defaults_applied_for_sparse_verdict(self):
        res = _parse_judge('{"verdicts": [{"idx": 0}]}')
        v = res.verdicts[0]
        self.assertEqual(v.role, "")
        self.assertTrue(v.relevant)        # default True
        self.assertEqual(v.confidence, 0.0)
        self.assertFalse(v.needs_user)     # default False

    def test_garbage_returns_empty(self):
        self.assertEqual(_parse_judge("the model said nothing useful").verdicts, [])


class BuildJudgeMessageTests(unittest.TestCase):
    """_build_judge_message: N image specs (bytes) → N image blocks, in order."""

    def test_one_block_per_image_in_order(self):
        images = [
            {"data": b"AAAA", "content_type": "image/png"},
            {"data": b"BBBB"},                                  # no content_type → default
        ]
        text, blocks = _build_judge_message(images, summary="a product")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["source"]["data"], base64.b64encode(b"AAAA").decode())
        self.assertEqual(blocks[0]["source"]["media_type"], "image/png")
        self.assertEqual(blocks[1]["source"]["data"], base64.b64encode(b"BBBB").decode())
        self.assertEqual(blocks[1]["source"]["media_type"], "image/jpeg")   # default
        self.assertIn("2 image(s)", text)

    def test_empty_images_empty_blocks(self):
        text, blocks = _build_judge_message([], summary="")
        self.assertEqual(blocks, [])
        self.assertIn("0 image(s)", text)


if __name__ == "__main__":
    unittest.main()
