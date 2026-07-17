import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.agents.adzump.agents.campaign.meta.agent import DetailedTargetingAgent, get_detailed_targeting_agent
from app.agents.adzump.agents.campaign.meta.models import (
    MetaTargetingSuggestionResult,
    TargetingCategory,
)
from app.agents.adzump.agents.campaign.meta.context import (
    DETAILED_TARGETING_SYSTEM_PROMPT,
)

# Helper to patch the BaseAgent.run method so we can inject synthetic tool results
async def _fake_run(self, *_args, session=None, event_stream=None, **_kwargs):
    """Inject fake tool_use / tool_result messages into the session.

    This mimics the LLM calling `browse_targeting` for each required category
    and returning a single item per category.
    """
    session.messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "fetch_interests",
                    "id": "t1",
                    "input": {"seeds": ["coffee"]},
                },
                {
                    "type": "tool_use",
                    "name": "fetch_demographics",
                    "id": "t2",
                    "input": {"seeds": ["owner"]},
                },
                {
                    "type": "tool_use",
                    "name": "fetch_behaviors",
                    "id": "t3",
                    "input": {"seeds": ["moved"]},
                }
            ]
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": '[{"id": "123", "name": "Coffee Lover", "category": "interests"}]'
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "t2",
                    "content": '[{"id": "456", "name": "Owner", "category": "demographics"}]'
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "t3",
                    "content": '[{"id": "789", "name": "Recently Moved", "category": "behaviors"}]'
                }
            ]
        }
    ]
    # Simulate validate_targeting saving the final output into context
    session.context["detailed_targeting"] = {
        "interests": [{"id": "123", "name": "Coffee Lover", "category": "interests", "type": "interests"}],
        "demographics": [{"id": "456", "name": "Owner", "category": "demographics", "type": "demographics"}],
        "behaviors": [{"id": "789", "name": "Recently Moved", "category": "behaviors", "type": "behaviors"}]
    }
    return

class DetailedTargetingAgentFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Obtain a fresh agent instance and replace its `run` method
        self.agent = get_detailed_targeting_agent()
        self.agent.run = _fake_run.__get__(self.agent, DetailedTargetingAgent)  # bind method
        # Mock the event stream – we only need it to satisfy the calls
        self.stream = AsyncMock()
        # Dummy auth context – only fields accessed are token‑related which we ignore
        self.auth = MagicMock()
        self.auth.client_code = "CC"
        self.auth.app_code = "marketingai"
        self.auth.to_headers.return_value = {"clientCode": "CC", "AppCode": "marketingai"}
        # Minimal parent session context
        self.parent_session_context = {"url": "https://example.com"}

    async def test_agent_returns_populated_result(self):
        result, _explanation = await self.agent.recommend(
            session_id="test-session",
            ad_account_id="act_123",
            parent_event_stream=self.stream,
            auth=self.auth,
            parent_session_context=self.parent_session_context,
        )
        # Verify result type and contents
        self.assertIsInstance(result, MetaTargetingSuggestionResult)
        self.assertEqual(len(result.interests), 1)
        self.assertEqual(result.interests[0].id, "123")
        self.assertEqual(result.interests[0].category, TargetingCategory.INTERESTS)
        self.assertEqual(len(result.demographics), 1)
        self.assertEqual(result.demographics[0].id, "456")
        self.assertEqual(result.demographics[0].category, TargetingCategory.DEMOGRAPHICS)
        self.assertEqual(len(result.behaviors), 1)
        self.assertEqual(result.behaviors[0].id, "789")
        self.assertEqual(result.behaviors[0].category, TargetingCategory.BEHAVIORS)
        # Ensure the craft emission was called
        self.assertTrue(self.stream.emit_craft.called)
        self.assertTrue(self.stream.emit_agent_finished.called)
    def test_system_prompt_loaded(self):
        """Ensure the detailed targeting system prompt is loaded correctly."""
        self.assertIsInstance(DETAILED_TARGETING_SYSTEM_PROMPT, str)
        self.assertIn("targeting", DETAILED_TARGETING_SYSTEM_PROMPT.lower())

if __name__ == "__main__":
    asyncio.run(unittest.main())
