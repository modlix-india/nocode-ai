# Optimization sub-agent — RESERVED (not implemented)

This directory is a **placeholder** for a future Optimization sub-agent. It contains no working code today.

## What it WILL do

Monitor a live campaign's performance and recommend/apply tuning decisions:

- **Budget reallocation** — shift spend from underperforming ad groups to high-converters
- **Bid adjustments** — by device, time-of-day, location, audience segment
- **Audience refinement** — narrow targeting based on conversion data
- **Negative-keyword updates** — add poor-performing search terms to the exclusion list (Google Ads)
- **Creative rotation** — pause low-CTR creatives, promote high-performers
- **Anomaly detection** — flag sudden drops in performance for human review

## Inputs it expects (when built)

- A live `campaign_id` (output from the Campaign sub-agent)
- Performance metrics pulled from Google Ads / Meta Insights APIs
- Time window (last 24h / 7d / 30d)
- Optimization mode (conservative auto-apply vs review-first)

## Sibling reference — the pattern to mirror

`agents/product/` is the implemented sub-agent. Same shape:

- `agent.py` — `OptimizationAgent(BaseAgent)` with isolated session
- `context.py` — system prompt (cautious tone — this agent spends money)
- `models.py` — Pydantic models for recommendation outputs
- `prompts/` — domain prompts per platform / metric class
- `tools/` — fetch_performance, propose_budget_change, apply_negative_keywords, etc.

## When to delete this stub

When `agents/optimization/agent.py` is committed with a real `OptimizationAgent`, replace this README with implementation docs and remove the "RESERVED" markers from `__init__.py`.
