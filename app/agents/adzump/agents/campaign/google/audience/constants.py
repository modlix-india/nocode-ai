"""Limits, guards and panel wording for campaign audience building.

Only the first group is fixed by the API; the rest we chose. Each carries its source.
"""

from __future__ import annotations

from typing import NamedTuple

# Google's limits — verified. Not tunable.
# https://github.com/googleapis/googleapis/blob/master/google/ads/googleads/v23/resources/custom_audience.proto
CUSTOM_KEYWORD_MAX_WORDS = 10
CUSTOM_KEYWORD_MAX_CHARS = 80
CUSTOM_KEYWORD_MAX_CHARS_CJK = 40  # double-width languages
CUSTOM_URL_MAX_CHARS = (
    2048  # proto: "An HTTP URL, protocol-included - at most 2048 characters"
)

# Kept though unused - Lookalike and audience reuse are paused, see AGENT.md 6.3.
# https://developers.google.com/google-ads/api/docs/remarketing/audience-segments/lookalike-audiences
LOOKALIKE_MIN_SEED_SIZE = 1000  # Demand Gen's own floor, not the general 100
USER_LIST_MIN_ACTIVE_SIZE = 100  # before a list can serve at all

# A recommendation, not a limit - one keyword is valid, so never reject a thinner segment.
# From the Demand Gen FAQ, NOT the "About custom segments" page, which states no number:
# https://support.google.com/google-ads/answer/14509385
CUSTOM_SEGMENT_KEYWORD_TARGET_MIN = 10
CUSTOM_SEGMENT_KEYWORD_TARGET_MAX = 15

# No published cap on members per custom audience - do not invent one.

# The only user_interest types an Audience segment accepts - a grouped-mode constraint, not
# an adapter one. https://developers.google.com/google-ads/api/docs/remarketing/audiences
SEGMENT_TAXONOMY_TYPES = ("AFFINITY", "IN_MARKET")


# Our guards — chosen, not published. Tune from campaign results.

# A ceiling, not a target. Google publishes no count; segments in one dimension are OR'd, so
# more is broader rather than sharper, and optimized targeting expands past whatever we pick.
MAX_SIGNALS_PER_KIND = 10

# Weak by necessity: a floor guards against an audience too narrow to spend, but Google
# exposes no size data to judge that with. Report a shortfall rather than pad to reach it.
MIN_SIGNALS_TOTAL = 3

MAX_SEARCH_RESULTS = 25  # what one search returns to the agent or the panel

# An API payload in an exception string will otherwise flood the log line.
LOG_ERROR_MAX_CHARS = 250

ERROR_ITEMS_DISPLAY_MAX = 5
DRAFT_TERMS_DISPLAY_MAX = 30


# What the panel says.

# Built from the limits above so the text and the validator can never disagree.
MEMBER_HELP = {
    "terms": (
        f"What people type into Google. {CUSTOM_SEGMENT_KEYWORD_TARGET_MIN}-"
        f"{CUSTOM_SEGMENT_KEYWORD_TARGET_MAX} works best - too few limits reach, too many "
        f"blurs the intent. Each is up to {CUSTOM_KEYWORD_MAX_WORDS} words and "
        f"{CUSTOM_KEYWORD_MAX_CHARS} characters."
    ),
    "urls": (
        "Pages your buyer browses - a competitor, a review site, a marketplace listing. "
        "Google finds people who visit sites LIKE these, not just these exact pages. "
        "Include https://."
    ),
    "apps": (
        "Android apps your buyer uses, by package name (com.nobroker.app) - copy it from "
        "the id= part of the Play Store link. iOS apps cannot be targeted this way."
    ),
}

class Dimension(NamedTuple):
    """One demographic filter, named for its DemographicSpec field. Everything the panel and
    the model need about it in one place - split across dicts, a new one arrives half-defined
    and fails at the first lookup that forgot it."""

    field: str
    label: str  # the panel's row and control heading
    help: str
    unknown_help: str  # shown beside the dimension's Unknown box


DIMENSIONS = (
    Dimension(
        "age_ranges",
        "Age",
        "Google's fixed brackets - you cannot pick an exact age. Leave it as Everyone "
        "unless the product genuinely does not apply to a whole bracket.",
        "Keep people whose age Google could not determine - a large share.",
    ),
    Dimension(
        "genders",
        "Gender",
        "Male and female are the only values Google exposes.",
        "Keep people whose gender Google could not determine - a large share.",
    ),
    Dimension(
        "income_ranges",
        "Household income",
        "A percentile band of household income in the country you target - not an amount. "
        "Pick a top band and a bottom one; Google only estimates income in some countries.",
        "Keep people whose household income Google could not determine. It only estimates "
        "income in some countries, so switching this off can remove nearly everyone.",
    ),
    Dimension(
        "parental_statuses",
        "Parental status",
        "Filters everyone else out. Different from the Detailed Demographics segment of the "
        "same name above, which instead ADDS parents to your reach.",
        "Keep people whose parental status Google could not determine - a large share.",
    ),
)

# The mix is what decides the segment's character, so it leads the section.
MEMBER_MIX_HELP = (
    "Mix all three where you can. Google reads the combination and picks whether to "
    "optimise this segment for Reach, Consideration or Performance - search terms alone "
    "give it less to work with."
)

# Our own resources, and the state that tracks them.

# Written into a resource's description; matched on this, never on name - users rename.
OWNED_MARKER = "adzump:v1:product="

# A blueprint until launch, when publish swaps this for the real resource name.
PENDING_PREFIX = "pending:customAudience:"
BLUEPRINTS_KEY = "aud_custom_blueprints"  # ref -> {label, terms, urls, apps}


def pending_ref(label: str) -> str:
    return f"{PENDING_PREFIX}{label}"


def is_pending(ref: str) -> bool:
    return str(ref).startswith(PENDING_PREFIX)
