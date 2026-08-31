"""Lore vocabulary and pure functions.

Everything here is deterministic and dependency-free so it can be unit tested
without a database or an LLM. The taxonomy lives here because it is the part of
lore that is hardest to change later: entry kinds end up in stored rows, in
prompts, and in the UI.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ── Observation kinds ────────────────────────────────────────────────────
# What produced the raw fact. Kept small on purpose; a new source should map
# onto an existing kind unless it genuinely behaves differently.

OBSERVATION_KINDS: tuple[str, ...] = (
    "chat",       # something said in an agent session (user or agent)
    "edit",       # a definition was created / changed / deleted
    "inventory",  # a snapshot of what objects exist in the app
    "doc",        # a KB section, blog post, markdown file, README
    "manual",     # a person wrote this down deliberately
    "run",        # a function execution, an error, a job outcome
    "review",     # feedback on something the agent did
)

# ── Entry kinds ──────────────────────────────────────────────────────────
# What kind of durable knowledge this is. An entry loses standing when
# something supersedes or contradicts it, not when time passes: see the note
# above TIME_BOUND_HALF_LIFE_DAYS below.

ENTRY_KINDS: tuple[str, ...] = (
    "purpose",      # what this app / object is for, in business terms
    "decision",     # a choice that was made, and why
    "convention",   # a pattern this app follows (naming, theming, binding)
    "constraint",   # a rule that must hold — business or technical
    "integration",  # an external system this app talks to, and how
    "glossary",     # a domain term and what it means *here*
    "gotcha",       # a trap someone already hit
    "howto",        # a repeatable procedure specific to this app
    "owner",        # who knows about what
    "status",       # what is in flight right now
)

ENTRY_KIND_HELP: dict[str, str] = {
    "purpose": "What this app or object exists to do, in the language of the business.",
    "decision": "A choice that was made and the reasoning behind it. Never silently rewritten.",
    "convention": "A pattern this app follows: naming, theming, binding, layout, file layout.",
    "constraint": "A rule that must hold. Breaking it breaks the app or the business.",
    "integration": "An external system this app talks to, and the shape of that contact.",
    "glossary": "A domain term and what it means in this app specifically.",
    "gotcha": "A trap that has already cost someone time. Written so the next person avoids it.",
    "howto": "A repeatable procedure specific to this app.",
    "owner": "Who knows about a part of this app, or who asked for it.",
    "status": "What is being worked on right now. The one kind that expires with time.",
}

# ── Why almost nothing decays with time ──────────────────────────────────
#
# The first version of this decayed every kind on a per-kind half-life, on the
# theory that age is a proxy for "this might be wrong". It is a bad proxy, and
# it fails in both directions: an app nobody has touched for two years has
# perfectly true lore that has decayed to noise, while an app under daily churn
# keeps high-confidence lore that went wrong last week. Worse, decay is silent.
# A fading entry never asks to be checked, it just stops surfacing, which
# deletes the knowledge instead of correcting it — the opposite of the point.
#
# Two things actually make an entry wrong, and both are observable:
#
#   supersession   something newer replaced it. The curator says so explicitly
#                  and the old row goes to status 'superseded'.
#   contradiction  something newer disagrees with it, and nobody has decided
#                  which wins. Recorded as a `contradicts` link.
#
# A third thing makes it *suspect* rather than wrong: the object it describes
# changed after the entry was last confirmed. That is a real signal the
# platform already carries (every definition has an updated timestamp), and it
# is surfaced as "unverified since X" rather than as a quietly shrinking
# number, so a person or the curator can settle it.
#
# Time decay survives only for the kinds whose truth is *defined* relative to
# now. For these, age is not a proxy for staleness, it is the semantics.
TIME_BOUND_HALF_LIFE_DAYS: dict[str, float] = {
    "status": 14.0,   # "we are mid-migration on X" is about today by definition
    "owner": 180.0,   # people move teams
}

# Each unresolved contradiction halves confidence. Two independent entries
# disagreeing is a strong signal that at least one is wrong, and we do not know
# which, so both drop and both surface as contested.
CONTRADICTION_FACTOR = 0.5

# The entry's subject was edited after the entry was last confirmed. Not wrong,
# but no longer evidenced. Deliberately mild: it flags, it does not bury.
STALE_SUBJECT_FACTOR = 0.75

ENTRY_STATUSES: tuple[str, ...] = ("active", "superseded", "retired", "draft")

LINK_RELATIONS: tuple[str, ...] = (
    "supersedes", "relates_to", "contradicts", "depends_on", "example_of",
)

# Order used when rendering a briefing. Purpose first, then the things that
# constrain what you may do, then the history, then what is happening now.
BRIEF_ORDER: tuple[str, ...] = (
    "purpose", "constraint", "convention", "glossary", "integration",
    "gotcha", "howto", "decision", "owner", "status",
)

# An entry the curator produced is a draft below this decayed confidence; the
# briefing marks anything below it as unverified rather than hiding it.
UNVERIFIED_BELOW = 40

MAX_TITLE = 240
MAX_BODY = 20000
MAX_SUBJECT = 160


# ── Subjects ─────────────────────────────────────────────────────────────
# A subject names what an entry is about. "app" is the whole application;
# anything else is "<type>:<name>" so it can be matched against the object tree.

_SUBJECT_RE = re.compile(r"^(app|[a-z][a-z0-9_]{1,30}:[A-Za-z0-9_.\-/{}]{1,120})$")

SUBJECT_TYPES: tuple[str, ...] = (
    "page", "storage", "function", "schema", "style", "theme", "template",
    "connection", "event", "workflow", "uripath", "role", "profile",
    "notification", "job", "domain", "release", "component",
)


def normalise_subject(subject: str | None) -> str:
    """Coerce a caller-supplied subject into the canonical form.

    Unrecognised shapes collapse to "app" rather than raising: a bad subject
    should degrade to app-level knowledge, never lose the observation.
    """
    if not subject:
        return "app"
    s = subject.strip()
    if not s or s.lower() == "app":
        return "app"
    if ":" in s:
        kind, _, name = s.partition(":")
        kind = kind.strip().lower()
        name = name.strip()
        if kind and name:
            s = f"{kind}:{name}"
    s = s[:MAX_SUBJECT]
    return s if _SUBJECT_RE.match(s) else "app"


def subject_type(subject: str) -> str:
    """"page:jobsToday" -> "page"; "app" -> "app"."""
    return subject.partition(":")[0] if ":" in subject else subject


# ── Hashing / fingerprints ───────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def normalise_text(text: str) -> str:
    """Whitespace- and case-insensitive form used for fingerprinting.

    Two observations that differ only in wrapping or capitalisation are the same
    observation, which is what makes repeat sightings collapse into SEEN_COUNT.
    """
    return _WS_RE.sub(" ", (text or "").strip()).lower()


def fingerprint(kind: str, subject: str, body: str) -> str:
    """Stable identity for an observation within one (client, app)."""
    payload = f"{kind}\x00{normalise_subject(subject)}\x00{normalise_text(body)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def body_hash(title: str, body: str) -> str:
    """Stable identity for an entry's content within one (client, app, kind)."""
    payload = f"{normalise_text(title)}\x00{normalise_text(body)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Confidence decay ─────────────────────────────────────────────────────


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def effective_confidence(
    kind: str,
    confidence: int,
    last_confirmed_at: Any,
    *,
    now: datetime | None = None,
    source_count: int = 1,
    pinned: bool = False,
    contradicted_by: int = 0,
    subject_changed_at: Any = None,
) -> int:
    """Recorded confidence adjusted for what we have since learned about it.

    In order of strength:

    * **contradiction** — each unresolved `contradicts` link halves it. This is
      the main mechanism: an entry loses standing because something disagrees
      with it, not because a calendar advanced.
    * **stale subject** — the object the entry is about was edited after the
      entry was last confirmed. A mild haircut and an "unverified" flag.
    * **age** — only for `status` and `owner`, whose truth is defined relative
      to now. Every other kind holds its confidence until contradicted.
    * **corroboration** — log2 of distinct observations, capped at +25%, so ten
      mentions of a wrong thing cannot outrank one confirmed fact.

    A pinned entry (a human vouched for it) is returned as recorded: none of
    the above applies, because a person already answered the question each of
    them is guessing at.
    """
    if pinned:
        return max(0, min(100, int(confidence)))

    now = now or datetime.now(timezone.utc)
    seen = _as_utc(last_confirmed_at)
    base = max(0.0, min(100.0, float(confidence)))

    if contradicted_by > 0:
        base *= CONTRADICTION_FACTOR ** min(contradicted_by, 4)

    if is_subject_stale(last_confirmed_at, subject_changed_at):
        base *= STALE_SUBJECT_FACTOR

    half_life = TIME_BOUND_HALF_LIFE_DAYS.get(kind)
    if half_life and seen is not None:
        age_days = max(0.0, (now - seen).total_seconds() / 86400.0)
        base *= 0.5 ** (age_days / half_life)

    if source_count > 1:
        base *= 1.0 + min(0.25, 0.08 * math.log2(source_count))

    return max(0, min(100, int(round(base))))


