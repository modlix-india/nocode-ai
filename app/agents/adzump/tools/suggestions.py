"""Suggestion tools - present a question + clickable options atomically.

`present_options` owns the *full* assistant turn for a discrete-choice ask:
it streams the question text into the assistant message AND emits the chips
event. The LLM can no longer write a question as free text and forget to
call the tool - because the question text is a tool argument, not free text.

`infer_suggestions` remains as a safety net for the rare case where the LLM
ignores the contract and writes a question without calling the tool.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.models import LEGACY_MARKER_TO_FIELD
from app.config import settings

logger = logging.getLogger(__name__)


def _norm_q(s: str) -> str:
    """Normalize a question for the v4 · F9 de-dup compare: lowercase, collapse
    whitespace, strip trailing punctuation. Lets "How long should it run?" match
    a prose "...how long should it run" the model already streamed."""
    return re.sub(r"\s+", " ", (s or "").lower()).strip().strip("?.!,: ").strip()


async def _present_options(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Stream a question to the user + emit clickable option chips.

    The tool owns the user-facing question text - pass it as ``question``.
    Free-text echoing of the question by the LLM is unnecessary (and in the
    summary we ask it not to). Each option is either a string (label==value)
    or a ``{label, value}`` dict (label is what the user sees; value is what
    the backend receives - needed for account picks where the label is a
    name but value must be a customer_id / business id).
    """
    question = (params.get("question") or "").strip()
    if not question:
        return ToolResult(
            success=False,
            error=(
                "`question` is required. Pass the exact question text the user "
                "should see - the tool emits it; do not also write it as free text."
            ),
        )

    options = params.get("options", [])
    if not options:
        return ToolResult(success=False, error="options array is required.")

    field = (params.get("field") or "").strip() or None

    def _answerless_refusal(offender: str) -> ToolResult:
        # Every chip on a field-tagged ask must say what it writes - a silent
        # fall-through is the bug class where a click lands nowhere and the
        # question re-fires. Self-healing (D13): hand back the corrected
        # options (answer == value; an invented "Custom" chip - deleted from
        # the flow, but old habits linger - maps to null so a click can never
        # store the literal string) - the retry is a copy-paste, never a
        # dead-end turn.
        corrected = []
        for o in options:
            label = o if isinstance(o, str) else str(o.get("label", ""))
            value = label if isinstance(o, str) else str(o.get("value") or label)
            answer = (o.get("answer") if isinstance(o, dict) and "answer" in o
                      else (None if value == "Custom" else value))
            corrected.append({"label": label, "value": value, "answer": answer})
        return ToolResult(
            success=False,
            error=(
                f'Option "{offender}" carries no "answer" key. On a field-tagged '
                f'ask (field="{field}") EVERY option must declare what it writes '
                'on click ("answer": null = a deliberate fall-through like '
                '"Facebook only"). Re-call present_options NOW with the SAME '
                f"question and options={json.dumps(corrected, ensure_ascii=False)}"
            ),
            display_error="Re-forming those options…",
        )

    normalized: list[dict[str, str]] = []
    answer_map: dict[str, str] = {}
    for opt in options:
        if isinstance(opt, str):
            if field:
                return _answerless_refusal(opt)
            normalized.append({"label": opt, "value": opt})
        elif isinstance(opt, dict) and opt.get("label"):
            label = str(opt["label"])
            value = str(opt.get("value") or label)
            if field and "answer" not in opt:
                return _answerless_refusal(label)
            normalized.append({"label": label, "value": value})
            # PR2 · a capturable option declares `answer` (the value to store on
            # click). An explicit "answer": null is a declared fall-through
            # ("Facebook only") - absent from the map → capture defers to the
            # LLM.
            if opt.get("answer") is not None:
                answer_map[value] = str(opt["answer"])
        else:
            return ToolResult(success=False, error=f"Invalid option: {opt!r}")

    mode = params.get("mode", "single")
    if mode not in ("single", "multi"):
        return ToolResult(success=False, error="mode must be 'single' or 'multi'.")

    suggestions = {"options": normalized, "mode": mode}

    parent_session = context.get("_session")
    session_ctx = parent_session.context if parent_session else context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")
    session_ctx["_pending_suggestions"] = suggestions
    # Per-field ask counter (slice 1d/1e): every field-tagged ask that goes on
    # screen bumps its count. Consumers: the creatives resolved predicate
    # (asked twice unanswered = settled, so a digression resurfaces an offer at
    # most ONCE and review is never held hostage) and the refused-required-slot
    # escape in the duration/budget steps (R12: repeated misses → "help me pick" chips).
    if field:
        counted = LEGACY_MARKER_TO_FIELD.get(field, field)
        asks = session_ctx.setdefault("_field_asks", {})
        asks[counted] = asks.get(counted, 0) + 1

    # Stream the question into the assistant message so it visually precedes
    # the chips. Wrapped in newlines so it separates from any conversational
    # lead-in the LLM streamed before this tool call.
    #
    # v4 · F9 - the tool OWNS the question, but the model sometimes ALSO writes
    # it as prose this turn (the system prompt forbids it; F4's steer discourages
    # it; it still happens). Emitting then would double-render the question (live
    # bug #10). So skip our emit when the question already appears in this turn's
    # streamed assistant text - exactly one copy either way. Normalized-contains
    # match (panel rec); a divergent paraphrase still falls through to emit.
    stream = context.get("event_stream")
    if stream is not None:
        streamed = getattr(parent_session, "_turn_assistant_text", "") if parent_session else ""
        # Slice 1e (R7) - the acknowledgement backstop: a capture landed this
        # turn but the model's streamed prose never named the value → prepend a
        # short visible ack. The F9 emit-skip below may skip the QUESTION,
        # never the ack - a click must never look ignored. Runs before the
        # core turn-break (core/agent.py:478-502), so the ack always streams.
        ack_pending = session_ctx.pop("_capture_ack_pending", None)
        ack_text = ""
        if ack_pending and str(ack_pending.get("value", "")) not in streamed:
            ack_field = str(ack_pending.get("field", "")).replace("_", " ")
            ack_text = f"Got it - {ack_field}: {ack_pending.get('value')}.\n"
        nq = _norm_q(question)
        already = bool(nq) and nq in _norm_q(streamed)
        if already:
            logger.info("present_options: question already in streamed prose - skip emit (F9)")
            if ack_text:
                await stream.emit_text(f"\n\n{ack_text}")
        else:
            await stream.emit_text(f"\n\n{ack_text}{question}\n")

    logger.info("present_options: mode=%s field=%s options=%s question=%r",
                mode, field, options, question[:80])
    return ToolResult(
        success=True,
        # PR2 · tag the elicitation so the harness captures the answer next turn.
        # Rides _pending_elicitation via core (same channel as elicit_expects);
        # None when untagged, so this stays inert for control-flow asks.
        data=({"elicit_field": field, "elicit_answers": answer_map} if field else None),
        summary=(
            f"Asked the user: \"{question[:120]}\" with {len(options)} options. "
            "Question is already on screen - do not write it again. "
            "Stop generating text now; wait for the user's reply."
        ),
    )


