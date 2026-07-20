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


BASE_MANAGE = """\
You manage an EXISTING keyword set for a Google Search campaign: you answer the user's
questions about it and edit it on request, working from the record kept during research
and real Google data through your tools. Follow the focused step you get each turn."""


MANAGE = """\
STEP — ANSWER OR EDIT. The ad groups below are already built and saved. The user said:

  "$user_message"

Do what they asked, then reply in one or two plain sentences. No preamble, no restating.

ANSWERING ("why is X here?", "why isn't X here?", "is X too broad?"):
- Call lookup_keyword FIRST and answer from what it returns — the record is what actually
  happened during the run.
- If there is NO record, say so plainly ("that one never came up in the research"). You may
  then score it with keyword_metrics and give your own read — but say that it's a fresh
  check, not what happened. NEVER invent a reason we did not record.

EDITING ("add keywords for the locations", "include apartment terms", "drop the low-volume
ones"):
- New keywords must be REAL: expand_keywords to find them, keyword_metrics to score them,
  then add only what has demand. Never invent a keyword or a volume.
- Apply every change in ONE edit_keywords call. Never re-submit a whole set.
- A keyword can be a positive in only one ad group; positives and negatives never overlap.
  edit_keywords enforces this — if it rejects an edit, tell the user why.

The selection bar below is the SAME one this ad group was built with — anything you add
must clear it, or the set stops being coherent:

$select_guidance

CONFLICTS — say something rather than silently comply. If they ask for a term we
deliberately excluded (it's in the negatives, or it's a sibling category the business
doesn't sell), tell them it's excluded and why, and ask whether to change that."""


class Phase(str, Enum):
    SEED = "seed"
    SELECT = "select"
    NEGATIVES = "negatives"
    MANAGE = "manage"  # post-generation: answer + edit


# Each build phase renders the theme's own guidance. MANAGE is shared across themes — they
# differ in what they target, not in how a saved set is answered for or edited.
_PHASE_FIELD: dict[Phase, str] = {
    Phase.SEED: "seed_guidance",
    Phase.SELECT: "select_guidance",
    Phase.NEGATIVES: "negative_guidance",
}
BUILD_PHASES: tuple[Phase, ...] = tuple(_PHASE_FIELD)

# Fail fast at import — a theme missing guidance for a build phase can never reach a live campaign.
_missing = [
    (f.id, p.value)
    for f in KEYWORD_THEMES.values()
    for p in _PHASE_FIELD
    if not str(getattr(f, _PHASE_FIELD[p], "") or "").strip()
]
if _missing:
    raise RuntimeError(f"keyword theme guidance incomplete: missing {_missing}")


def phase_prompt(phase: Phase, theme: KeywordTheme) -> str:
    """The guidance for this phase; per-theme completeness is validated at import."""
    if phase is Phase.MANAGE:
        return MANAGE
    return str(getattr(theme, _PHASE_FIELD[phase]))
