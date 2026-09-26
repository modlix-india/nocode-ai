import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.agents.adzump.agents.meta_detailed_targeting.agent import (
    DetailedTargetingAgent,
    get_detailed_targeting_agent,
)
from app.agents.adzump.agents.meta_detailed_targeting.models import (
    MetaTargetingSuggestionResult,
    TargetingEntity,
)
from app.agents.adzump.agents.meta_detailed_targeting.context import (
    DETAILED_TARGETING_SYSTEM_PROMPT,
)


async def _fake_run(self, *_args, session=None, event_stream=None, **_kwargs):
    """Inject fake tool_use / tool_result messages and validated output into the session context."""
    session.messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "fetch_interests",
                    "id": "t1",
                    "input": {"seeds": ["coffee"]},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": '[{"id": "123", "name": "Coffee Lover", "type": "interests"}]',
                }
            ],
        },
        {
            "role": "assistant",
            "content": "Selected Coffee Lover segment based on user persona.",
        },
    ]

    # Simulate validate_targeting saving the final output into _validated_targeting
    session.context["_validated_targeting"] = {
        "entities": [
            {
                "id": "123",
                "name": "Coffee Lover",
                "type": "interests",
                "audience_size_lower_bound": 1000,
                "audience_size_upper_bound": 50000,
            }
        ]
    }
    return


class DetailedTargetingAgentFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.agent = get_detailed_targeting_agent()
        self._orig_run = self.agent.run
        self.agent.run = _fake_run.__get__(self.agent, DetailedTargetingAgent)

        self.stream = AsyncMock()
        self.auth = MagicMock()
        self.auth.client_code = "CC"
        self.auth.client_id = 1
        self.auth.user_id = 2
        self.auth.app_code = "marketingai"
        self.auth.to_headers.return_value = {"clientCode": "CC", "AppCode": "marketingai"}

        self.parent_session_context = {
            "product_data": {"summary": "Premium Artisanal Coffee Subscriptions"},
            "campaign_spec": {"objective": "CONVERSIONS"},
        }

    async def asyncTearDown(self):
        # Restore singleton method to prevent poisoning subsequent tests
        self.agent.run = self._orig_run

    async def test_agent_returns_populated_result(self):
        result, explanation = await self.agent.recommend(
            session_id="test-session-123",
            ad_account_id="act_123456",
            parent_event_stream=self.stream,
            auth=self.auth,
            parent_session_context=self.parent_session_context,
            user_query="Recommend coffee targeting",
        )

        self.assertIsInstance(result, MetaTargetingSuggestionResult)
        self.assertEqual(len(result.entities), 1)
        self.assertEqual(result.entities[0].id, "123")
        self.assertEqual(result.entities[0].name, "Coffee Lover")
        self.assertEqual(result.entities[0].type, "interests")
        self.assertEqual(result.entities[0].audience_size_lower_bound, 1000)
        self.assertIn("Selected Coffee Lover", explanation)

        # Ensure craft emission and finished telemetry were called
        self.assertTrue(self.stream.emit_craft.called)
        self.assertTrue(self.stream.emit_agent_finished.called)

    def test_system_prompt_loaded(self):
        """Ensure the detailed targeting system prompt is loaded correctly."""
        self.assertIsInstance(DETAILED_TARGETING_SYSTEM_PROMPT, str)
        self.assertIn("targeting analyst", DETAILED_TARGETING_SYSTEM_PROMPT.lower())

    async def test_patch_session_context_key_file_store(self):
        """Verify patch_session_context_key atomically updates a single key in context."""
        from app.services.session_manager import get_session_manager
        sm = get_session_manager()

        session = await sm.create_session(
            client_code="CC",
            client_id=1,
            user_id=2,
            context_json='{"existing_key": "initial_val"}',
        )
        self.assertIsNotNone(session)
        session_id = session.session_id

        # Patch a specific key
        success = await sm.patch_session_context_key(
            session_id=session_id,
            key="detailed_targeting",
            value={"entities": [{"id": "100", "name": "Tech Enthusiast", "type": "interests"}]},
            user_id=2,
        )
        self.assertTrue(success)

        # Reload and verify both existing_key and detailed_targeting coexist
        reloaded = await sm.get_session(session_id)
        self.assertIsNotNone(reloaded)
        import json
        ctx = json.loads(reloaded.context_json)
        self.assertEqual(ctx.get("existing_key"), "initial_val")
        self.assertIn("detailed_targeting", ctx)
        self.assertEqual(ctx["detailed_targeting"]["entities"][0]["id"], "100")

    def test_resolve_ad_account_id_direct_campaign_spec(self):
        """Verify resolve_ad_account_id correctly resolves account from raw session context."""
        from app.agents.adzump.agents.meta_detailed_targeting.models import resolve_ad_account_id
        ctx = {"campaign_spec": {"account": "act_987654321"}}
        self.assertEqual(resolve_ad_account_id(ctx), "act_987654321")

    async def test_resolve_meta_token_uses_env_override(self):
        """Verify _resolve_meta_token returns local dev config token when present."""
        from unittest.mock import patch
        from app.agents.adzump.adapters.meta.targeting_adapter import _resolve_meta_token
        from app.agents.adzump.config import AdzumpConfig, MetaCreds

        mock_config = AdzumpConfig(meta=MetaCreds(access_token="EAA_LOCAL_OVERRIDE_123"))
        with patch("app.agents.adzump.adapters.meta.targeting_adapter.get_adzump_config", return_value=mock_config):
            with patch("app.agents.adzump.adapters.meta.targeting_adapter.fetch_meta_api_token") as mock_fetch:
                token = await _resolve_meta_token("OMDF", {"auth": "hdr"})
                self.assertEqual(token, "EAA_LOCAL_OVERRIDE_123")
                mock_fetch.assert_not_called()

    async def test_resolve_meta_token_falls_back_to_gateway(self):
        """Verify _resolve_meta_token queries gateway when no local config token exists."""
        from unittest.mock import patch
        from app.agents.adzump.adapters.meta.targeting_adapter import _resolve_meta_token
        from app.agents.adzump.config import AdzumpConfig, MetaCreds

        mock_config = AdzumpConfig(meta=MetaCreds(access_token=None))
        with patch("app.agents.adzump.adapters.meta.targeting_adapter.get_adzump_config", return_value=mock_config):
            with patch("app.agents.adzump.adapters.meta.targeting_adapter.fetch_meta_api_token", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = "EAA_GATEWAY_OAUTH_456"
                token = await _resolve_meta_token("OMDF", {"auth": "hdr"})
                self.assertEqual(token, "EAA_GATEWAY_OAUTH_456")
                mock_fetch.assert_awaited_once_with("OMDF", {"auth": "hdr"})


if __name__ == "__main__":
    asyncio.run(unittest.main())
