"""keyword_update — keyword panel widget parser + mutation handler.

The keyword review panel sends structured actions as pure JSON:
  {"type": "keyword_widget", "action": "add|delete|edit", "keyword_type": "brand|generic",
   "section": "positives|negatives", "keyword": "...", "match_type": "PHRASE|EXACT",
   "volume": 1200, "intent": "transactional"}   (intent/reason differ by section)
   "old_keyword": "..."  (edit only)

parse_keyword_widget_message() detects these and returns the payload — the router
bypasses the LLM and calls _update_keywords() directly, which mutates
session_ctx["keyword_research"] and re-emits only the keyword_review block (keyed
upsert, no panel flash).
"""

from __future__ import annotations

import difflib
import json as _json
import logging
from typing import Any

from app.core.tools.base import ToolResult

from app.agents.adzump.agents.campaign.craft import emit_section_update, keyword_review_block
from app.agents.adzump.agents.keyword.constants import (
    KEYWORD_MAX_LENGTH,
    KEYWORD_MAX_WORDS,
    KEYWORD_MIN_LENGTH,
)
from app.agents.adzump.agents.keyword.models import normalize as _normalize

logger = logging.getLogger(__name__)


def parse_keyword_widget_message(msg: str) -> dict[str, Any] | None:
    """Return the decoded payload if msg is a keyword widget JSON action, else None."""
    stripped = msg.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        payload = _json.loads(stripped)
    except _json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload.get("type") == "keyword_widget":
        return payload
    return None

_VALID_ACTIONS = frozenset({"add", "delete", "edit"})
_VALID_KEYWORD_TYPES = frozenset({"brand", "generic"})
_VALID_SECTIONS = frozenset({"positives", "negatives"})
_VALID_MATCH_TYPES = frozenset({"EXACT", "PHRASE"})


def _row_key(row: dict) -> str:
    return _normalize(row.get("keyword", ""))


def _validate_keyword(kw: str) -> str | None:
    """Return an error string if invalid, else None."""
    if len(kw) < KEYWORD_MIN_LENGTH:
        return f"Keyword is too short (minimum {KEYWORD_MIN_LENGTH} characters)."
    if len(kw) > KEYWORD_MAX_LENGTH:
        return f"Keyword exceeds the {KEYWORD_MAX_LENGTH}-character Google Ads limit."
    if len(kw.split()) > KEYWORD_MAX_WORDS:
        return f"Keyword exceeds the {KEYWORD_MAX_WORDS}-word Google Ads limit."
    return None


def _coerce_match_type(raw: object, fallback: str = "PHRASE") -> str:
    mt = str(raw or "").upper()
    return mt if mt in _VALID_MATCH_TYPES else fallback


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_normalize(text).split())


_BRAND_FUZZY_RATIO = 0.8  # token similarity that still counts as the brand (catches misspellings)


def _is_brandish(token: str, brand_tokens: frozenset[str]) -> bool:
    """True if a keyword token is a brand token or a near-miss of one (e.g. a misspelling)."""
    return any(
        difflib.SequenceMatcher(None, token, b).ratio() >= _BRAND_FUZZY_RATIO
        for b in brand_tokens
    )


def _check_section_signal(keyword: str, keyword_type: str, session_ctx: dict) -> str | None:
    """Return an error if the keyword clearly belongs in the other type's positives."""
    kw_tokens = _tokens(keyword)
    if not kw_tokens:
        return None

    product_name = str((session_ctx.get("product_data") or {}).get("product_name") or "")
    dump = session_ctx.get("keyword_research") or {}

    if keyword_type == "generic":
        # Block when ALL brand-name tokens are present — partial overlap is category noise.
        # Fuzzy (like the brand direction) so a misspelled brand ("dulingo") is caught too.
        brand_tokens = _tokens(product_name)
        if product_name and brand_tokens and all(_is_brandish(bt, kw_tokens) for bt in brand_tokens):
            return (
                f"'{keyword}' contains the full brand name — "
                "brand-specific keywords belong in the brand section."
            )
        # Same all-tokens rule against existing brand positives.
        for row in list((dump.get("brand") or {}).get("positives") or []):
            row_tokens = _tokens(row.get("keyword", ""))
            if row_tokens and row_tokens.issubset(kw_tokens):
                return (
                    f"'{keyword}' contains a brand keyword — "
                    "brand-specific keywords belong in the brand section."
                )

    else:  # brand
        if product_name:
            # Fuzzy match so deliberate brand misspellings (a real brand keyword) still pass.
            brand_tokens = _tokens(product_name)
            if not any(_is_brandish(t, brand_tokens) for t in kw_tokens):
                return (
                    f"'{keyword}' doesn't contain any brand name terms — "
                    "generic keywords belong in the generic section."
                )
        else:
            for row in list((dump.get("generic") or {}).get("positives") or []):
                row_tokens = _tokens(row.get("keyword", ""))
                if row_tokens and row_tokens.issubset(kw_tokens):
                    return (
                        f"'{keyword}' contains a generic keyword — "
                        "generic keywords belong in the generic section."
                    )

    return None


