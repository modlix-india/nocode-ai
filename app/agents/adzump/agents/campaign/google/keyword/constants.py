"""Tunable limits and selection rules for keyword research.

API limits and count caps live here as named constants so the pipeline has no
magic numbers. There are deliberately no business or linguistic word lists here:
intent is read from the ad market (the Planner's CPC + competition) and the
selection LLM, and negatives are reasoned from the business model — so nothing
depends on a hand-maintained dictionary that would go stale.
"""

from __future__ import annotations

import re

# Google Ads keyword limits (hard): a keyword is <= 80 chars and <= 10 words.
#   https://developers.google.com/google-ads/api/reference/rpc/v23/KeywordInfo
#   https://support.google.com/google-ads/answer/6372655
KEYWORD_MIN_LENGTH = 2  # chars — drop fragments
KEYWORD_MAX_LENGTH = (
    80  # chars — Google Ads keyword text limit (negatives use this too)
)
KEYWORD_MAX_WORDS = 10  # words — Google Ads keyword word limit

# Commercial-value signal: Google returns a competition level and top-of-page CPC;
# advertiser competition indicates a commercial query. Used only as a tiebreaker
# when no lexical modifier matches — no absolute CPC threshold (currency-dependent).
#   https://support.google.com/google-ads/answer/2472731
COMMERCIAL_VALUE_COMPETITION_LEVELS = frozenset({"MEDIUM", "HIGH"})

# Count caps — bound the work so the pipeline stays inside its latency budget.
MAX_SEEDS = 80  # seeds generated per type
MAX_SEEDS_TO_EXPAND = 30  # top seeds sent to autosuggest (caps fan-out)
MAX_EXPANSION_CANDIDATES = 200  # seeds+suggestions sent TO the Planner (caps API calls)
MAX_STORED_CANDIDATES = (
    600  # scored ideas kept FROM the Planner (it expands beyond the input)
)
TARGET_POSITIVE_COUNT = 30  # positives selected per type
MAX_NEGATIVE_COUNT = 40  # negatives kept per type
MAX_SERVICE_AREAS = 8  # service-area cities carried into the seed prompt
MAX_REJECTIONS_RECORDED = (
    50  # 'why not this keyword' ledger, per rule — rides the session
)

# Display / logging bounds.
MAX_CANDIDATES_SHOWN = 120  # expansion-pool phrases echoed back from expand_keywords
CANDIDATES_PAGE = 300  # scored candidates shown per keyword_metrics / fetch_more page
# Tool-result cap for the candidate page. This is the MODEL's working context only
# (the keyword agent swallows tool_result events — never reaches the chat UI; the user
# sees only the curated positives/negatives in the review panel).
KEYWORD_METRICS_RESULT_MAX = 16000
BUSINESS_TEXT_MAX = 4000  # business-context chars injected into the system prompt
LOG_TRUNCATE = 160  # max chars of an exception/error string in logs and messages

# A candidate negative sharing >= 80% of its tokens with the positive set is
# dropped — it would block core business traffic.
NEGATIVE_POSITIVE_TOKEN_OVERLAP_MAX = 0.8

# Safety filter — a negative matching any pattern is rejected (URLs, bare numbers,
# adult terms, blank strings).
SAFETY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"http[s]?://", re.IGNORECASE),
    re.compile(r"\.com", re.IGNORECASE),
    re.compile(r"\.net", re.IGNORECASE),
    re.compile(r"\.org", re.IGNORECASE),
    re.compile(r"^\d+$"),
    re.compile(r"(xxx|porn|adult|sex)", re.IGNORECASE),
    re.compile(r"^\s*$"),
)

# Extensions added per business profile on top of autosuggest's base default
# (autosuggest.DEFAULT_SOURCE_NAMES). Names index the autosuggest.SOURCES registry.
SOURCE_YOUTUBE = "youtube"

INFORMATIONAL_SOURCE_NAMES = (
    SOURCE_YOUTUBE,
)  # buyer's funnel includes how-to/informational
