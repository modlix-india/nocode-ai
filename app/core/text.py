"""Text helpers shared across the run loop and tools."""
from __future__ import annotations

import re


def _normalize(s: str) -> str:
    """Lowercase, collapse whitespace, strip surrounding punctuation."""
    return re.sub(r"\s+", " ", (s or "").lower()).strip().strip("?.!,: ").strip()


def contains_normalized(needle: str, haystack: str) -> bool:
    """True if `needle` appears in `haystack` after normalizing both. The de-dup
    behind "don't re-emit text the model already streamed": lets "How long should
    it run?" match a prose "...how long should it run". Empty needle → False."""
    n = _normalize(needle)
    return bool(n) and n in _normalize(haystack)
