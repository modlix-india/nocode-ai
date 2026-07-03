"""Reserved namespace for the Campaign sub-agent — NOT YET IMPLEMENTED.

This package will host the agent that takes a finalized product brief
(business + targeting + budget) from the main adzump agent and creates an
actual ad campaign on Google Ads / Meta via their respective APIs.

The empty `tools/google/` and `tools/meta/` directories preserve the
eventual import paths so a future implementation doesn't break callers
that may pre-reference them.

When implementation begins, mirror the `agents/product/` shape:
- `agent.py` — `CampaignAgent(BaseAgent)` with isolated session + tool registry
- `context.py` — system prompt builder (persona + non-negotiable rules)
- `models.py` — Pydantic output models (e.g. `CampaignCreationOutput`)
- `prompts/` — domain prompts per platform
- `tools/google/`, `tools/meta/` — platform-specific tools

See ./AGENT.md for scope details. Status: planned. No ETA committed.
"""
