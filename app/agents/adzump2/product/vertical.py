"""Vertical deduction — classify a studied product into a J5 vertical code.

The single highest-leverage A2 output (A2 §5.3): the deduced ``code`` selects
the J5 ``VerticalPlaybook`` (required slots, defaults, compliance, taxonomy,
critic rubric) for the rest of the build. LOW confidence → the ``generic``
playbook + a tagged user confirm (never silently mis-vertical a product).

Two paths, one result:
- ``VerticalDeducer.deduce`` runs a single-shot **MiniMax M3** classifier
  (the "A2 does the LLM reasoning" boundary) when it has an event stream + auth.
- With neither (offline / eval, or on any LLM failure) it falls back to
  ``deduce_vertical_heuristic`` — a deterministic, explainable signal scorer.
  This keeps deduction offline-provable (no live LLM in tests) and degrades
  gracefully. Live-M3 proving of the LLM path is a documented P1+ TODO.

The confidence→playbook decision (``apply_confidence_policy``) is pure Python in
one place, so both paths share the same low-confidence→generic+confirm rule.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.core.agent import BaseAgent
from app.core.context import BaseContext
from app.core.session import AuthContext, BaseSession
from app.core.streaming import AgentEventStream

from app.agents.adzump2.product.models import ProductProfile, VerticalGuess

logger = logging.getLogger(__name__)

# ── J5 registry (minimal for P1) ────────────────────────────────────────────
# The specific verticals A2 can deduce into. ``generic`` is the residual
# fallback playbook (not a "specific" vertical). J5 (Java) owns the full
# registry + playbooks; this is the deduction's knowledge of the codes.
SPECIFIC_VERTICAL_CODES: frozenset[str] = frozenset({"real_estate"})
KNOWN_VERTICAL_CODES: frozenset[str] = SPECIFIC_VERTICAL_CODES | {"generic"}

# Below this confidence we do NOT commit to a specific vertical — we default to
# the generic playbook and raise a confirm (A2 §9: tune against fixtures).
LOW_CONFIDENCE_THRESHOLD: float = 0.6

# Confidence floor to even PROPOSE real_estate over generic (some signal present).
_PROPOSE_FLOOR: float = 0.35

# ── real_estate signal lexicon (deterministic heuristic) ────────────────────
# STRONG signals are near-unambiguous for property; WEAK signals co-occur with
# real estate but also appear elsewhere, so they count for less.
_RE_STRONG: tuple[str, ...] = (
    "bhk", "rera", "sq ft", "sqft", "square feet", "square foot", "carpet area",
    "floor plan", "gated community", "possession", "apartment", "villa",
    "township", "penthouse", "site visit", "under construction", "ready to move",
    "pre-launch", "prelaunch", "duplex", "acres", "built-up area",
)
_RE_WEAK: tuple[str, ...] = (
    "real estate", "real-estate", "realty", "property", "properties",
    "residential", "developer", "builder", "project", "amenities", "homes",
    "home ", "plot", "flat", "configuration", "2 bhk", "3 bhk", "1 bhk",
    "housing", "layout", "commercial space",
)
# Explicit business-type phrasing → the strongest signal.
_RE_BUSINESS_TYPE: tuple[str, ...] = (
    "real estate", "real-estate", "realty", "property", "developer", "builder",
    "housing", "construction", "realtor",
)


def _profile_text(profile: ProductProfile) -> str:
    """Flatten the profile into a lowercase blob for signal scanning."""
    bits: list[str] = [
        profile.name,
        profile.pitch,
        profile.price_band,
        profile.brand,
        " ".join(profile.value_props),
        " ".join(profile.offerings),
    ]
    attrs = profile.attributes or {}
    for key in ("business_type", "positioning", "summary", "business_scale"):
        val = attrs.get(key)
        if isinstance(val, str):
            bits.append(val)
    return " ".join(b for b in bits if b).lower()


def deduce_vertical_heuristic(profile: ProductProfile) -> VerticalGuess:
    """Deterministic vertical guess from the studied profile (no LLM).

    Weighted distinct-signal scorer over the profile text. An explicit
    real-estate ``business_type`` is decisive; otherwise confidence rises with
    the count of distinct strong/weak property signals. Returns the strongest
    *specific* guess (real_estate) when there's any real signal, else
    ``generic`` at low confidence — ``apply_confidence_policy`` then decides
    whether a confirm is needed.
    """
    text = _profile_text(profile)
    business_type = str((profile.attributes or {}).get("business_type") or "").lower()

    strong_hits = sorted({s for s in _RE_STRONG if s in text})
    weak_hits = sorted({w for w in _RE_WEAK if w in text})
    bt_match = any(t in business_type for t in _RE_BUSINESS_TYPE)

    confidence = min(0.95, 0.30 * len(strong_hits) + 0.12 * len(weak_hits))
    if bt_match:
        confidence = max(confidence, 0.90)

    if confidence >= _PROPOSE_FLOOR:
        signal_desc = []
        if bt_match:
            signal_desc.append(f"business type reads as real estate ({business_type!r})")
        if strong_hits:
            signal_desc.append("strong signals: " + ", ".join(strong_hits[:5]))
        if weak_hits:
            signal_desc.append("supporting: " + ", ".join(weak_hits[:5]))
        rationale = "; ".join(signal_desc) or "real-estate signals present"
        return VerticalGuess(code="real_estate", confidence=round(confidence, 4),
                             rationale=rationale)

    return VerticalGuess(
        code="generic",
        confidence=0.2,
        rationale=(
            "no strong real-estate (or other known-vertical) signal in the "
            "profile — cannot commit to a specific vertical"
        ),
    )


def apply_confidence_policy(
    guess: VerticalGuess, threshold: float = LOW_CONFIDENCE_THRESHOLD,
) -> tuple[VerticalGuess, bool]:
    """Map a raw guess onto the *effective* vertical + a needs-confirm flag.

    - A specific code at/above ``threshold`` → keep it, no confirm.
    - A specific code below ``threshold`` → downgrade to ``generic`` + confirm
      (A2 §5.3: don't silently mis-vertical).
    - ``generic`` (residual) → always confirm (we're not sure it isn't some
      vertical J5 doesn't yet list).
    Returns ``(effective_guess, needs_confirm)``.
    """
    code, conf = guess.code, float(guess.confidence)
    if code in SPECIFIC_VERTICAL_CODES and conf >= threshold:
        return guess, False
    if code in SPECIFIC_VERTICAL_CODES:
        rationale = (
            (guess.rationale + "; " if guess.rationale else "")
            + f"confidence {conf:.0%} below {threshold:.0%} → defaulting to the "
            "generic playbook, confirm the vertical with the user"
        )
        return VerticalGuess("generic", conf, rationale), True
    # already generic (or an unknown code) → confirm
    return VerticalGuess("generic", conf, guess.rationale), True


# ── LLM deducer (single-shot MiniMax M3) ─────────────────────────────────────

_DEDUCER_SYSTEM_PROMPT = """You are a vertical classifier for an ad-campaign builder.

Given a studied product profile, decide which VERTICAL PLAYBOOK best fits. The
vertical you pick selects required slots, compliance rules, and the creative
taxonomy for the whole campaign build, so be honest about uncertainty.

Known vertical codes:
- "real_estate": residential/commercial property — apartments, villas, plots,
  projects by a developer/builder; signals like BHK, RERA, sq ft, floor plans,
  possession, site visits, gated community.
- "generic": anything that is NOT clearly one of the specific verticals above.
  Use this when you are unsure — do not force a product into a specific vertical.

Return ONLY a JSON object (no prose, no fences):
{"code": "<one of the known codes>", "confidence": <0..1 float>, "rationale": "<one sentence>"}

confidence is your confidence in a SPECIFIC vertical. If the product is not
clearly a listed specific vertical, return "generic" with a low confidence.
"""


def _build_deducer_context() -> BaseContext:
    ctx = BaseContext(doc_paths=[], static_prefix=_DEDUCER_SYSTEM_PROMPT)
    ctx._cached_static_text = ctx._static_prefix
    return ctx


class _SilentStream(AgentEventStream):
    """Drops user-facing output; forwards only agent-lifecycle + data events.

    The deducer's output is the JSON it returns to the orchestrator — never
    shown to the user — so text/thinking/tool events are swallowed. Self-
    contained (no legacy import) so this module loads on Python 3.9.
    """

    def __init__(self, parent: AgentEventStream | None) -> None:
        # Intentionally skip super().__init__(): pure delegate, no local queue.
        self._parent = parent

    @property
    def is_cancelled(self) -> bool:
        return getattr(self._parent, "is_cancelled", False)

    def cancel(self) -> None:
        try:
            if self._parent:
                self._parent.cancel()
        except Exception:
            pass

    async def emit_text(self, text: str) -> None:
        return

    async def emit_thinking(self, reasoning: str) -> None:
        return

    async def emit_tool_start(self, *a: Any, **kw: Any) -> None:
        return

    async def emit_tool_update(self, *a: Any, **kw: Any) -> None:
        return

    async def emit_tool_result(self, *a: Any, **kw: Any) -> None:
        return

    async def emit_error(self, message: str) -> None:
        logger.debug("vertical_deducer_substream_error: %s", str(message)[:200])

    async def emit_done(self, *a: Any, **kw: Any) -> None:
        return

    async def emit_keepalive(self) -> None:
        return

    async def emit_suggestions(self, options: Any, mode: str = "single") -> None:
        return

    async def emit_data(self, data_type: str, payload: dict) -> None:
        if self._parent:
            await self._parent.emit_data(data_type, payload)

    async def emit_agent_started(self, agent_id: str, label: str, parent_id: str = "root",
                                 parent_tool_use_id: str = "",
                                 agent_tool_use_id: str = "") -> None:
        if self._parent:
            await self._parent.emit_agent_started(
                agent_id, label, parent_id, parent_tool_use_id,
                agent_tool_use_id=agent_tool_use_id,
            )

    async def emit_agent_finished(self, agent_id: str, status: str = "success",
                                  duration_ms: int = 0, tokens_in: int = 0, tokens_out: int = 0,
                                  step_count: int = 0, summary: str = "") -> None:
        if self._parent:
            await self._parent.emit_agent_finished(
                agent_id, status, duration_ms, tokens_in, tokens_out, step_count, summary,
            )

    async def emit_agent_usage(self, agent_id: str, tokens_in: int, tokens_out: int) -> None:
        if self._parent:
            await self._parent.emit_agent_usage(agent_id, tokens_in, tokens_out)

    async def emit_craft(self, *a: Any, **kw: Any) -> None:
        return

    async def emit_craft_text(self, *a: Any, **kw: Any) -> None:
        return

    async def emit_feedback_request(self, session_id: str, turn_number: int) -> None:
        return


class VerticalDeducer(BaseAgent):
    """Single-shot MiniMax-M3 vertical classifier (no tools, one turn).

    Mirrors the SummaryAgent shape (an LLM work unit is a BaseAgent subclass).
    ``deduce`` returns a raw ``VerticalGuess``; the orchestrator applies the
    confidence policy. Falls back to the deterministic heuristic when there is
    no event stream / auth (offline) or on any LLM failure.
    """

    display_name = "Vertical Deducer"

    _instance: "VerticalDeducer | None" = None

    def __init__(self) -> None:
        super().__init__(
            name="vertical_deducer",
            tools=[],
            context_builder=_build_deducer_context(),
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=1,
            max_tokens=400,
            provider=getattr(settings, "ADZUMP2_PROVIDER", settings.LLM_PROVIDER),
        )

    @classmethod
    def get_instance(cls) -> "VerticalDeducer":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("VerticalDeducer created (single-shot, M3)")
        return cls._instance

    async def deduce(
        self,
        profile: ProductProfile,
        event_stream: AgentEventStream | None = None,
        auth: AuthContext | None = None,
    ) -> VerticalGuess:
        """Classify ``profile`` into a J5 vertical code + confidence.

        LLM path only when both ``event_stream`` and ``auth`` are present;
        otherwise (and on any failure) the deterministic heuristic. The result
        is always a valid ``VerticalGuess`` — deduction never raises.
        """
        if event_stream is None or auth is None:
            return deduce_vertical_heuristic(profile)

        try:
            sub_session = BaseSession(agent_name=self.name)
            await sub_session.get_or_create(None, auth)
            user_message = self._build_user_message(profile)
            await self.run(
                user_message=user_message,
                session=sub_session,
                event_stream=_SilentStream(event_stream),
            )
            guess = self._parse_guess(self._last_assistant_text(sub_session))
            if guess is not None:
                return guess
            logger.warning("vertical_deducer: unparseable LLM output → heuristic")
        except Exception as e:  # never let deduction take down the study
            logger.warning("vertical_deducer LLM path failed (%s) → heuristic",
                           type(e).__name__)
        return deduce_vertical_heuristic(profile)

    @staticmethod
    def _build_user_message(profile: ProductProfile) -> str:
        import json
        payload = {
            "name": profile.name,
            "pitch": profile.pitch,
            "value_props": profile.value_props,
            "offerings": profile.offerings,
            "price_band": profile.price_band,
            "business_type": (profile.attributes or {}).get("business_type", ""),
        }
        return "Classify this product profile:\n" + json.dumps(payload, default=str)

    @staticmethod
    def _last_assistant_text(session: BaseSession) -> str:
        for m in reversed(session.get_messages()):
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                if any(parts):
                    return "\n".join(p for p in parts if p)
        return ""

    @staticmethod
    def _parse_guess(text: str) -> VerticalGuess | None:
        """Parse the classifier JSON → a validated VerticalGuess, or None."""
        from app.agents.adzump._shared import extract_json

        payload = extract_json(text)
        if not isinstance(payload, dict):
            return None
        code = str(payload.get("code") or "").strip()
        if code not in KNOWN_VERTICAL_CODES:
            return None
        try:
            confidence = float(payload.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        rationale = str(payload.get("rationale") or "")[:400]
        return VerticalGuess(code=code, confidence=round(confidence, 4), rationale=rationale)


def get_vertical_deducer() -> VerticalDeducer:
    """Module-level accessor for the shared VerticalDeducer singleton."""
    return VerticalDeducer.get_instance()
