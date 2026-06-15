"""Tests for the per-app KB lifecycle.

Covers:
  - Service-layer primitives in ``app/services/app_kb.py``
    (hashing, section validation, reads, version bumping, export/import).
  - The propose-then-commit write flow in
    ``app/agents/appbuilder/tools/kb_app.py`` including the optimistic-lock
    collision check.

Everything goes through the ``mock_execute_query`` fixture — no MySQL needed.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.agents.appbuilder.tools.kb_app import (
    commit_kb_update_tool,
    propose_kb_update_tool,
)
from app.services import app_kb


# ── Pure helpers ─────────────────────────────────────────────────────────


def test_body_hash_stable() -> None:
    # Deterministic: same input → same output.
    assert app_kb.body_hash("hello world") == app_kb.body_hash("hello world")
    # Different input → different output.
    assert app_kb.body_hash("a") != app_kb.body_hash("b")
    # Length-stable (SHA-256 hex = 64 chars) regardless of input size.
    for sample in ("", "x", "hello", "a" * 10_000, "unicode: ☃"):
        h = app_kb.body_hash(sample)
        assert isinstance(h, str)
        assert len(h) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", h) is not None


def test_validate_section_accepts_known() -> None:
    for section in app_kb.SECTIONS:
        assert app_kb.validate_section(section) is None


def test_validate_section_rejects_unknown() -> None:
    err = app_kb.validate_section("garbage")
    assert isinstance(err, str)
    assert "garbage" in err
    # Surfaces the valid choices so the agent can self-correct.
    for section in app_kb.SECTIONS:
        assert section in err


# ── Reads ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_latest_returns_None_when_no_rows(mock_execute_query) -> None:
    # Default queue is empty → service returns None.
    result = await app_kb.get_latest("SYSTEM", "testapp", "overview")
    assert result is None
    # And it did hit the DB exactly once with the (client, app, section) tuple.
    assert len(mock_execute_query.calls) == 1
    _query, args = mock_execute_query.calls[0]
    assert args == ("SYSTEM", "testapp", "overview")


@pytest.mark.asyncio
async def test_get_latest_returns_row_when_present(mock_execute_query) -> None:
    row = {
        "ID": 1,
        "CLIENT_CODE": "SYSTEM",
        "APP_CODE": "testapp",
        "SECTION": "overview",
        "BODY": "first body",
        "BODY_HASH": app_kb.body_hash("first body"),
        "VERSION": 3,
        "UPDATED_BY": 42,
        "UPDATED_AT": "2026-06-06T00:00:00Z",
        "MESSAGE": "seed",
    }
    mock_execute_query.enqueue_rows([row])

    result = await app_kb.get_latest("SYSTEM", "testapp", "overview")
    assert isinstance(result, dict)
    assert result["BODY"] == "first body"
    assert result["VERSION"] == 3


# ── Version bump ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_next_version_increments(mock_execute_query) -> None:
    # When the COALESCE(MAX(VERSION),0)+1 query returns 6, next_version is 6.
    mock_execute_query.enqueue_rows([{"next": 6}])
    assert await app_kb.next_version("SYSTEM", "testapp", "overview") == 6

    # Empty queue → mock returns the default [] → next_version returns 1.
    assert await app_kb.next_version("SYSTEM", "testapp", "overview") == 1


# ── Propose / commit flow ────────────────────────────────────────────────


def _row(section: str, body: str, version: int) -> dict[str, Any]:
    return {
        "ID": version,
        "CLIENT_CODE": "SYSTEM",
        "APP_CODE": "testapp",
        "SECTION": section,
        "BODY": body,
        "BODY_HASH": app_kb.body_hash(body),
        "VERSION": version,
        "UPDATED_BY": 0,
        "UPDATED_AT": "2026-06-06T00:00:00Z",
        "MESSAGE": "",
    }


@pytest.mark.asyncio
async def test_propose_then_commit_flow(mock_execute_query, tool_context) -> None:
    # 1) propose_kb_update reads current latest (v1).
    mock_execute_query.enqueue_rows([_row("overview", "old body\n", 1)])
    proposal = await propose_kb_update_tool.execute(
        {
            "section": "overview",
            "body": "new body\n",
            "message": "rewrite for clarity",
        },
        tool_context,
    )
    assert proposal.success is True
    assert "Diff:" in proposal.summary
    # Pending UUID is stashed on the context for the commit step.
    pending_map = tool_context.get("pending_kb_updates") or {}
    assert len(pending_map) == 1
    pending_id = next(iter(pending_map.keys()))
    assert pending_id in proposal.summary  # surfaced to the agent

    # 2) commit_kb_update re-checks latest (still v1 — no race), then INSERTs.
    mock_execute_query.enqueue_rows([_row("overview", "old body\n", 1)])
    # The INSERT call's return value is unused by the service for content, so
    # an empty default ([]) is fine.
    pre_call_count = len(mock_execute_query.calls)
    commit = await commit_kb_update_tool.execute(
        {"pending_id": pending_id}, tool_context,
    )
    assert commit.success is True, commit.error
    assert "Committed v2" in commit.summary

    # An INSERT-shaped statement was emitted.
    insert_calls = [
        (q, a) for (q, a) in mock_execute_query.calls[pre_call_count:]
        if "INSERT INTO cfa_app_kb" in q
    ]
    assert len(insert_calls) == 1
    _q, args = insert_calls[0]
    # Args order: client, app, section, body, hash, new_version, updated_by, message
    assert args[0] == "SYSTEM"
    assert args[1] == "testapp"
    assert args[2] == "overview"
    assert args[3] == "new body\n"
    assert args[5] == 2  # expected_version (1) + 1
    # Pending entry is consumed.
    assert pending_id not in (tool_context.get("pending_kb_updates") or {})

    # 3) A follow-up get_latest now returns the new body (mock-fed).
    mock_execute_query.enqueue_rows([_row("overview", "new body\n", 2)])
    latest = await app_kb.get_latest("SYSTEM", "testapp", "overview")
    assert latest is not None
    assert latest["BODY"] == "new body\n"
    assert latest["VERSION"] == 2


@pytest.mark.asyncio
async def test_commit_unknown_pending_id_errors(tool_context) -> None:
    # No propose ran — commit with a fabricated UUID is rejected before any DB hit.
    result = await commit_kb_update_tool.execute(
        {"pending_id": "deadbeefdeadbeefdeadbeefdeadbeef"}, tool_context,
    )
    assert result.success is False
    assert "Unknown pending_id" in (result.error or "")


@pytest.mark.asyncio
async def test_commit_optimistic_lock(mock_execute_query, tool_context) -> None:
    # Propose against v1.
    mock_execute_query.enqueue_rows([_row("overview", "old body", 1)])
    proposal = await propose_kb_update_tool.execute(
        {"section": "overview", "body": "my edit", "message": "mine"},
        tool_context,
    )
    assert proposal.success is True
    pending_id = next(iter(tool_context["pending_kb_updates"].keys()))

    # Between propose and commit, someone else bumped it to v2.
    mock_execute_query.enqueue_rows([_row("overview", "their edit", 2)])
    result = await commit_kb_update_tool.execute(
        {"pending_id": pending_id}, tool_context,
    )
    assert result.success is False
    err = result.error or ""
    assert "Conflict" in err
    assert "v1" in err and "v2" in err
    # Stale pending is dropped so it can't be retried blindly.
    assert pending_id not in (tool_context.get("pending_kb_updates") or {})


# ── Append-only decisions_log ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decisions_log_append_only(mock_execute_query) -> None:
    # Each insert_version call computes its own next_version (without
    # expected_version), so two inserts produce two INSERT statements — the
    # earlier row is NOT overwritten.
    mock_execute_query.enqueue_rows([{"next": 1}])  # next_version for call 1
    await app_kb.insert_version(
        "SYSTEM", "testapp", "decisions_log",
        "decision A", updated_by=7, message="first",
    )
    mock_execute_query.enqueue_rows([{"next": 2}])  # next_version for call 2
    await app_kb.insert_version(
        "SYSTEM", "testapp", "decisions_log",
        "decision B", updated_by=7, message="second",
    )

    inserts = [
        (q, a) for (q, a) in mock_execute_query.calls
        if "INSERT INTO cfa_app_kb" in q
    ]
    assert len(inserts) == 2
    # Different bodies, different versions, both section=decisions_log.
    bodies = sorted(a[3] for (_, a) in inserts)
    versions = sorted(a[5] for (_, a) in inserts)
    sections = {a[2] for (_, a) in inserts}
    assert bodies == ["decision A", "decision B"]
    assert versions == [1, 2]
    assert sections == {"decisions_log"}
    # Sanity: decisions_log is in the declared append-only set.
    assert "decisions_log" in app_kb.APPEND_ONLY_SECTIONS


# ── Export → import roundtrip ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_then_import_roundtrip(mock_execute_query) -> None:
    # Source app has two sections populated.
    source_rows = [
        {
            "SECTION": "overview",
            "BODY": "overview body",
            "BODY_HASH": app_kb.body_hash("overview body"),
            "VERSION": 1,
            "UPDATED_BY": 1,
            "UPDATED_AT": "2026-06-06T00:00:00Z",
            "MESSAGE": "init",
        },
        {
            "SECTION": "conventions",
            "BODY": "conventions body",
            "BODY_HASH": app_kb.body_hash("conventions body"),
            "VERSION": 1,
            "UPDATED_BY": 1,
            "UPDATED_AT": "2026-06-06T00:00:00Z",
            "MESSAGE": "init",
        },
    ]
    mock_execute_query.enqueue_rows(source_rows)
    snapshot = await app_kb.export_app("SYSTEM", "testapp")
    assert snapshot["client_code"] == "SYSTEM"
    assert snapshot["app_code"] == "testapp"
    assert len(snapshot["rows"]) == 2

    # Re-import into a fresh (client, app). For each section, import_snapshot
    # calls get_latest (returns None → no skip) then insert_version (which
    # calls next_version → returns 1, then performs the INSERT).
    pre = len(mock_execute_query.calls)
    # For each of the 2 sections: get_latest [] + next_version [{next:1}] + INSERT [].
    mock_execute_query.enqueue_rows([])              # get_latest (overview)
    mock_execute_query.enqueue_rows([{"next": 1}])    # next_version (overview)
    mock_execute_query.enqueue_rows([])              # INSERT (overview)
    mock_execute_query.enqueue_rows([])              # get_latest (conventions)
    mock_execute_query.enqueue_rows([{"next": 1}])    # next_version (conventions)
    mock_execute_query.enqueue_rows([])              # INSERT (conventions)

    counts = await app_kb.import_snapshot(
        snapshot,
        target_client="OTHER",
        target_app="otherapp",
        updated_by=99,
        promotion_note="promote",
    )

    assert counts["sections_inserted"] == 2
    assert counts["decisions_added"] == 0
    assert counts["skipped"] == 0

    inserts = [
        (q, a) for (q, a) in mock_execute_query.calls[pre:]
        if "INSERT INTO cfa_app_kb" in q
    ]
    assert len(inserts) == 2
    # The inserted bodies match the source snapshot, written under the new tenant.
    seen = {(a[2], a[3]) for (_, a) in inserts}
    assert seen == {("overview", "overview body"), ("conventions", "conventions body")}
    for _, a in inserts:
        assert a[0] == "OTHER"
        assert a[1] == "otherapp"
        assert a[7] == "promote"  # message
