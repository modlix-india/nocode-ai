"""AssetAdapter protocol + registry.

Each adapter handles a MIME family (e.g. raster → png/jpeg/webp,
svg → svg+xml). Pipeline dispatches by canonical MIME after content-type
normalization.
"""

from __future__ import annotations

from typing import Protocol

from app.core.assets.refs import AssetRef
from app.core.assets.views import AssetView, RenderTarget


class AssetAdapter(Protocol):
    """One adapter per MIME family. `prepare` is the only contract."""

    async def prepare(self, ref: AssetRef, target: RenderTarget) -> AssetView:
        """Resolve `ref` to an `AssetView` for the given `target`.

        Implementations may raise; pipeline catches and adds to `dropped`.
        """
        ...


# MIME → AssetAdapter. Adapters register at module import.
_REGISTRY: dict[str, AssetAdapter] = {}


def register_adapter(mime: str, adapter: AssetAdapter) -> None:
    """Register `adapter` for `mime`. Last writer wins (intentional — lets
    callers override with a test adapter or a Phase-2 implementation)."""
    _REGISTRY[mime] = adapter


def get_adapter(mime: str) -> AssetAdapter | None:
    return _REGISTRY.get(mime)


def known_mimes() -> set[str]:
    return set(_REGISTRY.keys())
