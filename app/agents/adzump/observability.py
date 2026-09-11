"""Turn decision record - the rework's debugging instrument (slice 1c).

One structured ``logger.info`` line per AGENTIC turn (the engine re-runs per
LLM call, so mid-message prescription movement is visible), JSON payload.
Log-only: no new behavior, no UI - one grep answers "did anything re-ask
after the answer landed?" during manual testing.

``prior_capture`` is what makes ``repeat_ask`` meaningful: re-prescribing a
field whose answer was just STORED is the shipped-bug class; re-prescribing
after a REJECTED write is ordinary repair.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def prescription_field(missing: list[str]) -> str | None:
    """The spec-field name of the FIRST missing entry (prescription lines are
    ``"<field> - <prose>"``), underscored to match rail/spec field names.
    None = the engine reached review (nothing left to ask)."""
    if not missing:
        return None
    head = missing[0].split(" - ", 1)[0].strip()
    if head.startswith("review"):
        return None
    return head.replace(" ", "_")


def log_turn_decision(
    *,
    session_id: str,
    turn: int,
    agentic_turn: int,
    missing: list[str],
    steers: list[str],
    captures: list[dict],
    prior_capture: dict | None,
    open_rail_field: str | None,
    open_rail_untagged: bool,
    offers: dict[str, str] | None = None,
) -> None:
    """Emit the §8 record. ``open_rail_*`` describe the elicitation that was
    open when the message arrived (snapshotted before the resume section pops
    it): prescribing the SAME field again is ``repeat_ask``; an open rail with
    no field tag can't be compared - SUSPICIOUS, not ignorable."""
    prescription = prescription_field(missing)
    record = {
        "session": session_id,
        "turn": turn,
        "agentic_turn": agentic_turn,
        "prescription": prescription,
        "missing": [
            m.split(" - ", 1)[0].strip().replace(" ", "_") for m in missing
        ],
        "steers": steers,
        "captures": captures,
        "prior_capture": prior_capture,
        "repeat_ask": prescription is not None and prescription == open_rail_field,
        "repeat_ask_unmatched": prescription is not None and open_rail_untagged,
        # Slice 4 · WHY each offer is (or isn't) settled - the signal that was
        # invisible when a failed analysis silently mooted the creatives offer.
        "offers": offers or {},
    }
    logger.info("turn_decision %s", json.dumps(record, ensure_ascii=False, default=str))
