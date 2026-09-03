"""The bench oracle must assert what a run ACHIEVED, not which tool it called.

The route-based oracle was wrong in both directions on real 2026-09-02 runs:

  * `bulk-style-update` ("change every Button's backgroundColor") demanded
    `bulk_patch_component_props`. backgroundColor is a STYLE, so an agent
    correctly reaching for `bulk_patch_component_styles` was marked failed. It
    failed on all three runs for that reason.
  * `end-to-end-new-page` demanded `patch_component_styles`, though
    `add_components` now carries `style_properties` inline — and its composition
    group listed `add_component` / `replace_page_definition` while omitting
    `add_components`, the preferred tool. Run 2 failed with
    "none-of-group called" for exactly that.

Both are false negatives: the agent did the right thing and the oracle called it
a failure. That is the worst failure mode for a measurement instrument, because
it silently discredits good runs and hides real regressions in the noise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import bench_providers as bp  # noqa: E402  — path inserted above


def _call(name, args=None, ok=True):
    return bp.ToolCall(name, args or {}, ok)


def _conv(**kw):
    return bp.Conversation(name="t", description="", messages=["go"], **kw)


def _converged(conv, calls):
    metrics = bp.BenchMetrics(provider="deepseek", conversation="t")
    tool_calls = [(c.name, c.ok) for c in calls]
    return bp._convergence(conv, metrics, tool_calls, calls)


# ── the two bugs this exists to fix ────────────────────────────────────────


BG_ON_BUTTON = [{"effect": "sets_style", "property": "backgroundColor", "on_type": "Button"}]


def test_the_style_route_now_passes_where_it_used_to_fail():
    """bulk_patch_component_styles is the CORRECT tool for a background colour."""
    calls = [_call("bulk_patch_component_styles", {
        "page_name": "home", "filter": {"type": "Button"},
        "css_props": {"backgroundColor": "<Theme.primaryColor>"}})]
    ok, reason = _converged(_conv(must_achieve=BG_ON_BUTTON), calls)
    assert ok, reason


def test_the_route_the_old_oracle_demanded_does_not_achieve_it():
    """`bulk_patch_component_props` cannot set a background colour at all.

    Checked against nocode-ui: `backgroundColor` appears in the per-component
    `*StyleProperties.ts` files and ZERO times in `components/util/properties.ts`
    or `buttonProperties.ts`. It is styles-only. So the old oracle did not merely
    prefer one valid route over another — it demanded a route that cannot make
    the change, and would have passed a run that silently did nothing.
    """
    calls = [_call("bulk_patch_component_props", {
        "page_name": "home", "filter": {"type": "Button"},
        "properties": {"backgroundColor": "<Theme.primaryColor>"}})]
    ok, _ = _converged(_conv(must_achieve=BG_ON_BUTTON), calls)
    assert not ok


def test_an_outcome_oracle_must_not_just_bless_a_different_single_route():
    """Every route that genuinely sets the style has to count."""
    for tool, args in (
        ("bulk_patch_component_styles",
         {"filter": {"type": "Button"}, "css_props": {"backgroundColor": "red"}}),
        ("set_styles",
         {"component_key": "k", "style_properties": {"backgroundColor": "red"}}),
        ("add_components",
         {"components": [{"component_type": "Button",
                          "style_properties": {"backgroundColor": "red"}}]}),
        ("add_component",
         {"component_type": "Button", "style_properties": {"backgroundColor": "red"}}),
    ):
        ok, reason = _converged(_conv(must_achieve=BG_ON_BUTTON), [_call(tool, args)])
        assert ok, f"{tool}: {reason}"


def test_styles_set_inline_at_creation_count():
    """`add_components` carries style_properties; styling at birth is the better
    route and the old oracle failed it for not calling patch_component_styles."""
    calls = [_call("add_components", {"page_name": "home", "components": [
        {"component_type": "Grid", "properties": {}},
        {"component_type": "Button",
         "style_properties": {"backgroundColor": "<Theme.primaryColor>"}},
    ]})]
    ok, reason = _converged(_conv(must_achieve=BG_ON_BUTTON), calls)
    assert ok, reason


def test_add_components_satisfies_composition_without_being_named():
    """The group that omitted `add_components` is how run 2 failed."""
    calls = [_call("add_components", {"page_name": "p", "components": [
        {"component_type": "Grid"}]})]
    ok, reason = _converged(_conv(must_achieve=[{"effect": "adds_components"}]), calls)
    assert ok, reason


# ── it must still fail the things that should fail ─────────────────────────


def test_setting_a_different_property_does_not_satisfy_it():
    calls = [_call("bulk_patch_component_styles", {
        "filter": {"type": "Button"}, "css_props": {"color": "red"}})]
    ok, reason = _converged(_conv(must_achieve=BG_ON_BUTTON), calls)
    assert not ok
    assert "effects not achieved" in reason


def test_setting_it_on_the_wrong_type_does_not_satisfy_it():
    calls = [_call("bulk_patch_component_styles", {
        "filter": {"type": "Text"}, "css_props": {"backgroundColor": "red"}})]
    ok, _ = _converged(_conv(must_achieve=BG_ON_BUTTON), calls)
    assert not ok


def test_a_failed_call_achieves_nothing():
    """Trying the right thing and erroring is not the same as achieving it —
    the distinction the tool-name oracle could not make."""
    calls = [_call("bulk_patch_component_styles", {
        "filter": {"type": "Button"},
        "css_props": {"backgroundColor": "red"}}, ok=False)]
    ok, _ = _converged(_conv(must_achieve=BG_ON_BUTTON), calls)
    assert not ok


def test_doing_nothing_at_all_fails():
    ok, _ = _converged(_conv(must_achieve=BG_ON_BUTTON), [_call("list_pages")])
    assert not ok


def test_an_unknown_effect_name_fails_loudly_rather_than_passing():
    """A typo in the corpus must not silently make a conversation unfailable."""
    ok, reason = _converged(_conv(must_achieve=[{"effect": "sets_styel"}]), [_call("x")])
    assert not ok
    assert "unknown effect" in reason


# ── the deliberate permissive bias, documented ─────────────────────────────


def test_an_untypeable_target_is_accepted_rather_than_failed():
    """Patching by component_key hides the type. This oracle's failures have all
    been false negatives, so an unknowable type is 'cannot disprove', not a
    failure. A false pass is visible in the transcript; a false fail is not."""
    calls = [_call("patch_component_styles", {
        "page_name": "home", "component_key": "abc123",
        "css_props": {"backgroundColor": "red"}})]
    ok, reason = _converged(_conv(must_achieve=BG_ON_BUTTON), calls)
    assert ok, reason


# ── the rest of the vocabulary ─────────────────────────────────────────────


def test_creates_page_matches_by_name_case_insensitively():
    spec = [{"effect": "creates_page", "name": "ContactCFA"}]
    assert _converged(_conv(must_achieve=spec), [_call("create_page", {"name": "contactcfa"})])[0]
    assert not _converged(_conv(must_achieve=spec), [_call("create_page", {"name": "Other"})])[0]


def test_creates_page_also_sees_the_batched_tool():
    spec = [{"effect": "creates_page", "name": "ContactCFA"}]
    calls = [_call("create_pages", {"pages": [{"name": "home"}, {"name": "ContactCFA"}]})]
    assert _converged(_conv(must_achieve=spec), calls)[0]


def test_adds_components_can_require_a_type():
    spec = [{"effect": "adds_components", "type": "TextBox"}]
    yes = [_call("add_components", {"components": [{"component_type": "TextBox"}]})]
    no = [_call("add_components", {"components": [{"component_type": "Grid"}]})]
    assert _converged(_conv(must_achieve=spec), yes)[0]
    assert not _converged(_conv(must_achieve=spec), no)[0]


def test_authors_function_accepts_any_authoring_route():
    spec = [{"effect": "authors_function"}]
    for tool in ("create_page_event_function", "save_page_event_function_from_text",
                 "save_function_from_text", "add_step"):
        assert _converged(_conv(must_achieve=spec), [_call(tool)])[0], tool
    assert not _converged(_conv(must_achieve=spec), [_call("list_pages")])[0]


def test_screenshots_accepts_either_capture_tool():
    spec = [{"effect": "screenshots"}]
    assert _converged(_conv(must_achieve=spec), [_call("screenshot_page")])[0]
    assert _converged(_conv(must_achieve=spec), [_call("screenshot_external_url")])[0]


def test_called_is_the_escape_hatch_for_a_genuinely_specific_tool():
    spec = [{"effect": "called", "tool": "validate_page"}]
    assert _converged(_conv(must_achieve=spec), [_call("validate_page")])[0]
    assert not _converged(_conv(must_achieve=spec), [_call("get_page")])[0]


# ── coexistence with the old assertions ────────────────────────────────────


def test_route_and_outcome_assertions_both_apply():
    conv = _conv(must_call_tools=["get_page"], must_achieve=BG_ON_BUTTON)
    styled = _call("patch_component_styles", {"css_props": {"backgroundColor": "red"}})
    assert not _converged(conv, [styled])[0], "missing the required read"
    assert _converged(conv, [_call("get_page"), styled])[0]


def test_a_conversation_with_no_assertions_at_all_still_converges():
    assert _converged(_conv(), [_call("list_pages")])[0]


# ── the observer has to supply the arguments ───────────────────────────────


@pytest.mark.asyncio
async def test_observer_pairs_arguments_with_outcomes_by_id():
    obs = bp._make_observer()()
    await obs.emit_tool_start("add_components", {"page_name": "home"}, "tu_1")
    await obs.emit_tool_start("get_page", {"page_name": "login"}, "tu_2")
    # Results can come back in a different order than the starts — a parallel
    # batch dispatches concurrently.
    await obs.emit_tool_result("get_page", True, "ok", "tu_2")
    await obs.emit_tool_result("add_components", False, "boom", "tu_1")

    by_name = {c.name: c for c in obs.calls}
    assert by_name["add_components"].args == {"page_name": "home"}
    assert by_name["add_components"].ok is False
    assert by_name["get_page"].args == {"page_name": "login"}
    assert by_name["get_page"].ok is True
    # The legacy list stays intact for the metrics and the older tests.
    assert ("get_page", True) in obs.tool_calls


@pytest.mark.asyncio
async def test_a_result_with_no_matching_start_is_still_recorded():
    """Never drop a call: it must still count toward tool-name assertions."""
    obs = bp._make_observer()()
    await obs.emit_tool_result("mystery_tool", True, "ok", "unknown-id")
    assert obs.calls[0].name == "mystery_tool"
    assert obs.calls[0].args == {}


# ── no oracle verdict may trip the cascade breaker ─────────────────────────


def test_every_oracle_verdict_is_exempt_from_the_circuit_breaker():
    """Drives every failure path rather than restating the string list.

    The breaker exists to stop an auth/gateway cascade burning the corpus. An
    oracle verdict is a measurement, not an upstream fault, so it must classify
    as None. Adding "effects not achieved" without updating
    `_ORACLE_VERDICT_PREFIXES` immediately re-broke this: `end-to-end-new-page`
    was skipped as a cascade on its first run under the new oracle. Restating
    the list in a test would have missed it the same way, so this generates the
    verdicts instead.
    """
    cases = [
        _conv(must_call_tools=["never_called"]),
        _conv(must_call_any_of_groups=[["never_a", "never_b"]]),
        _conv(must_achieve=[{"effect": "screenshots"}]),
        _conv(must_succeed_on_kirun=True),
        _conv(must_succeed_on_kb_write=True),
    ]
    seen = set()
    for conv in cases:
        ok, reason = _converged(conv, [_call("list_pages")])
        assert not ok
        seen.add(reason.split(":")[0])
        assert bp._failure_class(reason) is None, (
            f"verdict {reason!r} would trip the circuit breaker and skip the "
            f"rest of the corpus"
        )
    assert len(seen) == len(cases), "each path should give a distinct verdict"


def test_a_genuine_upstream_fault_still_trips_it():
    assert bp._failure_class("AuthenticationError: 401") == "AuthenticationError"


# ── the real corpus ────────────────────────────────────────────────────────


def test_migrated_corpus_entries_declare_only_known_effects():
    corpus = bp._load_corpus(_SCRIPTS_DIR / "bench_corpus.yaml")
    migrated = [c for c in corpus if c.must_achieve]
    assert migrated, "the three proven-wrong conversations should be migrated"
    for conv in migrated:
        for spec in conv.must_achieve:
            assert spec["effect"] in bp._EFFECTS, f"{conv.name}: {spec}"


def test_bulk_style_update_would_now_pass_the_run_that_failed_it():
    """The exact shape of the call the agent made on 2026-09-02."""
    corpus = bp._load_corpus(_SCRIPTS_DIR / "bench_corpus.yaml")
    conv = next(c for c in corpus if c.name == "bulk-style-update")
    calls = [_call("bulk_patch_component_styles", {
        "page_name": "home", "filter": {"type": "Button"},
        "css_props": {"backgroundColor": "<Theme.primaryColor>"}})]
    ok, reason = _converged(conv, calls)
    assert ok, reason
