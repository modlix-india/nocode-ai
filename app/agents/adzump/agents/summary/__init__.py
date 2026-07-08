"""SummaryAgent - minimal one-shot BaseAgent wrapping the gpt-4o profile-summary call.

Replaces the direct ``openai.chat.completions.create(...)`` call in
``agents/product/tools/scrape/profile.py`` with a properly-named agent so it
shows up in trace/cost/observability surfaces alongside ProductAgent.
"""

from app.agents.adzump.agents.summary.agent import (
    SummaryAgent,
    get_summary_agent,
)

__all__ = ["SummaryAgent", "get_summary_agent"]
