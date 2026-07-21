"""Tests for the bench-runner internals in scripts/bench_providers.py.

What's tested:
  - The convergence oracle classifies each conv-flag combination correctly.
  - _classify_calls counts schema fetches / Kirun compiles / KB writes
    correctly and consistently with the oracle.
  - BenchObserver captures tool_result events into its .tool_calls list
    AND auto-approves confirmations (the legacy CRUD path uses these).
  - The dry-run MockSaasClient install is non-destructive (no real HTTP) +
    every modlix tool that resolves get_saas_client picks it up.
  - The Markdown summary renderer formats per-provider blocks correctly.

What's NOT tested here (and shouldn't be — the bench is an integration
harness):
  - Actually running the full agent loop against a real LLM.
  - Convergence on the curated corpus — that's what the bench itself
    measures when an operator runs it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# scripts/ isn't on the regular Python path; add it once at module load.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import bench_providers as bp  # noqa: E402  — path inserted above


# ─── Convergence oracle ────────────────────────────────────────────────────


def _conv(must_call=(), any_of_groups=(), kirun=False, kb=False) -> bp.Conversation:
    return bp.Conversation(
        name="t", description="x", messages=["hi"],
        must_call_tools=list(must_call),
        must_call_any_of_groups=[list(g) for g in any_of_groups],
        must_succeed_on_kirun=kirun, must_succeed_on_kb_write=kb,
    )


def _m() -> bp.BenchMetrics:
    return bp.BenchMetrics(provider="p", conversation="t")


def test_convergence_passes_when_all_required_called() -> None:
    c = _conv(must_call=["list_pages", "get_page"])
    calls = [("list_pages", True), ("get_page", True), ("extra_tool", True)]
    ok, reason = bp._convergence(c, _m(), calls)
    assert ok is True
    assert reason is None


def test_convergence_fails_on_missing_required_tool() -> None:
    c = _conv(must_call=["list_pages", "get_page"])
    calls = [("list_pages", True)]
    ok, reason = bp._convergence(c, _m(), calls)
    assert ok is False
    assert "get_page" in (reason or "")


def test_convergence_treats_failed_call_as_called_for_must_call_tools() -> None:
    """A required tool that was CALLED counts even if the call failed.

    `must_call_tools` is "the agent attempted the right action", not
    "the action succeeded end-to-end". Success flags handle the latter.
    """
    c = _conv(must_call=["list_pages"])
    calls = [("list_pages", False)]
    ok, _ = bp._convergence(c, _m(), calls)
    assert ok is True


def test_convergence_kirun_flag_needs_successful_compile() -> None:
    c = _conv(must_call=["compile_kirun_text"], kirun=True)
    failed = [("compile_kirun_text", False)]
    succeeded = [("compile_kirun_text", True)]
    assert bp._convergence(c, _m(), failed)[0] is False
    assert bp._convergence(c, _m(), succeeded)[0] is True


def test_convergence_kirun_flag_accepts_any_kirun_tool() -> None:
    """Either compile_kirun_text OR save_*_from_text counts."""
    c = _conv(kirun=True)
    for kirun_tool in bp._KIRUN_TOOLS:
        ok, _ = bp._convergence(c, _m(), [(kirun_tool, True)])
        assert ok is True, f"{kirun_tool} should satisfy must_succeed_on_kirun"


# ─── must_call_any_of_groups oracle ────────────────────────────────────────


def test_convergence_any_of_group_passes_when_any_member_called() -> None:
    """The whole point of equivalence groups: any of the listed tools satisfies."""
    c = _conv(any_of_groups=[["compile_kirun_text", "save_function_from_text", "create_server_function"]])
    # First alternative
    ok, _ = bp._convergence(c, _m(), [("compile_kirun_text", True)])
    assert ok is True
    # Second alternative
    ok, _ = bp._convergence(c, _m(), [("save_function_from_text", True)])
    assert ok is True
    # Third alternative
    ok, _ = bp._convergence(c, _m(), [("create_server_function", True)])
    assert ok is True


def test_convergence_any_of_group_fails_when_no_member_called() -> None:
    c = _conv(any_of_groups=[["get_page_summary", "get_page"]])
    ok, reason = bp._convergence(c, _m(), [("list_pages", True)])
    assert ok is False
    assert "none-of-group" in (reason or "")
    # Failure message must mention BOTH alternatives so the operator can
    # tell at a glance what the agent skipped.
    assert "get_page_summary" in (reason or "")
    assert "get_page" in (reason or "")


def test_convergence_multiple_groups_all_must_be_satisfied() -> None:
    """C4-style: agent must do a read (group 1) AND a write (group 2).
    Either group unsatisfied → failure."""
    c = _conv(any_of_groups=[
        ["get_function", "decompile_function"],
        ["add_step", "update_server_function"],
    ])
    # Only read done — write group unsatisfied
    ok, reason = bp._convergence(c, _m(), [("get_function", True)])
    assert ok is False
    assert "add_step" in (reason or "") or "update_server_function" in (reason or "")
    # Only write done — read group unsatisfied
    ok, reason = bp._convergence(c, _m(), [("update_server_function", True)])
    assert ok is False
    # Both done — converges
    ok, _ = bp._convergence(c, _m(), [("decompile_function", True), ("add_step", True)])
    assert ok is True


def test_convergence_mixes_must_call_and_any_of_group() -> None:
    """C6-style: definition side must use exactly create_storage; query side
    can use either of two read tools. Both checks must pass."""
    c = _conv(
        must_call=["create_storage"],
        any_of_groups=[["count_storage_rows", "query_storage_rows"]],
    )
    # must_call missing — fails before checking groups
    ok, reason = bp._convergence(c, _m(), [("count_storage_rows", True)])
    assert ok is False
    assert "create_storage" in (reason or "")
    # group missing — fails on groups
    ok, reason = bp._convergence(c, _m(), [("create_storage", True)])
    assert ok is False
    assert "none-of-group" in (reason or "")
    # both satisfied
    ok, _ = bp._convergence(c, _m(), [("create_storage", True), ("query_storage_rows", True)])
    assert ok is True


def test_convergence_failed_call_still_counts_for_any_of_group() -> None:
    """Mirroring the must_call_tools behaviour: a CALLED tool counts even
    if it failed. Group satisfaction is about "did the agent attempt the
    right action" — success is gated separately via must_succeed_on_*."""
    c = _conv(any_of_groups=[["save_function_from_text"]])
    ok, _ = bp._convergence(c, _m(), [("save_function_from_text", False)])
    assert ok is True


