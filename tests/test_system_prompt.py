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


def _capability_filtered_names() -> set[str]:
    """Names the index advertises but the registry drops for this deployment.

    `_collect_group_tool_names()` reads each module's raw TOOLS list, while the
    registry filters on top of it: `_filter_visual_tools` drops
    `describe_image` when the AppBuilder model has native vision, since the
    screenshot tools then attach the PNG itself. `_build_tool_index()` already
    intersects with ALL_TOOLS, so such a name is absent from the rendered
    prompt by design — not index drift.
    """
    from app.services.llm_provider import appbuilder_vision_capable

    return {"describe_image"} if appbuilder_vision_capable() else set()


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
    extra = covered - all_names - _capability_filtered_names()
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
    """Every advertised tool name appears verbatim in the rendered catalog.

    Excludes names the registry drops for this deployment: `_build_tool_index`
    renders only tools present in ALL_TOOLS, so a capability-filtered name is
    meant to be missing here (see `_capability_filtered_names`).
    """
    renderable = _ADVERTISED_NAMES - _capability_filtered_names()
    missing_from_render = [
        name for name in sorted(renderable)
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


# ── Prompt caching: process-static context must not ride in the tail ──────


def test_catalogs_are_not_appended_to_dynamic_context() -> None:
    """The component + API catalogs must stay OUT of build_dynamic_context.

    They are rendered once in `__init__` and never recomputed, so appending
    them to the per-request block re-sent ~10.6K tokens uncached on every
    turn — and on providers that flatten the system blocks into one string it
    pushed them behind the per-session app/client line, dropping them out of
    the shared prefix cache entirely. They belong in the cached static suffix.
    """
    from app.agents.appbuilder.agent import AppBuilderAgent

    source = inspect.getsource(AppBuilderAgent.build_dynamic_context)
    for attr in ("_catalog_context", "_api_catalog_context"):
        assert f"parts.append(self.{attr})" not in source, (
            f"build_dynamic_context appends `self.{attr}` again. That context "
            "is static for the process lifetime — register it with "
            "`context_builder.set_static_suffix(...)` in __init__ instead, so "
            "it lands in the cached prefix rather than the per-turn tail."
        )

    init_source = inspect.getsource(AppBuilderAgent.__init__)
    assert "set_static_suffix" in init_source, (
        "AppBuilderAgent.__init__ no longer registers the catalogs as a cached "
        "static suffix — they would fall back into the uncached per-turn tail."
    )


def test_static_suffix_is_cached_and_precedes_dynamic() -> None:
    """BaseContext must emit the suffix as a cached block BEFORE the dynamic one.

    Providers cache a *prefix*, so a per-session block placed ahead of static
    context ends the cacheable run and negates the whole point of the seam.
    """
    import asyncio

    from app.core.context import BaseContext

    ctx = BaseContext(static_prefix="PERSONA")
    ctx.set_static_suffix("CATALOG")
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(ctx.load())
    blocks = ctx.build_system_prompt(dynamic_context="DYNAMIC")

    texts = [b["text"] for b in blocks]
    assert texts == ["PERSONA", "CATALOG", "DYNAMIC"], texts
    assert "cache_control" in blocks[0], "static docs block lost its cache_control"
    assert "cache_control" in blocks[1], (
        "static suffix must carry cache_control — otherwise it is re-sent in "
        "full on every turn, which is the bug this seam exists to fix."
    )
    assert "cache_control" not in blocks[2], (
        "the dynamic block must stay uncached; caching per-session text "
        "burns a breakpoint and never hits."
    )


def test_tool_index_carries_names_only_not_descriptions() -> None:
    """The index groups tools; it must not restate their descriptions.

    Every advertised tool is already in the API's `tools=` payload with its own
    one-liner, so per-tool prose here is paid twice in the fixed prefix (it was
    ~4.5K tokens). Grouping is the one thing the flat `tools=` array cannot
    express, so grouping is what this index is for.
    """
    body = TOOL_GROUPS_SUMMARY.split("### ", 1)[1] if "### " in TOOL_GROUPS_SUMMARY else ""
    assert body, "tool index rendered no group sections at all"
    assert "` — " not in body, (
        "the tool index is rendering `name` — description again. Per-tool prose "
        "belongs on the tool's own `description` (which reaches the model via "
        "tools= and search_tools), not duplicated into the system prompt."
    )


def test_tool_index_stays_small() -> None:
    """A ceiling, so the index can't quietly regrow into a second catalog.

    Names-only for ~222 tools measures ~1.4K tokens; 2.5K leaves room for new
    tools and new groups while still failing loudly if prose returns.
    """
    approx_tokens = len(TOOL_GROUPS_SUMMARY) / 3.7
    assert approx_tokens < 2500, (
        f"tool index has grown to ~{approx_tokens:,.0f} tokens. It is paid on "
        "every request of every conversation — check whether per-tool "
        "descriptions crept back in."
    )


def test_tool_index_still_groups() -> None:
    """Name-only rendering must not have flattened the groups away."""
    assert TOOL_GROUPS_SUMMARY.count("### ") >= 8, (
        "the tool index lost its group headings — that grouping is the only "
        "thing it contributes over the raw tools= array"
    )


# ── Parameter naming convention ───────────────────────────────────────────


_NAMED_ENTITIES = frozenset({
    "page", "theme", "style", "storage", "schema", "function", "template",
    "notification", "connection", "role", "profile", "app", "component",
    "uri_path", "event_definition", "event_action", "server_function",
    "page_event_function",
})


def test_primary_entity_is_always_called_name() -> None:
    """A `<verb>_<entity>` tool must call its OWN entity `name`, not `<entity>_name`.

    The rule holds across the surface today; this pins it. Replaying real
    sessions, guessing between `name` and `<entity>_name` was the single biggest
    cause of a rejected first call — the model erred in both directions — so a
    tool that breaks the rule makes a genuinely confusing surface worse.
    """
    violations = []
    for tool in ALL_TOOLS:
        m = re.match(
            r"^(get|create|update|delete|read|list|validate|replace|reset)_(.+)$",
            tool.name,
        )
        if not m or m.group(2) not in _NAMED_ENTITIES:
            continue
        params = {p.name for p in tool.parameters}
        own = f"{m.group(2)}_name"
        if own in params and "name" not in params:
            violations.append(f"{tool.name} uses `{own}` for its own entity")
    assert not violations, (
        "these tools name their own primary entity `<entity>_name` instead of "
        f"`name`: {violations}"
    )


def test_app_code_has_one_spelling() -> None:
    """149 parameters name the app; they must all spell it `app_code`.

    `export_security_app` was the lone `application_code` holdout.
    """
    odd = [
        t.name for t in ALL_TOOLS
        if any(p.name in ("application_code", "appCode", "applicationCode")
               for p in t.parameters)
    ]
    assert not odd, f"tools spelling the app code unconventionally: {odd}"


def test_bare_page_parameter_is_always_pagination() -> None:
    """A bare `page` must be a number, never a page name.

    Both spellings coexisting for different meanings is fine; a bare `page`
    holding a page NAME would make the whole rule unlearnable.
    """
    wrong = [
        t.name for t in ALL_TOOLS
        for p in t.parameters
        if p.name == "page" and p.type not in ("integer", "number")
    ]
    assert not wrong, f"`page` is not a pagination number in: {wrong}"


def test_naming_rule_is_stated_in_the_prompt() -> None:
    """The rule must be written down, not just held by convention."""
    assert "Parameter naming rule" in TOOL_GROUPS_SUMMARY
    for marker in ("`name`", "app_code", "pagination"):
        assert marker in TOOL_GROUPS_SUMMARY, f"naming rule no longer mentions {marker}"


def test_static_suffix_defaults_to_absent() -> None:
    """Agents that never register a suffix keep the original two-block shape."""
    import asyncio

    from app.core.context import BaseContext

    ctx = BaseContext(static_prefix="PERSONA")
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(ctx.load())
    assert [b["text"] for b in ctx.build_system_prompt("DYN")] == ["PERSONA", "DYN"]
