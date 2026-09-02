"""Regression: a parallel tool_use batch must survive stream assembly.

Discovery (2026-09-02): a 13-conversation bench run recorded 147 tool calls
across 175 LLM turns with **no batch ever wider than one call**. The audit of
the Chit Fund build had put this down to model behaviour ("no prompt sentence
tells it to batch") and confirmed only the two ends of the pipe: the provider
buffers multiple `tool_calls` by index, and dispatch runs them through
`asyncio.gather`. The layer between them was the problem.

`scripts/probe_parallel_tool_calls.py` settles the model's half: DeepSeek
returns 2 parallel calls for two toy tools and 3 for the AppBuilder's real
171-tool payload. It was never declining to batch.

The assembly loop held ONE `current_tool` slot. The OpenAI-compatible
chat-completions path emits every `tool_use_start` up front, then the arguments
and ends keyed by id, so with a 3-call batch the second start overwrote the
first, the third overwrote the second, the first call's arguments landed on the
third call's block, and the two remaining ends found an empty slot and dropped
their calls. One call per turn, every turn, silently.
"""

from __future__ import annotations

import json

from app.core.agent import _ToolBlockAssembler


def _drain(asm, chunks):
    """Feed (kind, id, payload) tuples; return the blocks that closed."""
    out = []
    for kind, tid, payload in chunks:
        if kind == "start":
            asm.start(tid, payload)
        elif kind == "delta":
            asm.delta(tid, payload)
        elif kind == "end":
            blk = asm.end(tid)
            if blk is not None:
                out.append(blk)
    return out


# ── the DeepSeek / OpenAI-compatible ordering: all starts, then deltas+ends ──


def test_deepseek_batch_of_three_keeps_all_three_with_the_right_arguments():
    asm = _ToolBlockAssembler()
    blocks = _drain(asm, [
        ("start", "call_a", "list_themes"),
        ("start", "call_b", "list_pages"),
        ("start", "call_c", "get_app"),
        ("delta", "call_a", json.dumps({"app_code": "themes_app"})),
        ("end",   "call_a", None),
        ("delta", "call_b", json.dumps({"app_code": "pages_app"})),
        ("end",   "call_b", None),
        ("delta", "call_c", json.dumps({"app_code": "app_app"})),
        ("end",   "call_c", None),
    ])
    assert [b["name"] for b in blocks] == ["list_themes", "list_pages", "get_app"]
    # Arguments must follow their OWN call, not whichever slot was last open.
    assert blocks[0]["input"] == {"app_code": "themes_app"}
    assert blocks[1]["input"] == {"app_code": "pages_app"}
    assert blocks[2]["input"] == {"app_code": "app_app"}
    assert not asm  # nothing left open


def test_arguments_may_arrive_fragmented_and_interleaved_across_calls():
    asm = _ToolBlockAssembler()
    blocks = _drain(asm, [
        ("start", "a", "first"),
        ("start", "b", "second"),
        ("delta", "a", '{"x":'),
        ("delta", "b", '{"y":'),
        ("delta", "a", ' 1}'),
        ("delta", "b", ' 2}'),
        ("end",   "a", None),
        ("end",   "b", None),
    ])
    assert blocks[0]["input"] == {"x": 1}
    assert blocks[1]["input"] == {"y": 2}


# ── the Anthropic / fallback ordering: one at a time, deltas carry NO id ─────


def test_sequential_provider_with_idless_deltas_still_works():
    asm = _ToolBlockAssembler()
    blocks = _drain(asm, [
        ("start", "t1", "get_page"),
        ("delta", "", '{"page_name": "login"}'),   # Anthropic sends no id
        ("end",   "t1", None),
        ("start", "t2", "get_theme"),
        ("delta", "", '{"name": "dark"}'),
        ("end",   "t2", None),
    ])
    assert [b["name"] for b in blocks] == ["get_page", "get_theme"]
    assert blocks[0]["input"] == {"page_name": "login"}
    assert blocks[1]["input"] == {"name": "dark"}


def test_idless_end_closes_the_open_block():
    asm = _ToolBlockAssembler()
    blocks = _drain(asm, [
        ("start", "t1", "only_tool"),
        ("delta", "", '{"a": 1}'),
        ("end",   "", None),
    ])
    assert len(blocks) == 1
    assert blocks[0]["input"] == {"a": 1}


# ── degenerate provider behaviour must not collapse distinct calls ───────────


def test_missing_ids_do_not_merge_two_calls_into_one():
    asm = _ToolBlockAssembler()
    blocks = _drain(asm, [
        ("start", "", "alpha"),
        ("start", "", "beta"),
        ("end",   "", None),
        ("end",   "", None),
    ])
    assert [b["name"] for b in blocks] == ["beta", "alpha"]  # LIFO on id-less ends
    assert len(blocks) == 2


def test_repeated_id_across_a_batch_does_not_lose_a_call():
    asm = _ToolBlockAssembler()
    blocks = _drain(asm, [
        ("start", "dup", "alpha"),
        ("start", "dup", "beta"),
        ("end",   "dup", None),
        ("end",   "dup", None),
    ])
    assert len(blocks) == 2
    assert {b["name"] for b in blocks} == {"alpha", "beta"}


def test_malformed_argument_json_yields_empty_input_not_a_crash():
    asm = _ToolBlockAssembler()
    blocks = _drain(asm, [
        ("start", "t", "tool"),
        ("delta", "t", "{not json"),
        ("end",   "t", None),
    ])
    assert blocks[0]["input"] == {}


def test_a_call_with_no_arguments_still_closes():
    asm = _ToolBlockAssembler()
    blocks = _drain(asm, [("start", "t", "which_environment"), ("end", "t", None)])
    assert blocks[0]["input"] == {}


def test_end_without_a_start_is_ignored():
    asm = _ToolBlockAssembler()
    assert asm.end("nope") is None


def test_reset_drops_open_blocks():
    asm = _ToolBlockAssembler()
    asm.start("t", "tool")
    assert asm
    asm.reset()
    assert not asm
    assert asm.end("t") is None
