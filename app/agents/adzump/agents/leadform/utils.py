"""Helper utilities for the Lead Form Sub-Agent."""

from app.agents.adzump.agents.leadform.models import (
    BusinessContext,
    QuestionCategory,
    ContextCardStyle,
    ThankYouPageButtonType,
)


def extract_privacy_url(product_data: dict) -> str:
    """Extracts the privacy policy URL from scraped site links, falling back to the primary URL."""
    site_links = product_data.get("site_links", [])
    
    for link in site_links:
        text = ""
        href = ""
        if hasattr(link, "text") and hasattr(link, "href"):
            text = link.text or ""
            href = link.href or ""
        elif isinstance(link, dict):
            text = link.get("text", "")
            href = link.get("href", "")
            
        if "privacy" in text.lower():
            return href
            
    return product_data.get("primary_url", "")


def build_business_context(product_data: dict) -> BusinessContext:
    """Safely maps the product_data into the structured BusinessContext model."""
    privacy_url = extract_privacy_url(product_data)
    
    return BusinessContext(
        business_name=product_data.get("product_name", ""),
        industry=product_data.get("business_type", ""),
        business_summary=product_data.get("summary", ""),
        website_url=product_data.get("primary_url", ""),
        privacy_policy_url=privacy_url,
    )


def serialize_leadform_payload(draft: dict, fallback_website_url: str) -> dict:
    """Serializes the flat LeadFormRecommendation draft into Meta's strict POST /leadgen_forms payload."""
    questions_payload = []
    
    for q in draft.get("questions", []):
        q_type = q.get("type", "")
        if q_type in [cat.value for cat in QuestionCategory if cat not in (QuestionCategory.SHORT_ANSWER, QuestionCategory.MULTIPLE_CHOICE)]:
            questions_payload.append({"type": q_type, "key": q.get("key") or q_type.lower()})
        else:
            custom_q = {
                "type": "CUSTOM",
                "label": q.get("label", ""),
                "key": q.get("key", "")
            }
            if q.get("options"):
                custom_q["options"] = [
                    {
                        "value": str(opt),
                        "key": str(opt).strip().lower().replace(" ", "_")
                    }
                    for opt in q["options"]
                ]
            questions_payload.append(custom_q)
            
    context_card = draft.get("context_card", {})
    privacy_policy = draft.get("privacy_policy", {})
    custom_disclaimer = draft.get("custom_disclaimer", "")

    style = context_card.get("style", ContextCardStyle.PARAGRAPH_STYLE.value)
    if style not in (ContextCardStyle.PARAGRAPH_STYLE.value, ContextCardStyle.LIST_STYLE.value):
        style = ContextCardStyle.PARAGRAPH_STYLE.value

    cta_button_type = draft.get("cta_button_type") or ThankYouPageButtonType.VIEW_WEBSITE.value
    cta_button_text = draft.get("cta_button_text") or "Visit Website"
    
    thank_you_page = {
        "title": draft.get("thank_you_headline") or "Thanks, you're all set.",
        "body": draft.get("thank_you_description") or "We will contact you shortly.",
        "button_text": cta_button_text,
        "button_type": cta_button_type,
        "website_url": fallback_website_url,
    }

    if cta_button_type == ThankYouPageButtonType.CALL_BUSINESS.value:
        phone = draft.get("business_phone_number", "")
        if phone:
            thank_you_page["business_phone_number"] = phone

    form_name = draft.get("name") or "New Lead Form"

    context_card_payload = {
        "style": style,
        "title": context_card.get("title", ""),
        "content": context_card.get("content", [])
    }
    if context_card.get("cover_photo_id"):
        context_card_payload["cover_photo_id"] = context_card["cover_photo_id"]

    payload = {
        "name": form_name,
        "questions": questions_payload,
        "privacy_policy": {
            "url": privacy_policy.get("url") or fallback_website_url,
            "link_text": privacy_policy.get("link_text") or "Privacy Policy"
        },
        "follow_up_action": {
            "url": fallback_website_url
        },
        "context_card": context_card_payload,
        "question_page_custom_headline": draft.get("question_page_headline") or "Sign Up",
        "thank_you_page": thank_you_page,
    }
    
    if custom_disclaimer:
        payload["custom_disclaimer"] = {
            "title": "Disclaimer",
            "body": {
                "text": custom_disclaimer
            }
        }
    
    if draft.get("is_higher_intent"):
        payload["is_optimized_for_quality"] = True

    if draft.get("is_phone_sms_verify_enabled"):
        payload["is_phone_sms_verify_enabled"] = True
        
    return payload
