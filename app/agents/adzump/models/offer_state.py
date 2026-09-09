"""Three-valued offer lifecycle (HLD/LLD doc D8).

An offer (competitive analysis, competitor creatives, Instagram) is UNSET
until the user answers, then ACCEPTED or DECLINED. Replaces the legacy
``*_declined="true"`` string markers, whose absence conflated "never offered"
with "offered and unanswered" and whose presence-only shape could never
record a yes.
"""
from __future__ import annotations

from enum import Enum


class OfferState(str, Enum):
    """str-Enum: model_dump is JSON-safe and value equality (== "declined")
    holds during migration."""

    UNSET = "unset"        # not yet offered, or offered and unanswered
    ACCEPTED = "accepted"  # user said yes
    DECLINED = "declined"  # user said no; never re-offered unprompted

    @classmethod
    def from_legacy(cls, declined_marker: object) -> "OfferState":
        """Map a legacy ``*_declined`` marker: "true" (any case) -> DECLINED,
        anything else -> UNSET. ACCEPTED is never derived from a legacy marker;
        it only comes from a data-backed signal (a stored yes)."""
        return (
            cls.DECLINED
            if str(declined_marker).strip().lower() == "true"
            else cls.UNSET
        )


class OfferResolution(str, Enum):
    """WHY an offer no longer needs asking (slice 4). OfferState records the
    user's answer; resolution is the derived verdict the journey step, the
    review gate, and the turn record all read - typed, with the reason kept,
    so a wrongly-settled offer is visible in one grep (live 2026-09-08: a
    failed analysis stored an empty list, the offer silently went moot, and
    the boolean predicate hid which of five signals had fired)."""

    OPEN = "open"            # still owed: ask it (or fulfil an accepted one)
    DECLINED = "declined"    # user said no (to it, or to its prerequisite)
    FULFILLED = "fulfilled"  # the offered work actually happened
    MOOT = "moot"            # nothing to offer (e.g. analysis found no rivals)
    EXHAUSTED = "exhausted"  # asked twice, no answer - never nag further
