"""Keyword-research prompts — split by phase (seed / select / negatives), each with a brand +
generic template. Public API only; the logic lives in the modules below.
"""

from app.agents.adzump.agents.keyword.prompts.base import BASE
from app.agents.adzump.agents.keyword.prompts.registry import Phase, phase_prompt

__all__ = ["BASE", "Phase", "phase_prompt"]
