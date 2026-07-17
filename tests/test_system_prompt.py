"""Regression tests for the deferred-tool surface (Phase 3 wiring).

Locks in three invariants that quietly drifted before this round:

1. **Coverage**: every tool in `ALL_TOOLS` is either advertised in the
   system-prompt tool index OR explicitly listed in `_INTENTIONALLY_HIDDEN`.
   If someone adds a new modlix module and forgets to wire it into the
   `_GROUPS` source-of-truth in `appbuilder/context.py`, this test fires.

2. **No legacy-router refs in the persona / tool catalog**: the agent prompt
   used to teach the `execute(tool=..., params=...)` router pattern with
   `object_type=` discrimination. After Phase 3 wiring, all of that is dead
   text and the LLM should be steered toward direct tool-name calls. This
   test catches accidental re-introduction of the legacy phrasing.

3. **AppBuilderAgent runs in defer_schemas mode**: not the router-tool mode
   that left 200 tools unreachable. Asserts the constructor wires
   `defer_schemas=True` and leaves `_router_tool_name` unset.
"""

from __future__ import annotations

import inspect
import re

import pytest

from app.agents.appbuilder.context import (
    AGENT_PERSONA,
    TOOL_GROUPS_SUMMARY,
    TOOL_GROUP_DETAILS,
    _ADVERTISED_NAMES,
    _GROUPS,
    _INTENTIONALLY_HIDDEN,
    _GROUP_KEYWORDS,
    _TOOL_NAME_TO_GROUP,
)
from app.agents.appbuilder.tools.registry import ALL_TOOLS


# ── Coverage: every ALL_TOOLS entry is accounted for ──────────────────────


def test_every_tool_is_advertised_or_intentionally_hidden() -> None:
    """The tool index + hidden set must cover ALL_TOOLS exactly.

    Drift detector — fires when:
      - A new modlix module ships without wiring its TOOLS list into
        `_collect_group_tool_names()`.
      - A tool name changes in a module but the index source isn't updated.
      - A tool is removed from a module but still listed in
        `_INTENTIONALLY_HIDDEN`.
    """
    all_names = {t.name for t in ALL_TOOLS}
    covered = _ADVERTISED_NAMES | _INTENTIONALLY_HIDDEN
    missing = all_names - covered
    extra = covered - all_names
    assert not missing, (
        f"{len(missing)} tool(s) in ALL_TOOLS are not advertised in the system "
        f"prompt and not in _INTENTIONALLY_HIDDEN: {sorted(missing)}. Add them "
        f"to the appropriate group in _collect_group_tool_names() (or to "
        f"_INTENTIONALLY_HIDDEN if they should stay callable but hidden)."
    )
    assert not extra, (
        f"{len(extra)} name(s) referenced in the index or _INTENTIONALLY_HIDDEN "
        f"don't exist in ALL_TOOLS: {sorted(extra)}. Stale references — likely "
        f"a tool was renamed or removed."
    )


def test_advertised_and_hidden_are_disjoint() -> None:
    """A tool can't simultaneously be advertised and intentionally hidden."""
    overlap = _ADVERTISED_NAMES & _INTENTIONALLY_HIDDEN
    assert not overlap, f"{sorted(overlap)} appear in both the index and _INTENTIONALLY_HIDDEN"


def test_intentionally_hidden_targets_only_legacy() -> None:
    """The hidden set is the legacy-CRUD / version_api / lookup_api surface.

    If we ever delete those tools (Phase 9 cleanup), this test must be
    updated. Until then, the set should stay tight — no other tool should
    slip into the hidden bucket silently.
    """
    expected = {
        "list", "create", "read", "update", "delete", "copy",
        "list_versions", "read_version", "rollback_version",
        "lookup_api",
    }
    assert _INTENTIONALLY_HIDDEN == frozenset(expected), (
        f"_INTENTIONALLY_HIDDEN changed unexpectedly. Was: {sorted(expected)}. "
        f"Now: {sorted(_INTENTIONALLY_HIDDEN)}. If a tool moved into hidden, "
        f"document why; if a legacy tool was deleted, drop it here AND from "
        f"the legacy registration list."
    )


def test_advertised_count_floor() -> None:
    """The deferred surface should expose ~200 tools (sanity floor)."""
    assert len(_ADVERTISED_NAMES) >= 195, (
        f"Only {len(_ADVERTISED_NAMES)} tools advertised — expected at least 195. "
        f"Did a module's TOOLS list go missing from _collect_group_tool_names()?"
    )


# ── Index structure: groups are non-empty, ordered, deduplicated ──────────


def test_groups_are_nonempty_and_unique_within() -> None:
    """Each group has at least one tool; no duplicates within a group."""
    for label, names in _GROUPS:
        assert names, f"Group {label!r} is empty — likely a stale module reference"
        assert len(names) == len(set(names)), (
            f"Group {label!r} has duplicate tool names: "
            f"{[n for n in names if names.count(n) > 1]}"
        )


def test_no_tool_appears_in_multiple_groups() -> None:
    """A tool belongs to exactly one group in the catalog index."""
    seen: dict[str, str] = {}
    for label, names in _GROUPS:
        for n in names:
            if n in seen:
                pytest.fail(f"Tool {n!r} appears in both {seen[n]!r} and {label!r}")
            seen[n] = label


