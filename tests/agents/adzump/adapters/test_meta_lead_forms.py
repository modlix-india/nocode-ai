"""Unit tests for MetaLeadFormsAdapter token handling and Graph API interactions."""

import unittest
from unittest.mock import AsyncMock, patch

from app.agents.adzump.adapters.meta.lead_forms import MetaLeadFormsAdapter


class TestMetaLeadFormsAdapterTokenHandling(unittest.IsolatedAsyncioTestCase):
    """Verifies that adapter operations reuse provided page tokens and only fall back to resolution when omitted."""

    def setUp(self):
        self.adapter = MetaLeadFormsAdapter()
        self.page_id = "page_12345"
        self.client_code = "client_abc"
        self.auth_headers = {"Authorization": "Bearer user_token"}

    @patch("app.agents.adzump.adapters.meta.lead_forms.meta_client.get", new_callable=AsyncMock)
    @patch.object(MetaLeadFormsAdapter, "_get_page_token", new_callable=AsyncMock)
    async def test_get_leadgen_forms_uses_provided_page_token(self, mock_get_token, mock_meta_get):
        mock_meta_get.return_value = {"data": [{"id": "form_1", "status": "ACTIVE"}]}

        result = await self.adapter.get_leadgen_forms(
            page_id=self.page_id,
            client_code=self.client_code,
            auth_headers=self.auth_headers,
            page_token="explicit_page_token_abc",
        )

        mock_get_token.assert_not_called()
        mock_meta_get.assert_called_once()
        self.assertEqual(mock_meta_get.call_args.kwargs["access_token"], "explicit_page_token_abc")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "form_1")

    @patch("app.agents.adzump.adapters.meta.lead_forms.meta_client.get", new_callable=AsyncMock)
    @patch.object(MetaLeadFormsAdapter, "_get_page_token", new_callable=AsyncMock)
    async def test_get_leadgen_forms_falls_back_when_page_token_omitted(self, mock_get_token, mock_meta_get):
        mock_get_token.return_value = "resolved_page_token_xyz"
        mock_meta_get.return_value = {"data": []}

        result = await self.adapter.get_leadgen_forms(
            page_id=self.page_id,
            client_code=self.client_code,
            auth_headers=self.auth_headers,
        )

        mock_get_token.assert_called_once_with(self.page_id, self.client_code, self.auth_headers)
        self.assertEqual(mock_meta_get.call_args.kwargs["access_token"], "resolved_page_token_xyz")
        self.assertEqual(result, [])

    @patch("app.agents.adzump.adapters.meta.lead_forms.meta_client.post", new_callable=AsyncMock)
    @patch.object(MetaLeadFormsAdapter, "_get_page_token", new_callable=AsyncMock)
    async def test_create_leadgen_form_uses_provided_page_token(self, mock_get_token, mock_meta_post):
        mock_meta_post.return_value = {"id": "new_form_999"}

        result = await self.adapter.create_leadgen_form(
            page_id=self.page_id,
            form_payload={"name": "Test Form"},
            client_code=self.client_code,
            auth_headers=self.auth_headers,
            page_token="explicit_page_token_abc",
        )

        mock_get_token.assert_not_called()
        mock_meta_post.assert_called_once()
        self.assertEqual(mock_meta_post.call_args.kwargs["access_token"], "explicit_page_token_abc")
        self.assertEqual(result["id"], "new_form_999")

    @patch("app.agents.adzump.adapters.meta.lead_forms.meta_client.post", new_callable=AsyncMock)
    @patch.object(MetaLeadFormsAdapter, "_get_page_token", new_callable=AsyncMock)
    async def test_create_leadgen_form_falls_back_when_page_token_omitted(self, mock_get_token, mock_meta_post):
        mock_get_token.return_value = "resolved_page_token_xyz"
        mock_meta_post.return_value = {"id": "new_form_999"}

        result = await self.adapter.create_leadgen_form(
            page_id=self.page_id,
            form_payload={"name": "Test Form"},
            client_code=self.client_code,
            auth_headers=self.auth_headers,
        )

        mock_get_token.assert_called_once_with(self.page_id, self.client_code, self.auth_headers)
        self.assertEqual(mock_meta_post.call_args.kwargs["access_token"], "resolved_page_token_xyz")
        self.assertEqual(result["id"], "new_form_999")

    @patch("app.agents.adzump.adapters.meta.lead_forms.meta_client.post", new_callable=AsyncMock)
    @patch.object(MetaLeadFormsAdapter, "_get_page_token", new_callable=AsyncMock)
    async def test_upload_cover_photo_uses_provided_page_token(self, mock_get_token, mock_meta_post):
        mock_meta_post.return_value = {"id": "photo_123", "source": "https://fb.com/pic.jpg"}

        result = await self.adapter.upload_cover_photo(
            page_id=self.page_id,
            file_bytes=b"fake_image_bytes",
            filename="cover.jpg",
            content_type="image/jpeg",
            client_code=self.client_code,
            auth_headers=self.auth_headers,
            page_token="explicit_page_token_abc",
        )

        mock_get_token.assert_not_called()
        mock_meta_post.assert_called_once()
        self.assertEqual(mock_meta_post.call_args.kwargs["access_token"], "explicit_page_token_abc")
        self.assertEqual(result["photo_id"], "photo_123")
        self.assertEqual(result["source_url"], "https://fb.com/pic.jpg")

    @patch("app.agents.adzump.adapters.meta.lead_forms.meta_client.post", new_callable=AsyncMock)
    @patch.object(MetaLeadFormsAdapter, "_get_page_token", new_callable=AsyncMock)
    async def test_upload_cover_photo_falls_back_when_page_token_omitted(self, mock_get_token, mock_meta_post):
        mock_get_token.return_value = "resolved_page_token_xyz"
        mock_meta_post.return_value = {"id": "photo_123", "source": "https://fb.com/pic.jpg"}

        result = await self.adapter.upload_cover_photo(
            page_id=self.page_id,
            file_bytes=b"fake_image_bytes",
            filename="cover.jpg",
            content_type="image/jpeg",
            client_code=self.client_code,
            auth_headers=self.auth_headers,
        )

        mock_get_token.assert_called_once_with(self.page_id, self.client_code, self.auth_headers)
        self.assertEqual(mock_meta_post.call_args.kwargs["access_token"], "resolved_page_token_xyz")
        self.assertEqual(result["photo_id"], "photo_123")

    @patch("app.agents.adzump.adapters.meta.lead_forms.meta_client.get", new_callable=AsyncMock)
    async def test_get_page_info_queries_up_to_300_accounts(self, mock_meta_get):
        mock_meta_get.return_value = {
            "data": [
                {
                    "id": self.page_id,
                    "access_token": "tok_300",
                    "picture": {"data": {"url": "https://img.meta.com/page.jpg"}},
                }
            ]
        }

        result = await self.adapter.get_page_info(
            page_id=self.page_id,
            client_code=self.client_code,
            auth_headers=self.auth_headers,
        )

        mock_meta_get.assert_called_once()
        self.assertEqual(mock_meta_get.call_args.kwargs["params"]["limit"], 300)
        self.assertEqual(result["access_token"], "tok_300")
        self.assertEqual(result["picture_url"], "https://img.meta.com/page.jpg")

    @patch("app.agents.adzump.adapters.meta.lead_forms.meta_client.get", new_callable=AsyncMock)
    async def test_get_leadgen_forms_multi_page_pagination_and_sorting(self, mock_meta_get):
        mock_meta_get.side_effect = [
            {
                "data": [
                    {"id": "form_1", "status": "ACTIVE", "leads_count": 10},
                    {"id": "form_2", "status": "ACTIVE", "leads_count": 50},
                ],
                "paging": {
                    "next": "https://graph.facebook.com/v22.0/next_page",
                    "cursors": {"after": "cur_123"},
                },
            },
            {
                "data": [
                    {"id": "form_3", "status": "ACTIVE", "leads_count": 120},
                ],
                "paging": {},
            },
        ]

        result = await self.adapter.get_leadgen_forms(
            page_id=self.page_id,
            client_code=self.client_code,
            auth_headers=self.auth_headers,
            page_token="tok_abc",
        )

        self.assertEqual(mock_meta_get.call_count, 2)
        # First call has no 'after' parameter
        self.assertNotIn("after", mock_meta_get.call_args_list[0].kwargs["params"])
        # Second call passes after='cur_123'
        self.assertEqual(mock_meta_get.call_args_list[1].kwargs["params"]["after"], "cur_123")
        # Forms sorted by leads_count descending: 120 -> 50 -> 10
        self.assertEqual([f["id"] for f in result], ["form_3", "form_2", "form_1"])

    @patch("app.agents.adzump.adapters.meta.lead_forms.meta_client.get", new_callable=AsyncMock)
    async def test_get_leadgen_forms_respects_max_ceiling(self, mock_meta_get):
        # Return 300 forms each time
        mock_meta_get.side_effect = [
            {
                "data": [{"id": f"form_{i}", "status": "ACTIVE", "leads_count": i} for i in range(300)],
                "paging": {
                    "next": "https://graph.facebook.com/next",
                    "cursors": {"after": "cursor_batch1"},
                },
            },
            {
                "data": [{"id": f"form_{i}", "status": "ACTIVE", "leads_count": i} for i in range(300, 600)],
                "paging": {
                    "next": "https://graph.facebook.com/next2",
                    "cursors": {"after": "cursor_batch2"},
                },
            },
        ]

        result = await self.adapter.get_leadgen_forms(
            page_id=self.page_id,
            client_code=self.client_code,
            auth_headers=self.auth_headers,
            page_token="tok_abc",
        )

        self.assertEqual(len(result), 500)
        # Should stop without making a third call
        self.assertEqual(mock_meta_get.call_count, 2)

    @patch("app.agents.adzump.adapters.meta.lead_forms.meta_client.get", new_callable=AsyncMock)
    async def test_get_page_info_multi_page_found_on_second_page(self, mock_meta_get):
        mock_meta_get.side_effect = [
            {
                "data": [
                    {"id": "other_page_1", "access_token": "tok_other"},
                ],
                "paging": {
                    "next": "https://graph.facebook.com/v22.0/next_accounts",
                    "cursors": {"after": "acc_cur_1"},
                },
            },
            {
                "data": [
                    {
                        "id": self.page_id,
                        "access_token": "found_page_tok",
                        "picture": {"data": {"url": "https://img.meta.com/my_page.jpg"}},
                    },
                ],
                "paging": {
                    "next": "https://graph.facebook.com/v22.0/next_accounts_page3",
                    "cursors": {"after": "acc_cur_2"},
                },
            },
        ]

        result = await self.adapter.get_page_info(
            page_id=self.page_id,
            client_code=self.client_code,
            auth_headers=self.auth_headers,
        )

        # Early exit: found on page 2, so it shouldn't query page 3
        self.assertEqual(mock_meta_get.call_count, 2)
        self.assertEqual(mock_meta_get.call_args_list[1].kwargs["params"]["after"], "acc_cur_1")
        self.assertEqual(result["access_token"], "found_page_tok")
        self.assertEqual(result["picture_url"], "https://img.meta.com/my_page.jpg")

    @patch("app.agents.adzump.adapters.meta.lead_forms.meta_client.get", new_callable=AsyncMock)
    async def test_get_page_info_fallback_when_not_in_any_page(self, mock_meta_get):
        mock_meta_get.side_effect = [
            # Page 1 of accounts
            {
                "data": [{"id": "other_1"}],
                "paging": {
                    "next": "https://graph.facebook.com/v22.0/next",
                    "cursors": {"after": "c1"},
                },
            },
            # Page 2 of accounts - exhausted
            {
                "data": [{"id": "other_2"}],
                "paging": {},
            },
            # Fallback to direct /{page_id}
            {
                "id": self.page_id,
                "access_token": "direct_fallback_tok",
                "picture": {"data": {"url": "https://img.meta.com/direct.jpg"}},
            },
        ]

        result = await self.adapter.get_page_info(
            page_id=self.page_id,
            client_code=self.client_code,
            auth_headers=self.auth_headers,
        )

        self.assertEqual(mock_meta_get.call_count, 3)
        self.assertEqual(mock_meta_get.call_args_list[2].args[0], f"/{self.page_id}")
        self.assertEqual(result["access_token"], "direct_fallback_tok")
        self.assertEqual(result["picture_url"], "https://img.meta.com/direct.jpg")

    @patch.object(MetaLeadFormsAdapter, "_get_page_token", new_callable=AsyncMock)
    async def test_adapter_methods_raise_unified_error_with_valid_permissions(self, mock_get_token):
        mock_get_token.return_value = None

        # Verify get_leadgen_forms
        with self.assertRaises(RuntimeError) as ctx_get:
            await self.adapter.get_leadgen_forms(
                page_id=self.page_id,
                client_code=self.client_code,
                auth_headers=self.auth_headers,
            )
        err_msg = str(ctx_get.exception)
        self.assertIn("pages_manage_ads", err_msg)
        self.assertIn("pages_read_engagement", err_msg)
        self.assertIn("leads_retrieval", err_msg)
        self.assertNotIn("manage_pages", err_msg)

        # Verify create_leadgen_form
        with self.assertRaises(RuntimeError) as ctx_create:
            await self.adapter.create_leadgen_form(
                page_id=self.page_id,
                form_payload={"name": "Test"},
                client_code=self.client_code,
                auth_headers=self.auth_headers,
            )
        self.assertEqual(str(ctx_create.exception), err_msg)

        # Verify upload_cover_photo
        with self.assertRaises(RuntimeError) as ctx_upload:
            await self.adapter.upload_cover_photo(
                page_id=self.page_id,
                file_bytes=b"fake",
                filename="pic.jpg",
                content_type="image/jpeg",
                client_code=self.client_code,
                auth_headers=self.auth_headers,
            )
        self.assertEqual(str(ctx_upload.exception), err_msg)


