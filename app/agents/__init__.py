"""Multi-agent system for page generation"""
from app.agents.base import BaseAgent, AgentInput, AgentOutput
from app.agents.page_agent import (
    PageAgent,
    PageAgentRequest,
    PageAgentResponse,
    PageAgentMode,
    PageAgentOptions,
)

# Also expose page_generation submodule
from app.agents import page_generation

__all__ = [
    "BaseAgent",
    "AgentInput",
    "AgentOutput",
    "PageAgent",
    "PageAgentRequest",
    "PageAgentResponse",
    "PageAgentMode",
    "PageAgentOptions",
    "page_generation",
]

