"""Persistent browser sessions must not outlive the thing that opened them.

Discovery (2026-09-02): three sequential bench runs left Chromium processes
alive, one spinning 31% CPU, holding the parent's stdout pipe open so the run
loop never advanced. The third run degenerated and had to be discarded.

Discovery (2026-09-15, dev): eighteen live Chromium instances in ai-server-blue,
twelve of them idle for three hours, ~3.7 GB PSS and about 85% of the
container's memory. Two causes, both now closed:

  - `drive_page` minted a NEW session per call, because `session_id` was an
    optional param the model always omitted. The session key is now derived
    from the chat session, so a conversation reuses one tab.
  - `_reap_idle_sessions` ran only INSIDE a tool call, so a worker whose
    conversation ended reaped nothing. It is now a browser-pool sweep hook on
    a timer, and a finished run releases its own sessions directly.

A session now holds a BrowserContext from the shared pool, not a browser of
its own, so closing one frees a tab and leaves the browser for the next caller.
"""

from __future__ import annotations

import pytest

from app.agents.appbuilder.tools.modlix import visuals_browser as vb
from app.services import browser_pool as bp


class _FakeContext:
    def __init__(self, log, name="ctx"):
        self._log = log
        self.name = name
        self.closed = False

    async def close(self):
        self.closed = True
        self._log.append(f"context.close:{self.name}")


def _session(sid, log):
    return vb.BrowserSession(session_id=sid, context=_FakeContext(log, sid), page=None)


@pytest.fixture(autouse=True)
def _clean_registry():
    vb._sessions.clear()
    yield
    vb._sessions.clear()


@pytest.mark.asyncio
async def test_close_all_closes_every_session_and_empties_the_registry():
    log = []
    for sid in ("a", "b", "c"):
        vb._sessions[sid] = _session(sid, log)
    closed = await vb.close_all_browser_sessions()
    assert closed == 3
    assert vb._sessions == {}
    # Closing the CONTEXT is what frees the tab and its renderer. The browser
    # deliberately survives: it is shared, and the pool decides when it goes.
    assert log.count("context.close:a") == 1
    assert log.count("context.close:b") == 1
    assert log.count("context.close:c") == 1


@pytest.mark.asyncio
async def test_close_all_on_an_empty_registry_is_a_no_op():
    assert await vb.close_all_browser_sessions() == 0


@pytest.mark.asyncio
async def test_one_broken_context_does_not_block_the_rest():
    """Shutdown must never hang on a context whose browser is already gone."""
    log = []
    good = _session("good", log)

    class _Exploding(_FakeContext):
        async def close(self):
            raise RuntimeError("browser already gone")

    bad = _session("bad", log)
    bad.context = _Exploding(log, "bad")
    vb._sessions["bad"] = bad
    vb._sessions["good"] = good

    assert await vb.close_all_browser_sessions() == 2
    assert vb._sessions == {}
    assert good.context.closed is True


@pytest.mark.asyncio
async def test_idle_sessions_are_reaped_and_fresh_ones_kept():
    import time as _t
    log = []
    fresh = _session("fresh", log)
    stale = _session("stale", log)
    stale.last_used = _t.monotonic() - (vb._session_idle_ttl() + 60)
    vb._sessions.update({"fresh": fresh, "stale": stale})
    reaped = await vb._reap_idle_sessions()
    assert reaped == ["stale"]
    assert set(vb._sessions) == {"fresh"}


@pytest.mark.asyncio
async def test_a_finished_run_releases_only_its_own_sessions():
    """The primary release path: the run ending is a fact, the TTL is a guess."""
    log = []
    mine_a, mine_b, theirs = _session("a", log), _session("b", log), _session("t", log)
    mine_a.owner_run = mine_b.owner_run = "chat-1"
    theirs.owner_run = "chat-2"
    vb._sessions.update({"a": mine_a, "b": mine_b, "t": theirs})

    assert await vb.close_sessions_for_run("chat-1") == 2
    assert set(vb._sessions) == {"t"}
    assert theirs.context.closed is False


@pytest.mark.asyncio
async def test_close_sessions_for_run_ignores_an_empty_id():
    log = []
    vb._sessions["x"] = _session("x", log)
    assert await vb.close_sessions_for_run("") == 0
    assert set(vb._sessions) == {"x"}


@pytest.mark.asyncio
async def test_session_cap_closes_the_least_recently_used():
    """Sessions stay strictly below the pool's context cap, so a conversation
    holding tabs can never starve a one-shot screenshot of a permit."""
    import time as _t
    from app.config import settings
    log = []
    original = settings.BROWSER_MAX_SESSIONS
    settings.BROWSER_MAX_SESSIONS = 2
    try:
        old, recent = _session("old", log), _session("recent", log)
        old.last_used = _t.monotonic() - 500
        recent.last_used = _t.monotonic()
        vb._sessions.update({"old": old, "recent": recent})
        await vb._enforce_session_cap()
        assert set(vb._sessions) == {"recent"}
        assert old.context.closed is True
    finally:
        settings.BROWSER_MAX_SESSIONS = original


def test_the_reaper_is_registered_as_a_pool_sweep_hook():
    """This is the fix for the three-hour strand: reaping on a timer, not only
    inside a tool call a dead conversation will never make."""
    assert vb._reap_idle_sessions in bp._sweep_hooks


def test_screenshot_page_reaps_on_entry():
    """A conversation that only screenshots would otherwise hold an idle session
    for the whole run, since drive_page was the only reap point."""
    import inspect
    src = inspect.getsource(vb._execute_screenshot_page)
    assert "_reap_idle_sessions" in src


def test_drive_page_keys_sessions_on_the_chat_session():
    """The model omits `session_id`; every omission used to mint a new browser."""
    import inspect
    src = inspect.getsource(vb._execute_drive_page)
    assert 'context.get("session_id")' in src
    assert "_enforce_session_cap" in src


def test_run_end_releases_browser_sessions():
    import inspect
    from app.core import run_manager
    assert "close_sessions_for_run" in inspect.getsource(run_manager.AgentRun._pump)


def test_lifespan_starts_pool_maintenance_and_tears_it_down():
    """The production case: a worker that exits on redeploy/restart/OOM."""
    import inspect
    from app import main
    src = inspect.getsource(main.lifespan)
    assert "close_all_browser_sessions" in src
    assert "start_maintenance" in src
    assert "browser_pool.close_all" in src


def test_bench_closes_sessions_between_conversations():
    """17 conversations x N runs is how the orphans accumulated in the first place."""
    import sys
    from pathlib import Path
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import inspect
    import bench_providers as bp_bench
    assert "close_all_browser_sessions" in inspect.getsource(bp_bench._run_one)
