"""Unit tests for lead form generation tools."""

import unittest
from unittest.mock import AsyncMock, patch

from app.agents.adzump.agents.leadform.tools import _analyze_historical_forms


class MockAuthContext:
    def __init__(self, client_code="client_xyz", token="token_val"):
        self.client_code = client_code
        self.token = token

    def to_headers(self):
        return {"authorization": f"Bearer {self.token}"}


class TestAnalyzeHistoricalForms(unittest.IsolatedAsyncioTestCase):
    """Tests for _analyze_historical_forms tool execution and auth resolution."""

    async def test_missing_page_id_returns_error(self):
        context = {
            "session_context": {"campaign_spec": {}},
            "client_code": "tenant_123",
        }
        result = await _analyze_historical_forms({}, context)
        self.assertFalse(result.success)
        self.assertIn("No Facebook Page ID found", result.error)

    @patch("app.agents.adzump.agents.leadform.tools.meta_lead_forms_adapter.get_leadgen_forms", new_callable=AsyncMock)
    async def test_build_ds_headers_formats_headers_with_client_code(self, mock_get_forms):
        mock_get_forms.return_value = []
        context = {
            "session_context": {
                "campaign_spec": {"fb_page": "123456789"}
            },
            "headers": {"authorization": "Bearer user_tok"},
            "client_code": "client_abc",
        }
        result = await _analyze_historical_forms({}, context)
        self.assertTrue(result.success)
        mock_get_forms.assert_called_once()
        _, kwargs = mock_get_forms.call_args
        self.assertEqual(kwargs["page_id"], "123456789")
        self.assertEqual(kwargs["client_code"], "client_abc")
        self.assertIsInstance(kwargs["auth_headers"], dict)
        self.assertEqual(kwargs["auth_headers"].get("authorization"), "Bearer user_tok")
        self.assertEqual(kwargs["auth_headers"].get("clientCode"), "client_abc")

    @patch("app.agents.adzump.agents.leadform.tools.meta_lead_forms_adapter.get_leadgen_forms", new_callable=AsyncMock)
    async def test_analyze_historical_forms_passes_cached_page_token(self, mock_get_forms):
        mock_get_forms.return_value = []
        context = {
            "session_context": {
                "campaign_spec": {"fb_page": "123456789"},
                "_meta_page_cache": {"123456789": {"access_token": "cached_page_token_tok"}},
            },
            "headers": {"authorization": "Bearer user_tok"},
            "client_code": "client_abc",
        }
        result = await _analyze_historical_forms({}, context)
        self.assertTrue(result.success)
        mock_get_forms.assert_called_once()
        self.assertEqual(mock_get_forms.call_args.kwargs.get("page_token"), "cached_page_token_tok")

    @patch("app.agents.adzump.agents.leadform.tools.meta_lead_forms_adapter.get_leadgen_forms", new_callable=AsyncMock)
    async def test_auth_context_object_fallback_produces_dict(self, mock_get_forms):
        mock_get_forms.return_value = []
        auth_obj = MockAuthContext(
            client_code="client_xyz",
            token="token_val",
        )
        context = {
            "session_context": {
                "campaign_spec": {"fb_page": "987654321"}
            },
            "auth": auth_obj,
        }
        result = await _analyze_historical_forms({}, context)
        self.assertTrue(result.success)
        mock_get_forms.assert_called_once()
        _, kwargs = mock_get_forms.call_args
        self.assertEqual(kwargs["page_id"], "987654321")
        self.assertEqual(kwargs["client_code"], "client_xyz")
        self.assertIsInstance(kwargs["auth_headers"], dict)
        self.assertNotIsInstance(kwargs["auth_headers"], MockAuthContext)
        self.assertEqual(kwargs["auth_headers"].get("authorization"), "Bearer token_val")

    @patch("app.agents.adzump.agents.leadform.tools.meta_lead_forms_adapter.get_leadgen_forms", new_callable=AsyncMock)
    async def test_analyze_historical_forms_with_history_populates_knowledge_and_advances_phase(self, mock_get_forms):
        raw_forms = [
            {
                "id": "1001",
                "name": "Summer Form",
                "status": "ACTIVE",
                "leads_count": "42",
                "organic_leads_count": 5,
                "questions": [
                    {"type": "EMAIL", "key": "email", "label": "Email"},
                    {"type": "FULL_NAME", "key": "full_name", "label": "Full Name"},
                ],
                "privacy_policy_url": {"url": "https://example.com/privacy"},
            },
            {
                "id": "1002",
                "name": "Winter Form",
                "status": "ACTIVE",
                "leads_count": "10",
                "questions": [
                    {"type": "PHONE", "key": "phone", "label": "Phone Number"},
                ],
                "privacy_policy_url": "https://example.com/privacy",
            },
        ]
        mock_get_forms.return_value = raw_forms
        session_ctx = {"campaign_spec": {"fb_page": "123456789"}}
        context = {
            "session_context": session_ctx,
            "headers": {"authorization": "Bearer user_tok"},
            "client_code": "client_abc",
        }
        result = await _analyze_historical_forms({}, context)
        self.assertTrue(result.success)
        # Verify knowledge was extracted
        self.assertIn("advertiser_knowledge", session_ctx)
        knowledge = session_ctx["advertiser_knowledge"]
        self.assertEqual(knowledge["forms_analyzed"], 2)
        self.assertEqual(knowledge["total_leads_recorded"], 52)
        self.assertEqual(knowledge["historical_question_types"], ["EMAIL", "FULL_NAME", "PHONE"])
        # Verify phase advanced to RECOMMEND
        self.assertEqual(session_ctx["lf_phase"], "recommend")
        self.assertEqual(len(session_ctx["historical_forms"]), 2)



