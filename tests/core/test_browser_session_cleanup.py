"""Persistent Playwright sessions must not outlive the process.

Discovery (2026-09-02): three sequential bench runs left Chromium processes
alive, one spinning 31% CPU, and they held the parent's stdout pipe open so the
run loop never advanced. The third run degenerated (shopkeep 5 turns instead of
~50, clone-linear 0 turns) and had to be discarded.

Cause: `_reap_idle_sessions` runs only INSIDE a tool call, so once a process
stops taking calls nothing reaps anything. `drive_page` is the only creator of
persistent sessions, and there was no shutdown path at all.
"""

from __future__ import annotations

import pytest

from app.agents.appbuilder.tools.modlix import visuals_browser as vb


class _FakeBrowser:
    def __init__(self, log): self._log = log; self.closed = False
    async def close(self): self.closed = True; self._log.append("browser.close")


class _FakePlaywright:
    def __init__(self, log): self._log = log; self.stopped = False
    async def stop(self): self.stopped = True; self._log.append("pw.stop")


def _session(sid, log):
    return vb.BrowserSession(
        session_id=sid, playwright=_FakePlaywright(log), browser=_FakeBrowser(log),
        context=None, page=None,
    )


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
    # Both halves matter: closing the browser without stopping the driver leaves
    # the node process, and stopping the driver alone can orphan Chromium.
    assert log.count("browser.close") == 3
    assert log.count("pw.stop") == 3


@pytest.mark.asyncio
async def test_close_all_on_an_empty_registry_is_a_no_op():
    assert await vb.close_all_browser_sessions() == 0


@pytest.mark.asyncio
async def test_one_broken_browser_does_not_block_the_rest():
    """Shutdown must never hang on a browser that is already gone."""
    log = []
    good = _session("good", log)

    class _Exploding(_FakeBrowser):
        async def close(self): raise RuntimeError("browser already gone")

    bad = _session("bad", log)
    bad.browser = _Exploding(log)
    vb._sessions["bad"] = bad
    vb._sessions["good"] = good

    assert await vb.close_all_browser_sessions() == 2
    assert vb._sessions == {}
    assert good.browser.closed is True


@pytest.mark.asyncio
async def test_idle_sessions_are_reaped_and_fresh_ones_kept():
    import time as _t
    log = []
    fresh = _session("fresh", log)
    stale = _session("stale", log)
    stale.last_used = _t.monotonic() - (vb._SESSION_IDLE_TTL_SECONDS + 60)
    vb._sessions.update({"fresh": fresh, "stale": stale})
    reaped = await vb._reap_idle_sessions()
    assert reaped == ["stale"]
    assert set(vb._sessions) == {"fresh"}


def test_screenshot_page_reaps_on_entry():
    """A conversation that only screenshots would otherwise hold an idle session
    for the whole run, since drive_page was the only reap point."""
    import inspect
    src = inspect.getsource(vb._execute_screenshot_page)
    assert "_reap_idle_sessions" in src


def test_lifespan_shutdown_closes_browser_sessions():
    """The production case: a worker that exits on redeploy/restart/OOM."""
    import inspect
    from app import main
    src = inspect.getsource(main.lifespan)
    assert "close_all_browser_sessions" in src


def test_bench_closes_sessions_between_conversations():
    """17 conversations x N runs is how the orphans accumulated in the first place."""
    import sys
    from pathlib import Path
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import inspect
    import bench_providers as bp
    assert "close_all_browser_sessions" in inspect.getsource(bp._run_one)
