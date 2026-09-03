"""A session must follow the app it is actually building.

Reconstructed from a real production failure, dev session `SYSTEM_d97c2efe`
(2026-09-03, "I want to build A CRM with leads, deals and WhatsApp follow-ups"):

  * The chat request opened from appbuilder's own page, so the session carried
    ``app_code="appbuilder"``.
  * The agent created the `crm` app and its pages, passing ``app_code`` each
    time, and those landed correctly.
  * Nothing moved the session's app, so the pre-flight grounding kept naming
    `appbuilder` and listing its `TestPage3`..`TestPage7`, and every later call
    that omitted the optional ``app_code`` resolved back to `appbuilder`.
  * One assistant message fired 13 parallel `patch_component_props` calls that
    all died on ``Page 'leads' not found in app 'appbuilder'``. 59 LLM calls and
    17 minutes later the user killed the run.

The same defect hit `SYSTEM_cd3e31d8` ("Create a page in marketingai", also
opened on appbuilder) and left `Page 'blog' not found in app 'appbuilder'` rows
from five weeks earlier, so it is a class of failure and not one bad run.

Three guarantees, one per fix:
  1. a write to a named app moves the focus, and the resolver prefers it;
  2. reads never move it, so studying another app cannot hijack the next edit;
  3. the grounding block is keyed by app, so the prompt cannot keep describing
     an app the tools have stopped writing to.
"""

from __future__ import annotations

import pytest

from app.core.session import AuthContext, BaseSession
from app.core.tools.base import ToolResult
from app.agents.appbuilder.tools._shared import (
    FOCUS_APP_KEY,
    SEEN_APPS_KEY,
    app_scope_hint,
    resolve_app_code,
)


def _agent():
    from app.agents.appbuilder.agent import AppBuilderAgent
    from app.agents.appbuilder.context import build_appbuilder_context
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    return AppBuilderAgent(
        context_builder=build_appbuilder_context(), tools=ALL_TOOLS, provider="deepseek",
    )


def _session(app_code: str = "appbuilder") -> BaseSession:
    """A session as the router builds one: request app on auth AND context."""
    s = BaseSession(agent_name="appbuilder")
    s.session_id = "SYSTEM_test"
    s.auth = AuthContext(
        token="t", client_code="SYSTEM", client_id=1, user_id=142, app_code=app_code,
    )
    s.context["app_code"] = app_code
    return s


def _ok() -> ToolResult:
    return ToolResult(success=True, summary="done")


def _fail(error: str) -> ToolResult:
    return ToolResult(success=False, error=error)


# ── 1. resolution order ──────────────────────────────────────────────────────


def test_explicit_app_code_always_wins():
    ctx = {FOCUS_APP_KEY: "crm", "app_code": "appbuilder"}
    assert resolve_app_code({"app_code": "leadzump"}, ctx) == "leadzump"


def test_focus_beats_the_request_app():
    """The regression itself: an omitted app_code must not fall to appbuilder."""
    ctx = {FOCUS_APP_KEY: "crm", "app_code": "appbuilder"}
    assert resolve_app_code({}, ctx) == "crm"


def test_request_app_used_when_no_focus_yet():
    assert resolve_app_code({}, {"app_code": "appbuilder"}) == "appbuilder"


def test_empty_when_nothing_is_set():
    assert resolve_app_code({}, {}) == ""


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_explicit_app_code_falls_through_to_focus(blank):
    """A whitespace argument is an omission, not an app named ' '."""
    ctx = {FOCUS_APP_KEY: "crm", "app_code": "appbuilder"}
    assert resolve_app_code({"app_code": blank}, ctx) == "crm"


def test_explicit_app_code_is_stripped():
    assert resolve_app_code({"app_code": "  crm  "}, {}) == "crm"


def test_non_dict_inputs_do_not_raise():
    assert resolve_app_code(None, {"app_code": "x"}) == "x"
    assert resolve_app_code({"app_code": "x"}, None) == "x"


# ── 2. what moves the focus ──────────────────────────────────────────────────


def test_create_app_moves_the_focus():
    """The exact step that used to leave the session pointing at appbuilder."""
    agent, s = _agent(), _session()
    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    assert s.context[FOCUS_APP_KEY] == "crm"


def test_create_page_in_another_app_moves_the_focus():
    """`SYSTEM_cd3e31d8`: no app was created, the user just named one."""
    agent, s = _agent(), _session()
    agent.note_tool_outcome(
        "create_page", {"app_code": "marketingai", "name": "landing"}, _ok(), s,
    )
    assert s.context[FOCUS_APP_KEY] == "marketingai"


