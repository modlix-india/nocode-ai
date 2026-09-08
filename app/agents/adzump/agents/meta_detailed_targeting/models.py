from typing import Any
import logging
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TargetingEntity(BaseModel):
    """Individual Meta targeting segment details."""

    id: str
    name: str = ""
    type: str = ""
    audience_size_lower_bound: int | None = None
    audience_size_upper_bound: int | None = None
    path: list[str] = Field(default_factory=list)
    description: str = ""

    @classmethod
    def from_meta(cls, item: dict) -> "TargetingEntity | None":
        """THE parser for a raw Graph API segment - the only place raw dicts become entities."""
        if not isinstance(item, dict) or not item.get("id"):
            logger.debug("Skipping non-dict or missing id entity: %r", item)
            return None
        try:
            return cls(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                type=str(item.get("type", "")),
                audience_size_lower_bound=item.get("audience_size_lower_bound"),
                audience_size_upper_bound=item.get("audience_size_upper_bound"),
                path=item.get("path") or [],
                description=item.get("description") or "",
            )
        except Exception as exc:
            logger.debug("Skipping malformed entity: %r (%s)", item, exc)
            return None

    def to_validation_pair(self) -> dict[str, str]:
        """THE {'type', 'id'} pair targetingvalidation expects - always the granular type."""
        return {"type": self.type, "id": self.id}

    @property
    def audience_size(self) -> int:
        return self.audience_size_upper_bound or 0


class MetaTargetingSuggestionResult(BaseModel):
    """Final targeting suggestions output."""

    entities: list[TargetingEntity] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, targeting: dict[str, Any] | None) -> "MetaTargetingSuggestionResult":
        """Parse raw entity list or dict into a valid MetaTargetingSuggestionResult."""
        if not targeting or not isinstance(targeting, dict):
            return cls(entities=[])
        all_raw = targeting.get("entities", [])
        entities: list[TargetingEntity] = []
        for item in all_raw:
            if isinstance(item, TargetingEntity):
                entities.append(item)
            elif isinstance(item, dict):
                parsed = TargetingEntity.from_meta(item)
                if parsed:
                    entities.append(parsed)
        return cls(entities=entities)


def resolve_ad_account_id(context: dict[str, Any] | None) -> str:
    """Resolve Meta ad account ID from direct context, parent context, or campaign_spec."""
    if not context or not isinstance(context, dict):
        return ""
    account_id = (context.get("ad_account_id") or "").strip()
    if account_id:
        return account_id

    # Check directly on context (when raw session_ctx is passed)
    account_id = ((context.get("campaign_spec") or {}).get("account") or "").strip()
    if account_id:
        return account_id

    parent_ctx = context.get("parent_session_context") or {}
    account_id = ((parent_ctx.get("campaign_spec") or {}).get("account") or "").strip()
    if account_id:
        return account_id

    session_ctx = context.get("session_context") or {}
    account_id = ((session_ctx.get("campaign_spec") or {}).get("account") or "").strip()
    return account_id
