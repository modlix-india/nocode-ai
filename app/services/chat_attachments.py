"""Persisting the files a chat was given, and reading them back.

Deliberately NOT in `context_manager`. That module's job is to build the string
the LLM sees, and an attachment must never end up there: the bytes already reach
the model as image blocks on the turn they arrive, and a second copy in the
context string would be a stale text description of a picture the model cannot
look at. Keeping the two apart is what stops that happening by accident.

The retention story lives here too, in one number. A chat attachment is uploaded
with a ninety-day lifetime and `FILES_TTL_CLEANUP` -- a Quartz job that already
runs hourly in nocode-saas/worker and already sweeps both file stores -- removes
it when the lifetime runs out. A generated image is uploaded with NO lifetime and
is therefore invisible to that job at any age. No new worker, and nothing that
has to decide for itself which files it is allowed to delete.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Any, Optional

from app.config import settings
from app.core.file_upload import (
    UploadedFile,
    chat_attachment_dir,
    sanitise_file_name,
    upload_bytes,
)
from app.db.connection import get_connection, is_pool_available

logger = logging.getLogger(__name__)

KIND_CHAT = "chat"
KIND_GENERATED = "generated"


async def store_chat_attachments(
    attachments: list[Any],
    *,
    session_id: str,
    turn_number: int,
    client_code: str,
    access_app_code: str,
    headers: dict[str, str] | None = None,
) -> list[dict]:
    """Upload what the user attached and record where it went.

    Stores the ORIGINAL bytes, not the output of `compress_image_base64`.
    Compression exists so the picture fits the model's input limit; the user is
    entitled to get back the file they attached.

    Keeps `type: "file"` entries as well as images. `build_image_blocks` still
    drops those before the model sees them -- this does not make the model able
    to read a PDF -- but the file stops vanishing, which is the part the user
    was complaining about.
    """
    if not attachments:
        return []

    max_bytes = int(settings.MAX_ATTACHMENT_MB * 1024 * 1024)
    stored: list[dict] = []

    for att in attachments:
        data = getattr(att, "data", None)
        if not data:
            continue
        try:
            raw = base64.b64decode(data)
        except ValueError:  # binascii.Error is a subclass
            logger.warning("attachment %r is not valid base64; skipped", getattr(att, "name", ""))
            continue
        if not raw:
            continue
        if len(raw) > max_bytes:
            logger.warning(
                "attachment %r is %.1f MB, over the %.1f MB cap; not stored",
                getattr(att, "name", ""), len(raw) / 1024 / 1024, settings.MAX_ATTACHMENT_MB,
            )
            continue

        mime_type = getattr(att, "mime_type", None)
        file_name = sanitise_file_name(getattr(att, "name", ""), mime_type)
        uploaded = await upload_bytes(
            raw,
            store="secured",
            client_code=client_code,
            file_path=chat_attachment_dir(access_app_code, session_id, turn_number),
            file_name=file_name,
            expires_after_minutes=settings.CHAT_ATTACHMENT_TTL_MINUTES,
            headers=headers,
        )
        if uploaded is None:
            continue

        stored.append(
            {
                "kind": KIND_CHAT,
                "attachment_type": getattr(att, "type", "image") or "image",
                "name": getattr(att, "name", "") or uploaded.name,
                "mime_type": mime_type,
                "store": "secured",
                "uploaded": uploaded,
                "expires_after_minutes": settings.CHAT_ATTACHMENT_TTL_MINUTES,
            }
        )

    if stored:
        await _insert_rows(session_id, turn_number, stored)
    return stored


async def record_generated_asset(
    *,
    session_id: str,
    turn_number: int,
    name: str,
    url: str,
    file_path: str,
    mime_type: str | None = None,
    size_bytes: int | None = None,
) -> None:
    """Note that a tool made this file, for the session it was made in.

    No lifetime, ever. `EXPIRES_AFTER_MINUTES` NULL here mirrors NULL on the
    file's own row, and in both places it means never rather than immediately.
    Generated images are wired into pages that have to keep working.
    """
    if not session_id or not file_path:
        return
    await _insert_rows(
        session_id,
        turn_number,
        [
            {
                "kind": KIND_GENERATED,
                "attachment_type": "image",
                "name": name,
                "mime_type": mime_type,
                "store": "static",
                "uploaded": UploadedFile(
                    name=name, file_path=file_path, url=url, size_bytes=size_bytes or 0
                ),
                "expires_after_minutes": None,
            }
        ],
    )


async def _insert_rows(session_id: str, turn_number: int, rows: list[dict]) -> None:
    """Best-effort. A bookkeeping failure must not disturb a turn in flight."""
    if not is_pool_available():
        return
    try:
        async with get_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.executemany(
                    """
                    INSERT INTO ai_session_attachment (
                        SESSION_ID, TURN_NUMBER, KIND, ATTACHMENT_TYPE, NAME,
                        MIME_TYPE, STORE, FILE_PATH, FILE_URL, SIZE_BYTES,
                        EXPIRES_AFTER_MINUTES
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    -- The unique key is (SESSION_ID, FILE_PATH). A retry that
                    -- re-uploads the same path should refresh the row, not fail
                    -- the insert and lose every other row in the batch.
                    ON DUPLICATE KEY UPDATE
                        FILE_URL = VALUES(FILE_URL),
                        SIZE_BYTES = VALUES(SIZE_BYTES),
                        EXPIRES_AFTER_MINUTES = VALUES(EXPIRES_AFTER_MINUTES)
                    """,
                    [
                        (
                            session_id,
                            turn_number,
                            r["kind"],
                            r["attachment_type"],
                            (r.get("name") or "")[:255],
                            r.get("mime_type"),
                            r["store"],
                            r["uploaded"].file_path[:512],
                            r["uploaded"].url[:768],
                            r["uploaded"].size_bytes,
                            r.get("expires_after_minutes"),
                        )
                        for r in rows
                    ],
                )
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to record %d attachment(s) for %s: %s", len(rows), session_id, e)


async def get_attachments(
    session_id: str, turn_numbers: list[int]
) -> dict[int, list[dict]]:
    """The attachments for these turns, grouped by turn number.

    Empty for every session that predates this table, which is what makes the
    change need no backfill: the bytes for those sessions only ever existed in
    memory, so there is nothing that could have been recovered anyway.
    """
    if not session_id or not turn_numbers or not is_pool_available():
        return {}

    placeholders = ", ".join(["%s"] * len(turn_numbers))
    try:
        async with get_connection() as conn:
            async with conn.cursor() as cursor:
                # Expiry is worked out in SQL, by the same clock and the same
                # expression the files service uses in FileSystemDao.readExpired
                # (`now > UPDATED_AT + EXPIRES_AFTER_MINUTES`). Computing it in
                # Python instead would compare the DB's timestamps against the
                # app process's idea of now, and the two agree only as long as
                # nobody changes a timezone on either side.
                await cursor.execute(
                    f"""
                    SELECT ID, TURN_NUMBER, KIND, ATTACHMENT_TYPE, NAME, MIME_TYPE,
                           STORE, FILE_URL, SIZE_BYTES, UPLOADED_AT,
                           CASE WHEN EXPIRES_AFTER_MINUTES IS NULL THEN NULL
                                ELSE DATE_ADD(UPLOADED_AT,
                                              INTERVAL EXPIRES_AFTER_MINUTES MINUTE)
                           END AS EXPIRES_AT,
                           CASE WHEN EXPIRES_AFTER_MINUTES IS NOT NULL
                                 AND NOW() > DATE_ADD(UPLOADED_AT,
                                                      INTERVAL EXPIRES_AFTER_MINUTES MINUTE)
                                THEN 1 ELSE 0
                           END AS EXPIRED
                    FROM ai_session_attachment
                    WHERE SESSION_ID = %s AND TURN_NUMBER IN ({placeholders})
                    ORDER BY TURN_NUMBER, ID
                    """,
                    (session_id, *turn_numbers),
                )
                rows = await cursor.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to read attachments for %s: %s", session_id, e)
        return {}

    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row[1], []).append(_to_api_shape(row))
    return grouped


def _to_api_shape(row: tuple) -> dict:
    """One DB row as the client wants it.

    `expired` is a claim, not a guarantee, and the client treats it as one: the
    sweep runs hourly and an object delete can fail, so a file can outlive its
    `expires_at` for a while and can also vanish before it. A failed fetch is
    the other half of the signal -- see the render path in LazyPrompt.

    FILE_PATH is deliberately not returned. It is the internal handle used for
    deleting the file, and the browser has no use for it.
    """
    uploaded_at: Optional[datetime] = row[9]
    expires_at: Optional[datetime] = row[10]

    return {
        "id": row[0],
        "kind": row[2],
        "type": row[3],
        "name": row[4],
        "mime_type": row[5],
        "store": row[6],
        "url": row[7],
        "size_bytes": row[8],
        "uploaded_at": uploaded_at.isoformat() if uploaded_at else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "expired": bool(row[11]),
    }
