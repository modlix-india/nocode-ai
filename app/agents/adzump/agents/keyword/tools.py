"""Tools the KeywordResearchAgent calls during its loop.

Five tools — thin wrappers over real I/O or a deterministic validation gate; all
judgment stays in the agent's reasoning:

  expand_keywords          autosuggest -> real searched phrasings (broaden the net)
  keyword_metrics          Keyword Planner -> volume / competition / CPC (relevance gate)
  fetch_more_candidates    paginate through lower-volume scored candidates
  submit_positive_keywords validate + record positives
  submit_negative_keywords validate + record negatives, fetch their volumes

Per-run state lives in ``session.context`` under ``kw_*`` keys (plain dicts, so it
survives JSON persistence). The submit tools re-apply safety / dedup / overlap checks
deterministically: the LLM proposes, this layer disposes, so a bad model turn can
never produce an unsafe or self-conflicting keyword set. Review-panel emission is
handled by the orchestrator (keyword_research.py) after both types complete.
"""

from __future__ import annotations

import logging
import re

from pydantic import ValidationError

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from app.agents.adzump.adapters import autosuggest
from app.agents.adzump.adapters.google import keyword_planner

from app.agents.adzump.agents.keyword import constants
from app.agents.adzump.agents.keyword.models import (
    NegativeKeyword,
    OptimizedKeyword,
    normalize,
)

logger = logging.getLogger(__name__)

_WORD = re.compile(r"\w+")


def _state(context: dict) -> dict:
    """The agent's per-run state bag (seeded by research())."""
    return context.get("session_context") or {}


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(normalize(text)))


def _candidates_by_keyword(state: dict) -> dict[str, dict]:
    """keyword -> the full scored Planner candidate dict (volume/competition/CPC)."""
    return {normalize(i.get("keyword", "")): i for i in state.get("kw_candidates", [])}


def _planner_args(state: dict, context: dict) -> dict:
    """Shared Keyword Planner call kwargs (ad account + geo + language) from run state."""
    return dict(
        customer_id=state["kw_customer_id"],
        login_customer_id=state.get("kw_login_customer_id", ""),
        client_code=context.get("client_code", ""),
        auth_headers=context.get("headers", {}),
        geo_target_constants=state.get("kw_geo") or None,
        language=state.get("kw_language") or keyword_planner.DEFAULT_LANGUAGE,
    )


def _candidates_page(state: dict, lead: str) -> ToolResult:
    """Return the next page of scored candidates and advance the cursor.

    The list is the MODEL's working context only (the keyword agent swallows
    tool_result events, so it never reaches the chat UI); the raised result cap lets
    a full page through so the agent can judge relevance across all of them.
    """
    ideas = state.get("kw_candidates", [])
    offset = int(state.get("kw_shown_offset", 0))
    page = ideas[offset : offset + constants.CANDIDATES_PAGE]
    if not page:
        return ToolResult(success=True, summary="No more candidates to show.")
    state["kw_shown_offset"] = offset + len(page)
    end = state["kw_shown_offset"]
    body = "\n".join(
        f"{i['keyword']} | vol {i['volume']} | comp {i['competition']} | cpc {i['cpc_low']}-{i['cpc_high']}"
        for i in page
    )
    if end < len(ideas):
        tail = (
            f"\n\nShowing {offset + 1}-{end} of {len(ideas)} (sorted by volume). "
            "Call fetch_more_candidates for lower-volume terms if these lack good options for this business."
        )
    else:
        tail = f"\n\nShowing all {len(ideas)} candidates."
    return ToolResult(
        success=True,
        summary=f"{lead} (keyword | volume | competition | CPC range):\n{body}{tail}",
        MAX_RESULT_CHARS=constants.KEYWORD_METRICS_RESULT_MAX,
    )


# expand_keywords


async def _expand_keywords(params: dict, context: dict) -> ToolResult:
    seeds = list(
        dict.fromkeys(
            normalize(s)
            for s in (params.get("seeds") or [])
            if isinstance(s, str) and s.strip()
        )
    )
    if not seeds:
        return ToolResult(success=False, error="No seeds provided to expand.")

    state = _state(context)
    # Source selection is per business (BusinessProfile.source_names); default if unset.
    source_names = state.get("kw_sources") or constants.DEFAULT_SOURCE_NAMES
    sources = [autosuggest.SOURCES[n] for n in source_names if n in autosuggest.SOURCES]
    try:
        # Fan out autosuggest on the top seeds only — bounds the request count.
        suggestions = await autosuggest.fetch_suggestions(
            seeds[: constants.MAX_SEEDS_TO_EXPAND],
            sources=sources,
            hl=state.get("kw_hl", "en"),
            gl=state.get("kw_gl", "US"),
        )
    except Exception as exc:  # fail-soft: expansion is a booster, never fatal
        logger.warning("expand_keywords failed: %s", str(exc)[: constants.LOG_TRUNCATE])
        suggestions = []

    # All seeds + suggestions form the candidate pool; dedup and bound before the Planner.
    pool = list(dict.fromkeys(seeds + suggestions))[
        : constants.MAX_EXPANSION_CANDIDATES
    ]
    state["kw_pool"] = pool
    return ToolResult(
        success=True,
        summary=f"Expanded to {len(pool)} candidate phrases (seeds + real autosuggest queries):\n"
        + "\n".join(pool[: constants.MAX_CANDIDATES_SHOWN]),
    )


