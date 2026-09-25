"""Unit: app/core/streaming.py - every AgentEventStream subclass carries the base state.

BaseAgent.run drains steers from whatever stream it is handed, sub-agent
delegates included. A subclass that skipped super().__init__() crashed every
run it hosted once steering landed (live 2026-09-25: Profile Writer, Vision
Analyst and Essence Analyst all failed with "no attribute '_steers'").
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import unittest

import app.agents
from app.core.streaming import AgentEventStream


def _subclasses(cls: type) -> set[type]:
    found = set()
    for sub in cls.__subclasses__():
        found |= {sub, *_subclasses(sub)}
    return found


class StreamSubclassTests(unittest.TestCase):
    def test_every_app_subclass_answers_steer_checks(self):
        for module in pkgutil.walk_packages(app.agents.__path__, "app.agents."):
            importlib.import_module(module.name)
        streams = {cls for cls in _subclasses(AgentEventStream)
                   if cls.__module__.startswith("app.")}
        self.assertGreaterEqual(len(streams), 8)
        for cls in streams:
            with self.subTest(f"{cls.__module__}.{cls.__qualname__}"):
                required = [p for p in list(inspect.signature(cls.__init__).parameters.values())[1:]
                            if p.default is inspect.Parameter.empty]
                stream = cls(*[AgentEventStream() if p.name == "parent" else "x"
                               for p in required])
                self.assertEqual(stream.drain_steers(), [])
                self.assertFalse(stream.has_steers)


if __name__ == "__main__":
    unittest.main()
