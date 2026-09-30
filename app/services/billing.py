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


#: How long a "yes, this consumer may spend" answer is trusted inside one
#: long-running job.
#:
#: A sweep is one action to a person and sixty model calls to a wallet, so
#: gating it once at the start lets a consumer who runs out on object four go on
#: spending through object sixty. Re-asking before every call is the correct
#: grain and costs one 3-second HTTP call against a model call taking five to
#: thirty seconds — but sixty of them in a row is still noise on the security
#: service, so a YES is trusted for this long.
#:
#: A NO is never cached. Somebody who tops up mid-sweep should be able to carry
#: on, and the failure mode of caching a no is a job that stays dead after the
#: money arrives.
GATE_TTL_SECONDS = 30.0


class CallMeter:
    """Gate and charge LLM calls made OUTSIDE the agent loop.

    The agent meters itself: `BaseAgent.run` gates each turn against the wallet
    and charges each call as it lands. Anything calling a provider directly —
    the blueprint sweep, a one-off derivation — bypasses all of that, and for a
    while that is exactly what happened: a plan sweep ran forty model calls
    against a SUSPENDED wallet and was never billed for one of them, while the
    chat beside it correctly refused to answer. The two halves of the same
    screen disagreed about whether the customer had any money.

    Fail-open, deliberately and in both directions, matching the agent:
    `check_serving_status` returning True on error means a security hiccup does
    not stop the work, and a failed debit is logged loud rather than retried,
    because the charge is idempotent per `requestId` and can be reconciled from
    the log line.

    Not blueprint-specific. Every future direct call should take one of these.
    """

    __slots__ = ("auth", "session_id", "_allowed_until", "calls", "tokens")

    def __init__(self, auth: AuthContext, session_id: str = "") -> None:
        self.auth = auth
        #: What the charge is attributed to, for reconciliation. A job id reads
        #: better here than a synthetic session: it is the thing a person can
        #: point at and say "that is the sweep I ran".
        self.session_id = session_id or "direct"
        self._allowed_until = 0.0
        #: Counted so a job can report what it spent. A sweep that silently
        #: costs money is the thing this class exists to stop.
        self.calls = 0
        self.tokens = 0.0

    async def allowed(self) -> bool:
        """Whether this consumer may spend right now. Fail-open on any error."""
        import time

        now = time.monotonic()
        if now < self._allowed_until:
            return True
        if not await check_serving_status(self.auth):
            return False
        self._allowed_until = now + GATE_TTL_SECONDS
        return True

    async def charge(self, response: dict | None) -> None:
        """Charge one completed provider call, from its own usage block.

        Every provider in `llm_provider.py` returns `usage` in the shape
        `weighted_tokens` expects plus the resolved `model`, so this needs no
        per-provider special casing — and taking the model from the RESPONSE
        rather than from the requested tier is what keeps the weighting honest
        when a tier resolves to something heavier than expected.
        """
        import uuid

        if not isinstance(response, dict):
            return
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return
        model = response.get("model")
        self.calls += 1
        self.tokens += weighted_tokens(usage, model)
        await charge_llm_call(
            self.auth, usage, model, uuid.uuid4().hex, self.session_id,
        )


OUT_OF_TOKENS = "You're out of tokens. Top up your wallet to keep using AI."


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