def test_patch_moves_the_focus_too():
    agent, s = _agent(), _session()
    agent.note_tool_outcome(
        "patch_component_props", {"app_code": "crm", "page_name": "leads"}, _ok(), s,
    )
    assert s.context[FOCUS_APP_KEY] == "crm"


def test_reads_never_move_the_focus():
    """Reading leadzump's dashboard for reference must not redirect the next edit."""
    agent, s = _agent(), _session()
    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    for read in ("get_page", "list_pages", "get_app", "search_page_components",
                 "screenshot_page", "get_component_schema", "validate_page"):
        agent.note_tool_outcome(read, {"app_code": "leadzump"}, _ok(), s)
    assert s.context[FOCUS_APP_KEY] == "crm"


def test_failed_writes_do_not_move_the_focus():
    """A 404 against the wrong app is the symptom; it is not evidence of intent."""
    agent, s = _agent(), _session()
    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    agent.note_tool_outcome(
        "patch_component_props",
        {"app_code": "appbuilder", "page_name": "leads"},
        _fail("Page 'leads' not found in app 'appbuilder'."),
        s,
    )
    assert s.context[FOCUS_APP_KEY] == "crm"


def test_write_without_an_explicit_app_code_does_not_move_the_focus():
    """It resolved to the focus already; re-recording it would prove nothing."""
    agent, s = _agent(), _session()
    agent.note_tool_outcome("create_page", {"name": "home"}, _ok(), s)
    assert FOCUS_APP_KEY not in s.context


def test_pure_compute_helpers_are_excluded():
    """`build_authority` and the asset-URL builders write nothing."""
    agent, s = _agent(), _session()
    for name in ("build_authority", "build_static_asset_url",
                 "build_secured_asset_url", "generate_image"):
        agent.note_tool_outcome(name, {"app_code": "leadzump"}, _ok(), s)
    assert FOCUS_APP_KEY not in s.context


def test_focus_follows_the_latest_write():
    agent, s = _agent(), _session()
    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    agent.note_tool_outcome("create_page", {"app_code": "shop"}, _ok(), s)
    assert s.context[FOCUS_APP_KEY] == "shop"


def test_every_written_app_is_remembered_in_order():
    agent, s = _agent(), _session()
    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    agent.note_tool_outcome("create_page", {"app_code": "crm"}, _ok(), s)
    agent.note_tool_outcome("create_page", {"app_code": "shop"}, _ok(), s)
    assert s.context[SEEN_APPS_KEY] == ["crm", "shop"]


def test_hook_never_raises_on_odd_input():
    agent, s = _agent(), _session()
    for bad in (None, "a string", 42, []):
        agent.note_tool_outcome("create_app", bad, _ok(), s)
    agent.note_tool_outcome("create_app", {"app_code": 7}, _ok(), s)
    assert FOCUS_APP_KEY not in s.context


# ── 3. the whole round trip ──────────────────────────────────────────────────


def test_the_crm_session_replayed():
    """End to end over the real sequence, asserting where each call would land."""
    agent, s = _agent(), _session("appbuilder")

    # Before anything is built, the request app is correct: there is no CRM yet.
    assert resolve_app_code({}, agent.build_tool_context(s)) == "appbuilder"

    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    agent.note_tool_outcome(
        "create_pages", {"app_code": "crm", "pages": ["home", "leads"]}, _ok(), s,
    )

    # The call that used to 404. It now lands in crm without the argument.
    ctx = agent.build_tool_context(s)
    assert resolve_app_code({"page_name": "leads"}, ctx) == "crm"
    # And the prompt agrees with the dispatcher.
    assert agent._effective_app_code(s) == "crm"


def test_build_tool_context_carries_focus_and_seen_apps():
    agent, s = _agent(), _session()
    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    ctx = agent.build_tool_context(s)
    assert ctx[FOCUS_APP_KEY] == "crm"
    assert ctx[SEEN_APPS_KEY] == ["crm"]
    # The request app stays available; the resolver just ranks it lower.
    assert ctx["app_code"] == "appbuilder"


def test_effective_app_code_before_any_write_is_the_request_app():
    agent, s = _agent(), _session("marketingai")
    assert agent._effective_app_code(s) == "marketingai"


