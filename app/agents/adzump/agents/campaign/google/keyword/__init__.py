"""Keyword research for Google Search campaign creation.

An agentic ReAct loop (KeywordResearchAgent) that turns the user-confirmed business into
campaign-ready positive + negative keywords — one ad group per theme the user chose (see
themes.py). The agent reasons over real Keyword Planner data as it drives the phases
(seed → expand → score → select → negatives).
The tool layer applies deterministic safety gates (candidate membership, length, overlap,
cross-business → PHRASE); judgment stays in the agent. Pure helpers are unit-testable without
network or LLM; I/O lives in the shared adapters.
"""
