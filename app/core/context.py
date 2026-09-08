"""Base context builder for agent system prompts.

Loads static documentation files from disk, caches the concatenated text,
and builds Anthropic-format system prompt content blocks with prompt caching.

Architecture:
- Static docs (~100K tokens): loaded once, cached, marked with cache_control
  for Anthropic's prompt caching (~90% input token savings).
- Dynamic context (auth, app, tools summary): appended per-request.

Usage:
    context = BaseContext(
        doc_paths=["path/to/00-rules.md", "path/to/02-app-defs.md"],
        static_prefix="You are an expert application builder.",
    )
    await context.load()

    system_prompt = context.build_system_prompt(
        dynamic_context="Current app: myapp, client: ACME"
    )
    # Returns list of Anthropic content blocks with cache_control
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BaseContext:
    """Builds system prompts from static docs + dynamic context.

    Static docs are loaded once and cached for the process lifetime.
    The cached text is sent as a single content block with
    cache_control: {"type": "ephemeral"} so Anthropic caches it.
    """

    def __init__(
        self,
        doc_paths: list[str | Path] | None = None,
        static_prefix: str = "",
    ) -> None:
        """
        Args:
            doc_paths: File paths to static documentation (loaded once).
            static_prefix: Text prepended before the docs (persona, rules).
        """
        self._doc_paths = [Path(p) for p in (doc_paths or [])]
        self._static_prefix = static_prefix
        self._cached_static_text: str | None = None
        self._static_suffix: str = ""

    def set_static_suffix(self, text: str) -> None:
        """Register process-static text that only exists after construction.

        For context that is fixed for the process lifetime but is fetched at
        startup rather than read off disk in `load()` — the component catalog
        and the API catalog, which arrive from the CDN after this object is
        built. Without this seam the only place to put them is the per-request
        dynamic context, where they ride OUTSIDE the cached prefix and are
        re-sent in full on every turn of every conversation.

        Emitted as its OWN cache_control block rather than concatenated onto
        the docs, so republishing the catalog invalidates only its own suffix
        and leaves the (much larger, much more stable) persona + tool index
        cached.
        """
        self._static_suffix = text or ""

    async def load(self) -> None:
        """Load and cache static documentation from disk.

        Call once at startup (or lazily on first build_system_prompt).
        """
        parts: list[str] = []

        if self._static_prefix:
            parts.append(self._static_prefix)

        for path in self._doc_paths:
            try:
                text = path.read_text(encoding="utf-8")
                if text.strip():
                    parts.append(text)
                    logger.debug(f"Loaded doc: {path.name} ({len(text)} chars)")
            except FileNotFoundError:
                logger.warning(f"Doc not found: {path}")
            except Exception as e:
                logger.warning(f"Failed to load doc {path}: {e}")

        self._cached_static_text = "\n\n---\n\n".join(parts)
        logger.info(
            f"Context loaded: {len(self._doc_paths)} docs, "
            f"{len(self._cached_static_text)} chars"
        )

    def get_static_text(self) -> str:
        """Return the cached static docs text."""
        if self._cached_static_text is None:
            raise RuntimeError("Call load() before accessing static text")
        return self._cached_static_text

    def build_system_prompt(
        self,
        dynamic_context: str = "",
    ) -> list[dict[str, Any]]:
        """Build system prompt as Anthropic content blocks.

        Returns a list suitable for the `system` parameter in
        `client.messages.create(system=[...])`.

        Block 1: Static docs with cache_control (large, cached).
        Block 2: Static suffix with cache_control (startup-fetched, cached).
        Block 3: Dynamic context (small, changes per request).

        The two cached blocks MUST precede the dynamic one: providers cache a
        prefix, so anything that varies per session ends the cacheable run.

        Args:
            dynamic_context: Per-request context (auth info, app state, etc.)

        Returns:
            List of Anthropic content block dicts.
        """
        if self._cached_static_text is None:
            raise RuntimeError("Call load() before building system prompt")

        blocks: list[dict[str, Any]] = []

        # Block 1: Static docs — large and cacheable
        if self._cached_static_text:
            blocks.append({
                "type": "text",
                "text": self._cached_static_text,
                "cache_control": {"type": "ephemeral"},
            })

        # Block 2: Startup-fetched static context — also cacheable
        if self._static_suffix:
            blocks.append({
                "type": "text",
                "text": self._static_suffix,
                "cache_control": {"type": "ephemeral"},
            })

        # Block 3: Dynamic context — small, changes per session
        if dynamic_context:
            blocks.append({
                "type": "text",
                "text": dynamic_context,
            })

        return blocks