# ── 4. the cross-app hint on a genuine miss ──────────────────────────────────


def test_hint_names_the_other_apps():
    hint = app_scope_hint({SEEN_APPS_KEY: ["crm", "shop"]}, "appbuilder")
    assert "'crm'" in hint and "'shop'" in hint
    assert "app_code" in hint


def test_no_hint_when_the_searched_app_is_the_only_one_written():
    assert app_scope_hint({SEEN_APPS_KEY: ["crm"]}, "crm") == ""


def test_no_hint_before_anything_is_written():
    assert app_scope_hint({SEEN_APPS_KEY: []}, "appbuilder") == ""
    assert app_scope_hint({}, "appbuilder") == ""


def test_annotate_appends_the_hint_to_a_not_found_error():
    agent, s = _agent(), _session()
    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    note = agent.annotate_tool_error(
        "patch_component_props",
        {"page_name": "leads"},
        _fail("Page 'leads' not found in app 'appbuilder'."),
        s,
    )
    assert note and "'crm'" in note


def test_annotate_is_silent_on_unrelated_errors():
    agent, s = _agent(), _session()
    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    assert agent.annotate_tool_error(
        "patch_component_props", {}, _fail("`page_name` is required"), s,
    ) is None


def test_annotate_is_silent_when_the_miss_is_in_the_only_app_written():
    agent, s = _agent(), _session()
    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    assert agent.annotate_tool_error(
        "patch_component_props", {},
        _fail("Page 'ghost' not found in app 'crm'."), s,
    ) is None


# ── 5. grounding must not outlive the app it describes ───────────────────────


@pytest.mark.asyncio
async def test_grounding_is_refetched_when_the_focus_moves(monkeypatch):
    """The cache used to be `if isinstance(cached, str): return cached`, forever."""
    agent, s = _agent(), _session("appbuilder")
    fetched: list[str] = []

    async def fake_fetch(_session, app_code):
        fetched.append(app_code)
        return {"appCode": app_code}, [f"{app_code}Page"]

    monkeypatch.setattr(agent, "_fetch_grounding", fake_fetch)

    first = await agent._build_preflight_grounding(s)
    assert "appbuilder" in first
    # Cached: a second call in the same app must not refetch.
    assert await agent._build_preflight_grounding(s) == first
    assert fetched == ["appbuilder"]

    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    second = await agent._build_preflight_grounding(s)
    assert fetched == ["appbuilder", "crm"]
    assert "crm" in second
    assert "appbuilder" not in second


@pytest.mark.asyncio
async def test_note_tool_outcome_evicts_the_cached_grounding():
    agent, s = _agent(), _session()
    s.context["_preflight_grounding"] = "stale block naming appbuilder"
    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    assert "_preflight_grounding" not in s.context


@pytest.mark.asyncio
async def test_grounding_survives_a_write_to_the_same_app(monkeypatch):
    """Only a genuine move should cost a refetch."""
    agent, s = _agent(), _session("crm")
    calls: list[str] = []

    async def fake_fetch(_session, app_code):
        calls.append(app_code)
        return {"appCode": app_code}, ["home"]

    monkeypatch.setattr(agent, "_fetch_grounding", fake_fetch)

    await agent._build_preflight_grounding(s)
    agent.note_tool_outcome("create_page", {"app_code": "crm"}, _ok(), s)
    await agent._build_preflight_grounding(s)
    assert calls == ["crm"]


# ── 6. drift protection ──────────────────────────────────────────────────────


def test_every_app_scoped_mutating_tool_is_classified():
    """A new write tool must be a deliberate decision, not an oversight.

    The default for an unclassified tool is "does not move focus", which is the
    safe direction but silently reintroduces the bug for that tool. This fails
    when a mutating, app-scoped tool is in neither the focus set nor the
    explicit exclusion list.
    """
    import re

    from app.agents.appbuilder.agent import _FOCUS_MOVING_TOOLS
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    # Take an `app_code` but write nothing: they compute a string or a file.
    excluded = {
        "build_authority", "build_static_asset_url", "build_secured_asset_url",
        "generate_image",
    }
    mutating = re.compile(
        r"^(create|update|patch|add|set|delete|remove|save|move|rename|replace"
        r"|reset|bulk|copy|apply|assign|grant|commit|upload|make|configure"
        r"|import|merge|build|publish|discard)(_|$)"
    )
    app_scoped = {
        t.name for t in ALL_TOOLS if any(p.name == "app_code" for p in t.parameters)
    }
    unclassified = sorted(
        n for n in app_scoped
        if mutating.match(n) and n not in _FOCUS_MOVING_TOOLS and n not in excluded
    )
    assert not unclassified, (
        f"{len(unclassified)} app-scoped mutating tool(s) are unclassified: "
        f"{unclassified}. Add each to _FOCUS_MOVING_TOOLS in "
        "app/agents/appbuilder/agent.py, or to `excluded` here if it writes "
        "nothing that belongs to an app."
    )