def is_subject_stale(last_confirmed_at: Any, subject_changed_at: Any) -> bool:
    """Did the thing this entry describes change after we last confirmed it?

    Unknown either way is not stale. This is a signal to show a person, so a
    false positive costs more than a false negative.
    """
    seen = _as_utc(last_confirmed_at)
    changed = _as_utc(subject_changed_at)
    if seen is None or changed is None:
        return False
    return changed > seen


# ── Validation ───────────────────────────────────────────────────────────


def validate_observation_kind(kind: str) -> str | None:
    if kind not in OBSERVATION_KINDS:
        return f"Unknown observation kind '{kind}'. Valid: {', '.join(OBSERVATION_KINDS)}"
    return None


def validate_entry_kind(kind: str) -> str | None:
    if kind not in ENTRY_KINDS:
        return f"Unknown entry kind '{kind}'. Valid: {', '.join(ENTRY_KINDS)}"
    return None


def validate_status(status: str) -> str | None:
    if status not in ENTRY_STATUSES:
        return f"Unknown status '{status}'. Valid: {', '.join(ENTRY_STATUSES)}"
    return None


# ── Row shapes ───────────────────────────────────────────────────────────
# Thin dataclasses so callers get attribute access and a stable to_dict()
# instead of passing raw DictCursor rows around.


