"""LLM-callable tools for the LocationAgent's loop.

Four tools = the LLM's whole action space: two discovery paths (local scan vs
broad markets) and two manual edits (add / delete one area). Everything
downstream (platform mapping, persistence, craft re-render) is deterministic
side effects inside the tools' execute functions - every mutation ends in
``_shared.finalize_targets``.
"""

from app.agents.adzump.agents.location.tools.discover_neighborhoods import (
    discover_neighborhoods_tool,
)
from app.agents.adzump.agents.location.tools.edit_locations import (
    add_location_tool,
    delete_location_tool,
)
from app.agents.adzump.agents.location.tools.geocode_recommendations import (
    geocode_recommendations_tool,
)

LOCATION_AGENT_TOOLS = [
    discover_neighborhoods_tool,
    geocode_recommendations_tool,
    add_location_tool,
    delete_location_tool,
]
