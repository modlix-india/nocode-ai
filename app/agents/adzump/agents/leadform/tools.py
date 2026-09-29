"""Internal LLM tools for Lead Form Generation."""

import logging

from app.agents.adzump._shared import build_ds_headers
from app.agents.adzump.adapters.meta.lead_forms import meta_lead_forms_adapter
from app.agents.adzump.agents.leadform.context import Phase
from app.agents.adzump.agents.leadform.models import (
    CONTEXT_CARD_SCHEMA_PARAM,
    PRIVACY_POLICY_SCHEMA_PARAM,
    QUESTION_SCHEMA_PARAM,
    ContextCard,
    LeadFormQuestion,
    LeadFormRecommendation,
    PrivacyPolicy,
    ThankYouPageButtonType,
)
from app.agents.adzump.agents.leadform.parser import parse_leadgen_forms
from app.agents.adzump.agents.leadform.utils import serialize_leadform_payload
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


async def _analyze_historical_forms(params: dict, context: dict) -> ToolResult:
    """Fetches and analyzes past forms to understand advertiser preferences."""
    session_ctx = context.get("session_context", {})
    spec = session_ctx.get("campaign_spec", {})
    page_id = spec.get("fb_page")
    auth_headers = build_ds_headers(context)
    if not auth_headers and hasattr(context.get("auth"), "to_headers"):
        auth_headers = context["auth"].to_headers()

    if not page_id:
        return ToolResult(success=False, error="No Facebook Page ID found in campaign_spec (expected under 'fb_page').")

    # Use the caller's client_code so each tenant fetches its own Meta token.
    client_code = context.get("client_code") or getattr(context.get("auth"), "client_code", "")
    page_cache = session_ctx.setdefault("_meta_page_cache", {})
    cached_token = page_cache.get(page_id, {}).get("access_token")
    try:
        raw_forms = await meta_lead_forms_adapter.get_leadgen_forms(
            page_id=page_id,
            client_code=client_code,
            auth_headers=auth_headers,
            page_token=cached_token,
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

    total_forms = len(profiles)
    total_leads = sum(p.leads_count for p in profiles)
    higher_intent_count = sum(1 for p in profiles if p.is_higher_intent)

    seen_types: set[str] = set()
    question_types: list[str] = []
    for p in profiles:
        for q in p.questions:
            q_type = q.type.value if hasattr(q.type, "value") else str(q.type)
            if q_type not in seen_types:
                seen_types.add(q_type)
                question_types.append(q_type)

    session_ctx["advertiser_knowledge"] = {
        "summary": f"Advertiser has {total_forms} prior lead form(s) with {total_leads} total leads recorded.",
        "forms_analyzed": total_forms,
        "total_leads_recorded": total_leads,
        "higher_intent_ratio": f"{higher_intent_count}/{total_forms}",
        "historical_question_types": question_types,
    }
    session_ctx["historical_forms"] = [p.model_dump() for p in profiles]
    session_ctx["lf_phase"] = Phase.RECOMMEND.value

    return ToolResult(
        success=True, 
        summary=(
            f"Found and analyzed {total_forms} historical forms ({total_leads} total leads). "
            f"Extracted advertiser patterns. Advancing to RECOMMEND phase to build the lead form draft."
        )
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
    custom_disclaimer_title = params.get("custom_disclaimer_title")
    thank_you_headline = params.get("thank_you_headline")
    thank_you_description = params.get("thank_you_description")
    cta_button_type = params.get("cta_button_type", "VIEW_WEBSITE")
    cta_button_text = params.get("cta_button_text")
    business_phone_number = params.get("business_phone_number", "")

    try:
        parsed_questions = [LeadFormQuestion(**q) for q in questions]
    except (KeyError, ValueError, TypeError) as e:
        return ToolResult(success=False, error=f"Invalid questions format: {e}")

    session_ctx = context.get("session_context", {})
    campaign_spec = session_ctx.get("campaign_spec", {}) if isinstance(session_ctx, dict) else {}
    locale = (
        params.get("locale")
        or campaign_spec.get("locale")
        or campaign_spec.get("country_code")
        or ""
    )
    b_ctx = session_ctx.get("business_context", {})
    privacy_url = b_ctx.get("privacy_policy_url", "")

    # Omit optional thank-you fields when not supplied so Pydantic applies
    # their Field(default=...) from LeadFormRecommendation as the sole source.
    recommendation_kwargs: dict = {
        "name": name,
        "context_card": ContextCard(**context_card_data) if context_card_data else ContextCard(),
        "question_page_headline": question_page_headline,
        "is_higher_intent": is_higher_intent,
        "is_phone_sms_verify_enabled": is_phone_sms_verify_enabled,
        "questions": parsed_questions,
        "privacy_policy": PrivacyPolicy(url=privacy_url, link_text=privacy_policy_data.get("link_text", "Privacy Policy")),
        "custom_disclaimer": custom_disclaimer,
        "cta_button_type": cta_button_type,
        "business_phone_number": business_phone_number,
        "locale": locale,
    }
    if custom_disclaimer_title is not None:
        recommendation_kwargs["custom_disclaimer_title"] = custom_disclaimer_title
    if thank_you_headline is not None:
        recommendation_kwargs["thank_you_headline"] = thank_you_headline
    if thank_you_description is not None:
        recommendation_kwargs["thank_you_description"] = thank_you_description
    if cta_button_text is not None:
        recommendation_kwargs["cta_button_text"] = cta_button_text

    try:
        draft = LeadFormRecommendation(**recommendation_kwargs)
    except (KeyError, ValueError, TypeError) as e:
        return ToolResult(success=False, error=f"Validation error: {e}")

    # Save draft to session context
    session_ctx["lead_form_draft"] = draft.model_dump()
    session_ctx["lead_form_published"] = False
    session_ctx.pop("meta_lead_form_id", None)
    # Advance past RECOMMEND so build_turn_reminder does not re-inject
    # "Call build_form_recommendation" on the next LLM iteration, which
    # causes the model to spend an extra turn debating a duplicate call.
    session_ctx["lf_phase"] = Phase.MANAGE.value

    website_url = b_ctx.get("website_url", "")
    meta_payload = serialize_leadform_payload(draft.model_dump(), website_url)

    stream = context.get("event_stream")
    if stream:
        await stream.emit_data("leadform_payload_preview", meta_payload)
        
        craft_id = session_ctx.get("craft_id", "leadform_craft")
        spec = session_ctx.get("campaign_spec", {})
        page_id = spec.get("fb_page", "")
        page_logo_url = None
        auth = getattr(context.get("_session"), "auth", None)
        if page_id and auth:
            try:
                page_cache = session_ctx.setdefault("_meta_page_cache", {})
                if page_id in page_cache:
                    page_logo_url = page_cache[page_id].get("picture_url")
                else:
                    page_info = await meta_lead_forms_adapter.get_page_info(
                        page_id, auth.client_code, auth.to_headers()
                    )
                    page_cache[page_id] = page_info
                    page_logo_url = page_info.get("picture_url")
            except Exception as e:
                logger.warning("Failed to fetch page logo for page_id=%s: %s", page_id, e)

        await stream.emit_craft(
            craft_id=craft_id,
            title="Lead Form Draft",
            blocks=[
                {"type": "badge", "label": "Instant Form Preview"},
                {
                    "type": "lead_form",
                    "payload": meta_payload,
                    "page_id": page_id,
                    "page_logo_url": page_logo_url,
                    "cover_image_url": draft.context_card.cover_image_url,
                    "cover_photo_id": draft.context_card.cover_photo_id,
                    "is_phone_sms_verify_enabled": bool(draft.is_phone_sms_verify_enabled),
                },
            ]
        )

    return ToolResult(
        success=True, 
        summary="Form successfully drafted. The craft UI has been updated. The generation phase is complete."
    )


BUILD_FORM_RECOMMENDATION = ToolDefinition(
    name="build_form_recommendation",
    description="Draft the final Lead Form Recommendation.",
    parameters=[
        ToolParameter(name="name", type="string", description="Internal name of the form (max 60 chars).", required=True),
        ToolParameter(name="context_card", type="object", description=CONTEXT_CARD_SCHEMA_PARAM["description"], properties=CONTEXT_CARD_SCHEMA_PARAM["properties"], required=False),
        ToolParameter(name="question_page_headline", type="string", description="Question page headline (max 60 chars).", required=False),
        ToolParameter(name="is_higher_intent", type="boolean", description="Whether to include a review screen.", required=False),
        ToolParameter(name="is_phone_sms_verify_enabled", type="boolean", description="Whether to require SMS verification for phone numbers (OTP).", required=False),
        ToolParameter(name="thank_you_headline", type="string", description="Headline for thank you page (max 60 chars).", required=False),
        ToolParameter(name="thank_you_description", type="string", description="Description for thank you page (max 350 chars).", required=False),
        ToolParameter(
            name="cta_button_type",
            type="string",
            enum=[b.value for b in ThankYouPageButtonType],
            description="Call to action button type on the completion screen.",
            required=False,
        ),
        ToolParameter(name="cta_button_text", type="string", description="Text for the call to action button (max 30 chars).", required=False),
        ToolParameter(name="business_phone_number", type="string", description="Phone number with country code (e.g. +1234567890), required if cta_button_type is CALL_BUSINESS.", required=False),
        ToolParameter(name="custom_disclaimer", type="string", description="Custom legal disclaimer text, if required.", required=False),
        ToolParameter(name="custom_disclaimer_title", type="string", description="Title for custom disclaimer (max 60 chars). Default: 'Disclaimer'.", required=False),
        ToolParameter(name="privacy_policy", type="object", description=PRIVACY_POLICY_SCHEMA_PARAM["description"], properties=PRIVACY_POLICY_SCHEMA_PARAM["properties"], required=False),
        ToolParameter(name="locale", type="string", description="Optional Meta form locale (e.g. EN_US, ES_LA, FR_FR).", required=False),
        ToolParameter(name="questions", type="array", description=QUESTION_SCHEMA_PARAM["description"], items=QUESTION_SCHEMA_PARAM["items"], required=True),
    ],
    execute=_build_form_recommendation
)

ALL_TOOLS = [ANALYZE_HISTORICAL_FORMS, BUILD_FORM_RECOMMENDATION]
