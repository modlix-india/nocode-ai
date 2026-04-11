"""Base classes for agent tools.

ToolDefinition — declares a tool's name, description, parameters, and execute function.
ToolResult — structured return value from tool execution.
ToolParameter — declares a single parameter for a tool.

Usage:
    async def my_execute(params: dict, context: dict) -> ToolResult:
        return ToolResult(success=True, data={"key": "value"}, summary="Did the thing")

    tool = ToolDefinition(
        name="my_tool",
        description="Does a thing",
        parameters=[
            ToolParameter(name="input", type="string", description="The input", required=True),
        ],
        execute=my_execute,
    )

    # Convert to Anthropic API format
    anthropic_tool = tool.to_anthropic_tool()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable, Optional


class ResultTier(str, Enum):
    """Controls the maximum result size returned to the LLM."""

    COMPACT = "compact"      # 2,000 chars — tree structures, summaries, entity lists
    STANDARD = "standard"    # 6,000 chars — component reads, event reads, function DSL
    LARGE = "large"          # 12,000 chars — multi-component search, full section reads


RESULT_TIER_LIMITS: dict[ResultTier, int] = {
    ResultTier.COMPACT: 2_000,
    ResultTier.STANDARD: 6_000,
    ResultTier.LARGE: 12_000,
}


@dataclass(frozen=True)
class ToolParameter:
    """A single parameter for a tool definition."""

    name: str
    type: str  # "string", "integer", "number", "boolean", "array", "object"
    description: str
    required: bool = True
    enum: list[str] | None = None
    default: Any = None
    items: dict[str, Any] | None = None  # For array types: {"type": "string"}
    properties: dict[str, Any] | None = None  # For object types: nested schema

    def to_json_schema(self) -> dict[str, Any]:
        """Convert to JSON Schema property definition."""
        schema: dict[str, Any] = {
            "type": self.type,
            "description": self.description,
        }
        if self.enum is not None:
            schema["enum"] = self.enum
        if self.default is not None:
            schema["default"] = self.default
        if self.items is not None:
            schema["items"] = self.items
        if self.properties is not None:
            schema["properties"] = self.properties
        return schema


@dataclass
class ToolResult:
    """Structured return value from a tool execution.

    The ``result_tier`` field controls the maximum character limit sent to
    the LLM.  Tools should set an appropriate tier when constructing a
    result so that compact data (trees, summaries) isn't capped at the same
    size as large search results.

    When the formatted text exceeds the tier limit the content is stored in
    the session's ``ResultStore`` (if one is attached to the context) and a
    short reference is returned instead, allowing the LLM to page through
    the full result via the ``read_result`` tool.
    """

    success: bool
    data: Any = None
    summary: str = ""
    error: str = ""
    result_tier: ResultTier = ResultTier.STANDARD

    # Legacy flat cap kept for backward compatibility — callers that set
    # this directly override tier-based limits.
    _max_override: int | None = None

    @property
    def _char_limit(self) -> int:
        if self._max_override is not None:
            return self._max_override
        return RESULT_TIER_LIMITS.get(self.result_tier, 6_000)

    def to_tool_result_content(self, result_store: Any = None) -> str:
        """Format as text content for the tool_result message back to the LLM.

        Args:
            result_store: Optional ``ResultStore`` instance.  When provided
                and the text exceeds the tier limit the full content is
                persisted and a reference is returned.
        """
        if not self.success:
            return f"Error: {self.error}"
        if self.summary:
            text = self.summary
        elif self.data is not None:
            import json
            try:
                text = json.dumps(self.data, indent=2, default=str)
            except (TypeError, ValueError):
                text = str(self.data)
        else:
            return "OK"

        limit = self._char_limit
        if len(text) <= limit:
            return text

        # If a result store is available, persist the full text and return a
        # compact reference the LLM can page through.
        if result_store is not None:
            result_id = result_store.store(text)
            return (
                text[:limit]
                + f"\n\n... [truncated — full result stored as result_id={result_id}. "
                + "Use read_result tool to page through it.]"
            )

        return text[:limit] + "\n\n... [truncated — use more specific reads to see details]"


# Type alias for tool execute functions.
# Signature: (params: dict, context: dict) -> ToolResult
# - params: the input parameters from the LLM
# - context: session context (auth headers, app_code, client_code, etc.)
ToolExecuteFunc = Callable[[dict[str, Any], dict[str, Any]], Awaitable[ToolResult]]


@dataclass
class ToolDefinition:
    """Declares a tool that an agent can use.

    Attributes:
        name: Unique tool name (snake_case, e.g. "add_component").
        display_name: Human-friendly name for UI display (e.g. "Add Component").
            Auto-generated from name if not provided.
        description: Human-readable description shown to the LLM.
        parameters: List of ToolParameter definitions.
        execute: Async function that runs the tool.
        is_deferred: When True, tool schema is NOT included in the initial
            prompt.  The LLM must discover it via ToolSearchTool before it
            can call it.  Reduces initial prompt token cost.
        search_hint: Short phrase (3-10 words) used by ToolSearchTool for
            keyword matching when the tool is deferred.  Should contain
            terms NOT already in the tool name.
        result_tier: Default ResultTier for results returned by this tool.
            Individual ToolResult instances can override this.
    """

    name: str
    description: str
    display_name: str = ""
    parameters: list[ToolParameter] = field(default_factory=list)
    execute: Optional[ToolExecuteFunc] = None
    is_deferred: bool = False
    search_hint: str = ""
    result_tier: ResultTier = ResultTier.STANDARD

    def get_display_name(self) -> str:
        """Return display_name, falling back to title-cased name."""
        if self.display_name:
            return self.display_name
        return self.name.replace("_", " ").title()

    def to_anthropic_tool(self) -> dict[str, Any]:
        """Convert to Anthropic tool-use API format.

        Returns a dict suitable for the `tools` parameter in
        `client.messages.create(tools=[...])`.

        Format:
        {
            "name": "tool_name",
            "description": "...",
            "input_schema": {
                "type": "object",
                "properties": { ... },
                "required": [ ... ]
            }
        }
        """
        properties: dict[str, Any] = {}
        required: list[str] = []

        for param in self.parameters:
            properties[param.name] = param.to_json_schema()
            if param.required:
                required.append(param.name)

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required

        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }
