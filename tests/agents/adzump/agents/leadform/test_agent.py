"""Unit tests for lead form sub-agent lifecycle and execution."""

import unittest
from unittest.mock import AsyncMock, patch

from app.agents.adzump.agents.leadform.agent import run_leadform_session


class TestRunLeadformSession(unittest.IsolatedAsyncioTestCase):
    """Tests for run_leadform_session session setup and execution flow."""

    @patch("app.agents.adzump.agents.leadform.agent.get_leadform_agent")
    @patch("app.agents.adzump.agents.leadform.agent.BaseSession.get_or_create", new_callable=AsyncMock)
    async def test_generate_mode_does_not_seed_duplicate_user_message(self, mock_get_or_create, mock_get_agent):
        mock_get_or_create.return_value = "new_gen_session_123"
        mock_agent = AsyncMock()
        mock_get_agent.return_value = mock_agent

        parent_ctx = {
            "product_data": {"name": "Test Product"},
            "campaign_spec": {"fb_page": "123456789"},
        }

        status = await run_leadform_session(
            user_message="Create a lead form for my course",
            parent_ctx=parent_ctx,
            stream=None,
            tool_use_id="tool_1",
            auth_context=None,
        )

        self.assertEqual(status, "success")
        mock_get_agent.assert_called_once_with("generate")
        mock_agent.run.assert_called_once()
        _, run_kwargs = mock_agent.run.call_args
        session_arg = run_kwargs["session"]
        # Verify session messages list was not pre-seeded with a synthetic user message
        self.assertEqual(len(session_arg.messages), 0)
        self.assertEqual(run_kwargs["user_message"], "Create a lead form for my course")

    @patch("app.agents.adzump.agents.leadform.agent.get_leadform_agent")
    @patch("app.agents.adzump.agents.leadform.agent.BaseSession.get_or_create", new_callable=AsyncMock)
    async def test_session_routes_to_generate_when_no_draft_exists(self, mock_get_or_create, mock_get_agent):
        """Verify that when lead_form_draft is None (such as after publishing), GENERATE mode is selected."""
        mock_get_or_create.return_value = "new_gen_session_456"
        mock_agent = AsyncMock()
        mock_get_agent.return_value = mock_agent

        parent_ctx = {
            "lead_form_draft": None,
            "lead_form_published": True,
            "product_data": {"name": "Test Product"},
            "campaign_spec": {"fb_page": "123456789"},
        }

        status = await run_leadform_session(
            user_message="Build another lead form for summer",
            parent_ctx=parent_ctx,
            stream=None,
            tool_use_id="tool_2",
            auth_context=None,
        )

        self.assertEqual(status, "success")
        mock_get_agent.assert_called_once_with("generate")

    @patch("app.agents.adzump.agents.leadform.agent.get_leadform_agent")
    async def test_generate_mode_preserves_restored_session_context(self, mock_get_agent):
        """Verify that restored session context (historical forms, analysis, phase) is preserved in GENERATE mode."""
        mock_agent = AsyncMock()
        mock_get_agent.return_value = mock_agent

        async def fake_get_or_create(self_session, session_id, auth_context):
            self_session.context["historical_forms"] = [{"id": "form_1", "name": "Old Form"}]
            self_session.context["advertiser_knowledge"] = {"summary": "Prefers 3 questions"}
            self_session.context["lf_phase"] = "analyze"
            return "existing_gen_session_789"

        with patch("app.agents.adzump.agents.leadform.agent.BaseSession.get_or_create", side_effect=fake_get_or_create, autospec=True):
            parent_ctx = {
                "lf_gen_session_id": "existing_gen_session_789",
                "product_data": {"name": "New Product Update"},
                "campaign_spec": {"fb_page": "123456789"},
                "craft_id": "custom_leadform_craft",
            }

            status = await run_leadform_session(
                user_message="Proceed with the recommendation",
                parent_ctx=parent_ctx,
                stream=None,
                tool_use_id="tool_3",
                auth_context=None,
            )

            self.assertEqual(status, "success")
            mock_agent.run.assert_called_once()
            _, run_kwargs = mock_agent.run.call_args
            session_arg = run_kwargs["session"]
            # Ensure restored context keys were not erased by session.context = {}
            self.assertEqual(session_arg.context["historical_forms"], [{"id": "form_1", "name": "Old Form"}])
            self.assertEqual(session_arg.context["advertiser_knowledge"], {"summary": "Prefers 3 questions"})
            self.assertEqual(session_arg.context["lf_phase"], "analyze")
            self.assertEqual(session_arg.context["craft_id"], "custom_leadform_craft")
            self.assertEqual(session_arg.context["product_data"], {"name": "New Product Update"})

    @patch("app.agents.adzump.agents.leadform.agent.get_leadform_agent")
    async def test_subagent_transcript_is_bounded_to_max_recent_messages(self, mock_get_agent):
        """Verify that cumulative session messages are trimmed to _MAX_SUBAGENT_MESSAGES to prevent unbounded growth."""
        mock_agent = AsyncMock()
        mock_get_agent.return_value = mock_agent

        async def fake_get_or_create(self_session, session_id, auth_context):
            self_session.messages = [{"role": "user", "content": f"msg {i}"} for i in range(12)]
            return "long_manage_session_999"

        with patch("app.agents.adzump.agents.leadform.agent.BaseSession.get_or_create", side_effect=fake_get_or_create, autospec=True):
            parent_ctx = {
                "lead_form_draft": {"name": "Existing Form"},
                "lf_manage_session_id": "long_manage_session_999",
                "product_data": {"name": "Product"},
            }

            status = await run_leadform_session(
                user_message="Change headline",
                parent_ctx=parent_ctx,
                stream=None,
                tool_use_id="tool_4",
                auth_context=None,
            )

            self.assertEqual(status, "success")
            mock_agent.run.assert_called_once()
            _, run_kwargs = mock_agent.run.call_args
            session_arg = run_kwargs["session"]
            # Verify transcript was capped to the last 6 messages
            self.assertEqual(len(session_arg.messages), 6)
            self.assertEqual(session_arg.messages[0]["content"], "msg 6")
            self.assertEqual(session_arg.messages[-1]["content"], "msg 11")

    async def test_build_dynamic_context_caps_historical_forms_and_compacts_json(self):
        """Verify dynamic context limits historical forms to recent examples, strips dead weight, and uses compact JSON."""
        from app.agents.adzump.agents.leadform.agent import LeadFormAgent
        from app.core.session import BaseSession
        import json

        agent = LeadFormAgent("generate")
        session = BaseSession("leadform_generate")

        # 8 historical forms with dead weight fields (context_card_raw, thank_you_page_raw, etc.)
        session.context["historical_forms"] = [
            {
                "id": f"form_{i}",
                "name": f"Historical Form {i}",
                "status": "ACTIVE",
                "leads_count": i * 10,
                "is_higher_intent": (i % 2 == 0),
                "questions": [{"type": "EMAIL", "key": "email", "label": "Email"}],
                "created_time": "2026-01-01T00:00:00Z",
                "context_card_raw": {"title": f"Card {i}", "content": ["Heavy text"]},
                "thank_you_page_raw": {"title": "Thanks"},
                "privacy_policy_url": "https://example.com/privacy",
            }
            for i in range(8)
        ]

        dynamic_ctx = await agent.build_dynamic_context(session)

        self.assertIn("Historical Forms (Raw):", dynamic_ctx)
        # Extract the JSON payload after the header
        raw_section = dynamic_ctx.split("Historical Forms (Raw):\n")[1].strip()
        parsed_forms = json.loads(raw_section)

        # Capped to the 5 most recent forms
        self.assertEqual(len(parsed_forms), 5)
        self.assertEqual(parsed_forms[0]["name"], "Historical Form 0")
        self.assertEqual(parsed_forms[4]["name"], "Historical Form 4")

        # Dead-weight fields pruned from prompt representation
        for form in parsed_forms:
            self.assertNotIn("context_card_raw", form)
            self.assertNotIn("thank_you_page_raw", form)
            self.assertNotIn("created_time", form)
            self.assertNotIn("id", form)
            self.assertNotIn("status", form)
            self.assertIn("name", form)
            self.assertIn("questions", form)
            self.assertIn("is_higher_intent", form)
            self.assertIn("leads_count", form)

        # Verified compact JSON (no indentation spaces/newlines within objects)
        self.assertNotIn('{\n  "name"', raw_section)

    async def test_build_dynamic_context_omits_empty_historical_forms(self):
        """Verify dynamic context does not emit raw forms section when none exist."""
        from app.agents.adzump.agents.leadform.agent import LeadFormAgent
        from app.core.session import BaseSession

        agent = LeadFormAgent("generate")
        session = BaseSession("leadform_generate")
        session.context["historical_forms"] = []

        dynamic_ctx = await agent.build_dynamic_context(session)
        self.assertNotIn("Historical Forms (Raw):", dynamic_ctx)


