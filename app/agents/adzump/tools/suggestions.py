"""Suggestion tools — present clickable options to the user.

The LLM calls present_options when asking a question with fixed choices.
The options are stored in session context and emitted as an SSE event
after the agentic loop completes. The UI renders them as clickable buttons.

`infer_suggestions` is the fallback for when the LLM forgets to call the
tool — a cheap LLM call inspects the assistant text and returns options
if the message ends with a discrete-choice question.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.config import settings

logger = logging.getLogger(__name__)


async def _present_options(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Store suggested options for the UI to render as buttons."""
    options = params.get("options", [])
    if not options:
        return ToolResult(success=False, error="options array is required.")

    mode = params.get("mode", "single")
    if mode not in ("single", "multi"):
        return ToolResult(success=False, error="mode must be 'single' or 'multi'.")

    suggestions = {
        "options": [{"label": opt, "value": opt} for opt in options],
        "mode": mode,
    }

    # Write directly to the session object to guarantee the read-back works
    # in get_pending_suggestions. The shared session_ctx dict can get
    # detached after sub-agents replace sub_session.context.
    parent_session = context.get("_session")
    if parent_session:
        parent_session.context["_pending_suggestions"] = suggestions
    else:
        session_ctx = context.get("session_context")
        if session_ctx is None:
            return ToolResult(success=False, error="No session context available.")
        session_ctx["_pending_suggestions"] = suggestions

    logger.info("present_options: mode=%s options=%s", mode, options)
    return ToolResult(success=True, summary=f"{len(options)} options")


present_options = ToolDefinition(
    name="present_options",
    description=(
        "Show clickable option buttons to the user. Use when asking a question "
        "with fixed choices (e.g., platform selection, account selection). "
        "The user can click an option instead of typing."
    ),
    display_name="Quick Replies",
    parameters=[
        ToolParameter(
            name="options",
            type="array",
            description="List of option labels to show as buttons",
            required=True,
            items={"type": "string"},
        ),
        ToolParameter(
            name="mode",
            type="string",
            description="Selection mode: 'single' (click sends immediately) or 'multi' (toggle + confirm)",
            required=False,
            enum=["single", "multi"],
        ),
    ],
    execute=_present_options,
)

SUGGESTION_TOOLS = [present_options]


# ── Fallback inference ────────────────────────────────────────────────────

_INFER_PROMPT = """You decide whether an assistant message ends with a choice question that warrants clickable option buttons — and when it does, you propose SMART, CONTEXT-AWARE options tailored to the business the user is advertising.

Return STRICT JSON in one of these shapes:
- No buttons:  {"needs_options": false}
- Buttons:     {"needs_options": true, "options": ["Label 1", "Label 2", ...], "mode": "single"}

When to return needs_options=true:
- The message ends with a question that has a small set (2-6) of discrete, meaningful answers.
- Yes/No, A/B branch decisions → Yes.
- Numeric input IS fine when you can propose sensible presets from the business context (e.g. lead targets, budgets, durations).
- Free-text questions with no sensible discrete answers (URL, free description) → needs_options=false.

How to use the business context:
- If the context shows a luxury real-estate product at ₹4+ Cr, lead targets should be small (5, 10, 25, 50) and budgets should be high (₹5,000/day, ₹10,000/day, ₹25,000/day).
- If the context shows a mid-market SaaS at $49/mo, lead targets should be larger (100, 250, 500, 1000) and budgets much smaller.
- If the context shows a D2C consumer product at ₹500-1500, tune both down accordingly.
- Match labels to the currency/format already used in the conversation (₹/day vs $/day, "leads" vs "signups", etc.).
- Always include a sensible "Custom" option for numeric presets so the user can override.
- If the message lists options inline (e.g. "Google Ads or Meta?"), honour those exact labels — don't invent new ones.

Other rules:
- Use mode "multi" ONLY if the question explicitly asks for multiple selections; otherwise "single".
- Labels must be short, human-readable, ready to send as-is.
- Output JSON only. No prose, no fences."""


def _build_context_snippet(ctx: dict[str, Any] | None) -> str:
    """Build a compact business + campaign context string for the inferrer."""
    if not ctx:
        return ""
    business = ctx.get("product_data") or {}
    campaign = ctx.get("campaign_data") or {}
    lines: list[str] = []
    if business:
        bits = []
        if business.get("product_name"):
            bits.append(f"product={business['product_name']}")
        if business.get("business_type"):
            bits.append(f"type={business['business_type']}")
        if business.get("pricing"):
            bits.append(f"pricing={str(business['pricing'])[:120]}")
        loc = business.get("location")
        if isinstance(loc, str) and loc:
            bits.append(f"location={loc}")
        elif isinstance(loc, dict) and loc.get("location"):
            bits.append(f"location={loc['location']}")
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
