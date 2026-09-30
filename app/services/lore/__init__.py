"""Lore: curated, growing knowledge about each application we build.

The problem it solves: an app that took three months to build carries three
months of decisions, conventions, constraints and hard-won gotchas, and none of
that survives anywhere a second person can find it. The definitions say WHAT the
app is; nothing says WHY, or what was tried and abandoned, or which of the
sixteen possible ways to do a thing this app settled on. Six months later, the
person picking it up (or the agent) starts from zero.

Lore accumulates that second layer without anyone having to remember to write
it down, and serves it back as a briefing.

Two layers, kept apart on purpose:

    observations  raw, append-only, cheap. Anything that watches the app writes
                  them: agent turns, definition edits, inventory snapshots,
                  documents, run outcomes, and people writing notes.

    entries       curated knowledge. One durable claim per row, typed by kind,
                  carrying provenance back to the observations that produced it
                  and a confidence score. An entry loses standing when
                  something supersedes or contradicts it, or when the object it
                  describes changes under it. Only `status` and `owner` expire
                  with time.

The curator is the only thing that turns the first into the second, and it is
the only place an LLM is involved: the model proposes operations, this code
validates and applies them.

Module map
    models      taxonomy, hashing, effective confidence. Pure, no I/O.
    store       data access. No policy.
    ingest      source adapters. Best-effort, never raise into a caller.
    curator     observations -> entries. The LLM pass and its guard rails.
    retrieval   search, briefings, per-object knowledge, gap analysis.
    tools       the five agent verbs.
    router      HTTP surface at /api/ai/lore.

Relationship to cfa_app_kb: that table is six narrative sections per app that
the agent writes on request. Lore reads it as a source (`ingest.from_app_kb`)
and never writes to it.
"""

from app.services.lore import curator, ingest, models, retrieval, store, tools
from app.services.lore.models import (
    ENTRY_KINDS,
    OBSERVATION_KINDS,
    Entry,
    Observation,
    normalise_subject,
)

__all__ = [
    "curator",
    "ingest",
    "models",
    "retrieval",
    "store",
    "tools",
    "ENTRY_KINDS",
    "OBSERVATION_KINDS",
    "Entry",
    "Observation",
    "normalise_subject",
]
