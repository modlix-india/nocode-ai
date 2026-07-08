"""Typed prompt registry.

Validated complete at import: a missing or typo'd phase/type prompt fails at startup (here),
never mid-campaign. build_turn_reminder looks up via phase_prompt(phase, kw_type) — no
bare-string keys, no silent fallback.
"""

from __future__ import annotations

from enum import Enum

from app.agents.adzump.agents.keyword.models import KeywordType
from app.agents.adzump.agents.keyword.prompts.negatives import (
    NEGATIVES_BRAND,
    NEGATIVES_COMPETITOR_BRAND,
    NEGATIVES_GENERIC,
)
from app.agents.adzump.agents.keyword.prompts.seed import (
    SEED_BRAND,
    SEED_COMPETITOR_BRAND,
    SEED_GENERIC,
)
from app.agents.adzump.agents.keyword.prompts.select import (
    SELECT_BRAND,
    SELECT_COMPETITOR_BRAND,
    SELECT_GENERIC,
)


class Phase(str, Enum):
    SEED = "seed"
    SELECT = "select"
    NEGATIVES = "negatives"


_REGISTRY: dict[tuple[Phase, KeywordType], str] = {
    (Phase.SEED, KeywordType.BRAND): SEED_BRAND,
    (Phase.SEED, KeywordType.GENERIC): SEED_GENERIC,
    (Phase.SEED, KeywordType.COMPETITOR_BRAND): SEED_COMPETITOR_BRAND,
    (Phase.SELECT, KeywordType.BRAND): SELECT_BRAND,
    (Phase.SELECT, KeywordType.GENERIC): SELECT_GENERIC,
    (Phase.SELECT, KeywordType.COMPETITOR_BRAND): SELECT_COMPETITOR_BRAND,
    (Phase.NEGATIVES, KeywordType.BRAND): NEGATIVES_BRAND,
    (Phase.NEGATIVES, KeywordType.GENERIC): NEGATIVES_GENERIC,
    (Phase.NEGATIVES, KeywordType.COMPETITOR_BRAND): NEGATIVES_COMPETITOR_BRAND,
}

# Fail fast at import — every (phase, type) must exist; a gap can never reach a live campaign.
_missing = [(p.value, t.value) for p in Phase for t in KeywordType if (p, t) not in _REGISTRY]
if _missing:
    raise RuntimeError(f"keyword prompts incomplete: missing {_missing}")


def phase_prompt(phase: Phase, kw_type: KeywordType) -> str:
    """Typed lookup of the phase prompt; the registry is validated complete at import."""
    return _REGISTRY[(phase, kw_type)]
