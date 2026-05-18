from app.agents.adzump.agents.optimization.google.search_terms.tools.fetch_search_terms import (
    fetch_search_terms,
)
from app.agents.adzump.agents.optimization.google.search_terms.tools.analyze_term import (
    analyze_search_terms,
)

SEARCH_TERM_TOOLS = [fetch_search_terms, analyze_search_terms]

__all__ = ["SEARCH_TERM_TOOLS"]
