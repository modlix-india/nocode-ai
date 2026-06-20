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
from typing import Any, Callable, Awaitable, Literal, Optional


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
    """Structured return value from a tool execution."""

    success: bool
    data: Any = None
    summary: str = ""
    error: str = ""
    # When True, the run loop also posts `summary` to chat as assistant text
    # (and persists it), de-duped against text the model already wrote this
    # turn. For tools whose result IS the user-facing message (manage_assets,
    # analyze_competitors); the LLM then writes only a lead-in.
    relay_summary: bool = False

    # Hard cap on tool result content sent to the LLM.
    # Prevents a single read from consuming excessive context.
    MAX_RESULT_CHARS: int = 4000

    def to_tool_result_content(self) -> str:
        """Format as text content for the tool_result message back to the LLM."""
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

        if len(text) > self.MAX_RESULT_CHARS:
            return text[:self.MAX_RESULT_CHARS] + "\n\n... [truncated — use more specific reads to see details]"
        return text


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
        builtin_spec: If set, the tool is provider-executed (e.g., Anthropic's
            server-side web_search or OpenAI's web_search_preview). The dict is
            passed through verbatim to the matching provider's tool list; other
            providers drop it. Must include a ``provider`` key ("anthropic" or
            "openai"). In this case ``execute`` should be None and
            ``parameters`` may be empty.
            Example: {"provider": "anthropic", "type": "web_search_20250305",
                      "name": "web_search", "max_uses": 10}
    """

    name: str
    description: str
    display_name: str = ""
    parameters: list[ToolParameter] = field(default_factory=list)
    execute: Optional[ToolExecuteFunc] = None
    builtin_spec: Optional[dict[str, Any]] = None

    # Elicitation primitive. A "tool" is silent compute; an "elicitation" asks
    # the user for input. The LLM cannot see this field (it never reaches the
    # Anthropic API — see to_anthropic_tool); it is a framework hint that drives
    # the run-loop's turn-boundary behavior.
    #   kind="elicitation" + elicit_mode="deferred"  → the loop breaks after the
    #       tool returns, yielding the turn to the user, who replies next turn.
    #   kind="elicitation" + elicit_mode="blocking"  → the tool itself awaits a
    #       Future mid-execution (e.g. a confirmation prompt); the loop is NOT
    #       broken (it already paused in-tool). Declarative only.
    #   elicit_expects="single" → one user reply closes it.
    #   elicit_expects="multi"  → the reply may span several messages (e.g. file
    #       uploads); stays open until the LLM moves on.
    # A tool that only elicits CONDITIONALLY keeps kind="tool" and signals at
    # runtime via ToolResult.data["elicited"]=True (+ optional "elicit_expects").
    kind: Literal["tool", "elicitation"] = "tool"
    elicit_mode: Literal["deferred", "blocking"] = "deferred"
    elicit_expects: Literal["single", "multi"] = "single"

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

        If this tool has a `builtin_spec`, a marker dict is returned instead —
        providers that understand it pass the spec through, others drop it.
        The ``provider`` key inside ``spec`` identifies the target provider.
        """
        if self.builtin_spec:
            return {
                "__builtin__": True,
                "name": self.name,
                "provider": self.builtin_spec.get("provider", ""),
                "spec": dict(self.builtin_spec),
            }

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
