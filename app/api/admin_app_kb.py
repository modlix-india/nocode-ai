"""Admin endpoints for per-app KB cross-env promotion.

Two endpoints, both guarded by a shared `X-Admin-Token` header (settings.
ADMIN_TOKEN). Designed for use by scripts/promote_app_kb.py and by ad-hoc ops:

  POST /api/ai/admin/app-kb/export
       body: {"client_code": "...", "app_code": "..."}
       returns the portable snapshot dict from app_kb.export_app()

  POST /api/ai/admin/app-kb/import
       body: {"client_code": ..., "app_code": ..., "snapshot": {...},
              "mode": "overwrite"|"merge", "promotion_note": "..."}
       calls app_kb.import_snapshot() and returns the row counts.

Direction-of-flow safety lives in the CLIENT (promote_app_kb.py), not here —
this endpoint just provides the data plumbing.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.services import app_kb

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai/admin/app-kb", tags=["admin"])


def _require_admin_token(x_admin_token: Optional[str] = Header(default=None)) -> str:
    """Per-env admin token. Set via settings.ADMIN_TOKEN. No fallback default
    so an unconfigured env is hard-fail (better than silent insecure exposure)."""
    expected = getattr(settings, "ADMIN_TOKEN", "") or ""
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Admin endpoints disabled: settings.ADMIN_TOKEN is not configured.",
        )
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token")
    return x_admin_token


class ExportRequest(BaseModel):
    client_code: str
    app_code: str


class ImportRequest(BaseModel):
    client_code: str
    app_code: str
    snapshot: dict[str, Any]
    mode: Literal["overwrite", "merge"] = "overwrite"
    promotion_note: str = ""
    updated_by: int = 1  # admin user id used to stamp the imported rows


@router.post("/export")
async def export_app(
    body: ExportRequest, _token: str = Depends(_require_admin_token),
) -> dict[str, Any]:
    """Read-only snapshot of per-app KB rows for one (client, app)."""
    snapshot = await app_kb.export_app(body.client_code, body.app_code)
    return {
        "client_code": body.client_code,
        "app_code": body.app_code,
        "row_count": len(snapshot.get("rows") or []),
        "snapshot": snapshot,
    }


@router.post("/import")
async def import_app(
    body: ImportRequest, _token: str = Depends(_require_admin_token),
) -> dict[str, Any]:
    """Apply a snapshot to the destination (client, app)."""
    note = body.promotion_note or f"Promoted via /admin/app-kb/import ({body.mode})"
    counters = await app_kb.import_snapshot(
        body.snapshot,
        target_client=body.client_code,
        target_app=body.app_code,
        updated_by=body.updated_by,
        promotion_note=note,
        # mode='overwrite' default is the no-op-on-same-body behavior already
        # encoded in import_snapshot via skip_if_same=True. mode='merge' will
        # land when we add the field-level merge logic in a follow-up.
        skip_if_same=True,
    )
    return {
        "client_code": body.client_code,
        "app_code": body.app_code,
        "mode": body.mode,
        "counters": counters,
    }
