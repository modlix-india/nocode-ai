"""Models for SummaryAgent.

The agent emits free-form prose (the product summary text) - there's no
structured-output schema to enforce. This file exists for symmetry with
the other agents' folder shapes (every agent has its own ``models.py``)
and to hold the input shape if we need one later (e.g. when the prompt
starts accepting structured context blocks instead of raw text).
"""

from __future__ import annotations

from pydantic import BaseModel


class SummaryInput(BaseModel):
    """What the agent receives from the caller.

    Keeping this typed (vs raw kwargs) so the contract is greppable and
    the future migration to a tracing wrapper can serialise it cleanly.
    """
    url: str
    scraped_text: str   # already formatted by _format_page_for_profile()
    craft_id: str       # target craft block where the streamed summary lands


class SummaryOutput(BaseModel):
    """What the agent returns to the caller.

    Today this is just the accumulated text. We carry it inside a model
    so future fields (token usage, confidence, model id) can be added
    without changing the call surface.
    """
    text: str
