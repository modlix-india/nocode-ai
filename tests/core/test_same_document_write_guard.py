"""A parallel batch must not put two writes on one document.

Context: fixing the stream assembler (see test_parallel_tool_stream_assembly.py)
made batches genuinely dispatch through `asyncio.gather`. That un-masked a hazard
the bug had been hiding: `_load_save` in tools/modlix/pages.py is fetch → mutate
→ PUT with no version check, and `save_page` PUTs the whole document. Two writes
to one page in the same batch therefore both read the same version and the later
save silently discards the earlier edit.

The persona now tells the model not to batch same-page writes. These tests are
the guarantee behind that instruction, because a prompt is not a safety
mechanism. The guard is deliberately narrow: it serialises ONLY a genuine
same-document pair, so the batching win survives on the case the persona
actively encourages (patches to different pages in one message).
"""

from __future__ import annotations

import pytest

from app.core.agent import BaseAgent


def _agent():
    from app.agents.appbuilder.agent import AppBuilderAgent
    from app.agents.appbuilder.context import build_appbuilder_context
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    return AppBuilderAgent(
        context_builder=build_appbuilder_context(), tools=ALL_TOOLS, provider="deepseek",
    )


def _batch(*specs):
    return [{"name": n, "id": f"t{i}", "input": i_} for i, (n, i_) in enumerate(specs)]


# ── the hazard itself ───────────────────────────────────────────────────────


def test_two_writes_to_the_same_page_collide():
    a = _agent()
    hit = a._batch_write_collision(_batch(
        ("add_components", {"page_name": "home", "app_code": "x"}),
        ("patch_component_props", {"page_name": "home", "app_code": "x"}),
    ))
    assert hit == "page:home:x"


def test_a_page_event_function_write_collides_with_page_composition():
    """Page event functions live INSIDE the page document, so they race with it."""
    a = _agent()
    assert a._batch_write_collision(_batch(
        ("add_components", {"page_name": "home"}),
        ("save_page_event_function_from_text", {"page_name": "home"}),
    )) == "page:home:"


def test_update_page_and_a_component_patch_are_the_same_document():
    a = _agent()
    assert a._batch_write_collision(_batch(
        ("update_page", {"name": "home"}),
        ("remove_component", {"page_name": "home"}),
    )) is not None


def test_unresolvable_identity_serialises_rather_than_racing():
    """Two same-family writes whose target can't be read must NOT run parallel."""
    a = _agent()
    assert a._batch_write_collision(_batch(
        ("add_components", {}),
        ("patch_component_props", {}),
    )) == "page:*:"


# ── the batching win must survive ───────────────────────────────────────────


def test_writes_to_different_pages_still_run_in_parallel():
    a = _agent()
    assert a._batch_write_collision(_batch(
        ("add_components", {"page_name": "home", "app_code": "x"}),
        ("add_components", {"page_name": "login", "app_code": "x"}),
    )) is None


def test_the_same_page_name_in_two_apps_is_two_documents():
    a = _agent()
    assert a._batch_write_collision(_batch(
        ("add_components", {"page_name": "home", "app_code": "one"}),
        ("add_components", {"page_name": "home", "app_code": "two"}),
    )) is None


def test_many_reads_plus_one_write_is_not_a_collision():
    a = _agent()
    reads = [("get_page", {"page_name": f"p{i}"}) for i in range(6)]
    assert a._batch_write_collision(_batch(*reads, ("add_components", {"page_name": "home"}))) is None


def test_reads_never_produce_a_key():
    a = _agent()
    for name in ("get_page", "list_pages", "screenshot_page", "get_component", "validate_page"):
        assert a.write_conflict_key(name, {"page_name": "home"}) is None, name


def test_the_same_name_in_different_families_does_not_collide():
    """update_page(name='home') and update_theme(name='home') are different docs."""
    a = _agent()
    assert a._batch_write_collision(_batch(
        ("update_page", {"name": "home"}),
        ("update_theme", {"name": "home"}),
    )) is None


def test_creates_are_not_guarded():
    """Two creates of one name is a loud backend conflict, not a silent lost edit."""
    a = _agent()
    assert a.write_conflict_key("create_page", {"name": "home"}) is None
    assert a.write_conflict_key("create_theme", {"name": "dark"}) is None


# ── contract + drift ───────────────────────────────────────────────────────


def test_base_agent_default_serialises_nothing():
    """Core holds no table of another layer's tools, so it must opt out cleanly."""
    assert BaseAgent.write_conflict_key(None, "add_components", {"page_name": "home"}) is None


def test_every_guarded_tool_is_actually_registered():
    """A table naming a tool that does not exist guards nothing and misleads."""
    from app.agents.appbuilder.agent import _RMW_TOOLS
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    registered = {t.name for t in ALL_TOOLS}
    missing = sorted(n for n in _RMW_TOOLS if n not in registered)
    assert not missing, f"_RMW_TOOLS names unregistered tools: {missing}"


def test_every_load_save_page_tool_is_guarded():
    """The page tools that read-modify-write MUST all be in the table.

    Pinned explicitly: a new page-mutating tool added without a table entry is
    exactly how this hazard comes back, and it comes back silently.
    """
    from app.agents.appbuilder.agent import _RMW_TOOLS

    must_be_guarded = {
        "add_component", "add_components", "patch_component_props",
        "patch_component_styles", "bulk_patch_component_props",
        "bulk_patch_component_styles", "remove_component", "move_component",
        "rename_component", "set_styles", "set_bindings",
        "patch_component_bindings", "update_component_props",
        "remove_component_styles", "update_page", "replace_page_definition",
        "reset_page_composition",
    }
    assert not (must_be_guarded - set(_RMW_TOOLS))


def test_a_non_dict_input_does_not_crash_and_stays_conservative():
    a = _agent()
    assert a.write_conflict_key("add_components", "not a dict") == "page:*:*"
