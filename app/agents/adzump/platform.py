"""Single source of truth for the campaign ad-platform.

The LLM stores a free-text value in ``session.campaign_spec["platform"]``
(it might be ``"Google Ads"``, ``"google ads"``, ``"Meta"``, ``"facebook"``
or any near-variant). Every consumer that branches on this — the launch
event payload, the traceability check, the review-summary builder, the
account adapter selector — needs to agree on what each variant means.

Helpers here are the canonical answer. Don't inline `"meta" in str.lower()`
checks in new code; use ``Platform.from_value`` / ``is_google`` / ``is_meta``.

Conventions:
- ``Platform`` is the stable enum used in machine-readable payloads
  (e.g. the SSE ``complete`` event: ``"platform": "google" | "meta"``).
- ``CANONICAL_LABEL`` is the user-facing free-text label written into
  ``campaign_spec["platform"]`` when we resolve a user-typed message
  to a platform. Reading code never relies on the exact string — it
  goes through ``Platform.from_value`` first.
"""

from __future__ import annotations

import re
from enum import Enum


class Platform(str, Enum):
    """The two ad platforms adzump can launch into."""

    GOOGLE = "google"
    META = "meta"

    @classmethod
    def from_value(cls, value: str | None) -> "Platform | None":
        """Map any free-text platform string to the enum, or None if unknown.

        Matches whichever keyword appears first in ``value``. Order matters
        only for hypothetical ambiguous strings ("google meta") — first
        match wins, which is fine in practice.

        v9 I-2 fix: match keywords on WORD BOUNDARIES, not raw substrings.
        The old ``k in v`` test let the 2-char Meta abbreviations "ig"/"fb"
        false-match inside ordinary words — e.g. "ig" ∈ "r**ig**ht" made
        "let's continue, right now" parse as Meta, mis-prescribing a platform
        the user never picked. ``\\b`` requires the keyword to stand alone.
        """
        v = (value or "").strip().lower()
        if not v:
            return None
        if any(re.search(rf"\b{re.escape(k)}\b", v) for k in _GOOGLE_KEYWORDS):
            return cls.GOOGLE
        if any(re.search(rf"\b{re.escape(k)}\b", v) for k in _META_KEYWORDS):
            return cls.META
        return None


# Free-text variants we accept when parsing the LLM's stored spec value
# OR a raw user message. Used by both Platform.from_value and the
# traceability check in tools/campaign_data.py.
_GOOGLE_KEYWORDS = ("google", "adwords")
_META_KEYWORDS = ("meta", "facebook", "instagram", "fb", "ig")


# User-facing label written into campaign_spec["platform"] when the
# intent router resolves a typed message into a platform pick. The LLM
# may write its own variants too — readers must always normalize.
CANONICAL_LABEL: dict[Platform, str] = {
    Platform.GOOGLE: "Google Ads",
    Platform.META: "Meta",
}


def is_google(value: str | None) -> bool:
    return Platform.from_value(value) is Platform.GOOGLE


def is_meta(value: str | None) -> bool:
    return Platform.from_value(value) is Platform.META


def to_enum_value(value: str | None) -> str:
    """Stable enum string for JSON payloads — ``"google"`` / ``"meta"`` / ``""``."""
    p = Platform.from_value(value)
    return p.value if p else ""