class MockSession:
    def __init__(self, context=None):
        self.context = context if context is not None else {}


class TestPublishToMeta(unittest.IsolatedAsyncioTestCase):
    """Tests for _publish_to_meta covering consent, idempotency, page_id safety, and Meta id verification."""

    def setUp(self):
        self.base_draft = {
            "name": "Test Form",
            "context_card": {"title": "Welcome", "content": ["Info"], "cover_photo_id": "photo_1"},
            "questions": [{"type": "EMAIL", "label": "Email"}],
            "privacy_policy": {"url": "https://example.com/privacy"},
            "is_phone_sms_verify_enabled": False,
        }

    async def test_publish_blocked_when_already_published_idempotency(self):
        session = MockSession({
            "lead_form_published": True,
            "meta_lead_form_id": "meta_123456",
            "lf_user_message": "Yes, publish it",
        })
        context = {"_session": session}
        from app.agents.adzump.agents.leadform.manage_tools import _publish_to_meta

        result = await _publish_to_meta({}, context)
        self.assertFalse(result.success)
        self.assertIn("already been published to Meta", result.error)
        self.assertIn("meta_123456", result.error)

    async def test_publish_blocked_without_user_consent(self):
        session = MockSession({
            "lf_user_message": "Can you show me how it looks?",
            "campaign_spec": {"fb_page": "123456789"},
            "lead_form_draft": self.base_draft,
        })
        context = {"_session": session}
        from app.agents.adzump.agents.leadform.manage_tools import _publish_to_meta

        result = await _publish_to_meta({}, context)
        self.assertFalse(result.success)
        self.assertIn("user has not explicitly confirmed", result.error)

    async def test_publish_blocked_for_invalid_page_ids(self):
        from app.agents.adzump.agents.leadform.manage_tools import _publish_to_meta

        invalid_page_ids = ["me/accounts", "../../me", "123?fields=access_token", "", None, "abc_page"]
        for bad_id in invalid_page_ids:
            with self.subTest(page_id=bad_id):
                session = MockSession({
                    "lf_user_message": "Yes, publish it now please",
                    "campaign_spec": {"fb_page": bad_id},
                    "lead_form_draft": self.base_draft,
                })
                context = {"_session": session}
                result = await _publish_to_meta({}, context)
                self.assertFalse(result.success)
                self.assertIn("Page ID must be numeric", result.error)

    async def test_publish_blocked_when_privacy_policy_url_missing(self):
        draft_without_privacy = dict(self.base_draft)
        draft_without_privacy["privacy_policy"] = {"url": ""}
        session = MockSession({
            "lf_user_message": "Yes, publish it now please",
            "campaign_spec": {"fb_page": "999888777"},
            "lead_form_draft": draft_without_privacy,
            "business_context": {"website_url": "https://example.com"},
        })
        auth = MockAuthContext()
        context = {"_session": session, "auth": auth}
        from app.agents.adzump.agents.leadform.manage_tools import _publish_to_meta

        result = await _publish_to_meta({}, context)
        self.assertFalse(result.success)
        self.assertIn("Meta requires a valid Privacy Policy URL", result.error)

    @patch("app.agents.adzump.agents.leadform.manage_tools.meta_lead_forms_adapter.create_leadgen_form", new_callable=AsyncMock)
    async def test_publish_blocked_when_meta_response_missing_id(self, mock_create):
        mock_create.return_value = {"success": True}  # Missing 'id'
        session = MockSession({
            "lf_user_message": "Yes, please publish the form",
            "campaign_spec": {"fb_page": "999888777"},
            "lead_form_draft": self.base_draft,
            "business_context": {"website_url": "https://example.com"},
        })
        auth = MockAuthContext()
        context = {"_session": session, "auth": auth}
        from app.agents.adzump.agents.leadform.manage_tools import _publish_to_meta

        result = await _publish_to_meta({}, context)
        self.assertFalse(result.success)
        self.assertIn("did not return a valid form ID", result.error)
        self.assertFalse(session.context.get("lead_form_published", False))
        self.assertNotIn("meta_lead_form_id", session.context)

    @patch("app.agents.adzump.agents.leadform.manage_tools.meta_lead_forms_adapter.create_leadgen_form", new_callable=AsyncMock)
    async def test_publish_success_sets_published_flag_and_persists_id(self, mock_create):
        mock_create.return_value = {"id": "form_meta_555444"}
        session = MockSession({
            "lf_user_message": "Yes, go ahead and publish",
            "campaign_spec": {"fb_page": "999888777"},
            "lead_form_draft": self.base_draft,
            "business_context": {"website_url": "https://example.com"},
        })
        auth = MockAuthContext()
        mock_stream = AsyncMock()
        context = {"_session": session, "auth": auth, "event_stream": mock_stream}
        from app.agents.adzump.agents.leadform.manage_tools import _publish_to_meta

        result = await _publish_to_meta({}, context)
        self.assertTrue(result.success)
        self.assertIn("form_meta_555444", result.summary)
        self.assertTrue(session.context.get("lead_form_published"))
        self.assertEqual(session.context.get("meta_lead_form_id"), "form_meta_555444")
        self.assertIsNone(session.context.get("lead_form_draft"))
        mock_stream.emit_craft.assert_called_once()

    @patch("app.agents.adzump.agents.leadform.manage_tools.meta_lead_forms_adapter.create_leadgen_form", new_callable=AsyncMock)
    async def test_publish_threads_cached_page_token_to_adapter(self, mock_create):
        mock_create.return_value = {"id": "form_meta_555444"}
        session = MockSession({
            "lf_user_message": "Yes, go ahead and publish",
            "campaign_spec": {"fb_page": "999888777"},
            "lead_form_draft": self.base_draft,
            "business_context": {"website_url": "https://example.com"},
            "_meta_page_cache": {"999888777": {"access_token": "cached_page_token_abc"}},
        })
        auth = MockAuthContext()
        mock_stream = AsyncMock()
        context = {"_session": session, "auth": auth, "event_stream": mock_stream}
        from app.agents.adzump.agents.leadform.manage_tools import _publish_to_meta

        result = await _publish_to_meta({}, context)
        self.assertTrue(result.success)
        mock_create.assert_called_once()
        self.assertEqual(mock_create.call_args.kwargs.get("page_token"), "cached_page_token_abc")

    @patch("app.agents.adzump.agents.leadform.manage_tools.meta_lead_forms_adapter.create_leadgen_form", new_callable=AsyncMock)
    async def test_publish_threads_locale_from_campaign_spec(self, mock_create):
        mock_create.return_value = {"id": "form_meta_555444"}
        session = MockSession({
            "lf_user_message": "Yes, go ahead and publish",
            "campaign_spec": {"fb_page": "999888777", "country_code": "FR"},
            "lead_form_draft": self.base_draft,
            "business_context": {"website_url": "https://example.com"},
        })
        auth = MockAuthContext()
        context = {"_session": session, "auth": auth}
        from app.agents.adzump.agents.leadform.manage_tools import _publish_to_meta

        result = await _publish_to_meta({}, context)
        self.assertTrue(result.success)
        mock_create.assert_called_once()
        form_payload = mock_create.call_args.kwargs.get("form_payload")
        self.assertEqual(form_payload.get("locale"), "FR_FR")


