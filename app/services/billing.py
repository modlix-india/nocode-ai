"""Token-wallet billing for the AI agent (Phase 4).

AI is the one synchronous metered action. The agent gates each turn against the
consumer's wallet at the start (``ai_turn_allowed``) and charges the turn's
actual token usage at the end (``charge_ai_turn``). Both are best-effort and
fail-open: a billing or security hiccup must never break the agent — security
itself owns the block/allow and the allow-negative debit.

Billing maps to the platform model: the wallet is the consumer's
(``auth.client_code``); the action key is ``ai.llm`` priced on the builder app's
config. AI is provided through the builder (appbuilder/sitezump), which is
SYSTEM-owned, so the rate lives on the ``(builder, SYSTEM)`` config.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.core.session import AuthContext

logger = logging.getLogger(__name__)

# The builder app is SYSTEM-owned, so the ai.llm rate is on (builder, SYSTEM).
_AI_URL_CLIENT = "SYSTEM"
_TIMEOUT = 3.0

# Token components summed into the per-turn charge quantity.
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _wallet_base(auth: AuthContext) -> str:
    return f"{settings.SECURITY_SERVICE_URL}{auth.path_prefix}/api/security/wallets/internal"


def turn_token_delta(usage_before: dict[str, int], usage_after: dict[str, int]) -> int:
    """Tokens consumed in one turn = the delta on the session's accumulated usage."""
    return sum(max(0, usage_after.get(k, 0) - usage_before.get(k, 0)) for k in _USAGE_KEYS)


async def ai_turn_allowed(auth: AuthContext) -> bool:
    """Whether the consumer may run an AI turn now. Fail-open on any error."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_wallet_base(auth)}/creation-allowed",
                params={
                    "ownerClientCode": auth.client_code,
                    "appCode": auth.access_app_code,
                    "urlClientCode": _AI_URL_CLIENT,
                },
                headers=auth.to_headers(),
            )
            if resp.status_code == 200:
                return resp.json() is True
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("AI billing gate failed open: %s", e)
        return True


async def charge_ai_turn(auth: AuthContext, tokens: int, session_id: str, turn_number: int) -> None:
    """Charge a finished turn's token usage. Best-effort; allow-negative server-side."""
    if tokens <= 0:
        return
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            await client.post(
                f"{_wallet_base(auth)}/billing/charge-ai-turn",
                params={
                    "clientCode": auth.client_code,
                    "appCode": auth.access_app_code,
                    "urlClientCode": _AI_URL_CLIENT,
                    "tokens": tokens,
                    "sessionId": session_id,
                    "turnNumber": turn_number,
                },
                headers=auth.to_headers(),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("AI turn charge failed (best-effort): %s", e)
