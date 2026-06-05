"""Conservative typed-answer parsers for tagged elicitation capture (PR2 · D12).

When the user TYPES an answer to a chip question ("25 days", "4k", "facebook")
instead of clicking, ``parse_typed_answer`` reads a clean canonical value for the
tagged field — or returns ``None`` so the turn falls through to the LLM.

High precision over recall: parse only the *unambiguous*. Any correction cue
("no", "not", "instead", …) or more than one distinct number → ``None``. The
guard ``_field_traceable`` (campaign_data) parses both the candidate value and
the user's text and accepts on a canonical match, so the normalized output here
is trusted by the same validation the LLM's own writes go through.
"""

from __future__ import annotations

import re

from app.agents.adzump.platform import (
    Platform, CANONICAL_LABEL, _GOOGLE_KEYWORDS, _META_KEYWORDS,
)

# Real-estate detection mirrors CampaignContext.is_real_estate / _next_action's
# currency pick (agent.py). Kept here (not imported from agent.py) to avoid a
# circular import — campaign_data + agent both import this module.
_RE_KEYWORDS = (
    "real estate", "realty", "villa", "apartment", "residential",
    "property", "housing", "homes", "realtor", "township", "builder", "developer",
)

# Any whiff of correction/contradiction → don't capture; let the model decide.
_CUE = re.compile(r"\b(no|not|instead|rather|actually|nope|wait|never\s?mind|scratch)\b|n't")

_DURATION = re.compile(r"\b(\d+)\s*(days?|weeks?|months?|years?|yrs?|mo|wk)\b", re.I)
_DURATION_ONE = re.compile(r"\b(a|one)\s+(day|week|month|year)\b", re.I)
_UNIT = {"day": "day", "week": "week", "month": "month", "year": "year",
         "yr": "year", "mo": "month", "wk": "week"}

# A money amount with an optional magnitude suffix.
_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|l|lac|lakh|cr|crore|m|mn)?\b", re.I)
_MULT = {"k": 1_000, "l": 100_000, "lac": 100_000, "lakh": 100_000,
         "cr": 10_000_000, "crore": 10_000_000, "m": 1_000_000, "mn": 1_000_000}
_PERDAY = re.compile(r"(/\s?d(ay)?\b|per\s+day|a\s+day|daily)", re.I)
_SYMBOLS = [  # (detector, canonical symbol) — order: longest/specific first
    (re.compile(r"₹|\brs\.?\b|\binr\b|rupees?\b", re.I), "₹"),
    (re.compile(r"\$|\busd\b|dollars?\b", re.I), "$"),
]


def currency_for(session_ctx: dict | None) -> str:
    """₹ for real-estate sessions, else $ — matches _next_action's chip presets."""
    bt = ((session_ctx or {}).get("product_data") or {}).get("business_type", "")
    return "₹" if any(kw in bt.lower() for kw in _RE_KEYWORDS) else "$"


def _distinct_numbers(text: str) -> int:
    return len({n.replace(",", "") for n in re.findall(r"\d[\d,]*", text)})


def _parse_duration(text: str) -> str | None:
    m = _DURATION.search(text)
    if m:
        n = int(m.group(1))
        key = m.group(2).lower()
        key = key[:-1] if key.endswith("s") else key  # days->day, yrs->yr
        unit = _UNIT.get(key)
    else:
        m1 = _DURATION_ONE.search(text)
        if m1:
            n, unit = 1, _UNIT.get(m1.group(2).lower())
        elif (m2 := re.fullmatch(r"\s*(\d{1,3})\s*", text)):
            # v3 · F1 — a bare integer with NO unit, asked in a DURATION context,
            # reads as days: typed "30" → "30 days". Reached only when the caller
            # passes field=="duration" (the pending elicitation field, or the
            # field the LLM is writing), so it is pending-field-gated by
            # construction — budget keeps requiring a currency/suffix/per-day
            # marker. Bounded to 1–999 so a stray id/year can't masquerade as a
            # duration; >1 distinct number is already rejected upstream.
            n, unit = int(m2.group(1)), "day"
        else:
            return None
    if unit is None:
        return None
    return f"{n} {unit if n == 1 else unit + 's'}"


def _parse_budget(text: str, currency: str) -> str | None:
    sym = next((s for rx, s in _SYMBOLS if rx.search(text)), None)
    explicit = sym is not None
    m = _AMOUNT.search(text)
    if not m:
        return None
    amount = float(m.group(1).replace(",", ""))
    suffix = (m.group(2) or "").lower()
    if suffix:
        amount *= _MULT[suffix]
    # Require a marker: a currency symbol, a magnitude suffix, or a per-day phrase.
    if not (explicit or suffix or _PERDAY.search(text)):
        return None
    return f"{sym or currency}{int(round(amount)):,}/day"


def _parse_platform(text: str) -> str | None:
    lo = text.lower()
    g = any(re.search(rf"\b{re.escape(k)}\b", lo) for k in _GOOGLE_KEYWORDS)
    m = any(re.search(rf"\b{re.escape(k)}\b", lo) for k in _META_KEYWORDS)
    if g and m:  # both named → ambiguous
        return None
    p = Platform.from_value(text)
    return CANONICAL_LABEL.get(p) if p else None


def parse_typed_answer(field: str, text: str, currency: str = "$") -> str | None:
    """Canonical value for a typed reply to a `field` chip, or None to fall through.

    Conservative: returns None on any correction cue or >1 distinct number
    (duration/budget). Only duration/budget/platform are parseable.
    """
    text = (text or "").strip()
    if not text or _CUE.search(text.lower()):
        return None
    if field in ("duration", "budget") and _distinct_numbers(text) > 1:
        return None
    if field == "duration":
        return _parse_duration(text)
    if field == "budget":
        return _parse_budget(text, currency)
    if field == "platform":
        return _parse_platform(text)
    return None
