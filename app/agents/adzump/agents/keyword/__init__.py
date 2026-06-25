"""Keyword research for Google Search campaign creation.

A deterministic pipeline (seed → expand → Planner gate → select → negatives) that
turns the user-confirmed business into campaign-ready positive + negative keywords
for brand and generic. Pure logic (constants, text, intent, profile) is
unit-testable without network or LLM; I/O lives in the shared adapters.
"""
