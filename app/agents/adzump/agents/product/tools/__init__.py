"""Product agent tools — scraping, web search, web fetch, competitor discovery."""

from app.core.tools.base import ToolDefinition

from app.agents.adzump.agents.product.tools.scrape import scrape_url
from app.agents.adzump.agents.product.tools.comp_discovery import shortlist_competitors
from app.agents.adzump.tools._shared import AGGREGATOR_HOSTS


# Anthropic's server-executed web search. The agent declares it as a tool;
# Anthropic runs each query server-side and streams back server_tool_use +
# web_search_tool_result blocks. Our custom providers pass the spec through
# verbatim — see AnthropicProvider._convert_tools in app/services/llm_provider.py.
anthropic_web_search = ToolDefinition(
    name="web_search",
    description=(
        "Search the public web. Runs server-side — issue ONE focused query "
        "per call. You MUST search at least 5 times with different angles "
        "(product format, location, price tier, category) before calling "
        "shortlist_competitors."
    ),
    display_name="Web Search",
    parameters=[],
    execute=None,
    builtin_spec={
        "provider": "anthropic",
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 10,
    },
)


# Anthropic's server-executed web fetch. Complements scrape_url — fast text
# fetch without JS rendering or a screenshot. Used to pull a raw-HTML view
# of the primary business site (JSON-LD / og: tags / noscript fallbacks the
# rendered DOM may hide) and to verify competitor URLs surfaced by web_search.
# Claude can only fetch URLs that appeared in prior search/fetch results or
# the user's initial message — the primary URL qualifies; fabricated URLs
# don't. Activates via the ``web-fetch-2025-09-10`` beta header set in
# AnthropicProvider when this spec is in the tool list.
anthropic_web_fetch = ToolDefinition(
    name="web_fetch",
    description=(
        "Fetch a URL and read its full text content. Server-executed — fast "
        "and lightweight, no screenshot. Use it: (a) to get a raw-HTML view "
        "of the primary business URL after scrape_url, so the final summary "
        "uses both the rendered DOM and server-side HTML signals (JSON-LD, "
        "og: tags, meta, noscript); (b) to verify a specific competitor URL "
        "from web_search results. If web_fetch errors (Cloudflare / anti-bot), "
        "fall back to scrape_url on that URL."
    ),
    display_name="Web Fetch",
    parameters=[],
    execute=None,
    builtin_spec={
        "provider": "anthropic",
        "type": "web_fetch_20250910",
        "name": "web_fetch",
        "max_uses": 10,
        "citations": {"enabled": True},
        "max_content_tokens": 4000,
        "blocked_domains": sorted(AGGREGATOR_HOSTS),
    },
)


PRODUCT_TOOLS = [
    scrape_url,
    anthropic_web_search,
    anthropic_web_fetch,
    shortlist_competitors,
]
