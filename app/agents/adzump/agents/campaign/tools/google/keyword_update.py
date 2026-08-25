"""keyword_update — the keyword-review-panel mutation logic.

The panel sends structured actions as pure JSON:
  {"type": "keyword_widget", "action": "add|delete|edit", "keyword_type": "brand|generic",
   "section": "positives|negatives", "keyword": "...",
   "match_type": "EXACT|PHRASE (positives) or PHRASE|BROAD (negatives)",
   "volume": 1200, "intent": "transactional"}   (intent/reason differ by section)
   "old_keyword": "..."  (edit only)

update_keywords() applies the action to the campaign's saved keyword set (read and written
through ``campaign/models.py``, never by session key) and re-emits only the keyword_review
block (keyed upsert, no panel flash). The HTTP transport that routes these actions to it
lives in campaign/api.py.
"""

from __future__ import annotations

import difflib
import logging

from app.agents.adzump.agents.campaign.craft import (
    emit_section_update,
    keyword_review_block,
)
from app.agents.adzump.agents.campaign.google.keyword.constants import (
    KEYWORD_MAX_LENGTH,
    KEYWORD_MAX_WORDS,
    KEYWORD_MIN_LENGTH,
)
from app.agents.adzump.agents.campaign.google.keyword.models import (
    AdGroupStatus,
)
from app.agents.adzump.agents.campaign.google.keyword.models import (
    normalize as _normalize,
)
from app.agents.adzump.agents.campaign.google.keyword.themes import get_theme
from app.agents.adzump.agents.campaign.models import (
    keyword_research,
    set_keyword_research,
)
from app.core.tools.base import ToolResult

logger = logging.getLogger(__name__)


_VALID_ACTIONS = frozenset({"add", "delete", "edit"})
_VALID_SECTIONS = frozenset({"positives", "negatives"})
_POSITIVE_MATCH_TYPES = frozenset({"EXACT", "PHRASE"})  # positives target
_NEGATIVE_MATCH_TYPES = frozenset({"PHRASE", "BROAD"})  # negatives exclude a concept


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


def _coerce_match_type(raw: object, section: str, fallback: str = "PHRASE") -> str:
    # Positives are EXACT/PHRASE; negatives are PHRASE/BROAD (mirrors the keyword models).
    # The fallback is validated too - a stored value can be out-of-section (a legacy EXACT
    # negative), so coerce rather than persist it. PHRASE is valid for both.
    allowed = _NEGATIVE_MATCH_TYPES if section == "negatives" else _POSITIVE_MATCH_TYPES
    mt = str(raw or "").upper()
    if mt in allowed:
        return mt
    fb = str(fallback or "").upper()
    return fb if fb in allowed else "PHRASE"


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_normalize(text).split())


_BRAND_FUZZY_RATIO = (
    0.8  # token similarity that still counts as the brand (catches misspellings)
)


def _is_brandish(token: str, brand_tokens: frozenset[str]) -> bool:
    """True if a keyword token is a brand token or a near-miss of one (e.g. a misspelling)."""
    return any(
        difflib.SequenceMatcher(None, token, b).ratio() >= _BRAND_FUZZY_RATIO
        for b in brand_tokens
    )


