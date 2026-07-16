"""Creative Intelligence - the library of competitor market creatives.

A domain package that sits below the agents: they read it, background work
maintains it, and it owns no LLM loop. Public API:

    from app.agents.adzump.creative_intelligence import creatives_for_all, Competitor
"""

from app.agents.adzump.creative_intelligence.models import (
    Creative,
    Competitor,
    Essence,
)
from app.agents.adzump.creative_intelligence.enrich import (
    CreativeImage,
    EnrichCreatives,
)
from app.agents.adzump.creative_intelligence.store import competitor_key, is_stale
from app.agents.adzump.creative_intelligence.library import (
    creatives_for,
    creatives_for_all,
    competitor_identity,
)

__all__ = [
    "Creative",
    "Competitor",
    "Essence",
    "CreativeImage",
    "EnrichCreatives",
    "competitor_key",
    "is_stale",
    "creatives_for",
    "creatives_for_all",
    "competitor_identity",
]
