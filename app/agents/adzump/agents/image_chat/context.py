from __future__ import annotations

from pathlib import Path

from app.core.context import BaseContext


def build_image_context() -> BaseContext:
    """Build the ImageAgent system prompt from disk."""
    prompts_dir = Path(__file__).resolve().parent / "prompts"
    static_text = (prompts_dir / "system.txt").read_text(encoding="utf-8")
    ctx = BaseContext(doc_paths=[], static_prefix=static_text)
    ctx._cached_static_text = ctx._static_prefix
    return ctx
