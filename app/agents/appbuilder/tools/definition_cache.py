"""DefinitionCache — LRU in-memory cache for definition metadata.

Tracks lightweight structural knowledge of what the agent has seen so it
can avoid redundant API calls.  Also stores section versions for optimistic
locking in section-level PATCH operations.

The cache is attached to each session and passed to tools via
``build_tool_context()``.

Design:
    - LRU eviction per collection (OrderedDict).
    - Invalidated on writes (specific entries only, not full cache).
    - Max 5 MB estimated memory budget (soft limit).
    - One cache instance per session.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PageStructure:
    """Cached lightweight page structure (component tree)."""

    page_name: str
    app_code: str
    page_id: str
    root_component: str
    component_keys: list[str]
    component_types: dict[str, str]  # key → type
    children: dict[str, list[str]]   # key → child keys
    event_function_names: list[str]
    component_count: int
    event_count: int
    version: int
    component_versions: dict[str, int] | None = None
    event_function_versions: dict[str, int] | None = None


@dataclass
class AppIndex:
    """Cached application index (lightweight manifest)."""

    app_code: str
    app_type: str  # "APP" or "SITE"
    pages: list[dict[str, Any]]
    functions: list[dict[str, Any]]
    schemas: list[dict[str, Any]]
    themes: list[dict[str, Any]]
    styles: list[dict[str, Any]]
    uripaths: list[dict[str, Any]]
    applications: list[dict[str, Any]]


class DefinitionCache:
    """LRU cache for definition metadata across a session."""

    def __init__(
        self,
        max_page_structures: int = 50,
        max_component_details: int = 200,
        max_event_details: int = 100,
        max_function_steps: int = 50,
        max_remote_functions: int = 100,
    ) -> None:
        self.app_indexes: dict[str, AppIndex] = {}
        self.page_structures: OrderedDict[str, PageStructure] = OrderedDict()
        # Per-component and per-event-function versions for pages
        self.component_versions: dict[str, dict[str, int]] = {}  # "appCode/pageName" → {compKey → version}
        self.event_function_versions: dict[str, dict[str, int]] = {}  # "appCode/pageName" → {eventName → version}
        self.component_details: OrderedDict[str, Any] = OrderedDict()
        self.event_details: OrderedDict[str, Any] = OrderedDict()
        self.function_steps: OrderedDict[str, Any] = OrderedDict()
        self.remote_functions: OrderedDict[str, Any] = OrderedDict()

        self._max_page_structures = max_page_structures
        self._max_component_details = max_component_details
        self._max_event_details = max_event_details
        self._max_function_steps = max_function_steps
        self._max_remote_functions = max_remote_functions

    # ── App Index ────────────────────────────────────────────────

    def set_app_index(self, app_code: str, index: AppIndex) -> None:
        self.app_indexes[app_code] = index

    def get_app_index(self, app_code: str) -> AppIndex | None:
        return self.app_indexes.get(app_code)

    def get_app_type(self, app_code: str) -> str | None:
        idx = self.app_indexes.get(app_code)
        return idx.app_type if idx else None

    # ── Page Structure ───────────────────────────────────────────

    def set_page_structure(self, key: str, structure: PageStructure) -> None:
        self.page_structures[key] = structure
        self.page_structures.move_to_end(key)
        while len(self.page_structures) > self._max_page_structures:
            self.page_structures.popitem(last=False)

    def get_page_structure(self, key: str) -> PageStructure | None:
        ps = self.page_structures.get(key)
        if ps:
            self.page_structures.move_to_end(key)
        return ps

    # ── Component Versions (per-component within a page) ──────────

    def set_component_versions(self, page_key: str, versions: dict[str, int]) -> None:
        """Track component versions for a page (e.g. 'myapp/home')."""
        self.component_versions[page_key] = versions

    def get_component_version(self, page_key: str, component_key: str) -> int | None:
        """Get a specific component's version, or None if not tracked."""
        cv = self.component_versions.get(page_key)
        return cv.get(component_key) if cv else None

    def increment_component_version(self, page_key: str, component_key: str) -> None:
        """Increment a component version after a successful PATCH."""
        cv = self.component_versions.setdefault(page_key, {})
        cv[component_key] = cv.get(component_key, 1) + 1

    # ── Event Function Versions (per-event within a page) ────────

    def set_event_function_versions(self, page_key: str, versions: dict[str, int]) -> None:
        """Track event function versions for a page."""
        self.event_function_versions[page_key] = versions

    def get_event_function_version(self, page_key: str, event_name: str) -> int | None:
        """Get a specific event function's version, or None if not tracked."""
        ev = self.event_function_versions.get(page_key)
        return ev.get(event_name) if ev else None

    def increment_event_function_version(self, page_key: str, event_name: str) -> None:
        """Increment an event function version after a successful PATCH."""
        ev = self.event_function_versions.setdefault(page_key, {})
        ev[event_name] = ev.get(event_name, 1) + 1

    # ── Component Details ────────────────────────────────────────

    def set_component(self, key: str, detail: Any) -> None:
        self.component_details[key] = detail
        self.component_details.move_to_end(key)
        while len(self.component_details) > self._max_component_details:
            self.component_details.popitem(last=False)

    def get_component(self, key: str) -> Any:
        detail = self.component_details.get(key)
        if detail is not None:
            self.component_details.move_to_end(key)
        return detail

    # ── Event Details ────────────────────────────────────────────

    def set_event(self, key: str, detail: Any) -> None:
        self.event_details[key] = detail
        self.event_details.move_to_end(key)
        while len(self.event_details) > self._max_event_details:
            self.event_details.popitem(last=False)

    def get_event(self, key: str) -> Any:
        detail = self.event_details.get(key)
        if detail is not None:
            self.event_details.move_to_end(key)
        return detail

    # ── Function Steps ───────────────────────────────────────────

    def set_function_steps(self, key: str, steps: Any) -> None:
        self.function_steps[key] = steps
        self.function_steps.move_to_end(key)
        while len(self.function_steps) > self._max_function_steps:
            self.function_steps.popitem(last=False)

    def get_function_steps(self, key: str) -> Any:
        detail = self.function_steps.get(key)
        if detail is not None:
            self.function_steps.move_to_end(key)
        return detail

    # ── Remote Functions ─────────────────────────────────────────

    def set_remote_function(self, key: str, signature: Any) -> None:
        self.remote_functions[key] = signature
        self.remote_functions.move_to_end(key)
        while len(self.remote_functions) > self._max_remote_functions:
            self.remote_functions.popitem(last=False)

    def get_remote_function(self, key: str) -> Any:
        sig = self.remote_functions.get(key)
        if sig is not None:
            self.remote_functions.move_to_end(key)
        return sig

    # ── Invalidation ─────────────────────────────────────────────

    def invalidate_page(self, page_key: str) -> None:
        """Invalidate all cached data for a page after a write."""
        self.page_structures.pop(page_key, None)
        # Remove component and event details for this page
        prefix = page_key + "/"
        to_remove = [k for k in self.component_details if k.startswith(prefix)]
        for k in to_remove:
            del self.component_details[k]
        to_remove = [k for k in self.event_details if k.startswith(prefix)]
        for k in to_remove:
            del self.event_details[k]

    def invalidate_function(self, function_key: str) -> None:
        """Invalidate cached function steps after a write."""
        self.function_steps.pop(function_key, None)

    def invalidate_app(self, app_code: str) -> None:
        """Invalidate the app index after an app-level change."""
        self.app_indexes.pop(app_code, None)

    # ── Summary for post-compaction re-injection ─────────────────

    def to_compact_summary(self) -> str:
        """Build a compact text summary of cached state for re-injection after compaction."""
        lines: list[str] = []

        for app_code, idx in self.app_indexes.items():
            lines.append(f"App '{app_code}' (type={idx.app_type}):")
            lines.append(f"  Pages: {', '.join(p.get('name', '?') for p in idx.pages)}")
            if idx.functions:
                lines.append(f"  Functions: {', '.join(f.get('name', '?') for f in idx.functions)}")
            if idx.themes:
                lines.append(f"  Themes: {', '.join(t.get('name', '?') for t in idx.themes)}")

        for key, ps in self.page_structures.items():
            lines.append(
                f"Page '{ps.page_name}': {ps.component_count} components, "
                f"{ps.event_count} events, root='{ps.root_component}'"
            )

        if self.component_versions:
            lines.append("Component versions:")
            for pk, cv in self.component_versions.items():
                lines.append(f"  {pk}: {len(cv)} tracked components")

        return "\n".join(lines) if lines else ""
