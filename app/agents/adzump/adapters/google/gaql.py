"""GAQL identifier safety.

Google Ads resource IDs (campaign, ad group, …) are int64 — always all-digits. Any
id interpolated into a GAQL query string must therefore be numeric. Validating at
the query boundary closes the injection vector from LLM/user-supplied ids: GAQL is
read-only, but a crafted id could still widen a WHERE clause within the
authenticated customer's data. ``isdigit()`` is NOT enough — it accepts Unicode
digits; the regex is ASCII-only.
"""

from __future__ import annotations

import re

_NUMERIC_ID = re.compile(r"^[0-9]+$")


class InvalidGaqlId(ValueError):
    """A non-numeric id was about to be interpolated into a GAQL query."""


def is_numeric_id(value: object) -> bool:
    """True iff ``value`` is a non-empty all-ASCII-digit id (GAQL-safe)."""
    return bool(_NUMERIC_ID.match(str(value if value is not None else "").strip()))


def safe_id(value: object, field: str = "id") -> str:
    """Return ``value`` as a string iff it is a numeric Google Ads id; else raise
    ``InvalidGaqlId``."""
    text = str(value if value is not None else "").strip()
    if not _NUMERIC_ID.match(text):
        raise InvalidGaqlId(f"{field} must be a numeric Google Ads id, got {value!r}")
    return text


def safe_ids(values, field: str = "id") -> list[str]:
    """Validate every id in ``values`` (raises on the first non-numeric one)."""
    return [safe_id(v, field) for v in values]