def _check_section_signal(
    keyword: str, keyword_type: str, session_ctx: dict
) -> str | None:
    """Return an error if the keyword clearly belongs in a different ad group.

    Brand affinity is the theme's own declared rule, so a new theme states its stance
    (requires_brand_token) instead of being added to a branch here.
    """
    kw_tokens = _tokens(keyword)
    if not kw_tokens:
        return None

    theme = get_theme(keyword_type)
    dump = keyword_research(session_ctx) or {}
    themes = dump.get("themes") or {}
    # The brand the taxonomy named, not the whole company name — "Kajaria Ceramics" carries
    # its industry too. Sets built before it was recorded fall back to the name.
    brand_terms = (dump.get("meta") or {}).get("brand_terms") or []
    product_name = str(
        (session_ctx.get("product_data") or {}).get("product_name") or ""
    )
    brand_tokens = (
        _tokens(" ".join(brand_terms)) if brand_terms else _tokens(product_name)
    )

    def _positives_where(requires_brand: bool) -> list[dict]:
        return [
            row
            for fid, kset in themes.items()
            if fid != keyword_type
            and get_theme(fid).requires_brand_token is requires_brand
            for row in (kset.get("positives") or [])
        ]

    if theme.requires_brand_token:
        if brand_tokens:
            # Fuzzy match so deliberate brand misspellings (a real brand keyword) still pass.
            if not any(_is_brandish(t, brand_tokens) for t in kw_tokens):
                return (
                    f"'{keyword}' doesn't contain any brand name terms — "
                    "generic keywords belong in a generic ad group."
                )
        else:
            for row in _positives_where(requires_brand=False):
                row_tokens = _tokens(row.get("keyword", ""))
                if row_tokens and row_tokens.issubset(kw_tokens):
                    return (
                        f"'{keyword}' contains a generic keyword — "
                        "generic keywords belong in a generic ad group."
                    )
    else:
        # Block when ALL brand-name tokens are present — partial overlap is category noise.
        # Fuzzy (like the brand direction) so a misspelled brand ("dulingo") is caught too.
        if brand_tokens and all(_is_brandish(bt, kw_tokens) for bt in brand_tokens):
            return (
                f"'{keyword}' contains the full brand name — "
                "brand-specific keywords belong in the brand ad group."
            )
        # Same all-tokens rule against the brand ad groups' positives.
        for row in _positives_where(requires_brand=True):
            row_tokens = _tokens(row.get("keyword", ""))
            if row_tokens and row_tokens.issubset(kw_tokens):
                return (
                    f"'{keyword}' contains a brand keyword — "
                    "brand-specific keywords belong in the brand ad group."
                )

    return None


