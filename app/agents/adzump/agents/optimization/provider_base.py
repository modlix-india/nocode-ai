from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional, Any
from pydantic import BaseModel
from app.core.tools.base import ToolDefinition
from app.agents.adzump.agents.optimization.models import (
    BaseOptimizationFields,
    CampaignOverview,
    RecommendationStatus,
    WorkflowItem,
)

class PlatformCapabilities(BaseModel):
    has_recommendations: bool = False
    has_conversion_signal: bool = False
    has_scheduler: bool = False

class PlatformProvider(ABC):
    @property
    @abstractmethod
    def platform_name(self) -> str:
        pass

    @property
    @abstractmethod
    def tools(self) -> list[ToolDefinition]:
        pass

    @property
    @abstractmethod
    def system_instructions(self) -> str:
        pass

    @property
    @abstractmethod
    def capabilities(self) -> PlatformCapabilities:
        pass

    @property
    @abstractmethod
    def scheduler_tool_order(self) -> list[str]:
        pass

    @abstractmethod
    def merge_fields(
        self,
        existing_fields: Optional[BaseOptimizationFields],
        new_fields: BaseOptimizationFields,
        run_id: str,
        campaign_id: str = "",
    ) -> BaseOptimizationFields:
        pass

    @abstractmethod
    def build_fields_from_headless_results(
        self,
        tool_results: dict[str, Any],
        overview: Optional[CampaignOverview],
    ) -> BaseOptimizationFields:
        pass

    @abstractmethod
    def build_fields_from_session_context(
        self,
        session_context: dict[str, Any],
    ) -> BaseOptimizationFields:
        pass

    def summarize_fields(self, fields: BaseOptimizationFields) -> list[str]:
        """Return human-readable summary lines for a stored recommendation's fields.

        Default implementation reflects over WorkflowItem lists generically.
        Platform providers should override this to produce richer, platform-specific
        summaries (e.g. Google's budget/bidding/keywords/conversion health).
        """
        lines: list[str] = []
        for field_name in fields.model_fields:
            if field_name == "overview":
                continue
            val = getattr(fields, field_name, None)
            if isinstance(val, list) and val:
                lines.append(f"{field_name.replace('_', ' ').title()}: {len(val)} items")
            elif val is not None and not isinstance(val, (str, int, float, bool)):
                lines.append(f"{field_name.replace('_', ' ').title()}: available")
        return lines

    def calculate_fingerprint(self, item: Any, campaign_id: str) -> str:
        """Delegate to the item's own compute_fingerprint method.

        Providers may override this for custom dispatch, but the preferred
        approach is to implement compute_fingerprint on each WorkflowItem subclass.
        """
        if isinstance(item, WorkflowItem):
            return item.compute_fingerprint(campaign_id)
        return ""

    def populate_fingerprints(self, fields: BaseOptimizationFields, campaign_id: str) -> None:
        """Assign fingerprints to all WorkflowItem instances in fields."""
        def _populate(obj: Any) -> None:
            if isinstance(obj, list):
                for item in obj:
                    _populate(item)
            elif isinstance(obj, WorkflowItem):
                fp = obj.compute_fingerprint(campaign_id)
                if fp:
                    obj.fingerprint = fp
            elif isinstance(obj, BaseModel):
                for field_name in obj.model_fields:
                    val = getattr(obj, field_name)
                    if val is not None:
                        _populate(val)

        for field_name in fields.model_fields:
            if field_name == "overview":
                continue
            val = getattr(fields, field_name)
            if val is not None:
                _populate(val)

    def has_actionable_recommendations(self, fields: BaseOptimizationFields) -> bool:
        """Return True if fields contain at least one WorkflowItem."""
        def _check(obj: Any) -> bool:
            if isinstance(obj, list):
                return any(_check(item) for item in obj)
            if isinstance(obj, WorkflowItem):
                return True
            if isinstance(obj, BaseModel):
                for field_name in obj.model_fields:
                    val = getattr(obj, field_name)
                    if val is not None and _check(val):
                        return True
            return False

        for field_name in fields.model_fields:
            if field_name == "overview":
                continue
            val = getattr(fields, field_name)
            if val is not None and _check(val):
                return True
        return False


