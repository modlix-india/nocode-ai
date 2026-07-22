"""Token-wallet billing for the AI agent.

AI is the one synchronous metered action. Unlike the 15-minute rent actions, the
agent charges each LLM call's usage *immediately* (security is idempotent per
``requestId``) and gates each turn against the consumer's wallet at the start.
Both are best-effort and fail-open: a billing or security hiccup must never break
the agent — security itself owns the block/allow and the allow-negative debit.

Billing maps to the platform model: the wallet charged is ``wallet(M, builderApp)``
where M is ``auth.client_code`` and the builder app is ``auth.access_app_code``
(appbuilder/sitezump). Model weighting happens here (a per-model multiplier);
security applies the monthly free grant and the flat ``aiTokensPerMillion`` rate
on the already-weighted token count.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.core.session import AuthContext

logger = logging.getLogger(__name__)

_TIMEOUT = 3.0

# Raw usage components summed into a call's billable token count.
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# Per-model billing multipliers. Heavier models cost more billing tokens per raw
# token. Matched case-insensitively by substring; the first hit wins, else 1.0.
_MODEL_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("opus", 5.0),
    ("sonnet", 3.0),
    ("haiku", 1.0),
    ("gpt-4o", 3.0),
    ("deepseek", 1.0),
    ("minimax", 1.0),
)


def _internal_base(auth: AuthContext) -> str:
    return f"{settings.SECURITY_SERVICE_URL}{auth.path_prefix}/api/security/internal/billing"


def _model_weight(model: str | None) -> float:
    name = (model or "").lower()
    for needle, weight in _MODEL_WEIGHTS:
        if needle in name:
            return weight
    return 1.0


def weighted_tokens(usage: dict[str, int], model: str | None) -> float:
    """Model-weighted billable tokens for one LLM call."""
    raw = sum(max(0, int(usage.get(k, 0) or 0)) for k in _USAGE_KEYS)
    return raw * _model_weight(model)


async def check_serving_status(auth: AuthContext) -> bool:
    """Whether the consumer may run an AI turn now. Fail-open on any error."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_internal_base(auth)}/ai-allowed",
                params={"appCode": auth.access_app_code, "clientCode": auth.client_code},
                headers=auth.to_headers(),
            )
            if resp.status_code == 200:
                return resp.json() is not False
            logger.warning(
                "AI billing gate failed open (non-200): status=%s client=%s app=%s body=%.200s",
                resp.status_code, auth.client_code, auth.access_app_code, resp.text,
            )
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "AI billing gate failed open (error): client=%s app=%s err=%s",
            auth.client_code, auth.access_app_code, e,
        )
        return True


async def charge_llm_call(
    auth: AuthContext,
    usage: dict[str, int],
    model: str | None,
    request_id: str,
    session_id: str,
) -> None:
    """Charge one LLM call's usage immediately. Best-effort; idempotent per requestId."""
    tokens = weighted_tokens(usage, model)
    if tokens <= 0:
        return
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{_internal_base(auth)}/charge-ai",
                json={
                    "clientCode": auth.client_code,
                    "appCode": auth.access_app_code,
                    "model": model,
                    "weightedTokens": tokens,
                    "requestId": request_id,
                    "sessionId": session_id,
                },
                headers=auth.to_headers(),
            )
        if resp.status_code >= 300:
            # Uncharged AI usage: security rejected/failed the debit. Revenue-impacting,
            # so log loud with full context. The charge is idempotent per requestId, so
            # these lines are enough to replay/reconcile later.
            logger.warning(
                "AI usage UNCHARGED (non-2xx): status=%s tokens=%s model=%s client=%s app=%s "
                "requestId=%s session=%s body=%.200s",
                resp.status_code, tokens, model, auth.client_code, auth.access_app_code,
                request_id, session_id, resp.text,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "AI usage UNCHARGED (error): tokens=%s model=%s client=%s app=%s requestId=%s session=%s err=%s",
            tokens, model, auth.client_code, auth.access_app_code, request_id, session_id, e,
        )