# keyword_metrics


async def _keyword_metrics(params: dict, context: dict) -> ToolResult:
    state = _state(context)
    keywords = [
        k for k in (params.get("keywords") or []) if isinstance(k, str) and k.strip()
    ]
    keywords = keywords or state.get("kw_pool") or []
    if not keywords:
        return ToolResult(
            success=False, error="No keywords to score — expand or provide some first."
        )
    if not state.get("kw_customer_id"):
        return ToolResult(
            success=False,
            error="Ad account is not set for this run; cannot query the Planner.",
        )

    try:
        ideas = await keyword_planner.fetch_keyword_ideas(
            keywords,
            url=state.get("kw_business_url") or None,
            **_planner_args(state, context),
        )
    except Exception as exc:
        logger.warning("keyword_metrics failed: %s", str(exc)[: constants.LOG_TRUNCATE])
        return ToolResult(
            success=False,
            error=f"Keyword Planner request failed: {str(exc)[: constants.LOG_TRUNCATE]}",
        )

    # Demand gate: GENERIC drops 0-volume terms (they can't get traffic, so they'd
    # only bloat the list). BRAND keeps everything — brand protection: a new brand can
    # be 0-volume but we must still own it. The Planner expands well beyond the input
    # pool, so store a generous with-demand set (the agent pages through it).
    if state.get("kw_type") == "generic":
        ideas = [i for i in ideas if i.get("volume", 0) > 0]
    ideas = ideas[: constants.MAX_STORED_CANDIDATES]
    if not ideas:
        return ToolResult(
            success=True,
            summary="No keyword ideas with Google demand for these seeds — try broader or different seeds.",
        )
    state["kw_candidates"] = ideas
    state["kw_shown_offset"] = 0
    return _candidates_page(state, lead=f"Google demand for {len(ideas)} keywords")


async def _fetch_more_candidates(params: dict, context: dict) -> ToolResult:
    state = _state(context)
    if not state.get("kw_candidates"):
        return ToolResult(success=False, error="No candidates yet — call keyword_metrics first.")
    return _candidates_page(state, lead="More candidates")


# submit tools (deterministic gate + panel emission)


async def _submit_positive_keywords(params: dict, context: dict) -> ToolResult:
    items = params.get("keywords") or []
    if not items:
        return ToolResult(success=False, error="No keywords provided.")
    state = _state(context)
    by_kw = _candidates_by_keyword(state)

    kept: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        kw = normalize(item.get("keyword", ""))
        cand = by_kw.get(kw)
        if not kw or kw in seen or cand is None:
            continue  # must be a real scored candidate — no invented keywords
        try:
            # Model coerces match_type/intent and forces cross-business -> phrase.
            positive = OptimizedKeyword(
                keyword=kw,
                volume=cand.get("volume", 0),
                competition=cand.get("competition", "UNKNOWN"),
                competition_index=cand.get("competition_index", 0.0),
                cpc_low=cand.get("cpc_low", 0.0),
                cpc_high=cand.get("cpc_high", 0.0),
                source="planner",
                match_type=item.get("match_type"),
                intent=item.get("intent"),
                is_cross_business=bool(item.get("is_cross_business")),
                rationale=str(item.get("rationale", "")).strip(),
            )
        except ValidationError as exc:
            logger.debug("skip positive %r: %s", kw, exc)
            continue
        seen.add(kw)
        kept.append(positive.model_dump(mode="json"))

    if not kept:
        return ToolResult(
            success=False,
            error="None of your submitted keywords matched the scored candidates. Please select exact keywords from the data provided.",
        )

    state["kw_positives"] = kept
    dropped = len(items) - len(kept)
    logger.info(
        "kw_submit_positive type=%s submitted=%d kept=%d dropped=%d",
        state.get("kw_type"), len(items), len(kept), dropped,
    )
    note = f" ({dropped} not in the scored data or duplicate)" if dropped > 0 else ""
    return ToolResult(
        success=True, summary=f"Recorded {len(kept)} positive keywords{note}."
    )