def test_tool_groups_summary_lists_every_advertised_tool() -> None:
    """Every advertised tool name appears verbatim in the rendered catalog."""
    missing_from_render = [
        name for name in sorted(_ADVERTISED_NAMES)
        if f"`{name}`" not in TOOL_GROUPS_SUMMARY
    ]
    assert not missing_from_render, (
        f"{len(missing_from_render)} advertised tool name(s) missing from the "
        f"rendered TOOL_GROUPS_SUMMARY: {missing_from_render[:10]}..."
    )


# ── Persona / catalog: no residual legacy-router phrasing ─────────────────

_LEGACY_PHRASES = (
    'execute(tool="',
    'execute(tool=',
    'object_type="page"',
    'object_type="application"',
    'object_type="theme"',
    "list(object_type=",
    "create(object_type=",
    "update(object_type=",
    "delete(object_type=",
    "read(object_type=",
)


def test_persona_has_no_legacy_router_phrasing() -> None:
    """AGENT_PERSONA must steer toward direct tool-name calls."""
    persona_lower = AGENT_PERSONA.lower()
    bad_hits = [p for p in _LEGACY_PHRASES if p.lower() in persona_lower]
    assert not bad_hits, (
        f"AGENT_PERSONA still references the retired router pattern: {bad_hits}. "
        f"Phase 3 wired the deferred-schema surface — examples should call "
        f"named tools directly (e.g. `update_page(name='X', ...)`)."
    )


def test_tool_catalog_has_no_legacy_router_phrasing() -> None:
    """The rendered tool catalog must not advertise router-shape calls."""
    catalog_lower = TOOL_GROUPS_SUMMARY.lower()
    bad_hits = [p for p in _LEGACY_PHRASES if p.lower() in catalog_lower]
    assert not bad_hits, (
        f"TOOL_GROUPS_SUMMARY still references the retired router pattern: "
        f"{bad_hits}. Make sure all detail blocks were updated when Phase 3 "
        f"landed."
    )


def test_persona_describes_deferred_surface() -> None:
    """The persona must teach the deferred-schema fetch pattern."""
    persona_lower = AGENT_PERSONA.lower()
    required_markers = ("get_tool_schema", "search_tools")
    missing = [m for m in required_markers if m not in persona_lower]
    assert not missing, (
        f"AGENT_PERSONA doesn't mention deferred-schema entry points: {missing}. "
        f"The LLM needs to know how to fetch schemas and discover tools."
    )


# ── Detail blocks: keyword scorer + tool-name map are consistent ─────────


def test_detail_block_keys_match_keyword_groups() -> None:
    """Every detail-block key must have a matching keyword set (and vice versa)."""
    detail_keys = set(TOOL_GROUP_DETAILS.keys())
    keyword_keys = set(_GROUP_KEYWORDS.keys())
    only_in_details = detail_keys - keyword_keys
    only_in_keywords = keyword_keys - detail_keys
    assert not only_in_details, (
        f"Detail blocks {sorted(only_in_details)} have no keywords — they'll "
        f"never be selected by `_score_groups_by_keywords`."
    )
    assert not only_in_keywords, (
        f"Keyword groups {sorted(only_in_keywords)} have no matching detail "
        f"block in TOOL_GROUP_DETAILS — they'll be silently dropped by "
        f"`_build_details`."
    )


def test_tool_name_group_map_targets_known_groups() -> None:
    """`_TOOL_NAME_TO_GROUP` values must be real detail-block keys."""
    detail_keys = set(TOOL_GROUP_DETAILS.keys())
    invalid = {n: g for n, g in _TOOL_NAME_TO_GROUP.items() if g not in detail_keys}
    assert not invalid, (
        f"`_TOOL_NAME_TO_GROUP` maps tools to non-existent detail groups: "
        f"{invalid}. Either add the missing detail block or fix the map."
    )


# ── AppBuilderAgent: defer_schemas wiring ────────────────────────────────


def test_appbuilder_agent_uses_defer_schemas_mode() -> None:
    """The constructor must pass `defer_schemas=True` and NOT a `router_tool`.

    This catches accidental reversion to router mode (which left 200 tools
    unreachable). Reading the constructor source rather than instantiating
    the agent — instantiation pulls heavy deps (provider, catalog, KB) that
    we don't need for this assertion.
    """
    from app.agents.appbuilder.agent import AppBuilderAgent

    source = inspect.getsource(AppBuilderAgent.__init__)
    assert "defer_schemas=True" in source, (
        "AppBuilderAgent constructor must pass `defer_schemas=True` to "
        "BaseAgent. Without it the deferred-schema surface stays dormant."
    )
    # The constructor must not re-wire router_tool. We allow the symbol to
    # appear in a comment (the retirement note) but not as a keyword arg.
    router_arg_re = re.compile(r"router_tool\s*=\s*[A-Za-z_]")
    assert not router_arg_re.search(source), (
        "AppBuilderAgent constructor still passes `router_tool=...`. "
        "Phase 3 wired defer_schemas mode; the router was retired here."
    )
