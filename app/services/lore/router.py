"""HTTP surface for lore, mounted at /api/ai/lore.

Consumers:
  - App Builder's workspace sidebar ("what is known about this app")
  - the object editors ("what is known about this page")
  - the docs surface
  - a scheduled sweep that calls /curate

Auth reuses the shared agent auth dependency, so every request is scoped to the
caller's client and the app they name. There is no cross-tenant read.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import settings
from app.core.base_auth import require_auth_context
from app.core.session import AuthContext
from app.services.lore import access, curator, ingest, retrieval, store
from app.services.lore.access import LoreAccessError, LoreScope
from app.services.lore.models import (
    ENTRY_KIND_HELP,
    ENTRY_KINDS,
    OBSERVATION_KINDS,
    normalise_subject,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _read_scope(auth: AuthContext, app_code: str) -> LoreScope:
    """Resolve what this caller may do with this app's lore, or refuse.

    The client code always comes from the verified token, never from the
    request: the caller chooses which app to look at, not whose knowledge they
    write. App read access is checked against the security service every time.
    """
    try:
        scope = await access.resolve_scope(auth, app_code)
        scope.require_read()
        return scope
    except LoreAccessError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


async def _write_scope(auth: AuthContext, app_code: str) -> LoreScope:
    """As `_read_scope`, and additionally require EDIT access on the app."""
    try:
        scope = await access.resolve_scope(auth, app_code)
        scope.require_write()
        return scope
    except LoreAccessError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


# ── Reads ────────────────────────────────────────────────────────────────


@router.get("/brief")
async def get_brief(
    app_code: str = Query(..., description="The app to brief on"),
    subject: Optional[str] = Query(None, description="'<type>:<name>' to narrow to one object"),
    budget: int = Query(retrieval.BRIEF_BUDGET, ge=500, le=40000),
    include_unverified: bool = Query(True),
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Markdown briefing: what someone needs to know before working on this app."""
    scope = await _read_scope(auth, app_code)
    result = await retrieval.brief(
        scope, subject=subject, budget=budget,
        include_unverified=include_unverified,
    )
    return {**result, "scope": scope.to_dict()}


