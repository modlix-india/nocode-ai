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
        if not isinstance(item, dict):
            logger.debug("Skipping non-dict entity: %r", item)
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
