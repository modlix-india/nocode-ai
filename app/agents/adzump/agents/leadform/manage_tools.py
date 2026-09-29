"""Internal conversational LLM tools for Lead Form Edit mode."""

import base64
import logging
import re
from app.core.agent import ToolResult, ToolDefinition
from app.core.tools.base import ToolParameter
from app.agents.adzump.agents.leadform.models import (
    LeadFormRecommendation, LeadFormQuestion, ContextCard, PrivacyPolicy,
    QUESTION_SCHEMA_PARAM, CONTEXT_CARD_SCHEMA_PARAM, PRIVACY_POLICY_SCHEMA_PARAM,
    ThankYouPageButtonType,
)
from app.agents.adzump.agents.leadform.utils import serialize_leadform_payload
from app.agents.adzump.adapters.meta.lead_forms import meta_lead_forms_adapter

logger = logging.getLogger(__name__)

_PUBLISH_CONFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|publish\s+it|confirm(?:ed)?|approve(?:d)?|proceed|procced|"
    r"go ahead|do it|sure|ok(?:ay)?)\b",
    re.IGNORECASE,
)
_PUBLISH_DECLINE_RE = re.compile(
    r"\b(no\b(?!.*(?:publish|form))|don'?t\s+(?:publish|post)|cancel|wait|stop|hold|later|never|"
    r"not\s+(?:now|yet|ready|publish|sure))\b",
    re.IGNORECASE,
)


def _user_confirmed_publish(text: str) -> bool:
    """True only when the user's latest message contains an explicit publish go-ahead."""
    lu = (text or "").strip()
    if not lu or bool(_PUBLISH_DECLINE_RE.search(lu)):
        return False
    return bool(_PUBLISH_CONFIRM_RE.search(lu))

async def _publish_to_meta(params: dict, context: dict) -> ToolResult:
    """Publishes the finalized lead form draft to Meta."""
    session = context.get("_session")
    if not session:
        return ToolResult(success=False, error="No active session.")

    if session.context.get("lead_form_published"):
        existing_id = session.context.get("meta_lead_form_id") or ""
        id_msg = f" (Form ID: {existing_id})" if existing_id else ""
        return ToolResult(
            success=False,
            error=f"This lead form has already been published to Meta{id_msg}. It cannot be published again.",
        )

    session_ctx = session.context if hasattr(session, "context") and isinstance(session.context, dict) else {}
    last_user = session_ctx.get("lf_user_message", "")
    if not _user_confirmed_publish(last_user):
        session_ctx["awaiting_publish_confirmation"] = True
        logger.warning("publish_blocked_no_consent: last_user=%r", last_user[:120])
        return ToolResult(
            success=False,
            error=(
                "Cannot publish yet — the user has not explicitly confirmed. "
                "Reply with the form name and this one-line warning: "
                "'⚠️ Once published to Meta this form cannot be deleted.' "
                "Then ask: 'Do you want to publish it or discard the draft?' "
                "Offer two explicit paths: "
                "(1) publish — if they say 'Yes, publish it'/confirm/go ahead, call publish_to_meta; "
                "(2) discard — if they say discard/remove/don't want it, call discard_lead_form_draft. "
                "Only call publish_to_meta after an explicit affirmative confirmation."
            ),
        )

    spec = session.context.get("campaign_spec", {})
    raw_page_id = spec.get("fb_page") if isinstance(spec, dict) else None
    page_id = str(raw_page_id).strip() if raw_page_id is not None else ""
    if not page_id or not page_id.isdigit():
        return ToolResult(
            success=False,
            error=f"Invalid or missing Facebook Page ID ({raw_page_id!r}) in campaign spec. Page ID must be numeric.",
        )

    draft_dict = session.context.get("lead_form_draft")
    if not draft_dict:
        return ToolResult(success=False, error="No lead form draft found in session.")

    business_context = session.context.get("business_context", {})
    website_url = business_context.get("website_url", "") if isinstance(business_context, dict) else ""
    if not website_url:
        return ToolResult(
            success=False,
            error="No website URL found. A valid URL is required for Meta's follow-up action and privacy policy."
        )

    privacy_policy_url = (draft_dict.get("privacy_policy") or {}).get("url", "").strip()
    if not privacy_policy_url:
        return ToolResult(
            success=False,
            error=(
                "Meta requires a valid Privacy Policy URL to publish a Lead Form. "
                "No privacy policy URL was found for this business. "
                "Please provide a privacy policy URL before publishing."
            ),
        )

    auth = context.get("auth")
    if not auth:
        return ToolResult(success=False, error="Authentication context missing.")

    session_ctx = session.context if hasattr(session, "context") and isinstance(session.context, dict) else {}
    campaign_spec = session_ctx.get("campaign_spec", {}) if isinstance(session_ctx, dict) else {}
    locale = (
        draft_dict.get("locale")
        or campaign_spec.get("locale")
        or campaign_spec.get("country_code")
        or campaign_spec.get("country")
    )
    form_payload = serialize_leadform_payload(draft_dict, website_url, locale=locale)

    logger.info("Publishing Lead Form to Meta Page %s via Tool", page_id)

    page_cache = session.context.get("_meta_page_cache", {})
    cached_page = page_cache.get(page_id, {}) if isinstance(page_cache, dict) else {}
    cached_token = cached_page.get("access_token")

    try:
        result = await meta_lead_forms_adapter.create_leadgen_form(
            page_id=page_id,
            form_payload=form_payload,
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            page_token=cached_token,
        )
        form_id = result.get("id") if isinstance(result, dict) else None
        if not form_id:
            logger.error("Meta create_leadgen_form succeeded but returned no form ID: %r", result)
            return ToolResult(
                success=False,
                error=f"Meta did not return a valid form ID upon publishing. Response: {result}",
            )

        session.context["lead_form_published"] = True
        session.context["meta_lead_form_id"] = str(form_id)
        # Clear draft so publishing concludes this form's lifecycle and allows creating subsequent forms
        session.context["lead_form_draft"] = None
        session.context["awaiting_publish_confirmation"] = False

        stream = context.get("event_stream")
        if stream:
            craft_id = session.context.get("craft_id", "leadform_craft")
            await stream.emit_craft(
                craft_id=craft_id,
                title="Lead Form Published",
                blocks=[
                    {"type": "badge", "label": "Published on Meta"},
                    {
                        "type": "lead_form",
                        "payload": form_payload,
                        "page_id": page_id,
                        "cover_image_url": draft_dict.get("context_card", {}).get("cover_image_url") if isinstance(draft_dict.get("context_card"), dict) else "",
                        "cover_photo_id": draft_dict.get("context_card", {}).get("cover_photo_id") if isinstance(draft_dict.get("context_card"), dict) else "",
                        "is_phone_sms_verify_enabled": bool(draft_dict.get("is_phone_sms_verify_enabled")),
                        "published": True,
                        "meta_form_id": str(form_id),
                    },
                ]
            )

        return ToolResult(
            success=True,
            summary=f"Successfully published the lead form to Meta. Form ID: {form_id}"
        )
    except Exception as e:
        logger.error("Failed to publish lead form: %s", e)
        return ToolResult(success=False, error=str(e))

