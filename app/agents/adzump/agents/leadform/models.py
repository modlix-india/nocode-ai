"""Pydantic domain data models for the Lead Form Sub-Agent."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BusinessContext(BaseModel):
    """The normalized business identity passed to the recommendation engine."""
    business_name: str = ""
    industry: str = ""
    business_summary: str = ""
    website_url: str = ""
    privacy_policy_url: str = ""
    campaign_objective: str = "Lead Generation"


class QuestionCategory(str, Enum):
    """Supported Meta Graph API question types.
    Pre-fill fields (e.g. EMAIL, FULL_NAME) are handled automatically by Meta.
    Custom questions are free-text or multiple-choice.
    """
    FIRST_NAME = "FIRST_NAME"
    LAST_NAME = "LAST_NAME"
    FULL_NAME = "FULL_NAME"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    STREET_ADDRESS = "STREET_ADDRESS"
    CITY = "CITY"
    STATE = "STATE"
    ZIP = "ZIP"
    COUNTRY = "COUNTRY"
    COMPANY_NAME = "COMPANY_NAME"
    JOB_TITLE = "JOB_TITLE"
    WORK_EMAIL = "WORK_EMAIL"
    WORK_PHONE_NUMBER = "WORK_PHONE_NUMBER"
    DOB = "DOB"
    GENDER = "GENDER"
    MARITAL_STATUS = "MARITAL_STATUS"
    RELATIONSHIP_STATUS = "RELATIONSHIP_STATUS"
    MILITARY_STATUS = "MILITARY_STATUS"
    ID_CPF = "ID_CPF"
    NATIONAL_ID_NUMBER = "NATIONAL_ID_NUMBER"
    SHORT_ANSWER = "SHORT_ANSWER"
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE"


# Meta Instant Forms Character and Constraint Constants
MAX_FORM_NAME_LENGTH = 60
MAX_CONTEXT_CARD_TITLE_LENGTH = 60
MAX_CONTEXT_CARD_BULLET_LENGTH = 80
MAX_CONTEXT_CARD_BULLETS_COUNT = 5
MAX_QUESTION_PAGE_HEADLINE_LENGTH = 60
MAX_THANK_YOU_HEADLINE_LENGTH = 60
MAX_THANK_YOU_DESCRIPTION_LENGTH = 350
MAX_CTA_BUTTON_TEXT_LENGTH = 30
MAX_PRIVACY_LINK_TEXT_LENGTH = 70
MAX_CUSTOM_QUESTIONS_COUNT = 15
MAX_QUESTION_KEY_LENGTH = 30


class ThankYouPageButtonType(str, Enum):
    VIEW_WEBSITE = "VIEW_WEBSITE"
    CALL_BUSINESS = "CALL_BUSINESS"
    DOWNLOAD = "DOWNLOAD"
    MESSAGE_BUSINESS = "MESSAGE_BUSINESS"
    WHATSAPP = "WHATSAPP"
    SCHEDULE_APPOINTMENT = "SCHEDULE_APPOINTMENT"
    BOOK_ON_WEBSITE = "BOOK_ON_WEBSITE"
    PROMO_CODE = "PROMO_CODE"
    NONE = "NONE"


class ContextCardStyle(str, Enum):
    PARAGRAPH_STYLE = "PARAGRAPH_STYLE"
    LIST_STYLE = "LIST_STYLE"


class ContextCard(BaseModel):
    style: ContextCardStyle = ContextCardStyle.PARAGRAPH_STYLE
    title: str = Field(default="", max_length=MAX_CONTEXT_CARD_TITLE_LENGTH)
    content: list[str] = Field(default_factory=list)
    cover_photo_id: str = ""
    cover_image_url: str = ""


class PrivacyPolicy(BaseModel):
    url: str = ""
    link_text: str = Field(default="Privacy Policy", max_length=MAX_PRIVACY_LINK_TEXT_LENGTH)



class LeadFormQuestion(BaseModel):
    """A single question on the lead form."""
    type: QuestionCategory
    key: str = ""       # Required for custom questions
    label: str = ""     # The actual question text shown to the user
    options: list[str] = Field(default_factory=list)  # For MULTIPLE_CHOICE

    @model_validator(mode="after")
    def auto_key(self) -> LeadFormQuestion:
        if self.type in (QuestionCategory.SHORT_ANSWER, QuestionCategory.MULTIPLE_CHOICE):
            if not self.key and self.label:
                import re
                import uuid
                clean = re.sub(r'[^a-zA-Z0-9_\s]', '', self.label)
                derived_key = re.sub(r'\s+', '_', clean.strip()).lower()[:MAX_QUESTION_KEY_LENGTH]
                self.key = derived_key if derived_key else f"q_{uuid.uuid4().hex[:6]}"
        return self


class LeadFormProfile(BaseModel):
    """A parsed historical form fetched from the Meta Graph API."""
    id: str
    name: str
    status: str
    leads_count: int
    is_higher_intent: bool
    questions: list[LeadFormQuestion] = Field(default_factory=list)


class LeadFormRecommendation(BaseModel):
    """The final draft form state, supporting conversational mutation."""
    model_config = ConfigDict(extra="allow")

    name: str = Field(default="New Lead Form", max_length=MAX_FORM_NAME_LENGTH)
    context_card: ContextCard = Field(default_factory=ContextCard)
    question_page_headline: str = Field(default="", max_length=MAX_QUESTION_PAGE_HEADLINE_LENGTH)
    is_higher_intent: bool = False
    is_phone_sms_verify_enabled: bool = False
    questions: list[LeadFormQuestion] = Field(default_factory=list)
    privacy_policy: PrivacyPolicy = Field(default_factory=PrivacyPolicy)
    custom_disclaimer: str = ""
    thank_you_headline: str = Field(default="Thanks, you're all set.", max_length=MAX_THANK_YOU_HEADLINE_LENGTH)
    thank_you_description: str = Field(default="We will contact you shortly.", max_length=MAX_THANK_YOU_DESCRIPTION_LENGTH)
    cta_button_type: ThankYouPageButtonType = ThankYouPageButtonType.VIEW_WEBSITE
    cta_button_text: str = Field(default="Visit Website", max_length=MAX_CTA_BUTTON_TEXT_LENGTH)
    business_phone_number: str = ""

    @model_validator(mode="after")
    def validate_lead_form(self) -> 'LeadFormRecommendation':
        if len(self.context_card.title) > MAX_CONTEXT_CARD_TITLE_LENGTH:
            raise ValueError(f"context_card title exceeds {MAX_CONTEXT_CARD_TITLE_LENGTH} characters.")
        if self.context_card.style == ContextCardStyle.LIST_STYLE and len(self.context_card.content) > MAX_CONTEXT_CARD_BULLETS_COUNT:
            raise ValueError(f"context_card content list exceeds {MAX_CONTEXT_CARD_BULLETS_COUNT} items.")
        if self.context_card.style == ContextCardStyle.PARAGRAPH_STYLE and len(self.context_card.content) > 1:
            raise ValueError("context_card with PARAGRAPH_STYLE can only have 1 content item.")
        for text in self.context_card.content:
            if len(text) > MAX_CONTEXT_CARD_BULLET_LENGTH:
                raise ValueError(f"context_card content item exceeds {MAX_CONTEXT_CARD_BULLET_LENGTH} characters.")
                
        if len(self.question_page_headline) > MAX_QUESTION_PAGE_HEADLINE_LENGTH:
            raise ValueError(f"question_page_headline exceeds {MAX_QUESTION_PAGE_HEADLINE_LENGTH} characters.")
        
        custom_questions = [q for q in self.questions if q.type in (QuestionCategory.SHORT_ANSWER, QuestionCategory.MULTIPLE_CHOICE)]
        if len(custom_questions) > MAX_CUSTOM_QUESTIONS_COUNT:
            raise ValueError(f"Meta allows a maximum of {MAX_CUSTOM_QUESTIONS_COUNT} custom questions.")
        
        seen_keys = set()
        for q in custom_questions:
            if not q.key:
                raise ValueError("Custom questions must have a key.")
            if q.key in seen_keys:
                raise ValueError(f"Duplicate question key found: {q.key}")
            seen_keys.add(q.key)
            
            if q.type == QuestionCategory.MULTIPLE_CHOICE and len(q.options) < 2:
                raise ValueError(f"MULTIPLE_CHOICE question '{q.key}' must have at least 2 options.")

        if (
            self.cta_button_type == ThankYouPageButtonType.CALL_BUSINESS
            and not self.business_phone_number.strip()
        ):
            raise ValueError(
                "business_phone_number is required when cta_button_type is CALL_BUSINESS."
            )

        return self


# Reusable Anthropic-compatible JSON schema parameter for questions
QUESTION_SCHEMA_PARAM = {
    "type": "array",
    "description": "List of questions.",
    "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string"},
            "key": {"type": "string"},
            "label": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}}
        }
    }
}

CONTEXT_CARD_SCHEMA_PARAM = {
    "type": "object",
    "description": "The context card configuration.",
    "properties": {
        "style": {"type": "string", "enum": ["PARAGRAPH_STYLE", "LIST_STYLE"]},
        "title": {"type": "string"},
        "content": {"type": "array", "items": {"type": "string"}},
        "cover_photo_id": {"type": "string", "description": "Meta photo ID for custom background image (empty for default ad creative)."},
        "cover_image_url": {"type": "string", "description": "Meta CDN image URL for preview."}
    }
}

PRIVACY_POLICY_SCHEMA_PARAM = {
    "type": "object",
    "description": "The privacy policy configuration.",
    "properties": {
        "url": {"type": "string"},
        "link_text": {"type": "string"}
    }
}