async def _submit_negative_keywords(params: dict, context: dict) -> ToolResult:
    items = params.get("keywords") or []
    state = _state(context)
    positive_kws = {p.get("keyword", "") for p in state.get("kw_positives", [])}
    positive_tokens: set[str] = set()
    for p in state.get("kw_positives", []):
        positive_tokens |= _tokens(p.get("keyword", ""))

    kept: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        kw = normalize(item.get("keyword", ""))
        if not kw or kw in seen or kw in positive_kws:
            continue
        if any(p.search(kw) for p in constants.SAFETY_PATTERNS):
            continue
        kw_tokens = _tokens(kw)
        if (
            kw_tokens
            and len(kw_tokens & positive_tokens) / len(kw_tokens)
            >= constants.NEGATIVE_POSITIVE_TOKEN_OVERLAP_MAX
        ):
            continue  # too close to a positive — would block real traffic
        try:
            # Model coerces match_type and enforces the length limit; volume filled below.
            negative = NegativeKeyword(
                keyword=kw,
                reason=str(item.get("reason", "")).strip(),
                match_type=item.get("match_type"),
                kind=state.get("kw_type", "generic"),
            )
        except ValidationError as exc:
            logger.debug("skip negative %r: %s", kw, exc)
            continue
        seen.add(kw)
        kept.append(negative.model_dump(mode="json"))
        if len(kept) >= constants.MAX_NEGATIVE_COUNT:
            break

    if not kept and items:
        return ToolResult(
            success=False,
            error="None of your submitted keywords were valid (they overlap with positives, are unsafe, or are duplicates). Please try different negatives.",
        )

    await _attach_negative_volumes(context, kept)
    state["kw_negatives"] = kept
    dropped = len(items) - len(kept)
    logger.info(
        "kw_submit_negative type=%s submitted=%d kept=%d dropped=%d",
        state.get("kw_type"), len(items), len(kept), dropped,
    )
    note = (
        f" ({dropped} dropped: overlap with positives, unsafe, or duplicate)"
        if dropped > 0
        else ""
    )
    return ToolResult(
        success=True,
        summary=f"Recorded {len(kept)} negative keywords{note}. Keyword research complete.",
    )


async def _attach_negative_volumes(context: dict, negatives: list[dict]) -> None:
    """Fill each negative's volume — reuse the candidate pool, else historical metrics
    for the rest (negatives are usually wrong-category terms outside the ideas pool)."""
    if not negatives:
        return
    state = _state(context)
    by_kw = _candidates_by_keyword(state)
    missing: list[str] = []
    for neg in negatives:
        cand = by_kw.get(neg["keyword"])
        if cand is not None:
            neg["volume"] = int(cand.get("volume", 0))
        else:
            missing.append(neg["keyword"])
    if not (missing and state.get("kw_customer_id")):
        return
    metrics = await keyword_planner.fetch_keyword_historical_metrics(
        missing, **_planner_args(state, context)
    )
    if not metrics and missing:
        logger.warning("historical_metrics returned empty for %d negatives", len(missing))
    fetched = {m["keyword"]: m["volume"] for m in metrics}
    for neg in negatives:
        if neg["volume"] == 0:
            neg["volume"] = fetched.get(neg["keyword"], 0)


# tool definitions

EXPAND_KEYWORDS = ToolDefinition(
    name="expand_keywords",
    description="Broaden your seed terms into real searched phrasings using search autosuggest. Call with your draft seeds.",
    parameters=[
        ToolParameter(
            name="seeds",
            type="array",
            description="Seed search phrases to expand.",
            items={"type": "string"},
        )
    ],
    execute=_expand_keywords,
)

KEYWORD_METRICS = ToolDefinition(
    name="keyword_metrics",
    description="Get real Google search volume, competition and CPC for keywords via the Keyword Planner. This is the relevance gate — terms with no Google demand are not worth bidding on. Pass the expanded candidates.",
    parameters=[
        ToolParameter(
            name="keywords",
            type="array",
            description="Keywords to score; defaults to the expanded pool if omitted.",
            required=False,
            items={"type": "string"},
        )
    ],
    execute=_keyword_metrics,
)

FETCH_MORE_CANDIDATES = ToolDefinition(
    name="fetch_more_candidates",
    description="Show the next page of scored keyword candidates (lower-volume terms). Use only if the candidates shown so far lack good options for this business.",
    parameters=[],
    execute=_fetch_more_candidates,
)

SUBMIT_POSITIVE_KEYWORDS = ToolDefinition(
    name="submit_positive_keywords",
    description="Record the final positive keywords. Each must be an exact keyword from the scored data.",
    parameters=[
        ToolParameter(
            name="keywords",
            type="array",
            description="Chosen positives.",
            items={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "match_type": {"type": "string", "enum": ["exact", "phrase"]},
                    "intent": {
                        "type": "string",
                        "enum": [
                            "commercial",
                            "transactional",
                            "informational",
                            "navigational",
                        ],
                    },
                    "is_cross_business": {"type": "boolean"},
                    "rationale": {"type": "string"},
                },
            },
        )
    ],
    execute=_submit_positive_keywords,
)

SUBMIT_NEGATIVE_KEYWORDS = ToolDefinition(
    name="submit_negative_keywords",
    description="Record the final negative keywords (searches to exclude). Call this last to finish the run.",
    parameters=[
        ToolParameter(
            name="keywords",
            type="array",
            description="Chosen negatives.",
            items={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "reason": {"type": "string"},
                    "match_type": {"type": "string", "enum": ["exact", "phrase"]},
                },
            },
        )
    ],
    execute=_submit_negative_keywords,
)

ALL_TOOLS = [
    EXPAND_KEYWORDS,
    KEYWORD_METRICS,
    FETCH_MORE_CANDIDATES,
    SUBMIT_POSITIVE_KEYWORDS,
    SUBMIT_NEGATIVE_KEYWORDS,
]
