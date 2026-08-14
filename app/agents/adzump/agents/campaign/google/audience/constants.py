"""Limits and gates for campaign audience building.

Google's published limits first, then our own guards. The split matters: the first group is
fixed by the API, the second is tuning we chose. Sourcing and measurements are in
docs/demand-gen-audience-mechanisms.md.
"""

from __future__ import annotations

# Google's limits — verified. Not tunable.
# https://github.com/googleapis/googleapis/blob/master/google/ads/googleads/v23/resources/custom_audience.proto
CUSTOM_KEYWORD_MAX_WORDS = 10
CUSTOM_KEYWORD_MAX_CHARS = 80
CUSTOM_KEYWORD_MAX_CHARS_CJK = 40  # double-width languages
CUSTOM_URL_MAX_CHARS = (
    2048  # proto: "An HTTP URL, protocol-included - at most 2048 characters"
)

# INTEREST and PURCHASE_INTENT are rejected on create; AUTO is the documented default.
CUSTOM_AUDIENCE_TYPE = "AUTO"

# Demand Gen's own floor, not the general 100.
# https://developers.google.com/google-ads/api/docs/remarketing/audience-segments/lookalike-audiences
LOOKALIKE_MIN_SEED_SIZE = 1000
USER_LIST_MIN_ACTIVE_SIZE = 100  # before a list can serve at all

# Google's recommendation, not a limit — a custom segment is valid with one keyword. Aim for
# this band; do not reject a thinner one, since a niche product may not have ten terms worth
# using. https://support.google.com/google-ads/answer/9805516
CUSTOM_SEGMENT_KEYWORD_TARGET_MIN = 10
CUSTOM_SEGMENT_KEYWORD_TARGET_MAX = 15

# The only user_interest types an Audience segment accepts — "user list, affinity/in-market,
# life event, and detailed demographic segments". A grouped-mode constraint, so it belongs
# here rather than in the adapter.
# https://developers.google.com/google-ads/api/docs/remarketing/audiences
SEGMENT_TAXONOMY_TYPES = ("AFFINITY", "IN_MARKET")


# Our guards — chosen, not published. Tune from campaign results.

# Google gives no recommended segment count; its guidance is structural, not numeric. Kept
# small because segments in one dimension are OR'd (more = broader, not sharper) and
# optimized targeting already expands past whatever we pick. A ceiling, not a target.
MAX_SIGNALS_PER_KIND = 10

# Weak by necessity: a floor guards against an audience too narrow to spend, but Google
# exposes no size data to judge that with. Report a shortfall rather than pad to reach it.
MIN_SIGNALS_TOTAL = 3

MAX_REFINE_ROUNDS = 2
MAX_SEARCH_RESULTS = 25  # what one search returns to the agent or the panel

# Written into a created resource's description so we recognise our own work later. Matched
# on this, never on name — users rename things.
OWNED_MARKER = "adzump:v1:product="