def test_no_tool_bypasses_the_shared_resolver():
    """Every app_code fallback must go through `resolve_app_code`.

    Ten named `_resolve_app_code` copies plus eight inline ones all had the same
    `params.get("app_code") or context.get("app_code")` body, so a fix applied
    to one would have left the rest on the old behaviour.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent / "app"
    offenders = [
        f"{p.relative_to(root.parent)}:{i}"
        for p in root.rglob("*.py")
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if 'params.get("app_code") or context.get("app_code"' in line
    ]
    assert not offenders, (
        "These sites resolve app_code inline and so ignore the session focus: "
        f"{offenders}. Call tools._shared.resolve_app_code instead."
    )


# ── 7. the dispatcher actually calls both hooks ──────────────────────────────


@pytest.mark.asyncio
async def test_execute_tool_records_focus_and_annotates(monkeypatch):
    """Both hooks must fire from `BaseAgent._execute_tool`, not just exist.

    Drives the real dispatch path with a stub tool so a future refactor that
    drops either call is caught, rather than only the hook bodies being tested.
    """
    from app.core.tools.base import ToolDefinition, ToolParameter

    agent, s = _agent(), _session()
    outcomes: list[dict] = []

    async def _run(params, _context):
        outcomes.append(params)
        if params.get("fail"):
            return _fail("Page 'leads' not found in app 'appbuilder'.")
        return _ok()

    agent.tools["create_app"] = ToolDefinition(
        name="create_app", description="stub",
        parameters=[
            ToolParameter(name="app_code", type="string", description="target app"),
            ToolParameter(
                name="fail", type="boolean", required=False, description="force a miss",
            ),
        ],
        execute=_run,
    )
    # The gate would demand a schema fetch first; this call is about the hooks.
    monkeypatch.setattr(agent, "_gate_deferred_dispatch", lambda *a, **k: None)

    await agent._execute_tool("create_app", {"app_code": "crm"}, s)
    assert s.context[FOCUS_APP_KEY] == "crm"

    failed = await agent._execute_tool(
        "create_app", {"app_code": "appbuilder", "fail": True}, s,
    )
    assert "not found in app 'appbuilder'" in failed.error
    assert "'crm'" in failed.error, "the cross-app hint was not appended"


@pytest.mark.asyncio
async def test_a_broken_hook_cannot_fail_a_working_tool(monkeypatch):
    from app.core.tools.base import ToolDefinition

    agent, s = _agent(), _session()

    async def _run(_params, _context):
        return _ok()

    agent.tools["noop_tool"] = ToolDefinition(
        name="noop_tool", description="stub", parameters=[], execute=_run,
    )
    monkeypatch.setattr(agent, "_gate_deferred_dispatch", lambda *a, **k: None)

    def _boom(*_a, **_k):
        raise RuntimeError("hook is broken")

    monkeypatch.setattr(agent, "note_tool_outcome", _boom)
    monkeypatch.setattr(agent, "annotate_tool_error", _boom)

    result = await agent._execute_tool("noop_tool", {}, s)
    assert result.success is True


# ── 8. everything else that is scoped per app ────────────────────────────────


def test_kb_scope_follows_the_focus():
    """Notes taken while building `crm` must not land in appbuilder's KB."""
    from app.agents.appbuilder.tools.kb_app import _resolve_tenant

    agent, s = _agent(), _session()
    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    ctx = agent.build_tool_context(s)
    ctx["auth"] = s.auth
    _client, app_code, err = _resolve_tenant(ctx)
    assert err is None
    assert app_code == "crm"


def test_lore_scope_follows_the_focus():
    """Lore is knowledge about an app, so it follows the app being built."""
    from app.services.lore.tools import _tenant

    agent, s = _agent(), _session()
    agent.note_tool_outcome("create_app", {"app_code": "crm"}, _ok(), s)
    ctx = agent.build_tool_context(s)
    ctx["auth"] = s.auth
    _client, app_code, err = _tenant(ctx)
    assert err is None
    assert app_code == "crm"


