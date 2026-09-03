"""AppBuilder router — chat endpoint.

Common endpoints (models, sessions) are registered via create_common_routes().
Only the /chat endpoint with appbuilder-specific logic lives here.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.base_auth import require_auth_context
from app.core.base_router import (
    ChatAttachment,
    build_image_blocks,
    create_common_routes,
    stream_agent_response,
)
from app.core.session import BaseSession, AuthContext
from app.services.session_manager import get_session_manager
from app.services.security import ALLOWED_AI_APPS

logger = logging.getLogger(__name__)

router = APIRouter()
create_common_routes(router, agent_name="appbuilder")

_agent = None


def set_appbuilder_agent(agent) -> None:
    """Set the AppBuilderAgent instance (called from main.py lifespan)."""
    global _agent
    _agent = agent


async def require_ai_auth_context(
    auth: AuthContext = Depends(require_auth_context),
) -> AuthContext:
    """Verify access app is AI-enabled (appbuilder/sitezump)."""
    verified_app = auth.access_app_code
    if not verified_app or verified_app.lower() not in ALLOWED_AI_APPS:
        raise HTTPException(
            status_code=403,
            detail="AI features are only available in appbuilder or sitezump applications.",
        )
    return auth


class AppUserAuth(BaseModel):
    """Credentials for the app-user identity (separate from the caller's JWT).

    Used by tools that render or interact with the CUSTOMER'S app as one of
    its end users — screenshot_page, drive_page, call_as_app_user. The
    caller's JWT (developer identity) does ALL platform authoring; these
    credentials only authenticate against the target app's user pool.

    Pass either `token` (pre-obtained) or `username` + `password` (the
    session will run findUserClients + authenticate once and cache the
    resolved token for the conversation's lifetime).
    """

    token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


class OpenDraft(BaseModel):
    """One object the caller has open, unsaved.

    A page arrives as `overlay`: the components that differ from the saved
    version, because a real page reaches 1.4MB and shipping it whole on every
    message would put megabytes on the wire to say "nothing has changed". A clean
    page sends an empty overlay. Everything else is a form's worth of fields, so
    it arrives whole in `doc`.
    """

    kind: str = ""
    # The collection this object saves to, e.g. "/api/core/storages". An
    # alternative to naming the kind, and the usual one: the workspace keeps the
    # API on each tab but no kind name, and resolving one from the other on this
    # side means the mapping lives only in the intercept's table.
    api: str = ""
    id: str
    name: str = ""
    app_code: str = ""
    dirty: bool = False
    doc: Optional[dict] = None
    overlay: Optional[dict] = None


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    app_code: Optional[str] = None
    model: Optional[str] = None
    # Headless/harness callers set this to pre-approve all mutating tools
    # (create/update/delete/copy) so the agent runs fully autonomous without
    # waiting on the interactive confirmation flow. Defaults to the normal
    # interactive behaviour.
    auto_confirm: bool = False
    attachments: Optional[List[ChatAttachment]] = None
    # Optional app-user identity. Required only when a tool that interacts
    # with the customer's live app (screenshot_page / drive_page /
    # call_as_app_user) is invoked. Other tools ignore it.
    app_user: Optional[AppUserAuth] = None
    # What the caller's UI has open, for chats embedded in an editor (the
    # appbuilder sidekick). Free-form, but the keys the agent renders are
    # active_object, open_tabs and open_tab_ids. Lets the agent answer about
    # the thing in front of the user without a discovery round-trip first.
    editor_context: Optional[dict] = None
    # Objects the caller has open and unsaved. For exactly these, the agent reads
    # the caller's copy and holds its writes there instead of saving, so the user
    # can look at the change before committing it. Everything else is written
    # normally. A caller that sends nothing (the plain chat page) gets exactly the
    # behaviour it always had.
    open_drafts: Optional[List[OpenDraft]] = None
    # Send definition writes to the app's draft surface instead of live, so the
    # user gets a reviewable copy and the agent can screenshot its own work.
    # Off by default: turning it on silently would change where every existing
    # caller's edits land, and the agent degrades to live writes anyway on a
    # deployment that has no draft surface.
    draft_mode: bool = False


class TemplateAiRequest(BaseModel):
    """Request for the editor AI tab: a prompt plus the current (possibly unsaved) template."""

    prompt: str
    template: Optional[dict] = None
    language: Optional[str] = "en"
    part: Optional[str] = "body"
    templateType: Optional[str] = "email"


@router.post("/template")
async def author_template(
    body: TemplateAiRequest, auth: AuthContext = Depends(require_ai_auth_context)
):
    """Generate/revise template content from a prompt. Returns {subject, html, message}.

    Backs the Template Editor's AI tab (the aiEndpoint property). Stateless — the whole current
    template is sent so work-in-progress previews without saving.
    """
    from app.services.template_ai import generate_template_content

    if not body.prompt or not body.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")

    tpl = body.template or {}
    lang = body.language or tpl.get("defaultLanguage") or "en"
    part = body.part or "body"
    lang_parts = (tpl.get("templateParts") or {}).get(lang) or {}
    current_html = lang_parts.get(part, "") if isinstance(lang_parts, dict) else ""
    current_subject = lang_parts.get("subject", "") if isinstance(lang_parts, dict) else ""

    return await generate_template_content(
        prompt=body.prompt,
        template_type=body.templateType or tpl.get("templateType") or "email",
        current_html=current_html,
        current_subject=current_subject,
        language=lang,
    )


class WhatsappMessageAiRequest(BaseModel):
    """Request for the WhatsApp message library's AI panel."""

    prompt: str
    # How many interchangeable phrasings to write. Several rather than one is the point of the
    # feature, not a setting: a rule sends one body to every matching lead, and identical text at
    # volume is what gets a linked number banned.
    variantCount: Optional[int] = 4
    currentVariants: Optional[List[str]] = None
    language: Optional[str] = "en"
    tone: Optional[str] = ""


@router.post("/whatsapp/message")
async def author_whatsapp_message(
    body: WhatsappMessageAiRequest, auth: AuthContext = Depends(require_ai_auth_context)
):
    """Write several interchangeable versions of a WhatsApp message.

    Backs the message library editor. Stateless — the current variants are sent so an unsaved draft
    can be revised, matching how the template AI tab already works.

    Returns ``{variants, variables, message, warnings}``. The warnings are advisory: an unknown merge
    field or two near-identical versions are things somebody should see before saving, but refusing
    to return the draft would just lose their work.
    """
    from app.services.whatsapp_message_ai import generate_message_variants

    if not body.prompt or not body.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")

    return await generate_message_variants(
        prompt=body.prompt,
        variant_count=body.variantCount or 4,
        current_variants=body.currentVariants,
        language=body.language or "en",
        tone=body.tone or "",
    )


class VersionDiffRequest(BaseModel):
    """Request for the workspace version-history compare step.

    Both snapshots are sent by the caller. The service has no way to read an editor's
    current state on its own, and the two documents together are what the comparison
    needs, so the page posts them rather than the service fetching one of them back.
    """

    objectType: Optional[str] = ""
    name: Optional[str] = ""
    currentVersion: Optional[Any] = None
    versionNumber: Optional[Any] = None
    versionMessage: Optional[str] = ""
    current: Optional[dict] = None
    older: Optional[dict] = None


@router.post("/version-diff")
async def version_diff(
    body: VersionDiffRequest, auth: AuthContext = Depends(require_ai_auth_context)
):
    """Say what separates a saved version from what is live, before anyone loads it over their work.

    Stateless. The difference is computed exactly in Python and only that list goes to the
    model, so the answer is grounded and the cost does not scale with document size.
    """
    from app.services.version_diff import summarise_version_diff

    if body.older is None:
        raise HTTPException(status_code=400, detail="older is required")

    return await summarise_version_diff(
        object_type=body.objectType or "",
        name=body.name or "",
        current_version=body.currentVersion,
        version_number=body.versionNumber,
        version_message=body.versionMessage or "",
        current=body.current or {},
        older=body.older,
    )


@router.post("/chat")
async def chat(body: ChatRequest, auth: AuthContext = Depends(require_ai_auth_context)):
    """Stream an appbuilder agent response as SSE."""
    if _agent is None:
        raise HTTPException(status_code=503, detail="AppBuilder agent not initialized")

    if body.app_code:
        auth.app_code = body.app_code

    session = BaseSession(agent_name="appbuilder")
    if body.app_code:
        session.context["app_code"] = body.app_code
    if body.editor_context:
        session.context["editor_context"] = body.editor_context
    session.context["draft_mode"] = body.draft_mode
    if body.open_drafts:
        # Kept as plain dicts on the session so the agent can build the registry
        # when it has the event stream in hand. The documents themselves never go
        # near session.context's persisted half: a page reaches 1.4MB and that
        # column is not the place for it.
        session.open_drafts = [d.model_dump() for d in body.open_drafts]
    # Pre-approve mutating tools for headless/harness callers (see agent loop).
    session.context["auto_confirm"] = body.auto_confirm

    # Stash app-user credentials (token OR username+password) on the session.
    # Consumed lazily by tools that need an end-user identity (screenshot_page,
    # drive_page, call_as_app_user) — other tools ignore it entirely.
    if body.app_user is not None:
        session.set_app_user(body.app_user.model_dump(exclude_none=True))

    await session.get_or_create(body.session_id, auth)

    if not body.session_id:
        title = body.message[:100].strip()
        if title:
            await get_session_manager().update_session_title(
                session.session_id, title, auth.user_id
            )

    image_blocks = build_image_blocks(body.attachments, _agent._provider_name) if body.attachments else None
    return await stream_agent_response(_agent, body.message, session, image_blocks, model_override=body.model)
