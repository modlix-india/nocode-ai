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

# TODO(harness): replace these hardcoded per-field parsers with context-driven
# structured capture — code gathers (pending field + last reply), model decides
# the canonical value (or null). Frontier models handle corrections, ₹/$ and new
# fields without regex/_CUE upkeep. Keep a thin parser only as a last-resort net.
# Measure how often this actually catches a drop the LLM would've missed first.

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


# ── F24 · traceability-side reading (NOT auto-capture) ─────────────────────
# parse_typed_answer is deliberately conservative — it bails on a correction cue
# ("no wait, make it 60") or >1 number ("60 day, ₹15000 budget"). That is right
# for AUTO-capture but too strict as the traceability gate on the model's OWN
# set_campaign_spec write: the user stated a real value the gate then dropped
# (F24 — silent "No changes." / volunteered fields lost). field_candidates reads
# EVERY value the text genuinely supports for a field, with no cue gate and no
# multi-number bail. Anti-invention is preserved by _field_traceable's canonical
# equality (the model's value must equal one of these), NOT by digit-substring —
# so an invented value that matches no number/amount is still rejected (F1 stays
# closed). ASYMMETRY (Kiran): duration may read a FREE bare number (days implied
# by the field context); budget requires a money marker LOCAL to the amount, so a
# bare number is never a budget ("call me at 5000" stays out; a duration number
# can't masquerade as ₹/day).
_BARE_INT = re.compile(r"\b\d{1,3}\b")
_TIME_UNIT_AFTER = re.compile(r"\s*(days?|weeks?|months?|years?|yrs?|mo|wk)\b", re.I)
_SUFFIX_AFTER = re.compile(r"\s*(k|l|lac|lakh|cr|crore|m|mn)\b", re.I)


def _money_local(text: str, start: int, end: int) -> bool:
    """A money marker sits adjacent to text[start:end] — currency symbol just
    before, magnitude suffix just after, or a per-day phrase just after."""
    left = text[max(0, start - 5):start]
    right = text[end:end + 9]
    return (any(rx.search(left) for rx, _ in _SYMBOLS)
            or bool(_SUFFIX_AFTER.match(right)) or bool(_PERDAY.search(right)))


def _local_budget(text: str, m: "re.Match", currency: str) -> str | None:
    """Canonical budget for an _AMOUNT match iff a marker is LOCAL to it."""
    suffix = (m.group(2) or "").lower()
    if not (suffix or _money_local(text, m.start(), m.end())):
        return None
    left = text[max(0, m.start() - 5):m.start()]
    right = text[m.end():m.end() + 9]
    sym = next((s for rx, s in _SYMBOLS if rx.search(left)), None)
    amount = float(m.group(1).replace(",", ""))
    if not suffix:
        sm = _SUFFIX_AFTER.match(right)
        suffix = sm.group(1).lower() if sm else ""
    if suffix:
        amount *= _MULT[suffix]
    return f"{sym or currency}{int(round(amount)):,}/day"


def field_candidates(field: str, text: str, currency: str = "$") -> set[str]:
    """Every canonical duration/budget value `text` supports, cue-free and
    multi-number-tolerant. Used ONLY by _field_traceable (see note above)."""
    text = (text or "").strip()
    out: set[str] = set()
    if not text:
        return out
    if field == "duration":
        for m in list(_DURATION.finditer(text)) + list(_DURATION_ONE.finditer(text)):
            if (d := _parse_duration(m.group(0))):
                out.add(d)
        for m in _BARE_INT.finditer(text):
            if _TIME_UNIT_AFTER.match(text[m.end():m.end() + 9]):
                continue                                   # unit-bearing; added above
            if _money_local(text, m.start(), m.end()):
                continue                                   # it's money, not days
            n = int(m.group(0))
            if 1 <= n <= 999:
                out.add(f"{n} day" if n == 1 else f"{n} days")
    elif field == "budget":
        for m in _AMOUNT.finditer(text):
            if (b := _local_budget(text, m, currency)):
                out.add(b)
    return out