def _apply_row_action(
    action: str, rows: list[dict], params: dict, value_field: str, owner_label: str
) -> tuple[list[dict], str] | ToolResult:
    """Add/delete/edit one keyword within `rows` — normalize, validate, dedupe
    within THIS list, then mutate. Shared by brand/generic and competitor
    updates; callers needing cross-list checks (opposite section, other type,
    content heuristics) must run them before calling this for add/edit.
    `owner_label` names the list in error/summary text.
    """
    if action == "add":
        keyword = _normalize(str(params.get("keyword", "")))
        err = _validate_keyword(keyword)
        if err:
            return ToolResult(success=False, error=err)
        if any(_row_key(r) == keyword for r in rows):
            return ToolResult(success=False, error=f"'{keyword}' already exists in {owner_label}.")
        new_row: dict = {
            "keyword": keyword,
            "volume": int(params.get("volume", 0) or 0),
            "match_type": _coerce_match_type(params.get("match_type")),
            value_field: str(params.get(value_field, "") or ""),
        }
        return [new_row, *rows], f"Added '{keyword}' to {owner_label}."  # newest on top

    if action == "delete":
        keyword = _normalize(str(params.get("keyword", "")))
        updated = [r for r in rows if _row_key(r) != keyword]
        if len(updated) == len(rows):
            return ToolResult(success=False, error=f"'{keyword}' not found in {owner_label}.")
        return updated, f"Removed '{keyword}' from {owner_label}."

    # edit
    old_keyword = _normalize(str(params.get("old_keyword", "")))
    new_keyword = _normalize(str(params.get("keyword", old_keyword)))
    err = _validate_keyword(new_keyword)
    if err:
        return ToolResult(success=False, error=err)
    target = next((r for r in rows if _row_key(r) == old_keyword), None)
    if target is None:
        return ToolResult(success=False, error=f"'{old_keyword}' not found in {owner_label}.")
    if new_keyword != old_keyword and any(_row_key(r) == new_keyword for r in rows if r is not target):
        return ToolResult(success=False, error=f"'{new_keyword}' already exists in {owner_label}.")
    target["keyword"] = new_keyword
    target["match_type"] = _coerce_match_type(params.get("match_type"), target.get("match_type", "PHRASE"))
    target["volume"] = int(params.get("volume", target.get("volume", 0)) or 0)
    target[value_field] = str(params.get(value_field, target.get(value_field, "")) or "")
    return rows, f"Updated '{old_keyword}' in {owner_label}."


async def _emit_review_update(session_ctx: dict, context: dict) -> None:
    """Keyed re-emit of the keyword_review block after a mutation (both keyword
    types render into the same block, so rebuild it from both dicts)."""
    craft_id = session_ctx.get("campaign_craft_id") or f"campaign_{context.get('session_id', '')}"
    block = keyword_review_block(
        session_ctx.get("keyword_research") or {},
        session_ctx.get("competitor_keywords"),
    )
    await emit_section_update(context.get("event_stream"), craft_id, block)


