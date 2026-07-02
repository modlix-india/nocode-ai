"""LLM-callable tools for the LocationAgent's discover loop.

Two tools = the LLM's two decisions: which path (local scan vs broad markets)
and, for broad, which markets. Everything downstream (platform mapping,
persistence, craft re-render) is deterministic side effects inside the tools'
execute functions — see ``_shared.finalize_targets``.
"""

from app.agents.adzump.agents.location.tools.discover_neighborhoods import (
    discover_neighborhoods_tool,
)
from app.agents.adzump.agents.location.tools.geocode_recommendations import (
    geocode_recommendations_tool,
)

LOCATION_AGENT_TOOLS = [
    discover_neighborhoods_tool,
    geocode_recommendations_tool,
]