@router.get("/search")
async def get_search(
    app_code: str = Query(...),
    q: str = Query(..., min_length=1, description="What you want to know"),
    limit: int = Query(12, ge=1, le=100),
    kind: Optional[str] = Query(None, description="Restrict to one entry kind"),
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    if kind and kind not in ENTRY_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind '{kind}'. Valid: {', '.join(ENTRY_KINDS)}")
    scope = await _read_scope(auth, app_code)
    return await retrieval.search(scope, q, limit=limit, kinds=[kind] if kind else None)


@router.get("/about")
async def get_about(
    app_code: str = Query(...),
    subject: str = Query(..., description="'<type>:<name>', e.g. page:jobsToday"),
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    scope = await _read_scope(auth, app_code)
    return await retrieval.about(scope, subject)


@router.get("/entries")
async def list_entries(
    app_code: str = Query(...),
    kind: Optional[str] = Query(None),
    subject: Optional[str] = Query(None),
    status: str = Query("active", description="active|superseded|retired|draft|any"),
    limit: int = Query(100, ge=1, le=500),
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    scope = await _read_scope(auth, app_code)
    entries = await store.list_entries(
        scope.read_chain, scope.app_code,
        kinds=[kind] if kind else None,
        subject=normalise_subject(subject) if subject else None,
        status=status, limit=limit,
    )
    return {
        "count": len(entries),
        "entries": [e.to_dict() for e in entries],
        "scope": scope.to_dict(),
    }


async def _entry_in_scope(
    auth: AuthContext, entry_id: int, *, for_write: bool,
) -> tuple[Any, LoreScope]:
    """Load an entry and the caller's scope for its app, or 404/403.

    Visibility is by APP, not by owning client: an entry inherited from the app
    owner is one this caller can legitimately see and (with edit access) fork.
    Refusing it because `entry.client_code != auth.client_code` would make an
    overriding client unable to correct anything they were given the app for.
    """
    stored = await store.get_entry(entry_id)
    if not stored:
        raise HTTPException(status_code=404, detail=f"No lore entry {entry_id}")
    scope = await (_write_scope if for_write else _read_scope)(auth, stored.app_code)
    if stored.client_code not in scope.read_chain:
        raise HTTPException(status_code=404, detail=f"No lore entry {entry_id}")
    return stored, scope


@router.get("/entries/{entry_id}")
async def get_entry(
    entry_id: int,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """One entry with its full provenance: sources, revision history, links."""
    stored, scope = await _entry_in_scope(auth, entry_id, for_write=False)
    result = await retrieval.provenance(entry_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return {
        **result,
        "inherited": not scope.owns(stored.client_code),
        "editable_in_place": scope.owns(stored.client_code) and scope.can_write,
        "scope": scope.to_dict(),
    }


@router.get("/gaps")
async def get_gaps(
    app_code: str = Query(...),
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """What lore does not know yet, so a person can fill it in."""
    scope = await _read_scope(auth, app_code)
    return await retrieval.gaps(scope)


@router.get("/stats")
async def get_stats(
    app_code: str = Query(...),
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    scope = await _read_scope(auth, app_code)
    client_code, app_code = scope.client_code, scope.app_code
    return {
        "scope": scope.to_dict(),
        **await store.stats(client_code, app_code),
        "pending_observations": await store.count_pending(client_code, app_code),
        "recent_runs": await store.recent_runs(client_code, app_code, limit=5),
    }


@router.get("/taxonomy")
async def get_taxonomy() -> dict[str, Any]:
    """The vocabulary, so a UI can render pickers without hard-coding it."""
    return {
        "entry_kinds": [{"kind": k, "description": v} for k, v in ENTRY_KIND_HELP.items()],
        "observation_kinds": list(OBSERVATION_KINDS),
    }


# ── Writes ───────────────────────────────────────────────────────────────


class ObserveRequest(BaseModel):
    app_code: str
    kind: str = Field("manual", description="|".join(OBSERVATION_KINDS))
    source: str = Field("api", max_length=160)
    subject: str = "app"
    body: str = Field(..., min_length=1)
    meta: Optional[dict[str, Any]] = None


@router.post("/observe")
async def post_observe(
    body: ObserveRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Record a raw observation. Curation turns it into knowledge later."""
    scope = await _write_scope(auth, body.app_code)
    client_code, app_code = scope.client_code, scope.app_code
    if body.kind not in OBSERVATION_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown kind '{body.kind}'. Valid: {', '.join(OBSERVATION_KINDS)}",
        )
    result = await store.record_observation(
        client_code, app_code,
        kind=body.kind, source=body.source, subject=body.subject,
        body=body.body, meta=body.meta,
        observed_by=int(getattr(auth, "user_id", 0) or 0),
    )
    return result


class NoteRequest(BaseModel):
    app_code: str
    text: str = Field(..., min_length=10)
    subject: str = "app"
    author: str = "user"


@router.post("/note")
async def post_note(
    body: NoteRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """A person deliberately writing something down about the app."""
    scope = await _write_scope(auth, body.app_code)
    client_code, app_code = scope.client_code, scope.app_code
    return await ingest.note(
        client_code, app_code, text=body.text, subject=body.subject,
        author=body.author, user_id=int(getattr(auth, "user_id", 0) or 0),
    )


class EntryCreate(BaseModel):
    """A person writing a piece of knowledge down directly.

    Deliberately NOT routed through observation + curation. Someone typing into
    a Lore panel expects to see their entry, now. Curation is for turning
    incidental evidence into knowledge; this is knowledge already.
    """

    app_code: str
    kind: str = Field(..., description="One of the entry kinds; GET /taxonomy lists them")
    title: str = Field(..., min_length=4, max_length=240)
    body: str = Field(..., min_length=10)
    subject: str = "app"
    tags: list[str] = Field(default_factory=list)
    confidence: int = Field(90, ge=0, le=100)
    pinned: bool = Field(True, description="Human-authored entries are pinned by default")


@router.post("/entries", status_code=201)
async def create_entry(
    body: EntryCreate,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Write a piece of tribal knowledge down as a first-class entry."""
    scope = await _write_scope(auth, body.app_code)
    client_code, app_code = scope.client_code, scope.app_code
    if body.kind not in ENTRY_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown kind '{body.kind}'. Valid: {', '.join(ENTRY_KINDS)}",
        )
    user_id = int(getattr(auth, "user_id", 0) or 0)

    result = await store.add_entry(
        client_code, app_code,
        kind=body.kind, title=body.title, body=body.body,
        subject=body.subject, tags=body.tags[:8], confidence=body.confidence,
        pinned=body.pinned, created_by=user_id,
    )
    # add_entry collapses an identical body into a confirmation, which loses the
    # pin the author asked for. Apply it explicitly either way.
    if body.pinned:
        await store.set_pinned(result["id"], True, updated_by=user_id)

    entry = await store.get_entry(result["id"])
    return {
        "entry": entry.to_dict() if entry else None,
        "created": result["created"],
        "note": None if result["created"] else "An identical entry already existed; it was confirmed rather than duplicated.",
    }


class EntryPatch(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    tags: Optional[list[str]] = None
    confidence: Optional[int] = Field(None, ge=0, le=100)
    subject: Optional[str] = None
    status: Optional[Literal["active", "superseded", "retired", "draft"]] = None
    pinned: Optional[bool] = None
    message: str = ""


@router.patch("/entries/{entry_id}")
async def patch_entry(
    entry_id: int,
    body: EntryPatch,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Human edit of an entry.

    Pinning protects an entry from the curator, not from the person who wrote
    it, so this path edits pinned entries directly rather than making the caller
    unpin first.
    """
    stored, scope = await _entry_in_scope(auth, entry_id, for_write=True)
    user_id = int(getattr(auth, "user_id", 0) or 0)
    actions: list[str] = []
    target_id = entry_id

    if any(v is not None for v in (body.title, body.body, body.tags, body.confidence, body.subject)):
        # `edit_in_scope` revises our own entry in place, or forks an inherited
        # one into this client. Either way the caller learns which happened.
        outcome = await store.edit_in_scope(
            stored, scope,
            title=body.title, body=body.body, tags=body.tags,
            confidence=body.confidence, subject=body.subject,
            updated_by=user_id, message=body.message or "edited by a person",
        )
        if outcome["action"] == "missing":
            raise HTTPException(status_code=404, detail=f"No lore entry {entry_id}")
        target_id = outcome["id"]
        actions.append(
            f"forked into {scope.client_code} as #{target_id}, overriding #{entry_id}"
            if outcome["action"] == "forked"
            else f"revised to v{outcome.get('version')}"
        )

    if body.status == "retired":
        outcome = await store.retire_in_scope(stored, scope, updated_by=user_id)
        target_id = outcome["id"]
        actions.append("hidden for this client" if outcome["action"] == "hidden" else "retired")
    elif body.status:
        await store.set_entry_status(target_id, body.status, updated_by=user_id, force=True)
        actions.append(f"status={body.status}")

    if body.pinned is not None:
        await store.set_pinned(target_id, body.pinned, updated_by=user_id)
        actions.append("pinned" if body.pinned else "unpinned")

    entry = await store.get_entry(target_id)
    return {"entry": entry.to_dict() if entry else None, "actions": actions}


class DocumentRequest(BaseModel):
    """Paste an existing document in and let lore extract what is durable.

    The path for tribal knowledge that already lives in a README, a spec, a
    handover note or a Slack thread somebody pasted into a file.
    """

    app_code: str
    title: str = Field(..., min_length=1, max_length=240)
    content: str = Field(..., min_length=20)
    origin: str = Field("paste", max_length=120, description="Where it came from, for provenance")
    subject: str = "app"
    curate: bool = Field(True, description="Run curation now instead of waiting for the next pass")


@router.post("/document")
async def post_document(
    body: DocumentRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Ingest a document and, by default, turn it into entries immediately."""
    scope = await _write_scope(auth, body.app_code)
    client_code, app_code = scope.client_code, scope.app_code
    user_id = int(getattr(auth, "user_id", 0) or 0)

    ingested = await ingest.from_document(
        client_code, app_code, title=body.title, content=body.content,
        origin=body.origin, subject=body.subject, user_id=user_id,
    )
    result: dict[str, Any] = {"ingested": ingested}
    if body.curate and ingested.get("recorded"):
        result["curation"] = await curator.curate(
            client_code, app_code, trigger_source="document", updated_by=user_id,
        )
    return result


@router.delete("/entries/{entry_id}")
async def delete_entry(
    entry_id: int,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Retire an entry, or hide an inherited one for this client only.

    Nothing is ever hard-deleted: provenance is the point. Retiring somebody
    else's entry writes a tombstone in your client rather than touching theirs.
    """
    stored, scope = await _entry_in_scope(auth, entry_id, for_write=True)
    user_id = int(getattr(auth, "user_id", 0) or 0)
    outcome = await store.retire_in_scope(stored, scope, updated_by=user_id)
    return {
        "entry_id": outcome["id"],
        "status": "retired",
        "hidden_for_client_only": outcome["action"] == "hidden",
        "overrides": outcome.get("overrides"),
    }


# ── Curation ─────────────────────────────────────────────────────────────


class CurateRequest(BaseModel):
    app_code: str
    batch_size: int = Field(curator.BATCH_SIZE, ge=1, le=400)
    wait: bool = Field(True, description="False returns immediately and curates in the background")


@router.post("/curate")
async def post_curate(
    body: CurateRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Run a curation pass for one app."""
    scope = await _write_scope(auth, body.app_code)
    client_code, app_code = scope.client_code, scope.app_code
    user_id = int(getattr(auth, "user_id", 0) or 0)

    if not body.wait:
        asyncio.create_task(  # noqa: RUF006 — fire-and-forget by design
            curator.curate(
                client_code, app_code, trigger_source="api-async",
                batch_size=body.batch_size, updated_by=user_id,
            )
        )
        return {"status": "started", "app_code": app_code}

    return await curator.curate(
        client_code, app_code, trigger_source="api",
        batch_size=body.batch_size, updated_by=user_id,
    )


class BackfillRequest(BaseModel):
    app_code: str
    curate: bool = True


@router.post("/backfill/app-kb")
async def post_backfill(
    body: BackfillRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Seed lore from the app's existing hand-written knowledge base."""
    scope = await _write_scope(auth, body.app_code)
    client_code, app_code = scope.client_code, scope.app_code
    user_id = int(getattr(auth, "user_id", 0) or 0)
    seeded = await ingest.from_app_kb(client_code, app_code, user_id=user_id)
    result: dict[str, Any] = {"seeded": seeded}
    if body.curate and seeded.get("recorded"):
        result["curation"] = await curator.curate(
            client_code, app_code, trigger_source="backfill", updated_by=user_id,
        )
    return result


# ── Admin sweep ──────────────────────────────────────────────────────────


def _require_admin_token(x_admin_token: Optional[str] = Header(default=None)) -> str:
    expected = getattr(settings, "ADMIN_TOKEN", "") or ""
    if not expected:
        raise HTTPException(status_code=503, detail="Admin endpoints disabled: ADMIN_TOKEN not configured")
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token")
    return x_admin_token


@router.post("/admin/sweep")
async def post_sweep(
    min_pending: int = Query(5, ge=1, le=1000),
    max_apps: int = Query(10, ge=1, le=100),
    _token: str = Depends(_require_admin_token),
) -> dict[str, Any]:
    """Curate every app with enough pending observations. For a cron caller."""
    results = await curator.curate_all(
        min_pending=min_pending, max_apps=max_apps, trigger_source="sweep",
    )
    return {"apps": len(results), "results": results}


@router.get("/admin/apps")
async def get_known_apps(
    limit: int = Query(100, ge=1, le=500),
    _token: str = Depends(_require_admin_token),
) -> dict[str, Any]:
    """Every app lore knows about, and which ones are waiting on curation."""
    return {
        "known": await store.known_apps(limit=limit),
        "needing_curation": await store.apps_needing_curation(min_pending=1, limit=limit),
    }