def _apply_edit(params: dict, session_ctx: dict) -> tuple[bool, str]:
    """Apply ONE add/delete/edit to the saved set. Returns (ok, message).

    The single mutation path: panel clicks (0 LLM) and the keyword agent both come through
    here, so an edit made in words can't break an invariant a click couldn't.
    """
    dump = keyword_research(session_ctx)
    if not dump:
        return False, "No keyword research in session — run keyword research first."

    action = str(params.get("action", "")).lower()
    keyword_type = str(params.get("keyword_type", "")).lower()
    section = str(params.get("section", "")).lower()

    themes: dict[str, dict] = dump.get("themes") or {}

    if action not in _VALID_ACTIONS:
        return False, f"Invalid action '{action}'. Must be: add, delete, or edit."
    if keyword_type not in themes:
        return (
            False,
            (
                f"No '{keyword_type}' ad group in this campaign. Built: "
                f"{', '.join(sorted(themes)) or 'none'}. Research it first with "
                "research_ad_group - it cannot be edited into existence."
            ),
        )
    if section not in _VALID_SECTIONS:
        return False, f"Invalid section '{section}'. Must be positives or negatives."

    kset = themes[keyword_type]

    rows: list[dict] = list(kset.get(section) or [])
    opposite = "negatives" if section == "positives" else "positives"
    opposite_rows: list[dict] = list(kset.get(opposite) or [])

    # Positives-only, across every OTHER ad group: a keyword may legitimately be a positive
    # in one theme and a negative in another (standard Google Ads isolation pattern).
    cross_type_rows: list[dict] = [
        row
        for other_key, other_kset in themes.items()
        if other_key != keyword_type
        for row in (other_kset.get("positives") or [])
    ]

    if action == "add":
        keyword = _normalize(str(params.get("keyword", "")))
        err = _validate_keyword(keyword)
        if err:
            return False, err
        if any(_row_key(r) == keyword for r in rows):
            return False, f"'{keyword}' already exists in {keyword_type} {section}."
        if any(_row_key(r) == keyword for r in opposite_rows):
            return (
                False,
                f"'{keyword}' is already in {keyword_type} {opposite} — a keyword can't be in both lists.",
            )
        if any(_row_key(r) == keyword for r in cross_type_rows):
            return (
                False,
                f"'{keyword}' is already a positive in another ad group — a keyword can't be a positive in two ad groups.",
            )
        if section == "positives":
            section_err = _check_section_signal(keyword, keyword_type, session_ctx)
            if section_err:
                return False, section_err
        match_type = _coerce_match_type(params.get("match_type"), section)
        new_row: dict = {
            "keyword": keyword,
            "volume": int(params.get("volume", 0) or 0),
            "match_type": match_type,
        }
        if section == "positives":
            new_row["intent"] = str(params.get("intent", "") or "")
        else:
            new_row["reason"] = str(params.get("reason", "") or "")
        rows.insert(0, new_row)  # newest on top so the user sees their add immediately
        summary = f"Added '{keyword}' to {keyword_type} {section}."

    elif action == "delete":
        keyword = _normalize(str(params.get("keyword", "")))
        updated = [r for r in rows if _row_key(r) != keyword]
        if len(updated) == len(rows):
            return False, f"'{keyword}' not found in {keyword_type} {section}."
        rows = updated
        summary = f"Removed '{keyword}' from {keyword_type} {section}."

    else:  # edit
        old_keyword = _normalize(str(params.get("old_keyword", "")))
        new_keyword = _normalize(str(params.get("keyword", old_keyword)))
        err = _validate_keyword(new_keyword)
        if err:
            return False, err

        target = next((r for r in rows if _row_key(r) == old_keyword), None)
        if target is None:
            return False, f"'{old_keyword}' not found in {keyword_type} {section}."

        if new_keyword != old_keyword:
            if any(_row_key(r) == new_keyword for r in rows if r is not target):
                return (
                    False,
                    f"'{new_keyword}' already exists in {keyword_type} {section}.",
                )
            if any(_row_key(r) == new_keyword for r in opposite_rows):
                return (
                    False,
                    f"'{new_keyword}' is in {keyword_type} {opposite} — a keyword can't be in both lists.",
                )
            if any(_row_key(r) == new_keyword for r in cross_type_rows):
                return (
                    False,
                    f"'{new_keyword}' is already a positive in another ad group — a keyword can't be a positive in two ad groups.",
                )
            if section == "positives":
                section_err = _check_section_signal(
                    new_keyword, keyword_type, session_ctx
                )
                if section_err:
                    return False, section_err

        target["keyword"] = new_keyword
        target["match_type"] = _coerce_match_type(
            params.get("match_type"), section, target.get("match_type", "PHRASE")
        )
        target["volume"] = int(params.get("volume", target.get("volume", 0)) or 0)
        if section == "positives":
            target["intent"] = str(params.get("intent", target.get("intent", "")) or "")
        else:
            target["reason"] = str(params.get("reason", target.get("reason", "")) or "")
        summary = f"Updated '{old_keyword}' in {keyword_type} {section}."

    # Persist mutations back into session state.
    kset[section] = rows
    # An ad group whose run ended before its negatives phase is finished once they exist.
    if kset.get("status") == AdGroupStatus.PARTIAL.value and kset.get("negatives"):
        kset["status"] = AdGroupStatus.COMPLETE.value
    themes[keyword_type] = kset
    dump["themes"] = themes
    set_keyword_research(session_ctx, dump)

    logger.info(
        "kw_update type=%s action=%s section=%s keyword=%r rows=%d",
        keyword_type,
        action,
        section,
        _normalize(str(params.get("keyword") or params.get("old_keyword") or "")),
        len(rows),
    )

    return True, summary


async def _emit_panel(context: dict, session_ctx: dict) -> None:
    """Re-emit only the keyword block (keyed upsert, no panel flash)."""
    dump = keyword_research(session_ctx) or {}
    # Key off the craft_id the panel was ORIGINALLY drawn under — keyword_research records it
    # in the dump's meta. Reading it from the data (not a session key that only coincidentally
    # holds the same value) guarantees an edit upserts into the same craft container.
    craft_id = (
        (dump.get("meta") or {}).get("craft_id")
        or session_ctx.get("campaign_craft_id")
        or f"campaign_{context.get('session_id', '')}"
    )
    await emit_section_update(
        context.get("event_stream"), craft_id, keyword_review_block(dump)
    )


async def update_keywords(params: dict, context: dict) -> ToolResult:
    """Panel-click entry: one mechanical edit, zero LLM."""
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    ok, message = _apply_edit(params, session_ctx)
    if not ok:
        return ToolResult(success=False, error=message)

    await _emit_panel(context, session_ctx)
    return ToolResult(
        success=True,
        summary=message,
        data={
            "action": str(params.get("action", "")).lower(),
            "keyword_type": str(params.get("keyword_type", "")).lower(),
            "section": str(params.get("section", "")).lower(),
        },
    )
