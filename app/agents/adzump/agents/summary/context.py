"""System prompt for the SummaryAgent.

The prompt lives in ``agents/product/prompts/product_profile.txt`` today (it
predates this agent). We read it from there to preserve byte-for-byte
parity with the existing direct-call shape. A v2 refactor can move the
file under ``agents/summary/prompts/``; until then, callers other than this
agent might still import it from the old path.
"""

from __future__ import annotations

from pathlib import Path

from app.core.context import BaseContext


# DRAFT-NOTE · prompt currently lives in the product agent's prompt folder.
# When we move it (D3 in implementation-notes.md), change this path.
_PROFILE_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "product" / "prompts" / "product_profile.txt"
)


def _load_summary_prompt() -> str:
    """Read the gpt-4o profile-summary system prompt from disk."""
    return _PROFILE_PROMPT_PATH.read_text(encoding="utf-8")


def build_summary_context() -> BaseContext:
    """Build the BaseContext for SummaryAgent.

    The agent is single-shot (no tool decisions, no iteration), so the
    context is just the static system prompt — no dynamic per-turn slot
    filling.
    """
    return BaseContext(
        doc_paths=[],
        static_prefix=_load_summary_prompt(),
    )