_SCALAR_EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "locale",
    "custom_disclaimer",
    "custom_disclaimer_title",
    "question_page_headline",
    "is_higher_intent",
    "is_phone_sms_verify_enabled",
    "thank_you_headline",
    "thank_you_description",
    "cta_button_type",
    "cta_button_text",
    "business_phone_number",
)

_NESTED_EDITABLE_FIELDS: tuple[str, ...] = (
    "context_card",
    "privacy_policy",
    "questions",
)

_EDITABLE_FIELDS: frozenset[str] = frozenset(_SCALAR_EDITABLE_FIELDS + _NESTED_EDITABLE_FIELDS)


async def _update_form_recommendation(params: dict, context: dict) -> ToolResult:
    """Updates the existing lead form draft."""
    session_ctx = context.get("session_context", {})
    draft_dict = session_ctx.get("lead_form_draft")
    if not draft_dict:
        return ToolResult(success=False, error="No draft exists to update.")

    try:
        draft = LeadFormRecommendation(**draft_dict)
    except (KeyError, ValueError, TypeError) as e:
        return ToolResult(success=False, error=f"Corrupted draft state: {e}")

    applied_fields: list[str] = []

    for field in _SCALAR_EDITABLE_FIELDS:
        if field in params:
            setattr(draft, field, params[field])
            applied_fields.append(field)

    if "context_card" in params:
        raw_cc = params["context_card"]
        if not isinstance(raw_cc, dict):
            return ToolResult(success=False, error="Invalid context_card: expected a dictionary/object.")
        incoming_cc = dict(raw_cc)
        # Preserve existing cover image fields unless explicitly overridden or cleared.
        if "cover_photo_id" not in incoming_cc and draft.context_card.cover_photo_id:
            incoming_cc["cover_photo_id"] = draft.context_card.cover_photo_id
        if "cover_image_url" not in incoming_cc and draft.context_card.cover_image_url:
            incoming_cc["cover_image_url"] = draft.context_card.cover_image_url
        try:
            draft.context_card = ContextCard(**incoming_cc)
            applied_fields.append("context_card")
        except (KeyError, ValueError, TypeError) as e:
            return ToolResult(success=False, error=f"Invalid context_card format: {e}")

    if "privacy_policy" in params:
        raw_pp = params["privacy_policy"]
        if not isinstance(raw_pp, dict):
            return ToolResult(success=False, error="Invalid privacy_policy: expected a dictionary/object.")
        try:
            draft.privacy_policy = PrivacyPolicy(**raw_pp)
            applied_fields.append("privacy_policy")
        except (KeyError, ValueError, TypeError) as e:
            return ToolResult(success=False, error=f"Invalid privacy_policy format: {e}")

    if "questions" in params:
        try:
            draft.questions = [LeadFormQuestion(**q) for q in params["questions"]]
            applied_fields.append("questions")
        except (KeyError, ValueError, TypeError) as e:
            return ToolResult(success=False, error=f"Invalid questions format: {e}")

    # Handle user-attached image from chat input box for background/cover photo
    cover_photo_attached = False
    pending = session_ctx.get("_pending_uploads", [])
    has_pending_upload = bool(pending and pending[0].get("data"))

    if not applied_fields and not has_pending_upload:
        valid_fields = ", ".join(sorted(_EDITABLE_FIELDS))
        return ToolResult(
            success=False,
            error=f"No recognized editable field was provided. Editable fields are: {valid_fields}.",
        )

    upload_warning: str | None = None
    if pending:
        raw_b64 = pending[0].get("data", "")
        if raw_b64:
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

                    page_cache = session_ctx.setdefault("_meta_page_cache", {})
                    if page_id not in page_cache:
                        try:
                            page_cache[page_id] = await meta_lead_forms_adapter.get_page_info(
                                page_id, auth.client_code, auth.to_headers()
                            )
                        except Exception as pe:
                            logger.warning("Failed pre-resolving page info for page_id=%s: %s", page_id, pe)
                    cached_token = page_cache.get(page_id, {}).get("access_token")

                    upload_res = await meta_lead_forms_adapter.upload_cover_photo(
                        page_id=page_id,
                        file_bytes=file_bytes,
                        filename=filename,
                        content_type=content_type,
                        client_code=auth.client_code,
                        auth_headers=auth.to_headers(),
                        page_token=cached_token,
                    )
                    draft.context_card.cover_photo_id = upload_res["photo_id"]
                    draft.context_card.cover_image_url = upload_res["source_url"]
                    cover_photo_attached = True
                except Exception as e:
                    logger.warning("Failed to upload attached cover photo to Meta: %s", e)
                    upload_warning = (
                        f"The background image upload to Meta failed: {e}. "
                        "The form was saved but without the cover photo."
                    )

    # If only an upload was attempted and it failed, reject as an unsuccessful update
    if not applied_fields and not cover_photo_attached and upload_warning:
        return ToolResult(success=False, error=upload_warning)

    try:
        draft_dump = draft.model_dump()
        LeadFormRecommendation.model_validate(draft_dump)
    except (KeyError, ValueError, TypeError) as e:
        return ToolResult(success=False, error=f"Validation error: {e}")

    session_ctx["lead_form_draft"] = draft_dump
    session_ctx["awaiting_publish_confirmation"] = False

    # Consume the attached image only after the draft is validated and saved.
    if cover_photo_attached and pending:
        session_ctx["_pending_uploads"] = pending[1:]
    b_ctx = session_ctx.get("business_context", {})
    website_url = b_ctx.get("website_url", "")
    campaign_spec = session_ctx.get("campaign_spec", {}) if isinstance(session_ctx, dict) else {}
    locale = (
        draft_dump.get("locale")
        or campaign_spec.get("locale")
        or campaign_spec.get("country_code")
        or campaign_spec.get("country")
    )
    meta_payload = serialize_leadform_payload(draft_dump, website_url, locale=locale)

    stream = context.get("event_stream")
    if stream:
        await stream.emit_data("leadform_payload_preview", meta_payload)
        
        craft_id = session_ctx.get("craft_id", "leadform_craft")
        spec = session_ctx.get("campaign_spec", {})
        page_id = spec.get("fb_page", "")
        if not page_id:
            logger.warning("Missing fb_page in campaign_spec. Cannot fetch page logo for craft preview.")
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

    if cover_photo_attached:
        summary = (
            "Form successfully updated. The uploaded background image has been "
            "attached to the draft by the system (cover_photo_id set automatically). "
            "The craft UI now shows the new cover photo. Do not call this tool again "
            "for this image — the task is complete."
        )
        if applied_fields:
            summary += f" Updated fields: {', '.join(applied_fields)}."
    else:
        if applied_fields:
            summary = f"Form successfully updated. Updated fields: {', '.join(applied_fields)}. The craft UI has been updated."
        else:
            summary = "Form successfully updated. The craft UI has been updated."
    if upload_warning:
        summary += f" Warning: {upload_warning}"

    return ToolResult(success=True, summary=summary)


