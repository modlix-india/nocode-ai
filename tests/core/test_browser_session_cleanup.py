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
async def test_session_cap_reclaims_a_finished_run_first(monkeypatch):
    """Past the cap we take back tabs nobody is waiting on."""
    import time as _t
    from app.config import settings
    log = []
    monkeypatch.setattr(settings, "BROWSER_MAX_SESSIONS", 2)
    monkeypatch.setattr(vb, "_run_is_live", lambda run: run == "live-run")

    done, live = _session("done", log), _session("live", log)
    done.owner_run, live.owner_run = "dead-run", "live-run"
    done.last_used = live.last_used = _t.monotonic()
    vb._sessions.update({"done": done, "live": live})

    await vb._make_room_for_session()
    assert set(vb._sessions) == {"live"}
    assert done.context.closed is True


@pytest.mark.asyncio
async def test_session_cap_never_evicts_another_live_conversation(monkeypatch):
    """The multi-user case, and the reason plain LRU was wrong.

    Four people share a worker (gunicorn runs four for any number of users).
    One of them is waiting on an LLM turn, so their session looks idle. Closing
    it would drop their cookies, their logged-in end-user identity and their
    scroll position, and their next drive_page would silently get a blank
    anonymous tab. Going one over the soft cap is the cheaper mistake.
    """
    import time as _t
    from app.config import settings
    log = []
    monkeypatch.setattr(settings, "BROWSER_MAX_SESSIONS", 3)
    monkeypatch.setattr(vb, "_run_is_live", lambda run: True)  # everyone is mid-conversation

    now = _t.monotonic()
    for name, age in (("userA", 90), ("userB", 40), ("userC", 5)):
        s = _session(name, log)
        s.owner_run = f"chat-{name}"
        s.last_used = now - age
        vb._sessions[name] = s

    await vb._make_room_for_session()

    assert set(vb._sessions) == {"userA", "userB", "userC"}, \
        "a live conversation's tab was taken to make room"
    assert all(not s.context.closed for s in vb._sessions.values())


@pytest.mark.asyncio
async def test_session_cap_falls_back_to_idle_when_all_runs_are_live(monkeypatch):
    import time as _t
    from app.config import settings
    log = []
    monkeypatch.setattr(settings, "BROWSER_MAX_SESSIONS", 2)
    monkeypatch.setattr(settings, "BROWSER_SESSION_IDLE_TTL_SECONDS", 60)
    monkeypatch.setattr(vb, "_run_is_live", lambda run: True)

    now = _t.monotonic()
    stale, fresh = _session("stale", log), _session("fresh", log)
    stale.owner_run = fresh.owner_run = "still-running"
    stale.last_used = now - 900          # past the TTL: that conversation moved on
    fresh.last_used = now - 5
    vb._sessions.update({"stale": stale, "fresh": fresh})

    await vb._make_room_for_session()
    assert set(vb._sessions) == {"fresh"}
    assert stale.context.closed is True


@pytest.mark.asyncio
async def test_a_full_worker_serves_drive_page_on_an_ephemeral_tab(monkeypatch):
    """The 40-user case: every session slot is held by someone else's live
    conversation. The call is served on a throwaway tab rather than failed, so
    the render still happens and only the carried-over state is lost."""
    log = []
    calls = []

    async def fake_open_context(profile, *, persistent=False, timeout=None, **kw):
        calls.append(persistent)
        if persistent:
            raise bp.BrowserUnavailable("no persistent browser session slot free")
        return _FakeContext(log, "ephemeral")

    monkeypatch.setattr(bp, "open_context", fake_open_context)

    class _Page:
        async def goto(self, *a, **k): pass
        def on(self, *a, **k): pass

    async def fake_new_page():
        return _Page()

    monkeypatch.setattr(_FakeContext, "new_page", staticmethod(fake_new_page), raising=False)

    sess, err = await vb._new_session(
        "sid", "app", "SYSTEM", "home", None, 1440, 900, False, False, persistent=True)
    assert sess is None and "session slot" in err

    sess, err = await vb._new_session(
        "sid", "app", "SYSTEM", "home", None, 1440, 900, False, False, persistent=False)
    assert sess is not None and err is None
    assert sess.ephemeral is True, "a fallback tab must be marked throwaway"
    assert calls == [True, False]


def test_drive_page_falls_back_instead_of_failing():
    import inspect
    src = inspect.getsource(vb._execute_drive_page)
    assert "persistent=False" in src, "no ephemeral fallback on a full worker"
    assert "if sess.ephemeral" in src, "ephemeral tab is never released"
    assert "if not sess.ephemeral" in src, "ephemeral tab must not be registered"


def test_run_liveness_defaults_to_live_when_unknowable(monkeypatch):
    """Guessing 'dead' costs a user their tab; guessing 'live' costs a little RAM."""
    import app.core.run_manager as rm

    def _boom(_sid):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(rm, "get_local_run", _boom)
    assert vb._run_is_live("some-run") is True
    assert vb._run_is_live("") is False
    assert vb._run_is_live(None) is False


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
    assert "_make_room_for_session" in src


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