@dataclass
class Observation:
    id: int
    client_code: str
    app_code: str
    kind: str
    source: str
    subject: str
    body: str
    meta: dict[str, Any] = field(default_factory=dict)
    seen_count: int = 1
    observed_by: int = 0
    observed_at: Any = None
    last_seen_at: Any = None
    curated_at: Any = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Observation":
        import json as _json
        meta = row.get("META")
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except ValueError:
                meta = {}
        return cls(
            id=int(row["ID"]),
            client_code=row["CLIENT_CODE"],
            app_code=row["APP_CODE"],
            kind=row["KIND"],
            source=row["SOURCE"],
            subject=row["SUBJECT"],
            body=row["BODY"],
            meta=meta or {},
            seen_count=int(row.get("SEEN_COUNT") or 1),
            observed_by=int(row.get("OBSERVED_BY") or 0),
            observed_at=row.get("OBSERVED_AT"),
            last_seen_at=row.get("LAST_SEEN_AT"),
            curated_at=row.get("CURATED_AT"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "subject": self.subject,
            "body": self.body,
            "meta": self.meta,
            "seen_count": self.seen_count,
            "observed_at": str(self.observed_at) if self.observed_at else None,
            "curated": self.curated_at is not None,
        }


@dataclass
class Entry:
    id: int
    client_code: str
    app_code: str
    kind: str
    subject: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    confidence: int = 50
    status: str = "active"
    superseded_by: int | None = None
    # Set when this entry overrides a base-client entry for this client only.
    base_entry_id: int | None = None
    # Filled at read time by store.resolve_overrides: did this come from a
    # client above the caller in the app's inheritance chain?
    inherited: bool = False
    # Filled at read time by store.annotate_standing: how many active entries
    # currently contradict this one, and when the object it describes was last
    # edited. Both are the evidence that replaced time decay.
    contradicted_by: int = 0
    subject_changed_at: Any = None
    source_count: int = 1
    version: int = 1
    pinned: bool = False
    first_seen_at: Any = None
    last_confirmed_at: Any = None
    updated_at: Any = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Entry":
        import json as _json
        tags = row.get("TAGS")
        if isinstance(tags, str):
            try:
                tags = _json.loads(tags)
            except ValueError:
                tags = []
        return cls(
            id=int(row["ID"]),
            client_code=row["CLIENT_CODE"],
            app_code=row["APP_CODE"],
            kind=row["KIND"],
            subject=row["SUBJECT"],
            title=row["TITLE"],
            body=row["BODY"],
            tags=list(tags or []),
            confidence=int(row.get("CONFIDENCE") or 50),
            status=row.get("STATUS") or "active",
            superseded_by=(int(row["SUPERSEDED_BY"]) if row.get("SUPERSEDED_BY") else None),
            base_entry_id=(int(row["BASE_ENTRY_ID"]) if row.get("BASE_ENTRY_ID") else None),
            source_count=int(row.get("SOURCE_COUNT") or 1),
            version=int(row.get("VERSION") or 1),
            pinned=bool(row.get("PINNED")),
            first_seen_at=row.get("FIRST_SEEN_AT"),
            last_confirmed_at=row.get("LAST_CONFIRMED_AT"),
            updated_at=row.get("UPDATED_AT"),
        )

    @property
    def effective_confidence(self) -> int:
        return effective_confidence(
            self.kind, self.confidence, self.last_confirmed_at,
            source_count=self.source_count, pinned=self.pinned,
            contradicted_by=self.contradicted_by,
            subject_changed_at=self.subject_changed_at,
        )

    @property
    def standing(self) -> str | None:
        """Why this entry is not simply trusted, in one word, or None.

        Read by the briefing so a reader sees *why* an entry is marked down
        instead of only seeing a smaller number.
        """
        if self.pinned:
            return None
        if self.contradicted_by > 0:
            return "contested"
        if is_subject_stale(self.last_confirmed_at, self.subject_changed_at):
            return "unverified"
        return None

    def to_dict(self, *, include_body: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "client_code": self.client_code,
            "inherited": self.inherited,
            "kind": self.kind,
            "subject": self.subject,
            "title": self.title,
            "tags": self.tags,
            "confidence": self.confidence,
            "effective_confidence": self.effective_confidence,
            "status": self.status,
            "pinned": self.pinned,
            "source_count": self.source_count,
            "version": self.version,
            "first_seen_at": str(self.first_seen_at) if self.first_seen_at else None,
            "last_confirmed_at": str(self.last_confirmed_at) if self.last_confirmed_at else None,
        }
        if include_body:
            d["body"] = self.body
        if self.superseded_by:
            d["superseded_by"] = self.superseded_by
        if self.base_entry_id:
            d["overrides"] = self.base_entry_id
        if self.contradicted_by:
            d["contradicted_by"] = self.contradicted_by
        if self.standing:
            d["standing"] = self.standing
        if self.subject_changed_at:
            d["subject_changed_at"] = str(self.subject_changed_at)
        return d