UPDATE_FORM_RECOMMENDATION = ToolDefinition(
    name="update_form_recommendation",
    description="Update the existing lead form draft based on user feedback.",
    parameters=[
        ToolParameter(name="name", type="string", description="New internal name of the form (max 60 chars).", required=False),
        ToolParameter(name="context_card", type="object", description=CONTEXT_CARD_SCHEMA_PARAM["description"], properties=CONTEXT_CARD_SCHEMA_PARAM["properties"], required=False),
        ToolParameter(name="question_page_headline", type="string", description="New question page headline (max 60 chars).", required=False),
        ToolParameter(
            name="is_higher_intent",
            type="boolean",
            description=(
                "Form type / intent: "
                "false = 'More Volume' (removes the review step / review screen for faster submission). "
                "true = 'Higher Intent' (adds the review step / review screen where leads verify answers before submitting)."
            ),
            required=False,
        ),
        ToolParameter(name="is_phone_sms_verify_enabled", type="boolean", description="Update OTP requirement.", required=False),
        ToolParameter(name="thank_you_headline", type="string", description="New headline for thank you page (max 60 chars).", required=False),
        ToolParameter(name="thank_you_description", type="string", description="New description for thank you page (max 350 chars).", required=False),
        ToolParameter(
            name="cta_button_type",
            type="string",
            enum=[b.value for b in ThankYouPageButtonType],
            description="Call to action button type on the completion screen.",
            required=False,
        ),
        ToolParameter(name="cta_button_text", type="string", description="New text for the call to action button (max 30 chars).", required=False),
        ToolParameter(name="business_phone_number", type="string", description="Business phone number with country code (e.g. +1234567890), required if cta_button_type is CALL_BUSINESS.", required=False),
        ToolParameter(name="custom_disclaimer", type="string", description="Custom legal disclaimer text, if required.", required=False),
        ToolParameter(name="custom_disclaimer_title", type="string", description="New title/header for custom disclaimer (max 60 chars).", required=False),
        ToolParameter(name="privacy_policy", type="object", description=PRIVACY_POLICY_SCHEMA_PARAM["description"], properties=PRIVACY_POLICY_SCHEMA_PARAM["properties"], required=False),
        ToolParameter(name="locale", type="string", description="Optional Meta form locale (e.g. EN_US, ES_LA, FR_FR).", required=False),
        ToolParameter(
            name="questions",
            type="array",
            description="REPLACEMENT list of all questions. Include previous questions if they shouldn't be deleted.",
            items=QUESTION_SCHEMA_PARAM["items"],
            required=False,
        ),
    ],
    execute=_update_form_recommendation
)

