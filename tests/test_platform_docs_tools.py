"""Tests for app/agents/appbuilder/tools/platform_docs.py — exercises the
five doc-reading tools against the REAL aicontext tree on disk.

These tools are pure local file reads (no SaaS client, no DB) so the tests
hit the actual bundled docs and assert observable behaviour.
"""

from __future__ import annotations

import json

import pytest

from app.agents.appbuilder.tools.platform_docs import (
    pattern_read_tool,
    pattern_sample_tool,
    pattern_search_tool,
    platform_doc_list_tool,
    platform_doc_read_tool,
)


@pytest.mark.asyncio
async def test_platform_doc_list_returns_refs_and_patterns():
    result = await platform_doc_list_tool.execute({}, {})
    assert result.success is True
    summary = result.summary
    # At least one known reference doc surfaces (multiple options — any one is fine).
    known = ("design_system", "critical-rules", "kirun_remote_repository", "auth_lifecycle")
    assert any(k in summary for k in known), (
        f"expected one of {known} in summary; got: {summary[:500]}"
    )
    # The list output names a healthy number of patterns. We've got 159 on disk
    # and the listing caps at 40 visible plus a "... and N more" footer.
    assert "pattern" in summary.lower()
    # Either we see 40 listed (cap) or all of them if fewer exist.
    bulleted = [ln for ln in summary.splitlines() if ln.strip().startswith("- ")]
    assert len(bulleted) >= 40, f"expected >=40 bulleted entries; got {len(bulleted)}"


@pytest.mark.asyncio
async def test_platform_doc_read_known_doc():
    # `design_system` lives under reference/ on disk; tool resolves by stem.
    result = await platform_doc_read_tool.execute({"name": "design_system"}, {})
    assert result.success is True, f"unexpected error: {result.error}"
    assert result.summary
    # The header the tool prepends.
    assert "design_system" in result.summary
    # Body is non-trivial.
    assert len(result.summary) > 200


@pytest.mark.asyncio
async def test_platform_doc_read_unknown_returns_error():
    result = await platform_doc_read_tool.execute(
        {"name": "definitely-not-a-doc-1234"}, {},
    )
    assert result.success is False
    assert result.error
    assert "definitely-not-a-doc-1234" in result.error or "Unknown" in result.error


@pytest.mark.asyncio
async def test_platform_doc_read_missing_name_errors():
    result = await platform_doc_read_tool.execute({}, {})
    assert result.success is False
    assert "name" in result.error.lower()


@pytest.mark.asyncio
async def test_pattern_search_finds_login():
    result = await pattern_search_tool.execute({"query": "login"}, {})
    assert result.success is True
    assert "login-page" in result.summary, (
        f"expected 'login-page' slug in matches; got: {result.summary[:400]}"
    )


@pytest.mark.asyncio
async def test_pattern_search_no_query_errors():
    result = await pattern_search_tool.execute({"query": ""}, {})
    assert result.success is False
    assert "query" in result.error.lower()


@pytest.mark.asyncio
async def test_pattern_search_no_matches_clean_empty():
    # Tool returns success=True with a "(no patterns match ...)" message when
    # nothing hits — that's the documented clean-empty path.
    result = await pattern_search_tool.execute(
        {"query": "zzzzz-no-such-pattern-xyzzy"}, {},
    )
    assert result.success is True
    assert "no patterns match" in result.summary.lower()


@pytest.mark.asyncio
async def test_pattern_read_login_lists_samples():
    result = await pattern_read_tool.execute({"task_name": "login-page"}, {})
    assert result.success is True, f"unexpected error: {result.error}"
    summary = result.summary
    # The marker the tool appends before the sample listing.
    assert "Available sample files" in summary
    # At least one JSON page-def sample is listed.
    assert "leadzump.login.json" in summary
    # At least one decompiled Kirun DSL sample is listed.
    assert ".dsl" in summary


@pytest.mark.asyncio
async def test_pattern_read_unknown_errors():
    result = await pattern_read_tool.execute(
        {"task_name": "not-a-real-slug-zzz"}, {},
    )
    assert result.success is False
    assert "not-a-real-slug-zzz" in result.error or "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_pattern_read_missing_slug_errors():
    result = await pattern_read_tool.execute({}, {})
    assert result.success is False
    assert "task_name" in result.error.lower() or "required" in result.error.lower()


@pytest.mark.asyncio
async def test_pattern_sample_reads_json():
    result = await pattern_sample_tool.execute(
        {"task_name": "login-page", "file_name": "leadzump.login.json"}, {},
    )
    assert result.success is True, f"unexpected error: {result.error}"
    body = result.summary
    # The tool prepends a header, then includes the file body verbatim.
    assert "leadzump.login.json" in body
    # Sanity: it contains canonical Modlix page-def field(s), or at minimum
    # parses as JSON once we strip the header.
    has_canonical_field = any(
        field in body
        for field in ("componentDefinition", '"name"', '"appCode"', "rootComponent")
    )
    if not has_canonical_field:
        # Fall back: locate the first '{' and confirm JSON parses from there.
        brace = body.find("{")
        assert brace != -1, "expected a JSON object in the sample body"
        parsed = json.loads(body[brace:])
        assert isinstance(parsed, dict)


@pytest.mark.asyncio
async def test_pattern_sample_path_traversal_blocked():
    result = await pattern_sample_tool.execute(
        {"task_name": "login-page", "file_name": "../../../etc/passwd"}, {},
    )
    assert result.success is False
    err = result.error.lower()
    assert "file_name" in err or "path" in err or ".." in result.error


@pytest.mark.asyncio
async def test_pattern_sample_extension_allowlist():
    # README.md exists in the pattern dir but isn't a "sample" type.
    result = await pattern_sample_tool.execute(
        {"task_name": "login-page", "file_name": "README.md"}, {},
    )
    assert result.success is False
    assert "recognized sample type" in result.error.lower() or "supported" in result.error.lower()


@pytest.mark.asyncio
async def test_pattern_sample_missing_file():
    result = await pattern_sample_tool.execute(
        {"task_name": "login-page", "file_name": "no-such-file.json"}, {},
    )
    assert result.success is False
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_pattern_sample_missing_required_params():
    result = await pattern_sample_tool.execute({"task_name": "login-page"}, {})
    assert result.success is False
    assert "required" in result.error.lower() or "file_name" in result.error.lower()