async def _update_keywords(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    action = str(params.get("action", "")).lower()
    keyword_type = str(params.get("keyword_type", "")).lower()

    if keyword_type == "competitors":
        # section carries the competitor name (the accordion's key), not
        # positives/negatives — competitor tabs have no negatives section.
        return await _update_competitor_keywords(
            action, str(params.get("section", "")), params, session_ctx, context
        )

    dump = session_ctx.get("keyword_research")
    if not dump:
        return ToolResult(success=False, error="No keyword research in session — run keyword research first.")

    section = str(params.get("section", "")).lower()

    if action not in _VALID_ACTIONS:
        return ToolResult(success=False, error=f"Invalid action '{action}'. Must be: add, delete, or edit.")
    if keyword_type not in _VALID_KEYWORD_TYPES:
        return ToolResult(success=False, error=f"Invalid keyword_type '{keyword_type}'. Must be brand or generic.")
    if section not in _VALID_SECTIONS:
        return ToolResult(success=False, error=f"Invalid section '{section}'. Must be positives or negatives.")

    kset = dump.get(keyword_type)
    if kset is None:
        return ToolResult(success=False, error=f"No {keyword_type} keywords in session.")

    rows: list[dict] = list(kset.get(section) or [])
    opposite = "negatives" if section == "positives" else "positives"
    opposite_rows: list[dict] = list(kset.get(opposite) or [])

    other_type = "generic" if keyword_type == "brand" else "brand"
    other_kset = dump.get(other_type) or {}
    # Positives-only: a keyword may legitimately be a positive in one type and
    # a negative in the other (standard Google Ads isolation pattern).
    cross_type_rows: list[dict] = list(other_kset.get("positives") or [])

    def _cross_list_error(keyword: str) -> ToolResult | None:
        if any(_row_key(r) == keyword for r in opposite_rows):
            return ToolResult(
                success=False,
                error=f"'{keyword}' is already in {keyword_type} {opposite} — a keyword can't be in both lists.",
            )
        if any(_row_key(r) == keyword for r in cross_type_rows):
            return ToolResult(
                success=False,
                error=f"'{keyword}' already exists in {other_type} positives — a keyword can't be a positive in both brand and generic.",
            )
        if section == "positives":
            section_err = _check_section_signal(keyword, keyword_type, session_ctx)
            if section_err:
                return ToolResult(success=False, error=section_err)
        return None

    if action == "add":
        err = _cross_list_error(_normalize(str(params.get("keyword", ""))))
        if err:
            return err
    elif action == "edit":
        old_keyword = _normalize(str(params.get("old_keyword", "")))
        new_keyword = _normalize(str(params.get("keyword", old_keyword)))
        if new_keyword != old_keyword:
            err = _cross_list_error(new_keyword)
            if err:
                return err

    value_field = "intent" if section == "positives" else "reason"
    outcome = _apply_row_action(action, rows, params, value_field, f"{keyword_type} {section}")
    if isinstance(outcome, ToolResult):
        return outcome
    rows, summary = outcome

    # Persist mutations back into session state.
    kset[section] = rows
    dump[keyword_type] = kset
    session_ctx["keyword_research"] = dump

    logger.info(
        "kw_update type=%s action=%s section=%s keyword=%r rows=%d",
        keyword_type, action, section,
        _normalize(str(params.get("keyword") or params.get("old_keyword") or "")),
        len(rows),
    )

    await _emit_review_update(session_ctx, context)

    return ToolResult(
        success=True,
        summary=summary,
        data={"action": action, "keyword_type": keyword_type, "section": section},
    )


async def _update_competitor_keywords(
    action: str, name: str, params: dict, session_ctx: dict, context: dict
) -> ToolResult:
    """Add/delete/edit within ONE competitor's own positives list — no negatives
    section exists for this tab, and no cross-competitor checks (each
    competitor's list is independent)."""
    if action not in _VALID_ACTIONS:
        return ToolResult(success=False, error=f"Invalid action '{action}'. Must be: add, delete, or edit.")

    competitor_keywords = session_ctx.get("competitor_keywords") or {}
    kset = competitor_keywords.get(name)
    if kset is None:
        return ToolResult(success=False, error=f"No competitor keywords found for '{name}'.")

    rows: list[dict] = list(kset.get("positives") or [])
    outcome = _apply_row_action(action, rows, params, "intent", name)
    if isinstance(outcome, ToolResult):
        return outcome
    rows, summary = outcome

    kset["positives"] = rows
    competitor_keywords[name] = kset
    session_ctx["competitor_keywords"] = competitor_keywords

    logger.info(
        "kw_update_competitor name=%r action=%s keyword=%r rows=%d",
        name, action,
        _normalize(str(params.get("keyword") or params.get("old_keyword") or "")),
        len(rows),
    )

    await _emit_review_update(session_ctx, context)

    return ToolResult(
        success=True,
        summary=summary,
        data={"action": action, "keyword_type": "competitors", "section": name},
    )