class TestUpdateFormRecommendation(unittest.IsolatedAsyncioTestCase):
    """Tests for _update_form_recommendation covering J4 error handling."""

    def setUp(self):
        self.base_draft = {
            "name": "Test Form",
            "context_card": {"title": "Welcome", "content": ["Info"], "cover_photo_id": "photo_1"},
            "questions": [{"type": "EMAIL", "label": "Email"}],
            "privacy_policy": {"url": "https://example.com/privacy", "link_text": "Privacy Policy"},
            "is_phone_sms_verify_enabled": False,
        }

    async def test_update_non_dict_context_card_returns_error(self):
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        for bad_cc in [None, "invalid_str", [1, 2, 3]]:
            with self.subTest(bad_cc=bad_cc):
                context = {"session_context": {"lead_form_draft": dict(self.base_draft)}}
                result = await _update_form_recommendation({"context_card": bad_cc}, context)
                self.assertFalse(result.success)
                self.assertIn("Invalid context_card", result.error)

    async def test_update_overlong_context_card_title_returns_error(self):
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        context = {"session_context": {"lead_form_draft": dict(self.base_draft)}}
        result = await _update_form_recommendation({"context_card": {"title": "x" * 61}}, context)
        self.assertFalse(result.success)
        self.assertIn("Invalid context_card format", result.error)

    async def test_update_non_dict_privacy_policy_returns_error(self):
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        for bad_pp in [None, "invalid_str", 123]:
            with self.subTest(bad_pp=bad_pp):
                context = {"session_context": {"lead_form_draft": dict(self.base_draft)}}
                result = await _update_form_recommendation({"privacy_policy": bad_pp}, context)
                self.assertFalse(result.success)
                self.assertIn("Invalid privacy_policy", result.error)

    async def test_update_overlong_privacy_link_text_returns_error(self):
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        context = {"session_context": {"lead_form_draft": dict(self.base_draft)}}
        result = await _update_form_recommendation({"privacy_policy": {"link_text": "x" * 71}}, context)
        self.assertFalse(result.success)
        self.assertIn("Invalid privacy_policy format", result.error)

    async def test_update_valid_context_card_and_privacy_policy_passes(self):
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        context = {"session_context": {"lead_form_draft": dict(self.base_draft)}}
        result = await _update_form_recommendation({
            "context_card": {"title": "Updated Title"},
            "privacy_policy": {"link_text": "Our Privacy Terms"},
        }, context)
        self.assertTrue(result.success)
        updated_draft = context["session_context"]["lead_form_draft"]
        self.assertEqual(updated_draft["context_card"]["title"], "Updated Title")
        self.assertEqual(updated_draft["context_card"]["cover_photo_id"], "photo_1")  # Preserved
        self.assertEqual(updated_draft["privacy_policy"]["link_text"], "Our Privacy Terms")

    async def test_update_locale_passes(self):
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        context = {"session_context": {"lead_form_draft": dict(self.base_draft)}}
        result = await _update_form_recommendation({"locale": "es_la"}, context)
        self.assertTrue(result.success)
        updated_draft = context["session_context"]["lead_form_draft"]
        self.assertEqual(updated_draft["locale"], "es_la")

    async def test_update_validation_failure_preserves_pending_uploads(self):
        """Ensure _pending_uploads is not cleared if draft validation fails."""
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        uploaded_item = {"name": "bg.jpg", "data": "dummy_b64"}
        context = {
            "session_context": {
                "lead_form_draft": dict(self.base_draft),
                "_pending_uploads": [uploaded_item],
            }
        }
        # Force a validation error via invalid headline length > 60 chars
        result = await _update_form_recommendation({"question_page_headline": "x" * 61}, context)
        self.assertFalse(result.success)
        self.assertIn("Validation error", result.error)
        # Verify the pending upload is still present
        self.assertEqual(context["session_context"]["_pending_uploads"], [uploaded_item])

    @patch("app.agents.adzump.agents.leadform.manage_tools.meta_lead_forms_adapter.upload_cover_photo", new_callable=AsyncMock)
    async def test_update_consumes_only_first_upload_preserves_rest(self, mock_upload):
        """Ensure only the first attached image is consumed, preserving remaining uploads."""
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        mock_upload.return_value = {"photo_id": "meta_photo_777", "source_url": "https://meta.com/777.jpg"}
        img1 = {"name": "img1.jpg", "data": "b64_1"}
        img2 = {"name": "img2.jpg", "data": "b64_2"}
        session = MockSession()
        session.auth = MockAuthContext()
        context = {
            "_session": session,
            "session_context": {
                "lead_form_draft": dict(self.base_draft),
                "campaign_spec": {"fb_page": "123456789"},
                "_pending_uploads": [img1, img2],
            }
        }
        result = await _update_form_recommendation({}, context)
        self.assertTrue(result.success)
        mock_upload.assert_called_once()
        # Verify only the first image was removed, and img2 remains in _pending_uploads
        remaining = context["session_context"]["_pending_uploads"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0], img2)
        # Verify cover photo fields updated on draft
        updated_draft = context["session_context"]["lead_form_draft"]
        self.assertEqual(updated_draft["context_card"]["cover_photo_id"], "meta_photo_777")
        self.assertEqual(updated_draft["context_card"]["cover_image_url"], "https://meta.com/777.jpg")

    async def test_update_empty_params_without_upload_returns_error(self):
        """Ensure calling update with empty params and no pending upload is rejected as a no-op."""
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        context = {"session_context": {"lead_form_draft": dict(self.base_draft)}}
        result = await _update_form_recommendation({}, context)
        self.assertFalse(result.success)
        self.assertIn("No recognized editable field was provided", result.error)

    async def test_update_unrecognized_fields_returns_error(self):
        """Ensure calling update with only unrecognized keys is rejected."""
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        context = {"session_context": {"lead_form_draft": dict(self.base_draft)}}
        result = await _update_form_recommendation({"unknown_field": "some_value", "headline": "typo"}, context)
        self.assertFalse(result.success)
        self.assertIn("No recognized editable field was provided", result.error)

    async def test_update_scalar_fields_applies_and_reports_fields(self):
        """Ensure scalar fields are updated via loop and reported in the summary."""
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        context = {"session_context": {"lead_form_draft": dict(self.base_draft)}}
        result = await _update_form_recommendation({
            "name": "Brand New Form Name",
            "is_higher_intent": True,
            "thank_you_headline": "Thank You!",
        }, context)
        self.assertTrue(result.success)
        self.assertIn("Updated fields: name, is_higher_intent, thank_you_headline", result.summary)
        updated_draft = context["session_context"]["lead_form_draft"]
        self.assertEqual(updated_draft["name"], "Brand New Form Name")
        self.assertTrue(updated_draft["is_higher_intent"])
        self.assertEqual(updated_draft["thank_you_headline"], "Thank You!")

    async def test_update_mixed_valid_and_unrecognized_fields_applies_valid_only(self):
        """Ensure valid fields are applied and reported when mixed with unknown fields."""
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        context = {"session_context": {"lead_form_draft": dict(self.base_draft)}}
        result = await _update_form_recommendation({
            "name": "Updated Name Only",
            "fake_field": "ignored",
        }, context)
        self.assertTrue(result.success)
        self.assertIn("Updated fields: name", result.summary)
        self.assertNotIn("fake_field", result.summary)
        updated_draft = context["session_context"]["lead_form_draft"]
        self.assertEqual(updated_draft["name"], "Updated Name Only")

    @patch("app.agents.adzump.agents.leadform.manage_tools.meta_lead_forms_adapter.upload_cover_photo", new_callable=AsyncMock)
    async def test_update_upload_failure_without_fields_returns_error(self, mock_upload):
        """Ensure failed cover photo upload with no other fields returns error instead of false success."""
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        mock_upload.side_effect = RuntimeError("Meta API 500 server error")
        session = MockSession()
        session.auth = MockAuthContext()
        context = {
            "_session": session,
            "session_context": {
                "lead_form_draft": dict(self.base_draft),
                "campaign_spec": {"fb_page": "123456789"},
                "_pending_uploads": [{"name": "cover.jpg", "data": "dummy_b64"}],
            }
        }
        result = await _update_form_recommendation({}, context)
        self.assertFalse(result.success)
        self.assertIn("The background image upload to Meta failed", result.error)

    @patch("app.agents.adzump.agents.leadform.manage_tools.meta_lead_forms_adapter.get_page_info", new_callable=AsyncMock)
    @patch("app.agents.adzump.agents.leadform.manage_tools.meta_lead_forms_adapter.upload_cover_photo", new_callable=AsyncMock)
    async def test_update_upload_threads_cached_page_token(self, mock_upload, mock_get_page_info):
        mock_upload.return_value = {"photo_id": "cover_123", "source_url": "https://img.meta.com/cover.jpg"}
        session = MockSession()
        session.auth = MockAuthContext()
        mock_stream = AsyncMock()
        context = {
            "_session": session,
            "event_stream": mock_stream,
            "session_context": {
                "lead_form_draft": dict(self.base_draft),
                "campaign_spec": {"fb_page": "123456789"},
                "_pending_uploads": [{"name": "cover.jpg", "data": "ZHVtbXlfaW1hZ2VfYnl0ZXM="}],
                "_meta_page_cache": {"123456789": {"access_token": "pre_cached_token_123", "picture_url": "https://logo.com"}},
            }
        }
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        result = await _update_form_recommendation({}, context)
        self.assertTrue(result.success)
        mock_get_page_info.assert_not_called()
        mock_upload.assert_called_once()
        self.assertEqual(mock_upload.call_args.kwargs.get("page_token"), "pre_cached_token_123")

    async def test_update_questions_does_not_mutate_input_dicts(self):
        """Verify that caller question dictionaries are not mutated in place during update."""
        from app.agents.adzump.agents.leadform.manage_tools import _update_form_recommendation

        caller_q = {"type": "email", "label": "Email Address"}
        params = {"questions": [caller_q]}
        context = {"session_context": {"lead_form_draft": dict(self.base_draft)}}

        result = await _update_form_recommendation(params, context)
        self.assertTrue(result.success)
        self.assertEqual(caller_q["type"], "email")

    async def test_build_form_recommendation_does_not_mutate_input_dicts(self):
        """Verify that caller question dictionaries are not mutated in place during build."""
        from app.agents.adzump.agents.leadform.tools import _build_form_recommendation

        caller_q = {"type": "email", "label": "Email Address"}
        params = {
            "name": "Test Form",
            "questions": [caller_q],
        }
        context = {
            "session_context": {
                "business_context": {"website_url": "https://example.com"},
            },
            "event_stream": AsyncMock(),
        }

        result = await _build_form_recommendation(params, context)
        self.assertTrue(result.success)
        self.assertEqual(caller_q["type"], "email")



