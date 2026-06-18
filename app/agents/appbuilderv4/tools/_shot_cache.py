"""Process-level cache of source/build screenshots keyed by session_id.

Used by `screenshot_external_url` (writes) and `compare_to_source` (reads).
We deliberately do NOT use `session.context` for this — base64 PNGs would
blow the persisted `CONTEXT_JSON` MySQL column on every turn-end save.

Cache lifetime: the OS process. Cleared on restart. That's fine — clone
loops typically complete within a few minutes inside one session.
"""

from __future__ import annotations

from typing import Any


# Mapping: session_id → { source_handle → {url, image_base64, image_mime, ...} }
_CACHES: dict[str, dict[str, dict[str, Any]]] = {}

# Cap per-session cache size so a long-running session can't OOM the process.
MAX_SHOTS_PER_SESSION = 32


def get_shot_cache(session_id: str) -> dict[str, dict[str, Any]]:
    """Return the per-session cache dict; create lazily."""
    if session_id not in _CACHES:
        _CACHES[session_id] = {}
    return _CACHES[session_id]


def put_shot(session_id: str, handle: str, shot: dict[str, Any]) -> None:
    """Store one shot under (session_id, handle). Evicts oldest when over cap."""
    cache = get_shot_cache(session_id)
    if handle in cache:
        cache.pop(handle)
    if len(cache) >= MAX_SHOTS_PER_SESSION:
        try:
            cache.pop(next(iter(cache)))
        except StopIteration:
            pass
    cache[handle] = shot


def get_shot(session_id: str, handle: str) -> dict[str, Any] | None:
    return _CACHES.get(session_id, {}).get(handle)


def known_handles(session_id: str, limit: int = 8) -> list[str]:
    return list(_CACHES.get(session_id, {}).keys())[:limit]