present_options = ToolDefinition(
    name="present_options",
    description=(
        "Ask the user a discrete-choice question with clickable option chips. "
        "This tool emits BOTH the question text and the chips - do not write "
        "the question as free text yourself. You may write a brief one-line "
        "conversational lead-in (e.g. \"Got it.\") before calling the tool. "
        "Use whenever the answer is a small set (2-6) of meaningful choices: "
        "platform, duration, budget presets, accounts, Yes/No confirms. Each "
        "option is a plain string (label==value) or a {label, value} object "
        "(label is what the user sees; value is what you receive back - needed "
        "for account picks where the label is the human name but value must "
        "be the customer_id / business id)."
    ),
    display_name="Quick Replies",
    parameters=[
        ToolParameter(
            name="question",
            type="string",
            description=(
                "The exact question text shown to the user above the chips. "
                "End with '?'. Be concise (one sentence). Don't repeat options "
                "in the text - the chips show them."
            ),
            required=True,
        ),
        ToolParameter(
            name="options",
            type="array",
            description=(
                "List of options. Item is either a string (label==value) or a "
                "{label, value} object."
            ),
            required=True,
            items={
                "anyOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                            "answer": {"type": "string"},
                        },
                        "required": ["label", "value"],
                    },
                ],
            },
        ),
        ToolParameter(
            name="mode",
            type="string",
            description="Selection mode: 'single' (click sends immediately) or 'multi' (toggle + confirm)",
            required=False,
            enum=["single", "multi"],
        ),
        ToolParameter(
            name="field",
            type="string",
            description=(
                "Set ONLY for data-collection asks that fill a campaign field "
                "(platform / duration / budget / competitive_analysis / "
                "competitor_creatives / instagram / account picks). The harness "
                "then stores the user's answer directly. EVERY option on a "
                "field-tagged ask must carry an `answer` key - the value to "
                "store on click (usually == value; \"accepted\"/\"declined\" for "
                "offer Yes/No chips). Use `answer: null` ONLY for a deliberate "
                "fall-through option (\"Facebook only\"). Leave "
                "`field` unset for control-flow asks (launch confirmation)."
            ),
            required=False,
        ),
    ],
    execute=_present_options,
    # v8 Plan B WS3 · deferred elicitation. The run loop breaks after this so
    # the chips are the only ask in the turn. (The `mode` param above is the
    # chip selection mode; elicit_expects="single" because the user sends one
    # reply - a click or a confirmed multi-select - regardless of chip mode.)
    kind="elicitation",
    elicit_mode="deferred",
    elicit_expects="single",
)

