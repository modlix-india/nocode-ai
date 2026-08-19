"""Internal LLM tools for Lead Form Generation."""

import logging
from app.core.agent import ToolResult, ToolDefinition
from app.core.tools.base import ToolParameter
from app.agents.adzump.agents.leadform.models import (
    LeadFormRecommendation, LeadFormQuestion,
    ContextCard, PrivacyPolicy, QUESTION_SCHEMA_PARAM, CONTEXT_CARD_SCHEMA_PARAM, PRIVACY_POLICY_SCHEMA_PARAM
)
from app.agents.adzump.adapters.meta.lead_forms import meta_lead_forms_adapter
from app.agents.adzump.agents.leadform.parser import parse_leadgen_forms
from app.agents.adzump.agents.leadform.context import Phase

logger = logging.getLogger(__name__)


async def _analyze_historical_forms(params: dict, context: dict) -> ToolResult:
    """Fetches and analyzes past forms to understand advertiser preferences."""
    session_ctx = context.get("session_context", {})
    spec = session_ctx.get("campaign_spec", {})
    page_id = spec.get("fb_page")
    auth_headers = context.get("auth", {})

    if not page_id:
        return ToolResult(success=False, error="No Facebook Page ID found in campaign_spec (expected under 'fb_page').")

    # Use the caller's client_code so each tenant fetches its own Meta token.
    client_code = context.get("client_code", "")
    try:
        raw_forms = await meta_lead_forms_adapter.get_leadgen_forms(
            page_id=page_id,
            client_code=client_code,
            auth_headers=auth_headers
        )
    except Exception as e:
        logger.error("Failed to fetch forms: %s", e)
        return ToolResult(success=False, error="Failed to fetch historical forms from Meta.")

    profiles = parse_leadgen_forms(raw_forms)

    if not profiles:
        # No history
        session_ctx["advertiser_knowledge"] = {"summary": "Advertiser has no prior lead forms."}
        session_ctx["lf_phase"] = Phase.RECOMMEND.value
        return ToolResult(success=True, summary="No past forms found. Proceed with standard best practices.")

    session_ctx["historical_forms"] = [p.model_dump() for p in profiles]
    session_ctx["lf_phase"] = Phase.ANALYZE.value

    return ToolResult(
        success=True, 
        summary=f"Found {len(profiles)} historical forms. They have been added to the context. Proceed with analysis phase."
    )


ANALYZE_HISTORICAL_FORMS = ToolDefinition(
    name="analyze_historical_forms",
    description="Analyze the advertiser's historical lead forms to determine their preferences.",
    parameters=[],
    execute=_analyze_historical_forms
)


