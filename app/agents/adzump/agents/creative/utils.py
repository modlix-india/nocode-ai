import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agents.adzump.agents.creative.models import (
    CreativeText,
)


def load_prompt(filename: str) -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / filename

    return prompt_path.read_text(encoding="utf-8")


def extract_json(raw_text: str) -> dict[str, Any]:
    raw_text = raw_text.strip()

    if raw_text.startswith("```json"):
        raw_text = raw_text.removeprefix("```json").strip()

    if raw_text.endswith("```"):
        raw_text = raw_text.removesuffix("```").strip()

    return json.loads(raw_text)


def validate_creative_response(
    raw_output: str,
) -> dict[str, Any]:
    try:
        response_json = extract_json(raw_output)

        validated = CreativeText(**response_json)

        return validated.model_dump(mode="json")

    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(
            f"Failed to validate creative text response: {str(exc)}"
        ) from exc
