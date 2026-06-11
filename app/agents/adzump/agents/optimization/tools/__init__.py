"""Optimization agent tools registry.

Aggregates all tools — platform-agnostic ones directly, platform-specific
ones from the google/ and meta/ subfolders.
"""

from app.agents.adzump.agents.optimization.tools.get_recommendations import (
    get_recommendations,
)
from app.agents.adzump.agents.optimization.tools.google.budget import (
    get_budget_bidding_recommendations,
)
from app.agents.adzump.agents.optimization.tools.google.keyword import (
    get_keyword_recommendations,
)
from app.agents.adzump.agents.optimization.tools.google.verify_conversion_health import (
    verify_conversion_health,
)

# OPTIMIZATION_TOOLS lists all active optimization agent tool instances.
# This serves as a centralized reference list and import bridge for developer utilities,
# unit tests, and inspection helpers (e.g. to introspect tool schemas).
OPTIMIZATION_TOOLS = [
    get_recommendations,
    get_keyword_recommendations,
    get_budget_bidding_recommendations,
    verify_conversion_health,
]