def test_convergence_empty_group_is_filtered_by_parser(tmp_path) -> None:
    """Defensive: an empty group in YAML (operator typo) shouldn't reach
    the oracle. The parser filters empty groups out at load time."""
    yaml_text = """
conversations:
  - name: t
    description: x
    messages: ["hi"]
    must_call_any_of_groups:
      - []
      - [list_pages]
"""
    corpus_path = tmp_path / "tiny_corpus.yaml"
    corpus_path.write_text(yaml_text)
    convs = bp._load_corpus(corpus_path)
    assert convs[0].must_call_any_of_groups == [["list_pages"]]


# ─── Corpus integration: each updated conv has its alternative groups ──────


def test_corpus_yaml_has_relaxed_groups_for_problem_convs() -> None:
    """Anchor the 4 convs that switched from over-prescriptive must_call_tools
    to must_call_any_of_groups. If someone re-tightens them by accident, this
    test catches the regression."""
    import pathlib
    convs = bp._load_corpus(pathlib.Path("scripts/bench_corpus.yaml"))
    by_name = {c.name: c for c in convs}

    # C2: read-page-structure relaxed to accept get_page or get_page_summary
    c2 = by_name["read-page-structure"]
    assert c2.must_call_tools == [], "C2 should no longer use must_call_tools"
    assert any(
        set(g) == {"get_page_summary", "get_page"}
        for g in c2.must_call_any_of_groups
    ), f"C2 missing the page-read group: {c2.must_call_any_of_groups}"

    # C3: kirun-author-hello-fn relaxed across three valid authoring paths
    c3 = by_name["kirun-author-hello-fn"]
    flat = {t for g in c3.must_call_any_of_groups for t in g}
    assert {"compile_kirun_text", "save_function_from_text", "create_server_function"} <= flat

    # C4: kirun-add-step needs both a read AND a write group
    c4 = by_name["kirun-add-step-to-existing"]
    assert len(c4.must_call_any_of_groups) >= 2

    # C6: storage-define-then-query keeps create_storage as required + relaxes the read
    c6 = by_name["storage-define-then-query"]
    assert c6.must_call_tools == ["create_storage"]
    flat = {t for g in c6.must_call_any_of_groups for t in g}
    assert {"count_storage_rows", "query_storage_rows"} <= flat


def test_convergence_kb_flag_needs_commit_not_just_propose() -> None:
    c = _conv(kb=True)
    propose_only = [("propose_kb_update", True)]
    commit_failed = [("commit_kb_update", False)]
    commit_ok = [("commit_kb_update", True)]
    assert bp._convergence(c, _m(), propose_only)[0] is False
    assert bp._convergence(c, _m(), commit_failed)[0] is False
    assert bp._convergence(c, _m(), commit_ok)[0] is True


# ─── Call classifier ──────────────────────────────────────────────────────


