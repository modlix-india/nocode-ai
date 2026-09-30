"""web_fetch_adapter's Claude page check, against the installed SDK's real call
signature. Every other test mocks fetch_and_answer whole, so an argument the SDK
no longer accepts (SDK 1.x dropped `temperature`) passed CI and failed every
competitor page check on dev (2026-09-30)."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import anthropic
from anthropic.resources.messages import Messages

from app.agents.adzump.agents.product.adapters import web_fetch_adapter


class ExtractViaAnthropicTests(unittest.TestCase):
    def test_call_matches_the_installed_sdk(self):
        messages = mock.create_autospec(Messages, instance=True)
        messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=" Sobha Magnus official site ")])
        client = SimpleNamespace(messages=messages)
        with mock.patch.object(anthropic, "Anthropic", return_value=client):
            answer = asyncio.run(web_fetch_adapter._extract_via_anthropic("Whose site is this?"))
        self.assertEqual(answer, "Sobha Magnus official site")
        messages.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