class TestMetaClient(unittest.IsolatedAsyncioTestCase):
    """Verifies MetaClient connection pooling, API versioning, and error detail preservation."""

    def test_version_and_base_url_configuration(self):
        from app.agents.adzump.adapters.meta.client import (
            META_GRAPH_API_VERSION,
            META_BASE_URL,
            MetaClient,
        )
        self.assertEqual(META_GRAPH_API_VERSION, "v22.0")
        self.assertIn("v22.0", META_BASE_URL)
        self.assertEqual(MetaClient.GRAPH_API_VERSION, "v22.0")

    def test_raise_for_meta_error_success_response_does_nothing(self):
        import httpx
        from app.agents.adzump.adapters.meta.client import _raise_for_meta_error

        resp = httpx.Response(status_code=200, json={"success": True})
        _raise_for_meta_error(resp)

    def test_raise_for_meta_error_preserves_code_subcode_and_fbtrace_id(self):
        import httpx
        from app.agents.adzump.adapters.meta.client import _raise_for_meta_error

        resp = httpx.Response(
            status_code=400,
            json={
                "error": {
                    "message": "Invalid OAuth access token.",
                    "type": "OAuthException",
                    "code": 190,
                    "error_subcode": 463,
                    "fbtrace_id": "Ac_N4yQ_94k",
                }
            },
        )
        with self.assertRaises(RuntimeError) as ctx:
            _raise_for_meta_error(resp)

        msg = str(ctx.exception)
        self.assertIn("code 190, subcode 463", msg)
        self.assertIn("Invalid OAuth access token.", msg)
        self.assertIn("fbtrace_id: Ac_N4yQ_94k", msg)

    def test_raise_for_meta_error_handles_html_non_json_body(self):
        import httpx
        from app.agents.adzump.adapters.meta.client import _raise_for_meta_error

        resp = httpx.Response(
            status_code=502,
            content=b"<html><body>Bad Gateway 502</body></html>",
            headers={"Content-Type": "text/html"},
        )
        with self.assertRaises(RuntimeError) as ctx:
            _raise_for_meta_error(resp)

        msg = str(ctx.exception)
        self.assertIn("502", msg)
        self.assertIn("Bad Gateway", msg)

    async def test_client_reuses_underlying_async_client_session(self):
        from app.agents.adzump.adapters.meta.client import MetaClient

        client = MetaClient()
        c1 = client._get_client()
        c2 = client._get_client()
        self.assertIs(c1, c2)
        await client.close()
        self.assertIsNone(client._client)


