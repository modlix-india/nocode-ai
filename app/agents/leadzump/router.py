"""LeadZump router — chat endpoint and the agent's own access gate.

Two gates, and they answer different questions.

**Which app** — `ALLOWED_LEADZUMP_APPS`. `ALLOWED_AI_APPS`, over in
`app/services/security.py`, is the *AppBuilder* agent's gate and stays exactly
as it is: `leadzump` is deliberately not in it, because AppBuilder authors
applications and a CRM user has no business with that tool. This agent gets its
own list instead. Note the precedent here is weaker than it should be —
`adzump` and `adzump2` use `require_auth_context` with no app allow-list at all,
so a signed-in user of any app can drive them. That is not repeated here.

**Which side of the app** — `OWNER_LEVEL_TYPES`. LeadZump ships two products in
one app, split at runtime on the tenant's `levelType`: `CLIENT` is the
real-estate company that owns the data, `CUSTOMER` is the external channel
partner working a slice of it through the `bp*` portal. This assistant is for
the owner side only, matching where billing goes.

**This gate is load-bearing. Do not relax it as cosmetic.** It was written as a
product decision on the belief that the data was scoped either way. Measured on
local, that belief was half right:

* **Deals are scoped.** A partner user in `VIVOB` sees 25 of its owner org
  `PALLA7`'s 169 tickets — `ProcessorAccess.isOutsideUser()` makes
  `getUserField` return `createdBy`, so they get the deals they raised.
* **Leads are not.** The same user reads **all 164 of PALLA7's owners**, names
  and phone numbers included, and `lead_get` cheerfully returns
  `clientCode: PALLA7` to them. `OwnerDAO` declares no `userAccessField`, so
  `processorAccessCondition` applies no user filter at all — and a partner's
  `getEffectiveClientCode()` resolves to the *managed* client, which is the
  owner org. Products, sources and task types behave the same way.

The partner portal never asks for this: no `bp*` page calls `owners/*` at all.
It is an API-level exposure that only a tool like `lead_search` makes reachable,
which is precisely why this gate is the thing standing in front of it. Fixing it
properly means a user-access field on `Owner`, or `@PreAuthorize` in
entity-processor — a `nocode-saas` change affecting every existing LeadZump
page, and not this agent's to make.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.agents.leadzump.agent import LeadZumpAgent
from app.core.base_auth import require_auth_context
from app.core.base_router import (
    ChatAttachment,
    build_image_blocks,
    create_common_routes,
    stream_agent_response,
)
from app.core.session import AuthContext, BaseSession
from app.services.session_manager import get_session_manager

logger = logging.getLogger(__name__)

ALLOWED_LEADZUMP_APPS = {"leadzump"}

# `security_client.LEVEL_TYPE` is `enum('SYSTEM','CLIENT','CUSTOMER','CONSUMER')`,
# NOT NULL, default CLIENT. Note this is NOT the Java `ClientLevelType` enum,
# which reads CONSUMER/CUSTOMER/CLIENT/OWNER — `OWNER` never appears in the
# column and `SYSTEM` is absent from the Java enum. Trust the column: it is
# what `AuthenticationService` copies onto the security context.
#
# `CLIENT` is the real-estate company that owns the CRM data. `SYSTEM` is the
# platform tenant, admitted so an operator can exercise the agent. Refused:
# `CUSTOMER`, which is exactly the business-partner portal
# (`BusinessPartnerConstant.CLIENT_LEVEL_TYPE_BP`), and `CONSUMER` below it.
#
# An allow-list rather than a deny-list, and safe as one because the column is
# a NOT NULL enum: there is no fifth value for it to wrongly admit.
OWNER_LEVEL_TYPES = {"CLIENT", "SYSTEM"}

router = APIRouter()
create_common_routes(router, agent_name="leadzump")


async def require_leadzump_auth_context(
    auth: AuthContext = Depends(require_auth_context),
) -> AuthContext:
    """Admit only an owner-side LeadZump user.

    `access_app_code` is the `appCode` header, which the Prompt component sends
    from `Store.application.appCode` — the app hosting the panel, not a value
    the caller chooses freely.
    """
    if (auth.access_app_code or "").lower() not in ALLOWED_LEADZUMP_APPS:
        raise HTTPException(403, "This assistant is only available in LeadZump.")

    level = (auth.client_level_type or "").upper()
    if level and level not in OWNER_LEVEL_TYPES:
        raise HTTPException(
            403,
            "This assistant is available to the LeadZump owner organisation, "
            "not to the business-partner portal.",
        )
    if not level:
        # The security context always carries it for an authenticated user
        # (`AuthenticationService` builds ContextAuthentication from the
        # client's own levelType). Missing means something upstream changed,
        # and defaulting to "let them in" would silently open the partner
        # portal — so refuse and say why.
        logger.warning(
            "leadzump gate: no clientLevelType on the security context for user %s",
            auth.user_id,
        )
        raise HTTPException(
            403, "Could not determine your organisation type; access refused."
        )
    return auth


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    attachments: Optional[List[ChatAttachment]] = None


@router.post("/chat")
async def chat(
    body: ChatRequest, auth: AuthContext = Depends(require_leadzump_auth_context)
):
    """Stream a LeadZump agent response as SSE."""
    agent = LeadZumpAgent.get_instance()

    session = BaseSession(agent_name="leadzump")
    await session.get_or_create(body.session_id, auth)

    if not body.session_id:
        title = body.message[:100].strip()
        if title:
            await get_session_manager().update_session_title(
                session.session_id, title, auth.user_id
            )

    image_blocks = build_image_blocks(body.attachments) if body.attachments else None

    return await stream_agent_response(
        agent, body.message, session, image_blocks, model_override=body.model
    )
