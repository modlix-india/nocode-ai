"""Internal conversational LLM tools for Lead Form Edit mode."""

import logging
from app.core.agent import ToolResult, ToolDefinition
from app.core.tools.base import ToolParameter
from app.agents.adzump.agents.leadform.models import (
    LeadFormRecommendation, LeadFormQuestion, ContextCard, PrivacyPolicy,
    QUESTION_SCHEMA_PARAM, CONTEXT_CARD_SCHEMA_PARAM, PRIVACY_POLICY_SCHEMA_PARAM
)
from app.agents.adzump.agents.leadform.utils import serialize_leadform_payload
from app.agents.adzump.adapters.meta.lead_forms import meta_lead_forms_adapter

logger = logging.getLogger(__name__)

async def _publish_to_meta(params: dict, context: dict) -> ToolResult:
    """Publishes the finalized lead form draft to Meta."""
    session = context.get("_session")
    if not session:
        return ToolResult(success=False, error="No active session.")
        
    page_id = params.get("page_id")
    if not page_id:
        return ToolResult(success=False, error="Missing page_id in parameters.")

    draft_dict = session.context.get("lead_form_draft")
    if not draft_dict:
        return ToolResult(success=False, error="No lead form draft found in session.")

    business_context = session.context.get("business_context", {})
    website_url = business_context.get("website_url", "")
    if not website_url:
        return ToolResult(
            success=False, 
            error="No website URL found. A valid URL is required for Meta's follow-up action and privacy policy."
        )

    auth = context.get("auth")
    if not auth:
        return ToolResult(success=False, error="Authentication context missing.")

    form_payload = serialize_leadform_payload(draft_dict, website_url)

    logger.info("Publishing Lead Form to Meta Page %s via Tool", page_id)

    try:
        result = await meta_lead_forms_adapter.create_leadgen_form(
            page_id=page_id,
            form_payload=form_payload,
            client_code=auth.client_code,
            auth_headers=auth.to_headers()
        )
        session.context["lead_form_published"] = True
        
        stream = context.get("event_stream")
        if stream:
            craft_id = session.context.get("craft_id", "leadform_craft")
            await stream.emit_craft(
                craft_id=craft_id,
                title="Lead Form Published",
                blocks=[{
                    "type": "lead_form",
                    "payload": form_payload,
                    "page_id": page_id,
                    "cover_image_url": draft_dict.get("context_card", {}).get("cover_image_url"),
                    "cover_photo_id": draft_dict.get("context_card", {}).get("cover_photo_id"),
                    "is_phone_sms_verify_enabled": bool(draft_dict.get("is_phone_sms_verify_enabled")),
                    "published": True,
                    "meta_form_id": result.get("id")
                }]
            )

        return ToolResult(
            success=True, 
            summary=f"Successfully published the lead form to Meta. Form ID: {result.get('id')}"
        )
    except Exception as e:
        logger.error("Failed to publish lead form: %s", e)
        return ToolResult(success=False, error=str(e))

