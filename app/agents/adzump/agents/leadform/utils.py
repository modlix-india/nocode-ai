import re

from app.agents.adzump.agents.leadform.models import (
    COUNTRY_TO_META_LOCALE,
    LANGUAGE_TO_META_LOCALE,
    BusinessContext,
    ContextCardStyle,
    QuestionCategory,
    ThankYouPageButtonType,
)


def sanitize_option_key(text: str) -> str:
    """Sanitizes multiple-choice option text into a clean alphanumeric identifier for Meta Graph API."""
    clean = re.sub(r'[^a-zA-Z0-9_\s-]', '', str(text))
    return re.sub(r'[\s-]+', '_', clean.strip()).lower()


def format_meta_locale(raw_locale: str | None) -> str:
    """Formats a country code or locale string into Meta Graph API locale format (e.g. EN_US, ES_LA, FR_FR)."""
    if not raw_locale or not isinstance(raw_locale, str):
        return ""
    clean = raw_locale.strip()
    if not clean:
        return ""

    upper_clean = clean.upper()
    if upper_clean in COUNTRY_TO_META_LOCALE:
        return COUNTRY_TO_META_LOCALE[upper_clean]

    normalized = clean.replace("-", "_")
    parts = normalized.split("_")
    if len(parts) == 2 and len(parts[0]) == 2 and len(parts[1]) == 2:
        return f"{parts[0].upper()}_{parts[1].upper()}"

    if len(clean) == 2:
        lang = clean.lower()
        if lang in LANGUAGE_TO_META_LOCALE:
            return LANGUAGE_TO_META_LOCALE[lang]

    if "_" in upper_clean and len(upper_clean) <= 6:
        return upper_clean

    return ""


def extract_privacy_url(product_data: dict) -> str:
    """Extracts the privacy policy URL from scraped site links, returning an empty string if not found."""
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
            
        if href and "privacy" in text.lower():
            return href.strip()
            
    return ""


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


def format_meta_business_phone(phone_raw: str, default_country_code: str = "91") -> tuple[str, str]:
    """Parse business phone into (country_code, national_number) for Meta thank_you_page.

    Currently scoped strictly for India ('91').
    Handles +91, leading 0, 12-digit 91XXXXXXXXXX, or raw 10-digit formats.
    """
    if not phone_raw:
        return "", ""

    digits = re.sub(r"\D", "", phone_raw.strip())
    if not digits:
        return "", ""

    # Strip country code '91' if prefixed (e.g. +91XXXXXXXXXX or 91XXXXXXXXXX)
    if digits.startswith("91") and len(digits) > 10:
        return "91", digits[2:].lstrip("0")

    # Handle leading zero trunk prefix (e.g. 09876543210) or raw 10 digits
    return default_country_code, digits.lstrip("0")


_PREFILL_QUESTION_TYPES: frozenset[str] = frozenset(
    cat.value for cat in QuestionCategory
    if cat not in (QuestionCategory.SHORT_ANSWER, QuestionCategory.MULTIPLE_CHOICE)
)


def serialize_leadform_payload(draft: dict, fallback_website_url: str, locale: str | None = None) -> dict:
    """Serializes the flat LeadFormRecommendation draft into Meta's strict POST /leadgen_forms payload."""
    questions_payload = []
    
    for q in draft.get("questions", []):
        q_type = q.get("type", "")
        if q_type in _PREFILL_QUESTION_TYPES:
            questions_payload.append({"type": q_type, "key": q.get("key") or q_type.lower()})
        else:
            custom_q = {
                "type": "CUSTOM",
                "label": q.get("label", ""),
                "key": q.get("key", "")
            }
            if q.get("options"):
                serialized_options = []
                seen_keys: set[str] = set()
                for opt in q["options"]:
                    val_str = str(opt)
                    raw_key = sanitize_option_key(val_str) or "option"
                    unique_key = raw_key
                    suffix = 2
                    while unique_key in seen_keys:
                        unique_key = f"{raw_key}_{suffix}"
                        suffix += 1
                    seen_keys.add(unique_key)
                    serialized_options.append({"value": val_str, "key": unique_key})
                custom_q["options"] = serialized_options
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
        phone = (draft.get("business_phone_number") or "").strip()
        if phone:
            cc, num = format_meta_business_phone(phone, default_country_code="91")
            if num:
                thank_you_page["country_code"] = cc
                thank_you_page["business_phone_number"] = num
            else:
                thank_you_page["business_phone_number"] = phone

    # Meta Graph API requires enable_messenger=true when the CTA opens a Messenger
    # or WhatsApp conversation.  Without this field the publish call returns 400.
    if cta_button_type in (
        ThankYouPageButtonType.WHATSAPP.value,
        ThankYouPageButtonType.MESSAGE_BUSINESS.value,
    ):
        thank_you_page["enable_messenger"] = True

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
            "url": (privacy_policy.get("url") or "").strip(),
            "link_text": privacy_policy.get("link_text") or "Privacy Policy"
        },
        "follow_up_action_url": fallback_website_url,
        "context_card": context_card_payload,
        "thank_you_page": thank_you_page,
    }

    question_page_headline = (draft.get("question_page_headline") or "").strip()
    if question_page_headline:
        payload["question_page_custom_headline"] = question_page_headline
    
    if custom_disclaimer:
        disclaimer_title = (draft.get("custom_disclaimer_title") or "").strip() or "Disclaimer"
        payload["custom_disclaimer"] = {
            "title": disclaimer_title,
            "body": {
                "text": custom_disclaimer
            }
        }
    
    if draft.get("is_higher_intent"):
        payload["is_optimized_for_quality"] = True

    if draft.get("is_phone_sms_verify_enabled"):
        payload["is_phone_sms_verify_enabled"] = True

    resolved_locale = format_meta_locale(locale or draft.get("locale"))
    if resolved_locale:
        payload["locale"] = resolved_locale

    return payload
