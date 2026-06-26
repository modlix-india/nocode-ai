from __future__ import annotations

from pathlib import Path


def build_creative_context() -> str:
    """Load the creative copywriting system prompt."""
    prompts_dir = Path(__file__).resolve().parent / "prompts"
    return (prompts_dir / "creative_copy.txt").read_text(encoding="utf-8")
