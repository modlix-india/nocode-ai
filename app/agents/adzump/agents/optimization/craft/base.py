"""Platform renderer contract and registry.

``PlatformCraftRenderer`` defines the abstract interface every platform
renderer must implement.  ``PlatformCraftRegistry`` is a decorator-based
registry that maps platform strings to renderer instances.

Adding a new platform:
    1. Create a new file (e.g. ``tiktok.py``).
    2. Decorate the class:  ``@PlatformCraftRegistry.register("TIKTOK")``
    3. Import the module in ``__init__.py`` so the decorator executes.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Type

from app.agents.adzump.recommendations.models import CampaignRecommendation

logger = logging.getLogger(__name__)


class PlatformCraftRenderer(ABC):
    """Abstract base class for platform-specific recommendation renderers.

    Each subclass converts a ``CampaignRecommendation`` into a list of
    Craft blocks (dicts) that the frontend ``CraftRenderer.tsx`` consumes.
    """

    @abstractmethod
    def render_recommendation_blocks(self, rec: CampaignRecommendation) -> list[dict]:
        """Convert a recommendation bundle into visual Craft blocks.

        Must return a list of dicts, each conforming to a block type
        defined in ``builders.py``.  Implementations should use builder
        functions exclusively — never raw dict literals.
        """

    def render_compact_blocks(self, rec: CampaignRecommendation) -> list[dict]:
        """Compact variant for multi-campaign accordion views.

        Defaults to the full render.  Override in subclasses to produce
        a condensed layout suitable for accordion children.
        """
        return self.render_recommendation_blocks(rec)


class PlatformCraftRegistry:
    """Decorator-based registry mapping platform keys to renderer instances.

    Thread-safe for reads (populated at import time, never mutated after).
    """

    _registry: dict[str, PlatformCraftRenderer] = {}

    @classmethod
    def register(cls, platform: str):
        """Class decorator to register a renderer for a platform key.

        Usage::

            @PlatformCraftRegistry.register("GOOGLE")
            class GoogleCraftRenderer(PlatformCraftRenderer):
                ...
        """
        def decorator(renderer_cls: Type[PlatformCraftRenderer]):
            key = platform.upper()
            if key in cls._registry:
                logger.warning(
                    "PlatformCraftRegistry: overwriting existing renderer for %s", key
                )
            cls._registry[key] = renderer_cls()
            return renderer_cls
        return decorator

    @classmethod
    def get_renderer(cls, platform: str) -> PlatformCraftRenderer:
        """Retrieve the renderer for a platform string.

        Normalizes common variants (``FACEBOOK`` → ``META``) and falls
        back to ``GOOGLE`` if no exact match is found.  Raises
        ``RuntimeError`` if no renderers are registered at all.
        """
        plat_key = (platform or "GOOGLE").upper()

        # Normalize common aliases
        if "META" in plat_key or "FACEBOOK" in plat_key:
            plat_key = "META"
        elif "GOOGLE" in plat_key:
            plat_key = "GOOGLE"

        renderer = cls._registry.get(plat_key)
        if renderer:
            return renderer

        # Fallback chain: GOOGLE → first registered → error
        fallback = cls._registry.get("GOOGLE") or next(
            iter(cls._registry.values()), None
        )
        if not fallback:
            raise RuntimeError(
                "PlatformCraftRegistry: no renderers registered! "
                "Ensure platform modules (google.py, meta.py) are imported."
            )
        logger.warning(
            "PlatformCraftRegistry: no renderer for %r, falling back to %s",
            platform,
            type(fallback).__name__,
        )
        return fallback

    @classmethod
    def platforms(cls) -> list[str]:
        """Return list of registered platform keys (for diagnostics)."""
        return list(cls._registry.keys())
