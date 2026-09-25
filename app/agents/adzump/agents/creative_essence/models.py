"""Wire-shape models for the EssenceAnalyst's LLM output.

The output verdict IS the domain ``Essence`` plus the input-order ``idx`` the
resolver maps back to a ``content_hash``. One schema source of truth: the enums
live on ``Essence`` (``creative_intelligence/models.py``) and the wire shape
inherits them, so the domain schema and the vision contract can't drift. The
input unit (``CreativeImage``) is the domain's enrichment seam - re-exported
here so the agent's public surface stays one import.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.adzump.creative_intelligence.enrich import CreativeImage
from app.agents.adzump.creative_intelligence.models import Essence

__all__ = ["CreativeImage", "EssenceVerdict", "EssenceBatch"]


class EssenceVerdict(Essence):
    """One per-image verdict in the LLM's JSON output: the full ``Essence``
    plus the input-order index. ``to_essence()`` strips the index back off."""

    idx: int = Field(default=0, description="Index into the input image list.")

    def to_essence(self) -> Essence:
        return Essence.model_validate(self.model_dump(exclude={"idx"}))


class EssenceBatch(BaseModel):
    """The shape the LLM fills - one verdict per input image, in input order."""

    verdicts: list[EssenceVerdict] = Field(default_factory=list)