def generic_merge_fields(
    provider: PlatformProvider,
    existing: Optional[BaseOptimizationFields],
    new: BaseOptimizationFields,
    campaign_id: str,
) -> BaseOptimizationFields:
    """Generically and recursively merges two optimization fields instances."""
    if not existing or type(existing) is not type(new):
        provider.populate_fingerprints(new, campaign_id)
        return new

    # Ensure fingerprints are populated on both
    provider.populate_fingerprints(existing, campaign_id)
    provider.populate_fingerprints(new, campaign_id)

    def _merge_value(ex_val: Any, new_val: Any) -> Any:
        if new_val is None:
            return None
        if ex_val is None:
            return new_val

        # Case 1: list of items
        if isinstance(new_val, list):
            if not isinstance(ex_val, list):
                return new_val
            
            # Check if elements in either list carry fingerprints (workflow items)
            has_wf = any(hasattr(item, "fingerprint") and getattr(item, "fingerprint") for item in new_val) or \
                     any(hasattr(item, "fingerprint") and getattr(item, "fingerprint") for item in ex_val)
            
            if has_wf:
                # Group existing items by fingerprint
                existing_map = {}
                for item in ex_val:
                    fp = getattr(item, "fingerprint", None)
                    if fp:
                        existing_map[fp] = item
                
                merged_list = []
                merged_fps = set()
                
                for item in new_val:
                    fp = getattr(item, "fingerprint", None)
                    if fp and fp in existing_map:
                        ex_item = existing_map[fp]
                        # Copy workflow state
                        for field in ["status", "applied", "reviewed_at", "reviewed_by", "applied_at", "failure_reason"]:
                            if hasattr(item, field) and hasattr(ex_item, field):
                                setattr(item, field, getattr(ex_item, field))
                        merged_fps.add(fp)
                    merged_list.append(item)
                
                # Add back missing existing items as superseded
                for fp, ex_item in existing_map.items():
                    if fp not in merged_fps:
                        if hasattr(ex_item, "status") and getattr(ex_item, "status") == RecommendationStatus.PENDING:
                            setattr(ex_item, "status", RecommendationStatus.SUPERSEDED)
                        if hasattr(ex_item, "applied"):
                            setattr(ex_item, "applied", False)
                        merged_list.append(ex_item)
                return merged_list
            else:
                return new_val

        # Case 2: nested BaseModel
        if isinstance(new_val, BaseModel):
            if not isinstance(ex_val, BaseModel):
                return new_val
            
            # If the model itself is a workflow item (e.g. BudgetBiddingRecommendation)
            if hasattr(new_val, "fingerprint") and getattr(new_val, "fingerprint"):
                new_fp = getattr(new_val, "fingerprint")
                ex_fp = getattr(ex_val, "fingerprint", None)
                if new_fp == ex_fp:
                    for field in ["status", "applied", "reviewed_at", "reviewed_by", "applied_at", "failure_reason"]:
                        if hasattr(new_val, field) and hasattr(ex_val, field):
                            setattr(new_val, field, getattr(ex_val, field))
                return new_val

            # Recursively merge child attributes
            merged_data = {}
            for field_name in new_val.model_fields:
                nv = getattr(new_val, field_name)
                ev = getattr(ex_val, field_name, None)
                merged_data[field_name] = _merge_value(ev, nv)
            return type(new_val)(**merged_data)

        # Case 3: scalar or other types
        return new_val

    # Merge top level fields
    merged_fields_data = {}
    for field_name in new.model_fields:
        nv = getattr(new, field_name)
        ev = getattr(existing, field_name, None)
        merged_fields_data[field_name] = _merge_value(ev, nv)

    return type(new)(**merged_fields_data)