class TestToolParameterDefinitions(unittest.TestCase):
    """Verifies tool parameter schemas, enums, and required flags."""

    def test_build_form_recommendation_parameter_schema(self):
        """Verify build_form_recommendation parameters: required fields, enum enforcement, and typed schemas."""
        from app.agents.adzump.agents.leadform.tools import BUILD_FORM_RECOMMENDATION
        from app.agents.adzump.agents.leadform.models import ThankYouPageButtonType

        params_by_name = {p.name: p for p in BUILD_FORM_RECOMMENDATION.parameters}

        # Only name and questions are strictly required for creation
        self.assertTrue(params_by_name["name"].required)
        self.assertTrue(params_by_name["questions"].required)

        # Optional fields must not be marked required
        optional_fields = [
            "context_card",
            "question_page_headline",
            "is_higher_intent",
            "is_phone_sms_verify_enabled",
            "thank_you_headline",
            "thank_you_description",
            "cta_button_type",
            "cta_button_text",
            "business_phone_number",
            "custom_disclaimer",
            "privacy_policy",
        ]
        for field_name in optional_fields:
            self.assertFalse(
                params_by_name[field_name].required,
                f"Field '{field_name}' in build_form_recommendation should be optional (required=False).",
            )

        # cta_button_type must have enum list populated with all button types
        cta_param = params_by_name["cta_button_type"]
        expected_enums = [b.value for b in ThankYouPageButtonType]
        self.assertEqual(cta_param.enum, expected_enums)

        # Convert to Anthropic tool schema and verify JSON schema
        anthropic_tool = BUILD_FORM_RECOMMENDATION.to_anthropic_tool()
        input_schema = anthropic_tool["input_schema"]
        self.assertEqual(set(input_schema["required"]), {"name", "questions"})
        self.assertEqual(input_schema["properties"]["cta_button_type"]["enum"], expected_enums)

    def test_update_form_recommendation_parameter_schema(self):
        """Verify update_form_recommendation parameters: all optional, enum enforcement on CTA."""
        from app.agents.adzump.agents.leadform.manage_tools import UPDATE_FORM_RECOMMENDATION
        from app.agents.adzump.agents.leadform.models import ThankYouPageButtonType

        for param in UPDATE_FORM_RECOMMENDATION.parameters:
            self.assertFalse(
                param.required,
                f"Field '{param.name}' in update_form_recommendation must be optional (required=False).",
            )

        params_by_name = {p.name: p for p in UPDATE_FORM_RECOMMENDATION.parameters}
        cta_param = params_by_name["cta_button_type"]
        expected_enums = [b.value for b in ThankYouPageButtonType]
        self.assertEqual(cta_param.enum, expected_enums)

        # Convert to Anthropic tool schema and verify required list is empty
        anthropic_tool = UPDATE_FORM_RECOMMENDATION.to_anthropic_tool()
        input_schema = anthropic_tool["input_schema"]
        self.assertEqual(input_schema.get("required", []), [])
        self.assertEqual(input_schema["properties"]["cta_button_type"]["enum"], expected_enums)


