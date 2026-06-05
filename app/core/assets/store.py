"""AssetStore — content-addressed cache of prepared AssetViews.

v1: in-memory dict keyed by sha256(raw_bytes + target). v2 will swap in
Redis / S3 with the same surface. Idempotency invariant: same site +
same selector LLM = same picks = cheap re-runs.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from app.core.assets.views import AssetView, RenderTarget


class AssetStore:
    """sha256-keyed in-memory store. Thread-safe enough for asyncio (no awaits)."""

    def __init__(self) -> None:
        self._views: dict[str, AssetView] = {}

    @staticmethod
    def key_for(raw_bytes: bytes, target: RenderTarget) -> str:
        h = hashlib.sha256(raw_bytes).hexdigest()
        return f"{h}:{target.value}"

    def get(self, raw_bytes: bytes, target: RenderTarget) -> Optional[AssetView]:
        return self._views.get(self.key_for(raw_bytes, target))

    def put(self, raw_bytes: bytes, target: RenderTarget, view: AssetView) -> None:
        self._views[self.key_for(raw_bytes, target)] = view

    def clear(self) -> None:
        self._views.clear()


# Module-level default. Callers can pass their own AssetStore into
# AssetPipeline if they want isolation (e.g. tests).
_default_store: AssetStore = AssetStore()


def default_store() -> AssetStore:
    return _default_store
