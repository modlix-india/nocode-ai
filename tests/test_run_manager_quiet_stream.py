"""A quiet run must not look like a dead one.

Prod incident 2026-09-17, session HHARS1_984fdbf5: the subscriber blocked on
XREAD for KEEPALIVE_S (15s) while the Redis client's socket timeout was 5s, so
any pause longer than five seconds raised TimeoutError, hit a broad `except`,
and ended the stream with "Lost contact with the running agent." The agent is
quiet for longer than five seconds on most turns (one LLM turn in that session
took 19.5s), so this fired constantly.
"""

import asyncio

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core import run_manager
from app.core.run_manager import KEEPALIVE_S, _XREAD_BLOCK_S
from app.core.streaming import AgentEventType
from app.services.redis_client import SOCKET_TIMEOUT_S


class TestTimingInvariant:
    def test_block_window_stays_inside_the_socket_timeout(self):
        """The regression itself. If this fails, every quiet stretch longer than
        the socket timeout will kill a live stream again."""
        assert _XREAD_BLOCK_S < SOCKET_TIMEOUT_S

    def test_block_window_is_positive(self):
        assert _XREAD_BLOCK_S >= 1.0

    def test_keepalive_cadence_is_unchanged_for_clients(self):
        """Polling got faster; what the client sees must not."""
        assert KEEPALIVE_S == 15.0


class _FakeRedis:
    """Minimal stand-in for the bits _subscribe_remote touches."""

    def __init__(self, xread_results):
        self._xread_results = list(xread_results)
        self.blocks_seen = []

    async def xrange(self, _key):
        return []

    async def xread(self, _streams, count=None, block=None):
        self.blocks_seen.append(block)
        if not self._xread_results:
            raise AssertionError("xread called more times than the test scripted")
        nxt = self._xread_results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


async def _drain(gen, limit=40):
    out = []
    async for ev in gen:
        out.append(ev)
        if len(out) >= limit:
            break
    return out


def _done_entry():
    return [("ai:run:s", [("1-1", {"event": "done", "data": "{}"})])]


def _text_entry():
    return [("ai:run:s", [("1-0", {"event": "text", "data": '{"text": "hi"}'})])]


@pytest.mark.asyncio
class TestQuietStream:
    async def _drive(self, monkeypatch, xread_results, *, clock_step=0.1):
        """Run the real _subscribe_remote against a scripted Redis."""
        fake = _FakeRedis(xread_results)
        import app.services.redis_client as rc

        async def _get_redis_client():
            return fake

        async def _meta(_sid):
            return {"status": "running"}

        monkeypatch.setattr(rc, "get_redis_client", _get_redis_client)
        monkeypatch.setattr(run_manager, "_read_remote_meta", _meta)

        t = {"now": 0.0}

        def _clock():
            t["now"] += clock_step
            return t["now"]

        monkeypatch.setattr(run_manager.time, "monotonic", _clock)

        gen = run_manager._subscribe_remote("s", {"status": "running", "run_id": "r1"})
        return fake, await _drain(gen)

    async def test_socket_timeout_does_not_end_the_stream(self, monkeypatch):
        """The regression. Two socket timeouts then real events: the stream must
        carry on and deliver them, with no ERROR and no early return."""
        _, events = await self._drive(
            monkeypatch,
            [
                RedisTimeoutError("timed out"),
                RedisTimeoutError("timed out"),
                _text_entry(),
                _done_entry(),
            ],
        )
        kinds = [e.event for e in events]
        assert AgentEventType.ERROR not in kinds
        assert AgentEventType.TEXT in kinds
        assert kinds[-1] == AgentEventType.DONE

    async def test_a_real_failure_still_ends_the_stream(self, monkeypatch):
        """Only the timeout was misclassified; other errors stay fatal."""
        _, events = await self._drive(
            monkeypatch, [ConnectionResetError("gone")]
        )
        assert events[-1].event == AgentEventType.ERROR
        assert "Lost contact" in events[-1].data["message"]

    async def test_block_passed_to_redis_is_the_safe_window(self, monkeypatch):
        fake, _ = await self._drive(monkeypatch, [_done_entry()])
        assert fake.blocks_seen
        assert all(b == int(_XREAD_BLOCK_S * 1000) for b in fake.blocks_seen)
        assert all(b < SOCKET_TIMEOUT_S * 1000 for b in fake.blocks_seen)

    async def test_timeout_is_classified_as_retryable(self):
        """Guards the import: a redis TimeoutError must be catchable separately
        from the generic Exception branch."""
        assert issubclass(RedisTimeoutError, Exception)
        assert not issubclass(RedisTimeoutError, asyncio.CancelledError)


class TestErrorMessagesStillExist:
    """The fatal paths must survive; only the timeout was misclassified."""

    def test_lost_contact_message_is_still_present(self):
        import inspect

        src = inspect.getsource(run_manager._subscribe_remote)
        assert "Lost contact with the running agent" in src

    def test_timeout_branch_precedes_the_generic_branch(self):
        import inspect

        src = inspect.getsource(run_manager._subscribe_remote)
        timeout_at = src.index("except RedisTimeoutError")
        generic_at = src.index("except Exception")
        assert timeout_at < generic_at, (
            "the generic handler would swallow the timeout first"
        )