async def _update_form_recommendation(params: dict, context: dict) -> ToolResult:
    """Updates the existing lead form draft."""
    session_ctx = context.get("session_context", {})
    draft_dict = session_ctx.get("lead_form_draft")
    if not draft_dict:
        return ToolResult(success=False, error="No draft exists to update.")

    try:
        draft = LeadFormRecommendation(**draft_dict)
    except Exception as e:
        return ToolResult(success=False, error=f"Corrupted draft state: {e}")

    if "name" in params:
        draft.name = params["name"]

    if "context_card" in params:
        draft.context_card = ContextCard(**params["context_card"])
        
    if "privacy_policy" in params:
        draft.privacy_policy = PrivacyPolicy(**params["privacy_policy"])

    if "custom_disclaimer" in params:
        draft.custom_disclaimer = params["custom_disclaimer"]
        
    if "question_page_headline" in params:
        draft.question_page_headline = params["question_page_headline"]

    if "is_higher_intent" in params:
        draft.is_higher_intent = params["is_higher_intent"]
        
    if "is_phone_sms_verify_enabled" in params:
        draft.is_phone_sms_verify_enabled = params["is_phone_sms_verify_enabled"]
        
    if "thank_you_headline" in params:
        draft.thank_you_headline = params["thank_you_headline"]

    if "thank_you_description" in params:
        draft.thank_you_description = params["thank_you_description"]

    if "cta_button_type" in params:
        draft.cta_button_type = params["cta_button_type"]
        
    if "cta_button_text" in params:
        draft.cta_button_text = params["cta_button_text"]

    if "business_phone_number" in params:
        draft.business_phone_number = params["business_phone_number"]

    if "questions" in params:
        try:
            parsed_q = []
            for q in params["questions"]:
                if isinstance(q, dict) and "type" in q and isinstance(q["type"], str):
                    q["type"] = q["type"].upper()
                parsed_q.append(LeadFormQuestion(**q))
        except Exception as e:
            return ToolResult(success=False, error=f"Invalid questions format: {e}")
        draft.questions = parsed_q
        
    # Handle user-attached image from chat input box for background/cover photo
    pending = session_ctx.get("_pending_uploads", [])
    upload_warning: str | None = None
    if pending:
        raw_b64 = pending[0].get("data", "")
        if raw_b64:
            import base64
            if "," in raw_b64:
                raw_b64 = raw_b64.split(",", 1)[1]

            spec = session_ctx.get("campaign_spec", {})
            page_id = spec.get("fb_page", "")

            if not page_id or not hasattr(context.get("_session"), "auth"):
                upload_warning = (
                    "The background image could not be uploaded because the "
                    "Facebook Page ID or authentication context is missing."
                )
            else:
                try:
                    file_bytes = base64.b64decode(raw_b64)
                    filename = pending[0].get("name", "cover.jpg")
                    content_type = pending[0].get("mime_type", "image/jpeg")
                    auth = context["_session"].auth
                    upload_res = await meta_lead_forms_adapter.upload_cover_photo(
                        page_id=page_id,
                        file_bytes=file_bytes,
                        filename=filename,
                        content_type=content_type,
                        client_code=auth.client_code,
                        auth_headers=auth.to_headers(),
                    )
                    draft.context_card.cover_photo_id = upload_res["photo_id"]
                    draft.context_card.cover_image_url = upload_res["source_url"]
                except Exception as e:
                    logger.warning("Failed to upload attached cover photo to Meta: %s", e)
                    upload_warning = (
                        f"The background image upload to Meta failed: {e}. "
                        "The form was saved but without the cover photo."
                    )
        # Clear pending uploads so it is consumed only once
        session_ctx["_pending_uploads"] = []

    try:
        draft_dump = draft.model_dump()
        LeadFormRecommendation.model_validate(draft_dump)
    except Exception as e:
        return ToolResult(success=False, error=f"Validation error: {e}")

    session_ctx["lead_form_draft"] = draft_dump

    from app.agents.adzump.agents.leadform.utils import serialize_leadform_payload
    b_ctx = session_ctx.get("business_context", {})
    website_url = b_ctx.get("website_url", "")
    meta_payload = serialize_leadform_payload(draft_dump, website_url)

    stream = context.get("event_stream")
    if stream:
        await stream.emit_data("leadform_payload_preview", meta_payload)
        
        craft_id = session_ctx.get("craft_id", "leadform_craft")
        spec = session_ctx.get("campaign_spec", {})
        page_id = spec.get("fb_page", "")
        if not page_id:
            logger.warning("Missing fb_page in campaign_spec. Cannot fetch page logo for craft preview.")
        page_logo_url = None
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

    summary = "Form successfully updated. The craft UI has been updated."
    if upload_warning:
        summary += f" Warning: {upload_warning}"

    return ToolResult(success=True, summary=summary)


UPDATE_FORM_RECOMMENDATION = ToolDefinition(
    name="update_form_recommendation",
    description="Update the existing lead form draft based on user feedback.",
    parameters=[
        ToolParameter(name="name", type="string", description="New internal name of the form (max 60 chars).", required=False),
        ToolParameter(**{"name": "context_card", "required": False, **CONTEXT_CARD_SCHEMA_PARAM}),
        ToolParameter(name="question_page_headline", type="string", description="New question page headline (max 60 chars).", required=False),
        ToolParameter(name="is_higher_intent", type="boolean", description="Update intent type.", required=False),
        ToolParameter(name="is_phone_sms_verify_enabled", type="boolean", description="Update OTP requirement.", required=False),
        ToolParameter(name="thank_you_headline", type="string", description="New headline for thank you page (max 60 chars).", required=False),
        ToolParameter(name="thank_you_description", type="string", description="New description for thank you page (max 350 chars).", required=False),
        ToolParameter(name="cta_button_type", type="string", description="Call to action button type: 'VIEW_WEBSITE', 'CALL_BUSINESS', 'DOWNLOAD', 'WHATSAPP', 'MESSAGE_BUSINESS', 'SCHEDULE_APPOINTMENT', 'BOOK_ON_WEBSITE', 'PROMO_CODE', 'NONE'.", required=False),
        ToolParameter(name="cta_button_text", type="string", description="New text for the call to action button (max 30 chars).", required=False),
        ToolParameter(name="business_phone_number", type="string", description="Business phone number with country code (e.g. +1234567890), required if cta_button_type is CALL_BUSINESS.", required=False),
        ToolParameter(name="custom_disclaimer", type="string", description="Custom legal disclaimer text, if required.", required=False),
        ToolParameter(**{"name": "privacy_policy", "required": False, **PRIVACY_POLICY_SCHEMA_PARAM}),
        ToolParameter(**{
            "name": "questions",
            "required": False,
            **{**QUESTION_SCHEMA_PARAM, "description": "REPLACEMENT list of all questions. Include previous questions if they shouldn't be deleted."}
        })
    ],
    execute=_update_form_recommendation
)

PUBLISH_TO_META = ToolDefinition(
    name="publish_to_meta",
    description="Publishes the final lead form draft directly to the user's Meta Page. Only call this when the user explicitly asks to publish the form.",
    parameters=[
        ToolParameter(name="page_id", type="string", description="The Meta Page ID to publish the form to.", required=True),
    ],
    execute=_publish_to_meta
)

ALL_TOOLS = [UPDATE_FORM_RECOMMENDATION, PUBLISH_TO_META]
