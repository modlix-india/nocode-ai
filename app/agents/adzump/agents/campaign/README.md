# Campaign sub-agent — RESERVED (not implemented)

This directory is a **placeholder** for a future Campaign sub-agent. It contains no working code today.

## What it WILL do

Take a finalized `CampaignContext` from the main adzump agent and create an ad campaign on Google Ads or Meta:

- Create campaign + ad groups + ads via the respective platform API
- Upload creative images / videos (the assets already picked by the ProductAgent)
- Set budget, targeting (locations, demographics), bidding strategy
- Return the campaign ID + dashboard URL for the user to verify

## Sibling reference — the pattern to mirror

`agents/product/` is the implemented sub-agent. Mirror its shape:

- `agent.py` — `CampaignAgent(BaseAgent)` with its own session, tool registry, system prompt
- `context.py` — system prompt builder (persona + non-negotiable rules)
- `models.py` — Pydantic output models (e.g. `CampaignCreationOutput`)
- `prompts/` — domain prompts (one per platform if rules diverge)
- `tools/google/` and `tools/meta/` — platform-specific tools (`create_campaign`, `create_ad_group`, etc.)

## Why the empty subdirs already exist

`tools/google/__init__.py` and `tools/meta/__init__.py` exist so:
1. The eventual import path `app.agents.adzump.agents.campaign.tools.google` is reserved
2. Code searches / IDE navigation already know about the namespace
3. A flat `agents/` listing communicates that Campaign is on the roadmap (vs the surprise of someone adding `campaign/` later and breaking imports)

## When to delete this stub

When `agents/campaign/agent.py` is committed with a real `CampaignAgent` class, replace this README with real implementation docs and remove the "RESERVED" markers from `__init__.py`.
