"""Tools the KeywordResearchAgent calls during its loop.

Six thin wrappers over real I/O or a deterministic validation gate; all judgment
stays in the agent's reasoning:

  expand_keywords            autosuggest -> real searched phrasings (broaden the net)
  keyword_metrics            Keyword Planner -> volume / competition / CPC (relevance gate)
  fetch_more_candidates      paginate through lower-volume scored candidates
  submit_positive_keywords   validate + record positives
  submit_negative_keywords   validate + record negatives, fetch their volumes
  submit_competitor_keywords validate + record competitor positives

Per-run state lives in ``session.context`` under ``kw_*`` keys (plain dicts, so it
survives JSON persistence). The submit tools re-apply safety / dedup / overlap checks
deterministically — the LLM proposes, this layer disposes. Review-panel emission is
handled by the orchestrators (keyword_research.py / competitor_keywords.py).
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

    Model-only working context (the agent swallows tool_result events — never hits the
    chat UI); the raised char cap lets a full page through for relevance judgement.
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
    source_names = state.get("kw_sources") or autosuggest.DEFAULT_SOURCE_NAMES
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

    # Seeds + suggestions form the candidate pool. The top slice is expanded by the Planner
    # (generateKeywordIdeas); the overflow is real autosuggest queries we'd otherwise discard —
    # keyword_metrics scores it cheaply via historical metrics instead of throwing it away.
    unique = list(dict.fromkeys(seeds + suggestions))
    pool = unique[: constants.MAX_EXPANSION_CANDIDATES]
    state["kw_pool"] = pool
    state["kw_overflow"] = unique[constants.MAX_EXPANSION_CANDIDATES :]
    logger.info(
        "kw_expand type=%s seeds=%d autosuggest=%d pool=%d overflow=%d",
        state.get("kw_type"),
        len(seeds),
        len(suggestions),
        len(pool),
        len(state["kw_overflow"]),
    )
    return ToolResult(
        success=True,
        summary=f"Expanded to {len(pool)} candidate phrases (seeds + real autosuggest queries):\n"
        + "\n".join(pool[: constants.MAX_CANDIDATES_SHOWN]),
    )


# keyword_metrics


def _collapse_repeats(keyword: str) -> str | None:
    """Order-preserving de-duplication of repeated tokens; None if nothing repeats.

    Repairs duplicate-token phrases the Planner sometimes returns ("a glasses a" -> "a glasses").
    The collapsed form is only a CANDIDATE — it's re-scored via historical metrics and kept
    only if real, and the original is never dropped, so a wrong collapse can never corrupt data.
    """
    toks = keyword.split()
    out = list(dict.fromkeys(toks))
    return " ".join(out) if len(out) != len(toks) else None


def _store_scored_candidates(state: dict, scored: list[dict]) -> ToolResult:
    """Merge scored ideas into the candidate pool (higher volume wins on a dup),
    cap by volume, reset the page cursor, and return the first page. Shared by the
    offering (generateKeywordIdeas) and competitor-brand (exact historical metrics)
    scoring paths so they can't drift on how candidates are stored/paged."""
    if not scored:
        return ToolResult(
            success=True,
            summary="No keyword ideas with Google demand for these seeds — try broader or different seeds.",
        )
    # Merge (dedup, keep higher volume) instead of replacing — so picks from an earlier
    # keyword_metrics batch stay selectable if the agent scores seeds across calls.
    pool = {i["keyword"]: i for i in state.get("kw_candidates", [])}
    for idea in scored:
        kw = idea["keyword"]
        if kw not in pool or idea["volume"] > pool[kw]["volume"]:
            pool[kw] = idea
    merged = sorted(pool.values(), key=lambda i: i["volume"], reverse=True)[
        : constants.MAX_STORED_CANDIDATES
    ]
    state["kw_candidates"] = merged
    state["kw_shown_offset"] = 0
    logger.info("kw_candidates: scored=%d stored_pool=%d", len(scored), len(merged))
    return _candidates_page(state, lead=f"Google demand for {len(merged)} keywords")


