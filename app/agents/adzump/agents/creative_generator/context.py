"""Context and system prompt builders for the Creative Generator sub-agents."""

from __future__ import annotations

from pathlib import Path
from app.core.context import BaseContext

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def build_copy_context() -> BaseContext:
    """Build the BaseContext for the CreativeCopyAgent."""
    path = _PROMPTS_DIR / "creative_copy.txt"
    system_prompt = path.read_text(encoding="utf-8")
    context = BaseContext(doc_paths=[], static_prefix=system_prompt)
    context._cached_static_text = context._static_prefix
    return context


def build_selection_context() -> BaseContext:
    """Build the BaseContext for the CreativeSelectionAgent."""
    path = _PROMPTS_DIR / "creative_selection.txt"
    system_prompt = path.read_text(encoding="utf-8")
    context = BaseContext(doc_paths=[], static_prefix=system_prompt)
    context._cached_static_text = context._static_prefix
    return context
