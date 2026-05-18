from __future__ import annotations

from pathlib import Path

from app.core.context import BaseContext


PROMPTS_DIR = Path(__file__).parent / "prompts"


def _read_prompt(name: str) -> str:
    path = PROMPTS_DIR / name

    if not path.exists():
        return ""

    return path.read_text(encoding="utf-8")


def build_search_term_context() -> BaseContext:
    """
    Builds static optimization context for Search Term Agent.
    """

    system_prompt = _read_prompt("system_prompt.txt")

    prompt_context = f"""

# BRAND RELEVANCY PROMPT

{_read_prompt("brand_relevancy_prompt.txt")}


# CONFIGURATION RELEVANCY PROMPT

{_read_prompt("configuration_relevancy_prompt.txt")}


# LOCATION RELEVANCY PROMPT

{_read_prompt("location_relevancy_prompt.txt")}


# OVERALL RELEVANCY PROMPT

{_read_prompt("overall_relevancy_prompt.txt")}

"""

    return BaseContext(
        static_prefix=f"""
{system_prompt}

{prompt_context}
""",
    )