async def _keyword_metrics(params: dict, context: dict) -> ToolResult:
    state = _state(context)
    extra = [
        k for k in (params.get("keywords") or []) if isinstance(k, str) and k.strip()
    ]
    # Score the full pool; the model's list augments it, never replaces it (it can't
    # reliably re-echo 100+ candidates, so a replace would drop most of the expansion).
    keywords = list(dict.fromkeys([*(state.get("kw_pool") or []), *extra]))
    if not keywords:
        return ToolResult(
            success=False, error="No keywords to score — expand or provide some first."
        )
    if not state.get("kw_customer_id"):
        return ToolResult(
            success=False,
            error="Ad account is not set for this run; cannot query the Planner.",
        )

    if state.get("kw_type") == "competitor_brand":
        return await _competitor_keyword_metrics(extra, state, context)

    try:
        ideas = await keyword_planner.fetch_keyword_ideas(
            keywords,
            url=state.get("kw_business_url") or None,
            **_planner_args(state, context),
        )
    except keyword_planner.PlannerUnavailable:
        # Breaker open — be honest, don't imply the seeds have no demand.
        return ToolResult(
            success=False,
            error="Keyword research service is temporarily unavailable — stop and ask the user to retry shortly.",
        )
    except Exception as exc:
        logger.warning("keyword_metrics failed: %s", str(exc)[: constants.LOG_TRUNCATE])
        return ToolResult(
            success=False,
            error=f"Keyword Planner request failed: {str(exc)[: constants.LOG_TRUNCATE]}",
        )

    # Recover clean candidates the Planner's expansion misses: the discarded overflow (real
    # autosuggest queries beyond the cap) + de-mangled repairs of duplicate-token phrases it
    # returned. Both are scored EXACTLY via historical metrics (no re-expansion → no new
    # mangling) and kept only if real; originals are never dropped.
    idea_keys = {i["keyword"] for i in ideas}
    repairs = [c for i in ideas if (c := _collapse_repeats(i["keyword"]))]
    recover = [
        k
        for k in dict.fromkeys([*(state.get("kw_overflow") or []), *repairs])
        if k not in idea_keys
    ]
    recovered: list[dict] = []
    if recover:
        try:
            recovered = await keyword_planner.fetch_keyword_historical_metrics(
                recover, **_planner_args(state, context)
            )
        except Exception as exc:
            logger.warning(
                "keyword_metrics recover failed: %s", str(exc)[: constants.LOG_TRUNCATE]
            )
        recovered = [r for r in recovered if r.get("volume", 0) > 0]

    # Demand gate: the advertiser's OWN brand keeps 0-volume terms (a new brand can be
    # 0-volume but we must still own it — brand protection); generic drops them (a term
    # nobody searches is dead weight). Competitor-brand is scored on its own path above.
    if state.get("kw_type") != "brand":
        ideas = [i for i in ideas if i.get("volume", 0) > 0]
    scored = [*ideas, *recovered]
    logger.info(
        "kw_metrics type=%s sent=%d planner_ideas=%d recovered=%d",
        state.get("kw_type"),
        len(keywords),
        len(ideas),
        len(recovered),
    )
    return _store_scored_candidates(state, scored)


async def _competitor_keyword_metrics(
    extra: list[str], state: dict, context: dict
) -> ToolResult:
    """Score competitor-brand terms EXACTLY via historical metrics — no
    generateKeywordIdeas expansion.

    Conquest keywords must contain a competitor's brand name (SELECT_COMPETITOR_BRAND),
    so the Planner's generic expansion only floods the shared pool with high-volume
    non-brand terms that crowd the low-volume competitors out of the volume-capped top
    slots. Scoring the brand seed + autosuggest terms directly (one exact-metrics call,
    no expansion) keeps every competitor's terms in the pool and available to select."""
    keywords = list(
        dict.fromkeys(
            [*(state.get("kw_pool") or []), *(state.get("kw_overflow") or []), *extra]
        )
    )
    if not keywords:
        return ToolResult(
            success=False, error="No keywords to score — expand or provide some first."
        )
    # fetch_keyword_historical_metrics is fail-soft (returns [] on error); an empty
    # result surfaces as the "no demand" message from _store_scored_candidates.
    scored = await keyword_planner.fetch_keyword_historical_metrics(
        keywords, **_planner_args(state, context)
    )
    # Demand gate: a 0-volume competitor term has no traffic to intercept.
    scored = [i for i in scored if i.get("volume", 0) > 0]
    logger.info(
        "kw_metrics type=competitor_brand sent=%d scored=%d", len(keywords), len(scored)
    )
    return _store_scored_candidates(state, scored)


async def _fetch_more_candidates(params: dict, context: dict) -> ToolResult:
    state = _state(context)
    if not state.get("kw_candidates"):
        return ToolResult(
            success=False, error="No candidates yet — call keyword_metrics first."
        )
    return _candidates_page(state, lead="More candidates")


# submit tools (deterministic gate + panel emission)


def _build_optimized_keyword(
    kw: str, item: dict, cand: dict
) -> OptimizedKeyword | None:
    """Validate + build one positive from a scored candidate; None if invalid.

    Shared by submit_positive_keywords and submit_competitor_keywords — the model
    coerces match_type/intent and forces cross-business -> phrase.
    """
    try:
        return OptimizedKeyword(
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
        return None


async def _submit_positive_keywords(params: dict, context: dict) -> ToolResult:
    state = _state(context)
    if state.get("kw_type") == "competitor_brand":
        return ToolResult(
            success=False,
            error="Wrong tool. Use submit_competitor_keywords for competitor brand research.",
        )

    items = params.get("keywords") or []
    if not items:
        return ToolResult(success=False, error="No keywords provided.")
    by_kw = _candidates_by_keyword(state)

    kept: list[dict] = []
    seen: set[str] = set()
    drop_reasons: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        kw = normalize(item.get("keyword", ""))
        cand = by_kw.get(kw)
        if not kw:
            continue
        if kw in seen:
            drop_reasons["duplicate"] = drop_reasons.get("duplicate", 0) + 1
            continue
        if cand is None:
            drop_reasons["not_in_scored_data"] = (
                drop_reasons.get("not_in_scored_data", 0) + 1
            )
            continue  # must be a real scored candidate — no invented keywords
        positive = _build_optimized_keyword(kw, item, cand)
        if positive is None:
            drop_reasons["validation_failed"] = (
                drop_reasons.get("validation_failed", 0) + 1
            )
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
        "kw_submit_positive type=%s submitted=%d kept=%d dropped=%d drop_reasons=%r",
        state.get("kw_type"),
        len(items),
        len(kept),
        dropped,
        drop_reasons,
    )
    note = f" ({dropped} dropped: {drop_reasons})" if dropped > 0 else ""
    return ToolResult(
        success=True, summary=f"Recorded {len(kept)} positive keywords{note}."
    )