async def _build_form_recommendation(params: dict, context: dict) -> ToolResult:
    """Builds and saves the draft form."""
    name = params.get("name", "")
    context_card_data = params.get("context_card", {})
    question_page_headline = params.get("question_page_headline", "")
    is_higher_intent = params.get("is_higher_intent", False)
    is_phone_sms_verify_enabled = params.get("is_phone_sms_verify_enabled", False)
    questions = params.get("questions", [])
    privacy_policy_data = params.get("privacy_policy", {})
    custom_disclaimer = params.get("custom_disclaimer", "")
    thank_you_headline = params.get("thank_you_headline", "Thanks, you're all set.")
    thank_you_description = params.get("thank_you_description", "We will contact you shortly.")
    cta_button_type = params.get("cta_button_type", "VIEW_WEBSITE")
    cta_button_text = params.get("cta_button_text", "Visit Website")
    business_phone_number = params.get("business_phone_number", "")

    try:
        parsed_questions = []
        for q in questions:
            if isinstance(q, dict) and "type" in q and isinstance(q["type"], str):
                q["type"] = q["type"].upper()
            parsed_questions.append(LeadFormQuestion(**q))
    except Exception as e:
        return ToolResult(success=False, error=f"Invalid questions format: {e}")

    session_ctx = context.get("session_context", {})
    b_ctx = session_ctx.get("business_context", {})
    privacy_url = b_ctx.get("privacy_policy_url", "")

    try:
        draft = LeadFormRecommendation(
            name=name,
            context_card=ContextCard(**context_card_data) if context_card_data else ContextCard(),
            question_page_headline=question_page_headline,
            is_higher_intent=is_higher_intent,
            is_phone_sms_verify_enabled=is_phone_sms_verify_enabled,
            questions=parsed_questions,
            privacy_policy=PrivacyPolicy(url=privacy_url, link_text=privacy_policy_data.get("link_text", "Privacy Policy")),
            custom_disclaimer=custom_disclaimer,
            thank_you_headline=thank_you_headline,
            thank_you_description=thank_you_description,
            cta_button_type=cta_button_type,
            cta_button_text=cta_button_text,
            business_phone_number=business_phone_number,
        )
    except Exception as e:
        return ToolResult(success=False, error=f"Validation error: {e}")

    # Save draft to session context
    session_ctx["lead_form_draft"] = draft.model_dump()

    from app.agents.adzump.agents.leadform.utils import serialize_leadform_payload
    website_url = b_ctx.get("website_url", "")
    meta_payload = serialize_leadform_payload(draft.model_dump(), website_url)

    stream = context.get("event_stream")
    if stream:
        await stream.emit_data("leadform_payload_preview", meta_payload)
        
        craft_id = session_ctx.get("craft_id", "leadform_craft")
        spec = session_ctx.get("campaign_spec", {})
        page_id = spec.get("fb_page", "")
        page_logo_url = None
        # build_tool_context injects the session under "_session", not "session".
        # Using "session" always resolved to None, causing the logo fetch to be
        # silently skipped on every craft emit.
        if page_id and hasattr(context.get("_session"), "auth"):
            try:
                auth = context["_session"].auth
                page_logo_url = await meta_lead_forms_adapter.get_page_profile_picture(
                    page_id, auth.client_code, auth.to_headers()
                )
            except Exception:
                pass

        await stream.emit_craft(
            craft_id=craft_id,
            title="Lead Form Draft",
            blocks=[{
                "type": "lead_form",
                "payload": meta_payload,
                "page_id": page_id,
                "page_logo_url": page_logo_url,
                "cover_image_url": draft.context_card.cover_image_url,
                "cover_photo_id": draft.context_card.cover_photo_id,
                "is_phone_sms_verify_enabled": bool(draft.is_phone_sms_verify_enabled),
            }]
        )

    return ToolResult(
        success=True, 
        summary="Form successfully drafted. The craft UI has been updated. The generation phase is complete."
    )


BUILD_FORM_RECOMMENDATION = ToolDefinition(
    name="build_form_recommendation",
    description="Draft the final Lead Form Recommendation.",
    parameters=[
        ToolParameter(name="name", type="string", description="Internal name of the form (max 60 chars)."),
        ToolParameter(**{"name": "context_card", **CONTEXT_CARD_SCHEMA_PARAM}),
        ToolParameter(name="question_page_headline", type="string", description="Question page headline (max 60 chars)."),
        ToolParameter(name="is_higher_intent", type="boolean", description="Whether to include a review screen."),
        ToolParameter(name="is_phone_sms_verify_enabled", type="boolean", description="Whether to require SMS verification for phone numbers (OTP)."),
        ToolParameter(name="thank_you_headline", type="string", description="Headline for thank you page (max 60 chars)."),
        ToolParameter(name="thank_you_description", type="string", description="Description for thank you page (max 350 chars)."),
        ToolParameter(name="cta_button_type", type="string", description="Call to action button type: 'VIEW_WEBSITE', 'CALL_BUSINESS', 'DOWNLOAD', 'WHATSAPP', 'MESSAGE_BUSINESS', 'SCHEDULE_APPOINTMENT', 'BOOK_ON_WEBSITE', 'PROMO_CODE', 'NONE'."),
        ToolParameter(name="cta_button_text", type="string", description="Text for the call to action button (max 30 chars)."),
        ToolParameter(name="business_phone_number", type="string", description="Phone number with country code (e.g. +1234567890), required if cta_button_type is CALL_BUSINESS."),
        ToolParameter(name="custom_disclaimer", type="string", description="Custom legal disclaimer text, if required."),
        ToolParameter(**{"name": "privacy_policy", **PRIVACY_POLICY_SCHEMA_PARAM}),
        ToolParameter(**{
            "name": "questions",
            **QUESTION_SCHEMA_PARAM
        })
    ],
    execute=_build_form_recommendation
)

ALL_TOOLS = [ANALYZE_HISTORICAL_FORMS, BUILD_FORM_RECOMMENDATION]