def test_classify_counts_each_bucket() -> None:
    calls = [
        ("list_pages", True),
        ("get_tool_schema", True),
        ("get_tool_schema", False),
        ("compile_kirun_text", True),
        ("compile_kirun_text", False),
        ("commit_kb_update", True),
    ]
    c = bp._classify_calls(calls)
    assert c["tool_calls_total"] == 6
    assert c["tool_calls_succeeded"] == 4
    assert c["schema_fetches"] == 2
    assert c["schema_fetches_succeeded"] == 1
    assert c["kirun_compiles_total"] == 2
    assert c["kirun_compiles_succeeded"] == 1
    assert c["kb_writes_total"] == 1
    assert c["kb_writes_succeeded"] == 1


def test_classify_empty_list_zeros() -> None:
    c = bp._classify_calls([])
    assert all(v == 0 for v in c.values())


# ─── BenchObserver ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_observer_captures_tool_result() -> None:
    observer_cls = bp._make_observer()
    obs = observer_cls()
    await obs.emit_tool_result("list_pages", True, "ok", tool_use_id="u1")
    await obs.emit_tool_result("create_page", False, "boom", tool_use_id="u2")
    assert obs.tool_calls == [("list_pages", True), ("create_page", False)]


@pytest.mark.asyncio
async def test_observer_auto_approves_confirmation() -> None:
    observer_cls = bp._make_observer()
    obs = observer_cls()
    result = await obs.request_confirmation(
        confirmation_id="c1", message="delete this?",
        tool_name="delete", display_name="Delete",
    )
    assert result == {"approved": True, "selected": "approve"}


# ─── Mock SaasClient install ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_saas_client_install_returns_success() -> None:
    """Installing the dry-run mock makes get_saas_client return the mock and
    every modlix tool that resolves it picks up the success-by-default shape.
    """
    mock = bp._install_mock_saas_client()
    from app.agents.appbuilder.tools._shared import get_saas_client
    assert get_saas_client() is mock

    r = await mock.get("/api/ui/pages", headers={"appCode": "test"}, params=None)
    assert r.success is True
    assert "/api/ui/pages" in (r.summary or "")
    assert ("GET", "/api/ui/pages") in mock.calls


# ─── Summary renderer ──────────────────────────────────────────────────────


def test_render_provider_block_basic() -> None:
    rows = [
        bp.BenchMetrics(
            provider="anthropic", conversation="t1", converged=True,
            tool_calls_total=5, tool_calls_succeeded=5,
            kirun_compiles_total=2, kirun_compiles_succeeded=2,
            kb_writes_total=1, kb_writes_succeeded=1,
            input_tokens=1000, output_tokens=500, wall_seconds=3.0,
        ),
        bp.BenchMetrics(
            provider="anthropic", conversation="t2", converged=False,
            tool_calls_total=3, tool_calls_succeeded=2,
            input_tokens=800, output_tokens=200, wall_seconds=2.5,
            failure_reason="missing required tools: ['create_page']",
        ),
    ]
    lines = bp._render_provider_block("anthropic", rows)
    body = "\n".join(lines)
    assert "## anthropic" in body
    assert "converged: 1/2" in body
    assert "Kirun compile pass-rate: 2/2" in body
    assert "KB write pass-rate: 1/1" in body
    assert "t2: missing required tools" in body


def test_render_provider_block_no_kirun_no_kb() -> None:
    """When a provider's runs never touched Kirun or KB, the summary says so
    explicitly rather than showing 0/0 (which reads as 'all failed')."""
    rows = [
        bp.BenchMetrics(
            provider="x", conversation="t", converged=True,
            tool_calls_total=2, tool_calls_succeeded=2,
        ),
    ]
    body = "\n".join(bp._render_provider_block("x", rows))
    assert "Kirun: no compile attempts" in body
    assert "KB: no write attempts" in body


# ─── Provider-key precheck ────────────────────────────────────────────────


def test_check_provider_keys_returns_missing(monkeypatch) -> None:
    """If a provider's required env var is unset, it shows up in the list."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-for-test")

    missing = bp._check_provider_keys(["anthropic", "openai", "gemini"])
    names = {p for p, _env in missing}
    assert names == {"anthropic", "gemini"}, f"got {missing}"


def test_check_provider_keys_empty_when_all_set(monkeypatch) -> None:
    """All providers configured → empty list."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("OPENAI_API_KEY", "b")
    monkeypatch.setenv("GOOGLE_API_KEY", "c")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")
    assert bp._check_provider_keys(["anthropic", "openai", "gemini", "deepseek"]) == []


def test_check_provider_keys_ignores_unknown_provider(monkeypatch) -> None:
    """An unknown provider name doesn't get an env-var requirement here —
    the LLM factory will surface its own error downstream."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    missing = bp._check_provider_keys(["totally-not-a-provider"])
    assert missing == []


def test_validate_args_blocks_when_keys_missing(monkeypatch) -> None:
    """Pre-flight blocks before any LLM call when keys are missing."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class Args:
        mode = "dry-run"
        app_code = "x"
        client_code = "y"
        gateway_url = None

    err = bp._validate_args(Args(), ["anthropic"])
    assert err is not None
    assert "ANTHROPIC_API_KEY" in err


