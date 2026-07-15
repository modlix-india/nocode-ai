"""System prompt + the phase machine for the KeywordResearchAgent.

The base prompt stays small; ``build_turn_reminder`` injects only the current phase's guidance
via ``phase_prompt(phase, theme)``. The guidance itself belongs to the theme — see themes.py.
"""

from __future__ import annotations

from enum import Enum

from app.agents.adzump.agents.campaign.google.keyword.themes import KEYWORD_THEMES, KeywordTheme


BASE = """\
You are a Google Ads keyword strategist building ONE keyword set (brand OR generic —
see CAMPAIGN) for a Search campaign, using real Google data through your tools.

Flow — you decide each step; follow the focused guidance you get each turn:
1. Draft seeds, then call expand_keywords to broaden them with real autosuggest queries.
2. Call keyword_metrics for real Google volume / competition / CPC — the relevance gate;
   terms with no Google demand aren't worth bidding on.
3. Pick the positives and call submit_positive_keywords.
4. Derive the negatives and call submit_negative_keywords (this finishes the run).

Always:
- Anchor on the OFFERING and its CORE TERMS above. The SIBLING CATEGORIES are different
  products — usually negatives; target one as a positive only as a deliberate, controlled
  upsell (cross-business, phrase) when its buyer could convert up — the phase guidance says when.
- Positives must be exact keywords the Planner returned — never invent keywords.
- Match type: positives are EXACT or PHRASE (never broad); negatives are PHRASE or BROAD
  (never exact). The phase guidance sets which to use.
- Emit keywords only by calling the tools, never as prose."""


class Phase(str, Enum):
    SEED = "seed"
    SELECT = "select"
    NEGATIVES = "negatives"


_PHASE_FIELD: dict[Phase, str] = {
    Phase.SEED: "seed_guidance",
    Phase.SELECT: "select_guidance",
    Phase.NEGATIVES: "negative_guidance",
}

# Fail fast at import — a theme missing guidance for a phase can never reach a live campaign.
_missing = [
    (f.id, p.value)
    for f in KEYWORD_THEMES.values()
    for p in Phase
    if not str(getattr(f, _PHASE_FIELD[p], "") or "").strip()
]
if _missing:
    raise RuntimeError(f"keyword theme guidance incomplete: missing {_missing}")


def phase_prompt(phase: Phase, theme: KeywordTheme) -> str:
    """This theme's guidance for this phase; completeness is validated at import."""
    return str(getattr(theme, _PHASE_FIELD[phase]))
