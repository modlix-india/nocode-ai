"""DeepSeek context-cache accounting.

DeepSeek reports cache usage as `prompt_cache_hit_tokens` /
`prompt_cache_miss_tokens`, where **`prompt_tokens == hit + miss`** — unlike
Anthropic, whose `input_tokens` EXCLUDES cached reads. The provider originally
hardcoded `cache_read_input_tokens: 0` (correct when DeepSeek had no caching,
wrong once it shipped), which made every cached token invisible: a run reading
1.46M tokens showed no indication that 1.35M of them were cheap cache hits.

Mapping `input_tokens = miss` and `cache_read_input_tokens = hit` restores the
Anthropic contract, so `input + cache_read` is the true context size on every
provider and nothing double-counts. These tests pin that mapping, the
fallbacks for endpoints that do not report cache fields, and — most
importantly — that the split does not change what anyone is billed.
"""

from __future__ import annotations

import pytest

from app.core.session import BaseSession
from app.services.billing import weighted_tokens
from app.services.llm_provider import _openai_compatible_usage

MODEL = "deepseek-v4-flash-vision-exp"


class FakeUsage:
    """Stands in for an OpenAI-compatible usage object. Cache attributes are
    only set when given, so absent-field behaviour is testable."""

    def __init__(self, prompt: int, completion: int, hit: int | None = None, miss: int | None = None):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        if hit is not None:
            self.prompt_cache_hit_tokens = hit
        if miss is not None:
            self.prompt_cache_miss_tokens = miss


def test_cache_hit_and_miss_map_to_the_anthropic_contract() -> None:
    """Real numbers from a live warm call: 3584 of 3697 prompt tokens cached."""
    usage = _openai_compatible_usage(FakeUsage(3697, 12, hit=3584, miss=113))

    assert usage["input_tokens"] == 113, "input is the UNCACHED portion"
    assert usage["cache_read_input_tokens"] == 3584
    assert usage["output_tokens"] == 12
    assert usage["cache_creation_input_tokens"] == 0, "DeepSeek's cache is implicit"


def test_input_plus_cache_read_reconstructs_prompt_tokens() -> None:
    """The invariant everything downstream leans on. If this breaks, context
    occupancy is either short by the cached tokens or double-counts them."""
    usage = _openai_compatible_usage(FakeUsage(3697, 12, hit=3584, miss=113))
    assert usage["input_tokens"] + usage["cache_read_input_tokens"] == 3697


def test_billing_total_is_unchanged_by_the_split() -> None:
    """The whole point of splitting rather than adding: `weighted_tokens` sums
    all four keys, and miss + hit == prompt_tokens, so no customer's charge
    moves. A regression here would silently re-price every DeepSeek call.
    """
    old_mapping = {  # what the provider emitted before caching was wired up
        "input_tokens": 3697,
        "output_tokens": 12,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    new_mapping = _openai_compatible_usage(FakeUsage(3697, 12, hit=3584, miss=113))

    assert weighted_tokens(new_mapping, MODEL) == weighted_tokens(old_mapping, MODEL)


def test_endpoint_without_cache_fields_keeps_its_full_count() -> None:
    """MiniMax and other OpenAI-compatible endpoints reached through
    DeepSeekProvider may not report cache fields at all. Reading a missing
    field as zero would set input_tokens to 0 and lose the count entirely —
    under-billing and reporting an empty context.
    """
    usage = _openai_compatible_usage(FakeUsage(5000, 50))

    assert usage["input_tokens"] == 5000
    assert usage["cache_read_input_tokens"] == 0


def test_miss_is_derived_when_only_hit_is_reported() -> None:
    """Partial reporting must still satisfy the sum invariant."""
    usage = _openai_compatible_usage(FakeUsage(1000, 10, hit=800))

    assert usage["input_tokens"] == 200
    assert usage["input_tokens"] + usage["cache_read_input_tokens"] == 1000


def test_derived_miss_never_goes_negative() -> None:
    """A provider reporting hit > prompt_tokens must not produce negative
    input, which `weighted_tokens` would clamp while the context read wrong."""
    usage = _openai_compatible_usage(FakeUsage(100, 5, hit=500))
    assert usage["input_tokens"] == 0


def test_missing_usage_fields_do_not_raise() -> None:
    """Streaming chunks can carry a usage object with null counters."""
    class Empty:
        prompt_tokens = None
        completion_tokens = None

    usage = _openai_compatible_usage(Empty())
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0


def test_cached_tokens_count_toward_context_occupancy() -> None:
    """A cached token is still a token the model read, so it fills the window."""
    session = BaseSession("appbuilder")
    session.accumulate_usage(_openai_compatible_usage(FakeUsage(56000, 200, hit=52000, miss=4000)))

    assert session.get_usage_summary()["context_used"] == 56000


def test_context_is_the_last_call_not_the_running_sum() -> None:
    """The agent loop re-sends the whole conversation on every tool round-trip,
    so summing inputs measures spend, not occupancy. 26 iterations of a 56K
    conversation is still a 56K context.
    """
    session = BaseSession("appbuilder")
    for _ in range(26):
        session.accumulate_usage(_openai_compatible_usage(FakeUsage(56000, 200, hit=52000, miss=4000)))

    summary = session.get_usage_summary()
    assert summary["context_used"] == 56000
    assert summary["context_percent"] < 10, "a 56K conversation is not a full 1M window"


def test_total_tokens_still_counts_cached_reads() -> None:
    """Cached reads are billable tokens the model processed. Leaving them out
    would have made the displayed total collapse the moment cache reporting
    was switched on, for no real change in work done.
    """
    session = BaseSession("appbuilder")
    for _ in range(26):
        session.accumulate_usage(_openai_compatible_usage(FakeUsage(56000, 200, hit=52000, miss=4000)))

    summary = session.get_usage_summary()
    assert summary["total_tokens"] == 26 * (56000 + 200)
    assert summary["cache_read_tokens"] == 26 * 52000


def test_every_deepseek_usage_site_uses_the_shared_mapper() -> None:
    """DeepSeekProvider builds usage in three places — two non-streaming calls
    and the streaming path the agent actually runs on. Fixing some but not all
    is exactly how the cached tokens stayed invisible on the path that mattered,
    so assert no site hand-rolls `prompt_tokens` into `input_tokens` again.
    """
    import inspect

    import app.services.llm_provider as lp

    source = inspect.getsource(lp.DeepSeekProvider)
    assert '"input_tokens": response.usage.prompt_tokens' not in source
    assert '"input_tokens": chunk.usage.prompt_tokens' not in source
    assert source.count("_openai_compatible_usage(") >= 3
