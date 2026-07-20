"""LocationPassthroughEventStream - forward/drop wrapper over the parent stream.

Regression for PR #91 B5: the wrapper skipped ``super().__init__()``, so any
base member it does not override (emit_complete, request_confirmation,
resolve_confirmation, events) crashed with AttributeError on the missing
queue. The wrapper must survive those calls while still forwarding/dropping
per its matrix and delegating cancellation to the parent.
"""
import asyncio
import unittest

from app.core.streaming import AgentEventStream
from app.agents.adzump.agents.location.subagent_event_stream import (
    LocationPassthroughEventStream,
)


class PassthroughStreamTests(unittest.TestCase):
    def setUp(self):
        self.parent = AgentEventStream()
        self.stream = LocationPassthroughEventStream(self.parent)

    def test_unoverridden_base_members_survive(self):
        # Pre-fix: AttributeError (no _queue / _pending_confirmations).
        asyncio.run(self.stream.emit_complete({"result": "ok"}))
        self.assertFalse(self.stream.resolve_confirmation("unknown-id", {}))

    def test_cancel_delegates_to_parent(self):
        self.assertFalse(self.stream.is_cancelled)
        self.parent.cancel()
        self.assertTrue(self.stream.is_cancelled)

    def test_forwards_tool_events_and_drops_text(self):
        async def emit_both():
            await self.stream.emit_tool_update("t1", "working")  # forward
            await self.stream.emit_text("sub-agent final JSON")  # drop
        asyncio.run(emit_both())
        self.assertEqual(self.parent._queue.qsize(), 1)


if __name__ == "__main__":
    unittest.main()