PUBLISH_TO_META = ToolDefinition(
    name="publish_to_meta",
    description=(
        "Publishes the finalized lead form draft permanently to the user's Meta Page. "
        "The form CANNOT be deleted after publishing — it is a permanent action. "
        "Only call this after the user has explicitly confirmed they want to publish "
        "(e.g., said 'yes', 'go ahead', 'confirm'). "
        "The Page ID is resolved from the campaign spec automatically."
    ),
    parameters=[],
    execute=_publish_to_meta
)


async def _discard_lead_form_draft(params: dict, context: dict) -> ToolResult:
    """Discards the current lead form draft so the campaign can proceed without a form."""
    session_ctx = context.get("session_context", {})
    if not session_ctx.get("lead_form_draft"):
        return ToolResult(success=False, error="No draft exists to discard.")

    # Set lead_form_draft to None explicitly so the sync loop in
    # run_leadform_session propagates the None back to parent_ctx,
    # releasing the lead_form_active guard in the orchestrator.
    session_ctx["lead_form_draft"] = None
    session_ctx["lead_form_published"] = False
    session_ctx["awaiting_publish_confirmation"] = False

    stream = context.get("event_stream")
    if stream:
        craft_id = session_ctx.get("craft_id", "leadform_craft")
        await stream.emit_craft(
            craft_id=craft_id,
            title="Lead Form Removed",
            blocks=[{"type": "badge", "label": "No lead form attached to this campaign"}],
        )

    return ToolResult(
        success=True,
        summary=(
            "The lead form draft has been discarded. The campaign will proceed without "
            "a lead form. Confirm this to the user in one short sentence."
        ),
    )


DISCARD_LEAD_FORM_DRAFT = ToolDefinition(
    name="discard_lead_form_draft",
    description=(
        "Discards the current lead form draft entirely. "
        "Call this ONLY when the user explicitly says they do not want a lead form "
        "(e.g. 'remove the form', 'skip the lead form', 'launch without a form', "
        "'I don't want the form', 'discard it'). "
        "This clears the draft so the campaign can proceed to launch. "
        "A new form can be created at any time by the user."
    ),
    parameters=[],
    execute=_discard_lead_form_draft,
)

ALL_TOOLS = [UPDATE_FORM_RECOMMENDATION, PUBLISH_TO_META, DISCARD_LEAD_FORM_DRAFT]
