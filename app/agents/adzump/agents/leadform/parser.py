"""Graph API Response Parser Engine for the Lead Form Sub-Agent."""

import logging
from typing import Any

from app.agents.adzump.agents.leadform.models import (
    LeadFormProfile,
    LeadFormQuestion,
    QuestionCategory,
)

logger = logging.getLogger(__name__)


def _parse_question(raw_q: dict[str, Any]) -> LeadFormQuestion | None:
    """Parses a single raw question from Meta into a LeadFormQuestion.
    
    Meta's 'type' field is either a standard pre-fill field (like 'EMAIL') or 'CUSTOM'.
    For CUSTOM, the actual question text is usually in 'label' and options in 'options'.
    """
    raw_type = raw_q.get("type", "").upper()
    
    # Check if it's a standard pre-fill question supported by our Enum
    if raw_type in [cat.value for cat in QuestionCategory]:
        return LeadFormQuestion(
            type=QuestionCategory(raw_type),
            key=raw_q.get("key", raw_type.lower()),
            label=raw_q.get("label", raw_type.replace("_", " ").title()),
        )
        
    # Handle custom questions
    if raw_type == "CUSTOM":
        options = []
        raw_options = raw_q.get("options", [])
        if isinstance(raw_options, list):
            # Meta sometimes returns options as a list of dicts with 'value' keys, or just strings
            options = [str(opt.get("value", opt)) if isinstance(opt, dict) else str(opt) for opt in raw_options]
            
        category = QuestionCategory.MULTIPLE_CHOICE if options else QuestionCategory.SHORT_ANSWER
        
        return LeadFormQuestion(
            type=category,
            key=raw_q.get("key", "custom_question"),
            label=raw_q.get("label", ""),
            options=options,
        )
        
    # If it's a pre-fill field that isn't in our Enum (e.g. STREET_ADDRESS),
    # map it to SHORT_ANSWER so we don't lose the historical question count.
    logger.debug("Mapping unrecognized Meta question type to SHORT_ANSWER: %s", raw_type)
    return LeadFormQuestion(
        type=QuestionCategory.SHORT_ANSWER,
        key=raw_q.get("key", raw_type.lower()),
        label=raw_q.get("label", raw_type.replace("_", " ").title()),
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
                        pq = _parse_question(q)
                        if pq:
                            parsed_questions.append(pq)
                            
            profile = LeadFormProfile(
                id=str(form_id),
                name=raw.get("name", "Unnamed Form"),
                status=raw.get("status", "UNKNOWN"),
                leads_count=int(raw.get("leads_count", 0)),
                is_higher_intent=_parse_experience(raw),
                questions=parsed_questions,
            )
            profiles.append(profile)
            
        except Exception as e:
            logger.warning(
                "Failed to parse lead form %s: %s", raw.get("id", "unknown"), e
            )
            continue
            
    return profiles
