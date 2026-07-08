"""Reserved namespace for the Optimization sub-agent - NOT YET IMPLEMENTED.

This package will host the agent that monitors a live ad campaign's
performance and recommends / applies tuning decisions (budget shifts,
bid adjustments, audience refinement, negative-keyword updates, creative
rotation, anomaly flags).

Currently this directory contains no working code. The folder is kept
in tree so the eventual import path `app.agents.adzump.agents.optimization`
is reserved and code-search / IDE navigation already know about it.

When implementation begins, mirror the `agents/product/` shape - see
the Campaign sub-agent's __init__.py for the same pattern.

Tone for this agent matters: it spends real money on the user's behalf,
so its system prompt should default to review-first, not auto-apply.

See ./README.md for scope details. Status: planned. No ETA committed.
"""
