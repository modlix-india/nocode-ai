import os
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

def safe_truncate_to_sentence(text: str, max_length: int) -> str:
    """Simple truncation to avoid bringing in all text_utils right now."""
    if len(text) <= max_length:
        return text
    truncated = text[:max_length]
    last_period = truncated.rfind(".")
    if last_period > 0:
        return truncated[:last_period + 1]
    return truncated

def load_prompt(prompt_name: str) -> str:
    # Pointing to the new prompts directory in nocode-ai
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prompt_path = os.path.join(root_dir, "prompts", prompt_name)
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()

def format_prompt(prompt_file: str, **context: Any) -> str:
    try:
        template = load_prompt(prompt_file)
        format_dict = build_template_variables(template, context)
        formatted_prompt = template.format(**format_dict)
        logger.debug(f"Successfully formatted prompt: {prompt_file}")
        return formatted_prompt
    except FileNotFoundError:
        logger.error(f"Prompt file not found: {prompt_file}")
        raise
    except KeyError as e:
        logger.error(f"Missing required field in the prompt file {prompt_file}: {e}")
        logger.error(f"Available fields: {list(format_dict.keys())}")
        raise
    except Exception as e:
        logger.error(f"Error formatting prompt file {prompt_file}: {e}")
        raise

def build_template_variables(template: str, context: Dict[str, Any]) -> Dict[str, Any]:
    format_dict: Dict[str, Any] = {}
    for key, value in context.items():
        if value is None:
            continue
            
        if key == "scraped_data":
            format_dict["scraped_data"] = value
            if "{content_summary}" in template:
                format_dict["content_summary"] = safe_truncate_to_sentence(str(value), 2000)
            if "{business_summary}" in template:
                if "business_summary" not in context:
                    format_dict["business_summary"] = safe_truncate_to_sentence(str(value), 2500)
            continue
            
        if key == "brand_info":
            # Handles both dictionaries (from new pipeline) and BusinessMetadata objects
            b_name = value.get("brand_name", "") if isinstance(value, dict) else getattr(value, "brand_name", "")
            b_type = value.get("business_type", "") if isinstance(value, dict) else getattr(value, "business_type", "")
            b_loc = value.get("primary_location", "") if isinstance(value, dict) else getattr(value, "primary_location", "")
            b_areas = value.get("service_areas", []) if isinstance(value, dict) else getattr(value, "service_areas", [])
            
            format_dict["brand_name"] = b_name
            format_dict["business_type"] = b_type
            format_dict["primary_location"] = b_loc
            format_dict["service_areas"] = ", ".join(b_areas) if b_areas else "Not provided"

            if "{location_context}" in template:
                location_context = ""
                if b_loc and b_loc != "Unknown":
                    location_context += f"Primary Location: {b_loc}\n"
                if b_areas:
                    location_context += f"Service Areas: {', '.join(b_areas)}\n"
                format_dict["location_context"] = location_context
            format_dict["brand_info"] = value
            continue

        if key == "unique_features" and isinstance(value, list):
            format_dict["unique_features"] = value
            if "{features_context}" in template:
                format_dict["features_context"] = f"Unique Features: {', '.join(value)}\n" if value else ""
            continue

        if key == "suggestions" and isinstance(value, list) and value:
            # Handle list of dicts (from nocode-ai pipeline)
            if isinstance(value[0], dict) and "keyword" in value[0]:
                keyword_lines = []
                for s in value:
                    comp_idx = s.get("competitionIndex", 0)
                    roi = s.get("roi_score", s.get("volume", 0) / max(0.01, 1 + comp_idx))
                    keyword_lines.append(
                        f"{s['keyword']} | Volume:{s.get('volume',0)} | ROI:{roi:.0f} | Competition: {comp_idx:.2f}"
                    )
                format_dict["keywords_text"] = "\n".join(keyword_lines)
            continue

        if key == "positive_keywords" and isinstance(value, list):
            if value:
                if isinstance(value[0], dict) and "keyword" in value[0]:
                    format_dict["positive_text"] = ", ".join([kw["keyword"] for kw in value])
                else:
                    format_dict["positive_text"] = "No positive keywords provided"
            else:
                format_dict["positive_text"] = "No positive keywords provided"
            continue
            
        if key == "url":
            format_dict["url"] = value or "Not provided"
            continue
            
        format_dict[key] = value

    if "url" not in format_dict:
        format_dict["url"] = "Not provided"
        
    return format_dict
