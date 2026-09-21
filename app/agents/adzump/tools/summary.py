"""Code-rendered campaign review card.

The card is rendered by code from the typed context - a model re-typing it
rephrases fields, drops bullets, and swaps real IDs for placeholders.
``show_campaign_summary`` returns it audience="user" so the exact markdown
reaches the chat; the model only supplies a lead-in and the follow-up
launch ask.
"""

from __future__ import annotations

from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult
from app.agents.adzump.models import OfferState, offer_state
from app.agents.adzump.workflow import AdzumpContext
from app.agents.adzump.prompt_sections import account_display, website_display
from app.agents.adzump.tools.campaign_data import campaign_spec_complete


async def _show_campaign_summary(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Render the review card for the user. Refuses while the spec is
    incomplete - the completeness gate is campaign_data's, shared with the
    review hint, so the card and the prescription can never disagree."""
    session = context.get("_session")
    if session is None:
        return ToolResult(success=False, error="No session available.")
    actx = AdzumpContext.from_session(session)
    if not campaign_spec_complete(actx.spec, session.context):
        return ToolResult(
            success=False,
            error=(
                "The campaign spec is not complete - the summary renders only "
                "when every required field is set. Keep collecting what the "
                "turn reminder lists as missing."
            ),
            display_error="Waiting on the remaining campaign details…",
        )
    return ToolResult(
        success=True,
        audience="user",
        summary=render_summary_card(actx),
        model_summary=(
            "Summary card is on screen - do NOT write your own summary text. "
            'Now ask via the present_options tool: "Ready to launch the '
            'campaign?" with chips Yes, launch / No, make changes. On '
            "'Yes, launch', call launch_campaign()."
        ),
    )


def render_summary_card(actx: AdzumpContext) -> str:
    """Pure typed-context -> markdown card. Every bullet always present;
    account-like fields render '{Name} (ID: {id})' verbatim - never a
    placeholder."""
    spec = actx.spec

    def account(field: str) -> str:
        return account_display(spec.get(field), actx.account_names,
                               spec.get("platform"))

    lines = [
        "Here's your campaign summary:",
        "",
        f"  - **Product**: {actx.product.get('product_name') or '-'}",
        f"  - **Website**: {website_display(actx)}",
        f"  - **Location**: {spec.get('location') or '-'}",
        f"  - **Platform**: {spec.get('platform') or '-'}",
        f"  - **Duration**: {spec.get('duration') or '-'}",
        f"  - **Daily Budget**: {spec.get('budget') or '-'}",
        f"  - **Manager / Business Account**: {account('parent_account')}",
        f"  - **Ad Account**: {account('account')}",
    ]
    if actx.is_meta:
        lines.append(f"  - **Facebook Page**: {account('fb_page')}")
        lines.append(
            f"  - **Instagram Account**: {account('ig_page')}"
            if spec.get("ig_page")
            else "  - **Instagram Account**: not linked (Facebook only)"
        )
    lines.append(f"  - **Competitors**: {_competitors_line(actx)}")
    return "\n".join(lines)


def _competitors_line(actx: AdzumpContext) -> str:
    if actx.competitor_names:
        return ", ".join(actx.competitor_names)
    if offer_state(actx.spec, "competitive_analysis") is OfferState.DECLINED:
        return "declined"
    if actx.competitor_analysis_attempted:
        return "none analyzed"
    return "not analyzed"


show_campaign_summary = ToolDefinition(
    name="show_campaign_summary",
    description=(
        "Render the campaign review summary card to the user. Call this when "
        "every campaign field is set (the turn reminder tells you) - the card "
        "is rendered by code from stored state, so NEVER write the summary "
        "yourself. After it, ask the launch confirmation via present_options."
    ),
    display_name="Campaign Summary",
    parameters=[],
    execute=_show_campaign_summary,
)