class TestLeadFormEventStreamDrift(unittest.TestCase):
    """Guards against base-class drift between AgentEventStream and LeadFormEventStream."""

    def test_all_base_stream_events_accounted_for(self):
        """Every public emit_* or request_* method on AgentEventStream must either be
        explicitly handled in LeadFormEventStream or categorized as unhandled.
        """
        import inspect
        from app.core.streaming import AgentEventStream
        from app.agents.adzump.agents.leadform.subagent_event_stream import LeadFormEventStream

        base_methods = {
            name for name, _ in inspect.getmembers(AgentEventStream, predicate=inspect.isfunction)
            if name.startswith(("emit_", "request_"))
        }
        sub_methods = {
            name for name in dir(LeadFormEventStream)
            if name in LeadFormEventStream.__dict__
        }
        unhandled = {"emit_complete", "request_confirmation"}

        accounted_for = sub_methods | unhandled
        missing = base_methods - accounted_for

        self.assertEqual(
            missing,
            set(),
            f"New methods added to AgentEventStream not accounted for in LeadFormEventStream: {missing}",
        )

    def test_override_parameter_names_match_base(self):
        """Every overridden emit method must accept the exact parameter names of the base method."""
        import inspect
        from app.core.streaming import AgentEventStream
        from app.agents.adzump.agents.leadform.subagent_event_stream import LeadFormEventStream

        for method_name in dir(LeadFormEventStream):
            if not method_name.startswith(("emit_", "request_")):
                continue
            if method_name not in LeadFormEventStream.__dict__:
                continue

            base_params = list(inspect.signature(getattr(AgentEventStream, method_name)).parameters.keys())
            sub_params = list(inspect.signature(getattr(LeadFormEventStream, method_name)).parameters.keys())

            self.assertEqual(
                base_params,
                sub_params,
                f"Parameter mismatch for {method_name}: expected {base_params}, got {sub_params}",
            )







