"""Platform registry for OptimizationAgent tools.

The OptimizationAgent is the orchestrator. Platform packages provide the
implementation for each logical optimization capability.
"""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition
from app.agents.adzump.recommendations.models import Platform
from app.agents.adzump.agents.optimization.tools.get_recommendations import (
    get_recommendations,
)
from app.agents.adzump.agents.optimization.provider_base import PlatformProvider
from app.agents.adzump.agents.optimization.platforms.google import (
    GooglePlatformProvider,
)
from app.agents.adzump.agents.optimization.platforms.meta import (
    MetaPlatformProvider,
)
from app.agents.adzump.agents.optimization.tools.google.conversion_signal import (
    compute_signal as google_compute_signal,
)

logger = logging.getLogger(__name__)


# Platform Providers & Conversion Signals mapping

PROVIDERS: dict[str, PlatformProvider] = {
    "GOOGLE": GooglePlatformProvider(),
    "META": MetaPlatformProvider(),
}

CONVERSION_SIGNALS = {
    "GOOGLE": google_compute_signal,
}


# Registry Constants

SHARED_TOOLS = [get_recommendations]
REGISTERED_PLATFORMS = ("GOOGLE", "META")


# Registry Helpers & Resolvers


def normalize_platform(platform: str | None) -> str | None:
    """Normalize user/storage platform names to registry keys (the string form
    PROVIDERS/storage are keyed by). Delegates to ``Platform.coerce`` — the single
    source of the matching rules. Returns None when missing or unrecognized.
    """
    matched = Platform.coerce(platform)
    return matched.value if matched else None


def get_provider(platform: str | None) -> PlatformProvider | None:
    normalized = normalize_platform(platform)
    if not normalized:
        return None
    return PROVIDERS.get(normalized)


def get_platform_tools(platform: str | None) -> tuple[dict[str, ToolDefinition], bool]:
    """Return platform-specific tools and whether the platform is implemented."""
    provider = get_provider(platform)  # get_provider normalizes internally
    if provider:
        tools = {t.name: t for t in provider.tools}
        return tools, True
    else:
        return {}, False


def build_optimization_tool_map(platform: str | None) -> dict[str, ToolDefinition]:
    """Build request-scoped tool map: shared tools plus platform tools."""
    platform_tools, _ = get_platform_tools(platform)
    tools = {t.name: t for t in SHARED_TOOLS}
    tools.update(platform_tools)
    return tools


def is_platform_implemented(platform: str | None) -> bool:
    """True if the optimization package has a real implementation."""
    provider = get_provider(platform)
    return provider is not None


def get_platform_instructions(platform: str | None) -> str:
    """Return the platform-specific instructions for the system prompt.

    This allows context.py to remain platform-agnostic while still
    injecting specific diagnostic rules (like Google's GAQL health checks).
    """
    provider = get_provider(platform)
    if provider:
        return provider.system_instructions
    return ""


def get_platform_conversion_signal_fn(platform: str | None):
    """Return the ``compute_signal`` async function for the platform, or None.

    The bridge (tools/optimize.py) uses this to pre-fetch the conversion
    signal without exposing it as an LLM-callable tool.
    """
    normalized = normalize_platform(platform)
    if not normalized:
        return None
    return CONVERSION_SIGNALS.get(normalized)


def validate_platform_tools() -> list[str]:
    """Import all registered platform packages and report errors.

    Returns a list of error messages (empty if all OK). Call this at
    startup to catch missing dependencies / syntax errors early.
    """
    errors: list[str] = []
    for candidate in REGISTERED_PLATFORMS:
        try:
            get_platform_tools(candidate)
        except Exception as exc:
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
    return errors
