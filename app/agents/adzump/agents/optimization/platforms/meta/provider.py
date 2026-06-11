"""Stub MetaPlatformProvider — capability boundary for Meta Ads.

Meta account discovery and Craft rendering are implemented. Live recommendation
generation is not yet implemented. This provider exists solely to:
  1. Make get_provider("META") return a non-None value everywhere.
  2. Surface a clear capability boundary (has_recommendations=False) so callers
     can return user-facing "not yet supported" messages instead of KeyErrors.

To add real Meta support:
  - Implement tools/meta/ (budget, conversion health, placement tools)
  - Implement calculate_fingerprint(), build_fields_from_headless_results(),
    build_fields_from_session_context(), and merge_fields() below
  - Set capabilities.has_recommendations / has_scheduler to True
  - Register a compute_signal fn in platform_registry.CONVERSION_SIGNALS["META"]
"""

from __future__ import annotations

from typing import Optional, Any

from app.core.tools.base import ToolDefinition
from app.agents.adzump.agents.optimization.provider_base import (
    PlatformCapabilities,
    PlatformProvider,
)
from app.agents.adzump.agents.optimization.models import (
    BaseOptimizationFields,
    CampaignOverview,
    MetaOptimizationFields,
)


class MetaPlatformProvider(PlatformProvider):
    """Stub provider — Meta account discovery only, recommendations not yet implemented."""

    @property
    def platform_name(self) -> str:
        return "META"

    @property
    def tools(self) -> list[ToolDefinition]:
        return []

    @property
    def system_instructions(self) -> str:
        return ""

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            has_recommendations=False,
            has_conversion_signal=False,
            has_scheduler=False,
        )

    @property
    def scheduler_tool_order(self) -> list[str]:
        return []

    def merge_fields(
        self,
        existing_fields: Optional[BaseOptimizationFields],
        new_fields: BaseOptimizationFields,
        run_id: str,
        campaign_id: str = "",
    ) -> BaseOptimizationFields:
        raise NotImplementedError(
            "MetaPlatformProvider does not support recommendation merging yet."
        )

    def build_fields_from_headless_results(
        self,
        tool_results: dict[str, Any],
        overview: Optional[CampaignOverview],
    ) -> MetaOptimizationFields:
        raise NotImplementedError(
            "MetaPlatformProvider does not support headless result building yet."
        )

    def build_fields_from_session_context(
        self,
        session_context: dict[str, Any],
    ) -> MetaOptimizationFields:
        raise NotImplementedError(
            "MetaPlatformProvider does not support session context extraction yet."
        )
