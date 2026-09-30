"""Chat attachments reach secured storage at a path a browser can read back.

Two things here are load-bearing and neither is obvious from the call site.

**The `_withInClient` prefix is not decoration.**
`SecuredFileResourceService.checkReadAccessWithClientCode` short-circuits its
`files_access_path` lookup for exactly four special first folders and falls
through to `hasReadAccess` for everything else. A path under a plain prefix like
`/aichat/...` therefore 403s for every user until somebody seeds access-path
rows for it. WhatsApp's `/whatsapp/{app}/...` paths get away without one only
because the bridge reads them back server-side through the internal endpoint,
which a browser cannot reach. Ours are fetched by the chat, so the prefix has to
be there. Losing it would not fail here; it would fail as an image that never
loads for anyone.

**NULL lifetime means never, in both tables.**
`FILES_TTL_CLEANUP` deletes only files that were given an `EXPIRES_AFTER_MINUTES`,
so a generated image uploaded without one is invisible to it at any age. That is
the whole mechanism keeping retention off generated work, and it is one keyword
argument away from being wrong in either direction.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.core.file_upload import (
    SECURED_ROOT,
    UploadedFile,
    chat_attachment_dir,
    sanitise_file_name,
)
from app.services import chat_attachments


def _attachment(name="hero.png", mime="image/png", data=b"\x89PNG-bytes", type_="image"):
    return SimpleNamespace(
        type=type_, name=name, mime_type=mime, data=base64.b64encode(data).decode()
    )


# ── Path shape ──────────────────────────────────────────────────


class TestSecuredPath:
    def test_lives_under_withinclient(self):
        """See the module docstring: without this the image 403s for everyone."""
        assert SECURED_ROOT == "_withInClient"
        assert chat_attachment_dir("appbuilder", "FIN_abc", 1).startswith(
            "/_withInClient/"
        )

    def test_carries_app_session_and_turn(self):
        assert chat_attachment_dir("sitezump", "FIN_abc", 7) == (
            "/_withInClient/aichat/sitezump/FIN_abc/t7"
        )

    def test_defaults_the_app_rather_than_emitting_an_empty_segment(self):
        # access_app_code is routinely absent; an empty segment would produce a
        # double slash and a path the files service resolves differently.
        assert "//" not in chat_attachment_dir("", "FIN_abc", 1)


class TestFileNames:
    def test_path_separators_cannot_escape_the_directory(self):
        assert "/" not in sanitise_file_name("../../etc/passwd")
        assert ".." not in sanitise_file_name("../../etc/passwd")

    def test_two_files_of_the_same_name_do_not_collide(self):
        # createFileFromInputStream answers a name clash without `override` by
        # returning the EXISTING file and a 200, so a collision would silently
        # hand back the wrong image rather than failing.
        assert sanitise_file_name("image.png") != sanitise_file_name("image.png")

    def test_empty_name_gets_an_extension_from_the_mime_type(self):
        assert sanitise_file_name("", "application/pdf").endswith(".pdf")

    def test_the_javascript_stringifications_of_nothing_are_not_names(self):
        # `undefined` reaching a file name is a whole genus of bug in this
        # platform; the files service refuses it outright.
        assert "undefined" not in sanitise_file_name("undefined")

    def test_long_names_stay_within_the_index_ceiling(self):
        # FILE_PATH is a 768-byte unique key on ai_session_attachment.
        assert len(sanitise_file_name("a" * 500 + ".png")) <= 120


# ── Lifetimes ───────────────────────────────────────────────────


class TestRetention:
    def test_ninety_days(self):
        assert settings.CHAT_ATTACHMENT_TTL_MINUTES == 90 * 24 * 60 == 129_600

    @pytest.mark.asyncio
    async def test_a_chat_attachment_is_uploaded_with_that_lifetime(self):
        uploaded = UploadedFile(
            name="hero.png", file_path="/p/hero.png", url="/api/files/x", size_bytes=9
        )
        with patch.object(
            chat_attachments, "upload_bytes", AsyncMock(return_value=uploaded)
        ) as up, patch.object(chat_attachments, "_insert_rows", AsyncMock()):
            await chat_attachments.store_chat_attachments(
                [_attachment()],
                session_id="FIN_abc",
                turn_number=1,
                client_code="FIN",
                access_app_code="appbuilder",
            )
        assert up.await_args.kwargs["expires_after_minutes"] == 129_600
        assert up.await_args.kwargs["store"] == "secured"

    @pytest.mark.asyncio
    async def test_a_generated_image_is_recorded_with_no_lifetime(self):
        """NULL is what makes FILES_TTL_CLEANUP unable to touch it, at any age."""
        with patch.object(chat_attachments, "_insert_rows", AsyncMock()) as ins:
            await chat_attachments.record_generated_asset(
                session_id="FIN_abc",
                turn_number=2,
                name="hero.png",
                url="/api/files/static/file/FIN/app/hero.png",
                file_path="/app/hero.png",
            )
        row = ins.await_args.args[2][0]
        assert row["expires_after_minutes"] is None
        assert row["kind"] == chat_attachments.KIND_GENERATED
        assert row["store"] == "static"


# ── What gets stored ────────────────────────────────────────────


class TestSelection:
    @pytest.mark.asyncio
    async def test_the_client_code_comes_from_the_caller_not_the_attachment(self):
        # The files endpoint behind this is permitAll inside the cluster, so
        # this argument is the only thing deciding whose storage is written.
        uploaded = UploadedFile(name="a", file_path="/p/a", url="/u", size_bytes=1)
        with patch.object(
            chat_attachments, "upload_bytes", AsyncMock(return_value=uploaded)
        ) as up, patch.object(chat_attachments, "_insert_rows", AsyncMock()):
            await chat_attachments.store_chat_attachments(
                [_attachment()],
                session_id="s",
                turn_number=1,
                client_code="FIN",
                access_app_code="appbuilder",
            )
        assert up.await_args.kwargs["client_code"] == "FIN"

    @pytest.mark.asyncio
    async def test_non_image_files_are_stored_too(self):
        """`build_image_blocks` drops these before the model sees them. That is
        still true and out of scope; the point is the file stops vanishing."""
        uploaded = UploadedFile(name="c.pdf", file_path="/p/c.pdf", url="/u", size_bytes=4)
        with patch.object(
            chat_attachments, "upload_bytes", AsyncMock(return_value=uploaded)
        ), patch.object(chat_attachments, "_insert_rows", AsyncMock()):
            stored = await chat_attachments.store_chat_attachments(
                [_attachment(name="contract.pdf", mime="application/pdf", type_="file")],
                session_id="s",
                turn_number=1,
                client_code="FIN",
                access_app_code="appbuilder",
            )
        assert len(stored) == 1
        assert stored[0]["attachment_type"] == "file"

    @pytest.mark.asyncio
    async def test_an_oversized_attachment_is_skipped_not_raised(self):
        big = _attachment(data=b"x" * (int(settings.MAX_ATTACHMENT_MB * 1024 * 1024) + 1))
        with patch.object(chat_attachments, "upload_bytes", AsyncMock()) as up:
            stored = await chat_attachments.store_chat_attachments(
                [big],
                session_id="s",
                turn_number=1,
                client_code="FIN",
                access_app_code="appbuilder",
            )
        assert stored == []
        up.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_upload_drops_that_file_and_keeps_the_rest(self):
        ok = UploadedFile(name="b", file_path="/p/b", url="/u", size_bytes=1)
        with patch.object(
            chat_attachments, "upload_bytes", AsyncMock(side_effect=[None, ok])
        ), patch.object(chat_attachments, "_insert_rows", AsyncMock()):
            stored = await chat_attachments.store_chat_attachments(
                [_attachment(name="a.png"), _attachment(name="b.png")],
                session_id="s",
                turn_number=1,
                client_code="FIN",
                access_app_code="appbuilder",
            )
        assert len(stored) == 1

    @pytest.mark.asyncio
    async def test_garbage_base64_is_skipped_not_raised(self):
        bad = SimpleNamespace(
            type="image", name="x.png", mime_type="image/png", data="not!base64!"
        )
        with patch.object(chat_attachments, "upload_bytes", AsyncMock()) as up:
            assert (
                await chat_attachments.store_chat_attachments(
                    [bad],
                    session_id="s",
                    turn_number=1,
                    client_code="FIN",
                    access_app_code="appbuilder",
                )
                == []
            )
        up.assert_not_awaited()


# ── The API shape the chat renders from ─────────────────────────


class TestApiShape:
    def test_expiry_is_reported_but_the_internal_path_is_not(self):
        import datetime as dt

        row = (
            1, 4, "chat", "image", "hero.png", "image/png", "secured",
            "/api/files/secured/file/FIN/x.png", 99,
            dt.datetime(2026, 1, 1, 0, 0), dt.datetime(2026, 4, 1, 0, 0), 1,
        )
        shape = chat_attachments._to_api_shape(row)
        assert shape["expired"] is True
        assert shape["expires_at"].startswith("2026-04-01")
        # FILE_PATH is the delete handle; the browser has no use for it.
        assert "file_path" not in shape

    def test_a_permanent_asset_reports_no_expiry(self):
        import datetime as dt

        row = (
            2, 4, "generated", "image", "hero.png", "image/png", "static",
            "/api/files/static/file/FIN/x.png", 99,
            dt.datetime(2026, 1, 1, 0, 0), None, 0,
        )
        shape = chat_attachments._to_api_shape(row)
        assert shape["expires_at"] is None
        assert shape["expired"] is False
