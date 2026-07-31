"""Smoke tests for the provider bench corpus.

The bench harness ([scripts/bench_providers.py](../scripts/bench_providers.py))
reads its conversations from [scripts/bench_corpus.yaml](../scripts/bench_corpus.yaml).
These tests catch drift between the corpus and the actual tool surface:

1. YAML parses + every conversation has the required fields.
2. Every `must_call_tools` entry resolves to a real tool in ALL_TOOLS — if a
   tool gets renamed or removed, the bench gates wouldn't fire and the
   convergence oracle would silently degrade.
3. The corpus covers a minimum surface (Kirun, KB, screenshot, code-workspace,
   search-tools, page-CRUD) so future curators don't drop important categories.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.agents.appbuilder.tools.registry import ALL_TOOLS


_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORPUS_PATH = _REPO_ROOT / "scripts" / "bench_corpus.yaml"


def _load() -> list[dict]:
    raw = yaml.safe_load(_CORPUS_PATH.read_text())
    return (raw or {}).get("conversations") or []


def test_corpus_yaml_parses() -> None:
    """The corpus file must parse and contain at least one conversation."""
    convs = _load()
    assert convs, "bench corpus is empty — bench can't run"


def test_every_conversation_has_required_fields() -> None:
    """Each entry must have name + description + messages."""
    for c in _load():
        assert c.get("name"), f"conversation missing name: {c}"
        assert c.get("description"), f"{c['name']}: missing description"
        msgs = c.get("messages") or []
        assert msgs, f"{c['name']}: must have at least one user message"
        for i, m in enumerate(msgs):
            assert isinstance(m, str) and m.strip(), (
                f"{c['name']}.messages[{i}]: must be a non-empty string"
            )


def test_must_call_tools_all_resolve() -> None:
    """Every required-tool name must exist in ALL_TOOLS.

    If a modlix tool gets renamed, this fires. The bench harness reads the
    corpus verbatim — silently-missing tool names would make the
    convergence oracle pass for the wrong reasons.
    """
    known = {t.name for t in ALL_TOOLS}
    bad: list[tuple[str, str]] = []
    for c in _load():
        for t in c.get("must_call_tools") or []:
            if t not in known:
                bad.append((c["name"], t))
    assert not bad, (
        f"{len(bad)} bench-required tool(s) don't exist in ALL_TOOLS: "
        f"{bad}. Either rename the corpus entry or the tool."
    )


def test_corpus_covers_minimum_surface() -> None:
    """Drop-protection: the corpus must keep covering these capability areas.

    Each entry maps to a representative tool name. If the bench gets
    edited and drops one of these tools entirely from required-tools,
    this fires.
    """
    required_anchors = {
        "page-CRUD": "list_pages",
        "kirun-DSL": "compile_kirun_text",
        "page-event-function": "create_page_event_function",
        "storage-data-readonly": "count_storage_rows",
        "screenshot-vision": "screenshot_page",
        "kb-propose-then-commit": "commit_kb_update",
        "code-workspace": "code_grep",
        "deferred-tool-discovery": "search_tools",
    }
    convs = _load()
    all_required: set[str] = set()
    for c in convs:
        all_required.update(c.get("must_call_tools") or [])
        # must_call_any_of_groups also counts — an anchor tool that's listed
        # as one of several valid alternatives still represents corpus coverage
        # for that capability area (the agent's free to pick equivalents).
        for group in c.get("must_call_any_of_groups") or []:
            all_required.update(group)
    missing = {label: tool for label, tool in required_anchors.items() if tool not in all_required}
    assert not missing, (
        "Bench corpus lost coverage for these capability areas (anchor tool "
        f"no longer in any must_call_tools or any group): {missing}. Add a "
        f"conversation that exercises the missing anchor."
    )


def test_kirun_and_kb_flags_have_coverage() -> None:
    """At least one conversation must exercise each convergence flag.

    The flags are how the bench knows whether Kirun authoring + KB writes
    held end-to-end. If no conversation sets them, the bench's compile +
    KB pass-rate columns stay vacuously at 0 — useless signal.
    """
    convs = _load()
    assert any(c.get("must_succeed_on_kirun") for c in convs), (
        "No conversation has must_succeed_on_kirun=true. Add one that "
        "exercises compile_kirun_text + save."
    )
    assert any(c.get("must_succeed_on_kb_write") for c in convs), (
        "No conversation has must_succeed_on_kb_write=true. Add one that "
        "exercises propose_kb_update → commit_kb_update."
    )
