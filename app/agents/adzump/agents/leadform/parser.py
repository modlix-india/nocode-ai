"""Graph API Response Parser Engine for the Lead Form Sub-Agent."""

import logging
from typing import Any

from app.agents.adzump.agents.leadform.models import (
    LeadFormProfile,
    LeadFormQuestion,
    QuestionCategory,
)

logger = logging.getLogger(__name__)


_SUPPORTED_QUESTION_TYPES: frozenset[str] = frozenset(cat.value for cat in QuestionCategory)


def _parse_question(raw_q: dict[str, Any]) -> LeadFormQuestion | None:
    """Parses a single raw question from Meta into a LeadFormQuestion.
    
    Meta's 'type' field is either a standard pre-fill field (like 'EMAIL') or 'CUSTOM'.
    For CUSTOM, the actual question text is usually in 'label' and options in 'options'.
    """
    if not isinstance(raw_q, dict) or not raw_q:
        return None

    raw_type_val = raw_q.get("type")
    if not isinstance(raw_type_val, str):
        raw_type = str(raw_type_val).strip().upper() if raw_type_val is not None else ""
    else:
        raw_type = raw_type_val.strip().upper()

    raw_label = str(raw_q.get("label") or "") if raw_q.get("label") is not None else ""
    raw_key = str(raw_q.get("key") or "") if raw_q.get("key") is not None else ""

    if (
        raw_q.get("type") is None
        and raw_q.get("label") is None
        and raw_q.get("key") is None
        and raw_q.get("options") is None
    ):
        return None

    # Check if it's a standard pre-fill question supported by our Enum
    if raw_type in _SUPPORTED_QUESTION_TYPES:
        return LeadFormQuestion(
            type=QuestionCategory(raw_type),
            key=raw_key or raw_type.lower(),
            label=raw_label or raw_type.replace("_", " ").title(),
        )
        
    # Handle custom questions
    if raw_type == "CUSTOM":
        options = []
        raw_options = raw_q.get("options", [])
        if isinstance(raw_options, list):
            # Meta sometimes returns options as a list of dicts with 'value' keys, or just strings
            options = [
                str(opt.get("value", opt)) if isinstance(opt, dict) else str(opt)
                for opt in raw_options
                if opt is not None
            ]
            
        category = QuestionCategory.MULTIPLE_CHOICE if options else QuestionCategory.SHORT_ANSWER
        
        return LeadFormQuestion(
            type=category,
            key=raw_key or "custom_question",
            label=raw_label,
            options=options,
        )
        
    # If it's a pre-fill field that isn't in our Enum,
    # map it to SHORT_ANSWER so we don't lose the historical question count.
    logger.debug("Mapping unrecognized Meta question type to SHORT_ANSWER: %s", raw_type)
    return LeadFormQuestion(
        type=QuestionCategory.SHORT_ANSWER,
        key=raw_key or (raw_type.lower() if raw_type else "question"),
        label=raw_label or (raw_type.replace("_", " ").title() if raw_type else "Question"),
    )


def _parse_experience(raw_form: dict[str, Any]) -> bool:
    """Detects if the form is 'Higher Intent' (requires review screen)."""
    # Meta uses 'is_optimized_for_quality' flag to indicate Higher Intent forms
    return bool(raw_form.get("is_optimized_for_quality", False))


def parse_leadgen_forms(raw_forms: list[dict[str, Any]]) -> list[LeadFormProfile]:
    """Safely normalizes a batch of raw Meta form dictionaries into Pydantic models.
    
    Uses fail-soft iteration: if one form crashes the parser, it is skipped 
    so the rest of the batch survives.
    """
    profiles: list[LeadFormProfile] = []
    
    for raw in raw_forms:
        if not isinstance(raw, dict):
            continue
        try:
            form_id = raw.get("id")
            if not form_id:
                logger.warning("Skipping lead form with no ID.")
                continue
                
            parsed_questions: list[LeadFormQuestion] = []
            raw_questions = raw.get("questions", [])
            if isinstance(raw_questions, list):
                for q in raw_questions:
                    if isinstance(q, dict):
                        try:
                            pq = _parse_question(q)
                            if pq:
                                parsed_questions.append(pq)
                        except Exception as q_err:
                            logger.warning(
                                "Failed to parse question in lead form %s: %s", form_id, q_err
                            )

            raw_pp = raw.get("privacy_policy_url")
            if isinstance(raw_pp, str):
                privacy_policy_url = raw_pp.strip()
            elif isinstance(raw_pp, dict):
                privacy_policy_url = str(raw_pp.get("url") or "").strip()
            else:
                privacy_policy_url = ""

            profile = LeadFormProfile(
                id=str(form_id),
                name=raw.get("name", "Unnamed Form"),
                status=raw.get("status", "UNKNOWN"),
                leads_count=int(raw.get("leads_count", 0)),
                is_higher_intent=_parse_experience(raw),
                questions=parsed_questions,
                created_time=raw.get("created_time", ""),
                context_card_raw=raw.get("context_card") or None,
                thank_you_page_raw=raw.get("thank_you_page") or None,
                privacy_policy_url=privacy_policy_url,
            )
            profiles.append(profile)
            
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            logger.warning(
                "Failed to parse lead form %s: %s", raw.get("id", "unknown"), e
            )
            continue
            
    return profiles
