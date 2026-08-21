"""Canonical duration/budget reading for the write-boundary validation.

``field_candidates`` reads EVERY canonical value a user message genuinely
supports for a field; ``_field_traceable`` (campaign_data) accepts a write iff
the model's value parses to one of them. The model normalizes, the framework
validates - never the reverse (rework slice 1b): the old ``parse_typed_answer``
auto-capture parser is retired, typed replies land via the steered model.

High precision over recall: budget requires a money marker LOCAL to the
amount; a bare number is never money. Anti-invention is canonical equality,
not digit-substring, so F1 (a stored "5 days" tracing to "15 properties")
stays closed.
"""

from __future__ import annotations

import re

# Real-estate detection mirrors CampaignContext.is_real_estate / _next_action's
# currency pick (agent.py). Kept here (not imported from agent.py) to avoid a
# circular import - campaign_data + agent both import this module.
_RE_KEYWORDS = (
    "real estate", "realty", "villa", "apartment", "residential",
    "property", "housing", "homes", "realtor", "township", "builder", "developer",
)

_DURATION = re.compile(r"\b(\d+)\s*(days?|weeks?|months?|years?|yrs?|mo|wk)\b", re.I)
_DURATION_ONE = re.compile(r"\b(a|one)\s+(day|week|month|year)\b", re.I)
_UNIT = {"day": "day", "week": "week", "month": "month", "year": "year",
         "yr": "year", "mo": "month", "wk": "week"}

# A money amount with an optional magnitude suffix.
_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|l|lac|lakh|cr|crore|m|mn)?\b", re.I)
_MULT = {"k": 1_000, "l": 100_000, "lac": 100_000, "lakh": 100_000,
         "cr": 10_000_000, "crore": 10_000_000, "m": 1_000_000, "mn": 1_000_000}
_PERDAY = re.compile(r"(/\s?d(ay)?\b|per\s+day|a\s+day|daily)", re.I)
_SYMBOLS = [  # (detector, canonical symbol) - order: longest/specific first
    (re.compile(r"₹|\brs\.?\b|\binr\b|rupees?\b", re.I), "₹"),
    (re.compile(r"\$|\busd\b|dollars?\b", re.I), "$"),
]

_BARE_INT = re.compile(r"\b\d{1,3}\b")
_TIME_UNIT_AFTER = re.compile(r"\s*(days?|weeks?|months?|years?|yrs?|mo|wk)\b", re.I)
_SUFFIX_AFTER = re.compile(r"\s*(k|l|lac|lakh|cr|crore|m|mn)\b", re.I)


def currency_for(session_ctx: dict | None) -> str:
    """₹ for real-estate sessions, else $ - matches _next_action's chip presets."""
    bt = ((session_ctx or {}).get("product_data") or {}).get("business_type", "")
    return "₹" if any(kw in bt.lower() for kw in _RE_KEYWORDS) else "$"


def _canonical_duration(text: str) -> str | None:
    """"25 days" / "a week" / "2 mo" -> canonical "N unit(s)"."""
    m = _DURATION.search(text)
    if m:
        n = int(m.group(1))
        key = m.group(2).lower()
        key = key[:-1] if key.endswith("s") else key  # days->day, yrs->yr
        unit = _UNIT.get(key)
    else:
        m1 = _DURATION_ONE.search(text)
        if not m1:
            return None
        n, unit = 1, _UNIT.get(m1.group(2).lower())
    if unit is None:
        return None
    return f"{n} {unit if n == 1 else unit + 's'}"


def _money_local(text: str, start: int, end: int) -> bool:
    """A money marker sits adjacent to text[start:end] - currency symbol just
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
    multi-number-tolerant (F24: corrections and volunteered fields count).
    ASYMMETRY (Kiran): duration may read a FREE bare number (days implied by
    the field context); budget requires a money marker LOCAL to the amount, so
    a bare number is never a budget ("call me at 5000" stays out)."""
    text = (text or "").strip()
    out: set[str] = set()
    if not text:
        return out
    if field == "duration":
        for m in list(_DURATION.finditer(text)) + list(_DURATION_ONE.finditer(text)):
            if (d := _canonical_duration(m.group(0))):
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