def test_session_app_code_helper():
    """The core-side resolver the run loop's lore hooks use."""
    from app.core.session import session_app_code

    s = _session("appbuilder")
    assert session_app_code(s) == "appbuilder"
    s.context[FOCUS_APP_KEY] = "crm"
    assert session_app_code(s) == "crm"
    s.context[FOCUS_APP_KEY] = "   "
    assert session_app_code(s) == "appbuilder"


def test_core_and_tools_agree_on_the_key():
    """One definition, re-exported. Two literals would drift apart silently."""
    from app.core.session import FOCUS_APP_KEY as core_key
    from app.core.session import SEEN_APPS_KEY as core_seen

    assert FOCUS_APP_KEY is core_key
    assert SEEN_APPS_KEY is core_seen


def test_every_module_resolver_honours_the_focus():
    """Each per-module `_resolve_app_code` must delegate, not keep its own copy.

    Ten modules each carried an identical private resolver. Fixing one and
    missing the others is exactly how this bug would come back, so assert the
    behaviour through each real entry point rather than through the shared
    helper only.
    """
    import importlib

    ctx = {FOCUS_APP_KEY: "crm", "app_code": "appbuilder"}
    # (module, resolver name, returns a plain string rather than a tuple)
    resolvers = [
        ("modlix.app_admin", "_resolve_app_code", True),
        ("modlix.messaging", "_resolve_app_code", True),
        ("modlix.runtime", "_resolve_app_code", True),
        ("modlix.draft_tools", "_app_code", True),
        ("modlix.pages", "_resolve_app_code", False),
        ("modlix.kirun", "_resolve_app_code", False),
        ("modlix.kirun_events", "_resolve_app_code", False),
        ("modlix.schemas", "_resolve_app_code", False),
        ("modlix.visuals", "_resolve_app_code", False),
        ("crud._handlers", "_resolve_app_code", False),
        ("crud.page_ops", "_resolve_app_code", False),
    ]
    wrong = []
    for mod_name, fn_name, is_plain in resolvers:
        mod = importlib.import_module(f"app.agents.appbuilder.tools.{mod_name}")
        out = getattr(mod, fn_name)({}, ctx)
        got = out if is_plain else out[0]
        if got != "crm":
            wrong.append(f"{mod_name}.{fn_name} -> {got!r}")
    assert not wrong, (
        "These resolvers ignore the session focus and would send an omitted "
        f"app_code back to the request app: {wrong}"
    )


# ── 9. an explicit app switch beats an inferred focus ────────────────────────


def test_app_switch_clears_the_focus():
    """Opening a different app in the workspace must win over earlier writes.

    Without this, a session that built `crm` would keep writing to `crm` after
    the user navigated to `cxapp` — trading the original bug for a worse one,
    since the edits would land in an app the user is not even looking at.
    """
    s = _session("cxapp")            # this request: cxapp
    s.context[FOCUS_APP_KEY] = "crm"  # restored from the DB
    s.context["_preflight_grounding"] = "block describing crm"
    s.context["_preflight_grounding_app"] = "crm"
    s._clear_focus_on_app_switch("appbuilder")   # previous request: appbuilder
    assert FOCUS_APP_KEY not in s.context
    assert "_preflight_grounding" not in s.context
    from app.core.session import session_app_code
    assert session_app_code(s) == "cxapp"


def test_same_app_on_the_next_turn_keeps_the_focus():
    """Turn two of "build me a CRM" is asked from the same page, so nothing moved."""
    s = _session("appbuilder")
    s.context[FOCUS_APP_KEY] = "crm"
    s._clear_focus_on_app_switch("appbuilder")
    assert s.context[FOCUS_APP_KEY] == "crm"


def test_a_request_with_no_app_code_keeps_the_focus():
    s = _session("appbuilder")
    del s.context["app_code"]
    s.context[FOCUS_APP_KEY] = "crm"
    s._clear_focus_on_app_switch("appbuilder")
    assert s.context[FOCUS_APP_KEY] == "crm"


def test_first_turn_of_a_session_keeps_the_focus():
    """No prior request app means nothing to compare, so nothing to invalidate."""
    s = _session("appbuilder")
    s.context[FOCUS_APP_KEY] = "crm"
    s._clear_focus_on_app_switch(None)
    assert s.context[FOCUS_APP_KEY] == "crm"