def test_validate_args_blocks_prod_in_client_code(monkeypatch) -> None:
    """Refuses to live-bench against anything that looks like prod."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")

    class Args:
        mode = "live"
        app_code = "x"
        client_code = "PROD_TENANT"
        gateway_url = None

    err = bp._validate_args(Args(), ["anthropic"])
    assert err is not None
    assert "prod" in err.lower()


# ─── Failure-class extraction ─────────────────────────────────────────────


def test_failure_class_pulls_exception_name() -> None:
    assert bp._failure_class("AuthenticationError: 401 invalid x-api-key") == "AuthenticationError"
    assert bp._failure_class("Agent error: AuthenticationError: 401") == "AuthenticationError"
    assert bp._failure_class("ConnectError: nope") == "ConnectError"


def test_failure_class_handles_non_exception_reason() -> None:
    assert bp._failure_class("missing required tools: ['list_pages']") == "missing required tools"


def test_failure_class_handles_none_and_empty() -> None:
    assert bp._failure_class(None) is None
    assert bp._failure_class("") is None


# ─── Failure-reason precedence ────────────────────────────────────────────


def test_resolve_failure_reason_precedence() -> None:
    """existing > observer_errors > oracle_reason."""
    assert bp._resolve_failure_reason("pre-existing", ["obs-err"], "oracle") == "pre-existing"
    assert bp._resolve_failure_reason(None, ["obs-err"], "oracle") == "obs-err"
    assert bp._resolve_failure_reason(None, [], "oracle") == "oracle"
    assert bp._resolve_failure_reason(None, [], None) is None


# ─── Observer captures emit_error ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_observer_captures_emit_error() -> None:
    """The agent loop emits errors via emit_error (top-level handler).
    The observer must capture them so the bench can surface them as the
    upstream failure_reason instead of the oracle's downstream cascade."""
    observer_cls = bp._make_observer()
    obs = observer_cls()
    await obs.emit_error("Agent error: AuthenticationError: 401 invalid x-api-key")
    await obs.emit_error("Second error during retry")
    assert obs.errors == [
        "Agent error: AuthenticationError: 401 invalid x-api-key",
        "Second error during retry",
    ]


# ─── Circuit breaker ──────────────────────────────────────────────────────


def _metric(converged: bool, reason: str | None = None) -> bp.BenchMetrics:
    m = bp.BenchMetrics(provider="p", conversation="c")
    m.converged = converged
    m.failure_reason = reason
    return m


def test_circuit_breaker_trips_on_consecutive_same_class() -> None:
    """Same exception class N consecutive times → should_abort fires."""
    consec, cls, abort = 0, None, False

    consec, cls, abort = bp._update_circuit_breaker(
        _metric(False, "AuthenticationError: 401"), consec, cls,
    )
    assert (consec, cls, abort) == (1, "AuthenticationError", False)

    consec, cls, abort = bp._update_circuit_breaker(
        _metric(False, "AuthenticationError: 401 again"), consec, cls,
    )
    # At the limit of 2, abort fires.
    assert abort is True
    assert consec == 2
    assert cls == "AuthenticationError"


def test_circuit_breaker_resets_on_convergence() -> None:
    """A converged run resets the consecutive counter, ending any cascade."""
    consec, cls, _ = bp._update_circuit_breaker(
        _metric(False, "AuthenticationError: x"), 0, None,
    )
    assert consec == 1
    consec, cls, abort = bp._update_circuit_breaker(_metric(True), consec, cls)
    assert (consec, cls, abort) == (0, None, False)


def test_circuit_breaker_resets_on_different_class() -> None:
    """A different exception class restarts the counter at 1."""
    consec, cls, abort = bp._update_circuit_breaker(
        _metric(False, "AuthenticationError: x"), 0, None,
    )
    assert (consec, cls, abort) == (1, "AuthenticationError", False)
    consec, cls, abort = bp._update_circuit_breaker(
        _metric(False, "ConnectError: gateway down"), consec, cls,
    )
    assert (consec, cls, abort) == (1, "ConnectError", False)


def test_circuit_breaker_does_not_trip_on_unclassifiable_reason() -> None:
    """An empty/None failure_reason can't form a cascade — won't abort."""
    consec, cls, abort = bp._update_circuit_breaker(_metric(False, None), 0, None)
    assert abort is False
    consec, cls, abort = bp._update_circuit_breaker(_metric(False, None), consec, cls)
    # Even multiple Nones in a row don't trip the breaker — no class to match.
    assert abort is False