class TestMetaLeadFormsAdapterRetry(unittest.IsolatedAsyncioTestCase):
    """Unit tests for MetaLeadFormsAdapter._call_with_retry."""

    def setUp(self):
        self.adapter = MetaLeadFormsAdapter()

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_read_operation_recovers_after_transient_500(self, mock_sleep):
        mock_fn = AsyncMock(side_effect=[
            RuntimeError("Meta Graph API 500: code 2 - Service temporarily unavailable"),
            {"data": [{"id": "form_ok"}]},
        ])

        result = await self.adapter._call_with_retry(mock_fn, "arg1", kwarg="val")
        self.assertEqual(result, {"data": [{"id": "form_ok"}]})
        self.assertEqual(mock_fn.call_count, 2)
        mock_sleep.assert_called_once()
        # Verify delay is ~1s (2^0 + jitter between 0.1 and 0.4)
        slept = mock_sleep.call_args[0][0]
        self.assertGreaterEqual(slept, 1.0)
        self.assertLess(slept, 1.5)

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_read_operation_recovers_after_rate_limit_429(self, mock_sleep):
        mock_fn = AsyncMock(side_effect=[
            RuntimeError("Meta Graph API 429: code 17 - User request limit reached"),
            {"data": []},
        ])

        result = await self.adapter._call_with_retry(mock_fn)
        self.assertEqual(result, {"data": []})
        self.assertEqual(mock_fn.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_fast_fail_on_client_400_validation_error(self, mock_sleep):
        mock_fn = AsyncMock(
            side_effect=RuntimeError("Meta Graph API 400: code 100 - Invalid parameter name")
        )

        with self.assertRaises(RuntimeError) as ctx:
            await self.adapter._call_with_retry(mock_fn)

        self.assertIn("400", str(ctx.exception))
        self.assertEqual(mock_fn.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_fast_fail_on_auth_401_error(self, mock_sleep):
        mock_fn = AsyncMock(
            side_effect=RuntimeError("Meta Graph API 401: code 190 - Invalid OAuth access token")
        )

        with self.assertRaises(RuntimeError) as ctx:
            await self.adapter._call_with_retry(mock_fn)

        self.assertIn("code 190", str(ctx.exception))
        self.assertEqual(mock_fn.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_mutation_does_not_retry_to_prevent_duplicate_forms(self, mock_sleep):
        mock_fn = AsyncMock(
            side_effect=RuntimeError("Meta Graph API 500: Server timeout during form write")
        )

        with self.assertRaises(RuntimeError) as ctx:
            await self.adapter._call_with_retry(mock_fn, is_mutation=True)

        self.assertIn("500", str(ctx.exception))
        self.assertEqual(mock_fn.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_exhausted_retries_raises_original_error(self, mock_sleep):
        mock_fn = AsyncMock(
            side_effect=RuntimeError("Meta Graph API 503: code 2 - Service unavailable")
        )

        with self.assertRaises(RuntimeError) as ctx:
            await self.adapter._call_with_retry(mock_fn, max_retries=3)

        self.assertIn("503", str(ctx.exception))
        self.assertEqual(mock_fn.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()