async def _submit_competitor_keywords(params: dict, context: dict) -> ToolResult:
    """Like submit_positive_keywords, but grouped by competitor_name — one shared
    candidate pool covers every competitor in the batch, so each item must say
    which competitor it belongs to."""
    state = _state(context)
    if state.get("kw_type") != "competitor_brand":
        return ToolResult(
            success=False,
            error="Wrong tool. This tool is only for competitor brand research. Use submit_positive_keywords instead.",
        )

    items = params.get("keywords") or []
    if not items:
        return ToolResult(success=False, error="No keywords provided.")
    by_kw = _candidates_by_keyword(state)
    known_names = {c.get("name", "") for c in state.get("kw_competitors") or []}

    grouped: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    drop_reasons: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        kw = normalize(item.get("keyword", ""))
        name = str(item.get("competitor_name", "")).strip()
        cand = by_kw.get(kw)
        if not kw:
            continue
        if not name or name not in known_names:
            drop_reasons["unknown_competitor"] = (
                drop_reasons.get("unknown_competitor", 0) + 1
            )
            continue
        if (name, kw) in seen:
            drop_reasons["duplicate"] = drop_reasons.get("duplicate", 0) + 1
            continue
        if cand is None:
            drop_reasons["not_in_scored_data"] = (
                drop_reasons.get("not_in_scored_data", 0) + 1
            )
            continue
        positive = _build_optimized_keyword(kw, item, cand)
        if positive is None:
            drop_reasons["validation_failed"] = (
                drop_reasons.get("validation_failed", 0) + 1
            )
            continue
        seen.add((name, kw))
        grouped.setdefault(name, []).append(positive.model_dump(mode="json"))

    kept = sum(len(v) for v in grouped.values())
    if not kept:
        return ToolResult(
            success=False,
            error="None of your submitted keywords matched the scored candidates and a known competitor_name. Please select exact keywords from the data and a valid competitor_name.",
        )

    state["kw_competitor_positives"] = grouped
    dropped = len(items) - kept
    logger.info(
        "kw_submit_competitor competitors=%d submitted=%d kept=%d dropped=%d drop_reasons=%r",
        len(grouped),
        len(items),
        kept,
        dropped,
        drop_reasons,
    )
    note = f" ({dropped} dropped: {drop_reasons})" if dropped > 0 else ""
    return ToolResult(
        success=True,
        summary=f"Recorded {kept} competitor keywords across {len(grouped)} competitors{note}.",
    )


async def _submit_negative_keywords(params: dict, context: dict) -> ToolResult:
    state = _state(context)
    if state.get("kw_type") == "competitor_brand":
        return ToolResult(
            success=False,
            error="Wrong tool. Competitor brand research has no negatives phase.",
        )

    items = params.get("keywords") or []
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
        state.get("kw_type"),
        len(items),
        len(kept),
        dropped,
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
        logger.warning(
            "historical_metrics returned empty for %d negatives", len(missing)
        )
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
    description="Get real Google search volume, competition and CPC via the Keyword Planner. This is the relevance gate — terms with no Google demand are not worth bidding on. The full expanded pool is always scored; just call it after expand_keywords.",
    parameters=[
        ToolParameter(
            name="keywords",
            type="array",
            description="Optional EXTRA keywords to score on top of the expanded pool (the whole pool is always scored). Leave empty unless you have additional terms to add.",
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

SUBMIT_COMPETITOR_KEYWORDS = ToolDefinition(
    name="submit_competitor_keywords",
    description="Record the final competitor-brand positive keywords, grouped by competitor. Each must be an exact keyword from the scored data, tagged with which competitor it belongs to. Call this last to finish the run — competitor-brand research has no negatives phase.",
    parameters=[
        ToolParameter(
            name="keywords",
            type="array",
            description="Chosen positives, one entry per keyword.",
            items={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "competitor_name": {
                        "type": "string",
                        "description": "Must match one of the competitor names given in the context.",
                    },
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
    execute=_submit_competitor_keywords,
)

ALL_TOOLS = [
    EXPAND_KEYWORDS,
    KEYWORD_METRICS,
    FETCH_MORE_CANDIDATES,
    SUBMIT_POSITIVE_KEYWORDS,
    SUBMIT_NEGATIVE_KEYWORDS,
    SUBMIT_COMPETITOR_KEYWORDS,
]