SUGGESTION_TOOLS = [present_options]


# ── Fallback inference ────────────────────────────────────────────────────

_INFER_PROMPT = """You decide whether an assistant message ends with a choice question that warrants clickable option buttons - and when it does, you propose SMART, CONTEXT-AWARE options tailored to the business the user is advertising.

Return STRICT JSON in one of these shapes:
- No buttons:  {"needs_options": false}
- Buttons:     {"needs_options": true, "options": ["Label 1", "Label 2", ...], "mode": "single"}

When to return needs_options=true:
- The message ends with a question that has a small set (2-6) of discrete, meaningful answers.
- Yes/No, A/B branch decisions → Yes.
- Numeric input IS fine when you can propose sensible presets from the business context (e.g. budgets, durations).
- Free-text questions with no sensible discrete answers (URL, free description) → needs_options=false.

How to use the business context:
- If the context shows a luxury real-estate product at ₹4+ Cr, budgets should be high (₹5,000/day, ₹10,000/day, ₹25,000/day).
- If the context shows a mid-market SaaS at $49/mo, budgets should be much smaller.
- If the context shows a D2C consumer product at ₹500-1500, tune down accordingly.
- Match labels to the currency/format already used in the conversation (₹/day vs $/day).
- Numeric preset questions must END with "or type your own" - never add a "Custom" chip (typed replies are handled).
- If the message lists options inline (e.g. "Google Ads or Meta?"), honour those exact labels - don't invent new ones.

Other rules:
- Use mode "multi" ONLY if the question explicitly asks for multiple selections; otherwise "single".
- Labels must be short, human-readable, ready to send as-is.
- Output JSON only. No prose, no fences."""


def _build_context_snippet(ctx: dict[str, Any] | None) -> str:
    """Build a compact business + campaign context string for the inferrer."""
    if not ctx:
        return ""
    business = ctx.get("product_data") or {}
    campaign = ctx.get("campaign_spec") or {}
    lines: list[str] = []
    if business:
        bits = []
        if business.get("product_name"):
            bits.append(f"product={business['product_name']}")
        if business.get("business_type"):
            bits.append(f"type={business['business_type']}")
        if business.get("pricing"):
            bits.append(f"pricing={str(business['pricing'])[:120]}")
        loc = (business.get("place") or {}).get("address") or ""
        if loc:
            bits.append(f"location={loc}")
        if bits:
            lines.append("Business: " + ", ".join(bits))
        summary = business.get("summary")
        if summary:
            lines.append(f"Summary: {str(summary)[:400]}")
    if campaign:
        bits = [f"{k}={v}" for k, v in campaign.items() if v]
        if bits:
            lines.append("Campaign so far: " + ", ".join(bits))
    return "\n".join(lines)


async def infer_suggestions(
    text: str, session_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """If the assistant text ends with a choice question, return smart,
    context-aware {options, mode} using the business + campaign state."""
    if not text or "?" not in text[-300:]:
        return None

    context_snippet = _build_context_snippet(session_context)
    user_content = text[-800:]
    if context_snippet:
        user_content = f"## Context\n{context_snippet}\n\n## Assistant message\n{user_content}"

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _INFER_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        data = json.loads((resp.choices[0].message.content or "").strip())
    except Exception as e:
        logger.debug("infer_suggestions failed: %s: %s", type(e).__name__, str(e)[:200])
        return None

    if not data.get("needs_options"):
        return None

    options = data.get("options") or []
    if not isinstance(options, list) or not (2 <= len(options) <= 8):
        return None

    mode = data.get("mode") if data.get("mode") in ("single", "multi") else "single"
    formatted = [{"label": str(o), "value": str(o)} for o in options if str(o).strip()]
    if len(formatted) < 2:
        return None

    logger.info("inferred_suggestions: mode=%s options=%s", mode, [o["value"] for o in formatted])
    return {"options": formatted, "mode": mode}
