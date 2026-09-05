"""Data access for lore. No business policy, no LLM.

Everything is scoped by (client_code, app_code). The only cleverness here is
that writes are idempotent by content hash:

  - `record_observation` collapses a repeat sighting into SEEN_COUNT + LAST_SEEN_AT
  - `add_entry` collapses an identical entry into a confirmation

That is what lets a caller fire observations liberally without worrying about
producing duplicates.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Sequence

from app.db.connection import execute_query
from app.services.lore.models import (
    Entry,
    Observation,
    body_hash,
    fingerprint,
    normalise_subject,
)

logger = logging.getLogger(__name__)

_OBS_COLS = """ID, CLIENT_CODE, APP_CODE, KIND, SOURCE, SUBJECT, BODY, META,
               SEEN_COUNT, OBSERVED_BY, OBSERVED_AT, LAST_SEEN_AT, CURATED_AT"""

_ENTRY_COLS = """ID, CLIENT_CODE, APP_CODE, KIND, SUBJECT, TITLE, BODY, TAGS,
                 CONFIDENCE, STATUS, SUPERSEDED_BY, BASE_ENTRY_ID, SEED_KEY,
                 SEED_SOURCE, SOURCE_COUNT,
                 BODY_HASH, VERSION, PINNED, CREATED_BY, UPDATED_BY,
                 FIRST_SEEN_AT, LAST_CONFIRMED_AT, UPDATED_AT"""


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return None


# ── Observations ─────────────────────────────────────────────────────────


async def record_observation(
    client_code: str,
    app_code: str,
    *,
    kind: str,
    source: str,
    body: str,
    subject: str = "app",
    meta: dict[str, Any] | None = None,
    observed_by: int = 0,
) -> dict[str, Any]:
    """Insert an observation, or bump the existing identical one.

    Returns {"id": int, "created": bool, "seen_count": int}. Never raises on a
    duplicate — the unique fingerprint index is the dedupe mechanism, and a
    second sighting is a signal (this fact keeps coming up), not an error.
    """
    subject = normalise_subject(subject)
    fp = fingerprint(kind, subject, body)

    # ON DUPLICATE KEY makes this one round trip and one atomic decision, which
    # matters because several agent turns can land concurrently for one app.
    await execute_query(
        """INSERT INTO lore_observation
               (CLIENT_CODE, APP_CODE, KIND, SOURCE, SUBJECT, BODY, META,
                FINGERPRINT, OBSERVED_BY)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON DUPLICATE KEY UPDATE
               SEEN_COUNT = SEEN_COUNT + 1,
               LAST_SEEN_AT = CURRENT_TIMESTAMP,
               CURATED_AT = NULL""",
        (client_code, app_code, kind, source[:160], subject, body,
         _json_or_none(meta), fp, observed_by),
    )
    rows = await execute_query(
        """SELECT ID, SEEN_COUNT FROM lore_observation
            WHERE CLIENT_CODE=%s AND APP_CODE=%s AND FINGERPRINT=%s""",
        (client_code, app_code, fp),
    )
    if not rows:
        # Should not happen; surfacing it beats returning a fake id.
        raise RuntimeError("lore: observation insert produced no row")
    row = rows[0]
    seen = int(row["SEEN_COUNT"])
    return {"id": int(row["ID"]), "created": seen == 1, "seen_count": seen}


async def record_observations(
    client_code: str, app_code: str, observations: Iterable[dict[str, Any]],
) -> dict[str, int]:
    """Bulk variant. Each dict takes the same keys as `record_observation`."""
    created = repeated = 0
    for obs in observations:
        try:
            result = await record_observation(client_code, app_code, **obs)
        except Exception:
            logger.exception("lore: failed to record observation for %s/%s", client_code, app_code)
            continue
        if result["created"]:
            created += 1
        else:
            repeated += 1
    return {"created": created, "repeated": repeated}


async def pending_observations(
    client_code: str, app_code: str, limit: int = 60, *, max_attempts: int = 3,
) -> list[Observation]:
    """Observations the curator has not yet processed, oldest first.

    Oldest-first matters: knowledge should accumulate in the order it happened,
    so a later correction supersedes an earlier claim rather than racing it.
    """
    limit = max(1, min(limit, 400))
    # CURATION_ATTEMPTS is the loop-breaker. An observation the model has been
    # shown `max_attempts` times without it yielding anything is dropped from
    # the queue rather than marked curated: those two states mean different
    # things, and CURATED_AT is read by annotate_standing.
    rows = await execute_query(
        f"""SELECT {_OBS_COLS} FROM lore_observation
             WHERE CLIENT_CODE=%s AND APP_CODE=%s AND CURATED_AT IS NULL
               AND CURATION_ATTEMPTS < %s
          ORDER BY ID ASC LIMIT %s""",
        (client_code, app_code, max_attempts, limit),
    )
    return [Observation.from_row(r) for r in (rows or [])]


async def bump_curation_attempts(ids: Sequence[int]) -> int:
    """Record that these observations were shown to the model.

    Called for the rows a pass actually rendered, whether or not the pass
    produced anything. Separate from mark_observations_curated so a failed
    pass can leave rows pending without them re-entering every batch forever.
    """
    if not ids:
        return 0
    marks = ",".join(["%s"] * len(ids))
    return int(await execute_query(
        f"UPDATE lore_observation SET CURATION_ATTEMPTS = CURATION_ATTEMPTS + 1 "
        f"WHERE ID IN ({marks})",
        tuple(int(i) for i in ids),
    ) or 0)


async def mark_observations_curated(ids: Sequence[int]) -> int:
    if not ids:
        return 0
    placeholders = ",".join(["%s"] * len(ids))
    return await execute_query(
        f"""UPDATE lore_observation SET CURATED_AT = CURRENT_TIMESTAMP
             WHERE ID IN ({placeholders})""",
        tuple(int(i) for i in ids),
    )


async def get_observations(ids: Sequence[int]) -> list[Observation]:
    if not ids:
        return []
    placeholders = ",".join(["%s"] * len(ids))
    rows = await execute_query(
        f"SELECT {_OBS_COLS} FROM lore_observation WHERE ID IN ({placeholders})",
        tuple(int(i) for i in ids),
    )
    return [Observation.from_row(r) for r in (rows or [])]


async def count_pending(client_code: str, app_code: str) -> int:
    rows = await execute_query(
        """SELECT COUNT(*) AS n FROM lore_observation
            WHERE CLIENT_CODE=%s AND APP_CODE=%s AND CURATED_AT IS NULL""",
        (client_code, app_code),
    )
    return int(rows[0]["n"]) if rows else 0


async def busiest_pending_subject(client_code: str, app_code: str) -> tuple[str, int]:
    """The subject with the most uncurated observations, and how many.

    ("", 0) when nothing is pending. Used to trigger curation on depth about
    one object rather than only on app-wide volume: eight edits against one
    page is a curatable story, thirty scattered across thirty objects is not.
    """
    rows = await execute_query(
        """SELECT SUBJECT, COUNT(*) AS n FROM lore_observation
            WHERE CLIENT_CODE=%s AND APP_CODE=%s AND CURATED_AT IS NULL
         GROUP BY SUBJECT ORDER BY n DESC LIMIT 1""",
        (client_code, app_code),
    )
    if not rows:
        return "", 0
    return str(rows[0]["SUBJECT"]), int(rows[0]["n"])


# ── Entries ──────────────────────────────────────────────────────────────


async def add_entry(
    client_code: str,
    app_code: str,
    *,
    kind: str,
    title: str,
    body: str,
    subject: str = "app",
    tags: list[str] | None = None,
    confidence: int = 50,
    status: str = "active",
    pinned: bool = False,
    created_by: int = 0,
    source_ids: Sequence[int] = (),
    base_entry_id: int | None = None,
    seed_key: str | None = None,
    seed_source: str | None = None,
) -> dict[str, Any]:
    """Insert a new entry under `client_code`, or confirm the identical one.

    "Identical" is (client, app, kind, body_hash). Re-deriving the same fact
    from new observations is a confirmation: it bumps SOURCE_COUNT and
    LAST_CONFIRMED_AT, which resets the decay clock. That is the mechanism by
    which knowledge that stays true stays trusted.

    `base_entry_id` makes this row an OVERRIDE of a base-client entry: the base
    stays untouched and other clients keep seeing it, while this client sees
    this instead. That is how a client with edit access on somebody else's app
    changes what they know without changing what the owner knows.
    """
    subject = normalise_subject(subject)
    bh = body_hash(title, body)

    existing = await execute_query(
        f"""SELECT {_ENTRY_COLS} FROM lore_entry
             WHERE CLIENT_CODE=%s AND APP_CODE=%s AND KIND=%s AND BODY_HASH=%s""",
        (client_code, app_code, kind, bh),
    )
    if existing:
        entry_id = int(existing[0]["ID"])
        await confirm_entry(entry_id, source_ids=source_ids, confidence=confidence)
        return {"id": entry_id, "created": False}

    entry_id = await execute_query(
        """INSERT INTO lore_entry
               (CLIENT_CODE, APP_CODE, KIND, SUBJECT, TITLE, BODY, TAGS,
                CONFIDENCE, STATUS, SOURCE_COUNT, BODY_HASH, PINNED,
                CREATED_BY, UPDATED_BY, BASE_ENTRY_ID, SEED_KEY, SEED_SOURCE)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (client_code, app_code, kind, subject, title[:240], body,
         _json_or_none(tags or []), max(0, min(100, int(confidence))), status,
         max(1, len(source_ids) or 1), bh, 1 if pinned else 0,
         created_by, created_by, base_entry_id,
         (seed_key or None), (seed_source or None)),
    )
    await link_sources(int(entry_id), source_ids)
    return {"id": int(entry_id), "created": True}


async def confirm_entry(
    entry_id: int,
    *,
    source_ids: Sequence[int] = (),
    confidence: int | None = None,
) -> None:
    """Another observation says this entry is still true.

    Resets the decay clock and recounts distinct backing observations. Takes the
    HIGHER of old and new confidence rather than overwriting, so a low-confidence
    re-derivation cannot demote a fact a human already vouched for.
    """
    await link_sources(entry_id, source_ids)
    if confidence is None:
        await execute_query(
            """UPDATE lore_entry
                  SET LAST_CONFIRMED_AT = CURRENT_TIMESTAMP,
                      SOURCE_COUNT = GREATEST(SOURCE_COUNT,
                          (SELECT COUNT(*) FROM lore_entry_source WHERE ENTRY_ID=%s))
                WHERE ID=%s""",
            (entry_id, entry_id),
        )
    else:
        await execute_query(
            """UPDATE lore_entry
                  SET LAST_CONFIRMED_AT = CURRENT_TIMESTAMP,
                      CONFIDENCE = GREATEST(CONFIDENCE, %s),
                      SOURCE_COUNT = GREATEST(SOURCE_COUNT,
                          (SELECT COUNT(*) FROM lore_entry_source WHERE ENTRY_ID=%s))
                WHERE ID=%s""",
            (max(0, min(100, int(confidence))), entry_id, entry_id),
        )


async def revise_entry(
    entry_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
    confidence: int | None = None,
    subject: str | None = None,
    updated_by: int = 0,
    message: str = "",
    source_ids: Sequence[int] = (),
    force: bool = False,
) -> dict[str, Any] | None:
    """Rewrite an entry in place, keeping the previous body in history.

    Pinning protects an entry from the CURATOR, not from people. A person who
    wrote something down is allowed to change their mind about it, and making
    them unpin first would be a two-step dance for no gain. So the human paths
    (the PATCH endpoint, `lore_correct`) pass ``force=True``; the curator never
    does and is refused.

    Returns the new version number, or None if the entry is gone, or is pinned
    and the caller is the curator.
    """
    rows = await execute_query(
        f"SELECT {_ENTRY_COLS} FROM lore_entry WHERE ID=%s", (entry_id,),
    )
    if not rows:
        return None
    current = Entry.from_row(rows[0])
    if current.pinned and not force:
        return None

    new_title = (title or current.title)[:240]
    new_body = body if body is not None else current.body
    new_hash = body_hash(new_title, new_body)

    await execute_query(
        """INSERT IGNORE INTO lore_entry_history
               (ENTRY_ID, VERSION, TITLE, BODY, BODY_HASH, CONFIDENCE, STATUS,
                CHANGED_BY, MESSAGE)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (entry_id, current.version, current.title, current.body,
         body_hash(current.title, current.body), current.confidence,
         current.status, updated_by, (message or "")[:512]),
    )

    await execute_query(
        """UPDATE lore_entry
              SET TITLE=%s, BODY=%s, BODY_HASH=%s, TAGS=%s, CONFIDENCE=%s,
                  SUBJECT=%s, VERSION=VERSION+1, UPDATED_BY=%s,
                  LAST_CONFIRMED_AT=CURRENT_TIMESTAMP
            WHERE ID=%s""",
        (new_title, new_body, new_hash,
         _json_or_none(tags if tags is not None else current.tags),
         max(0, min(100, int(confidence if confidence is not None else current.confidence))),
         normalise_subject(subject) if subject else current.subject,
         updated_by, entry_id),
    )
    await link_sources(entry_id, source_ids)
    return {"id": entry_id, "version": current.version + 1}


async def edit_in_scope(
    entry: Entry,
    scope: Any,
    *,
    title: str | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
    confidence: int | None = None,
    subject: str | None = None,
    updated_by: int = 0,
    message: str = "",
) -> dict[str, Any]:
    """Edit an entry as this caller, respecting who owns it.

    Two outcomes, and the caller is told which:

      - the entry belongs to the caller's own client -> revised in place;
      - the entry is INHERITED from the app owner -> the caller's client gets a
        fork that overrides it. The owner's row is untouched, and every other
        client still sees the original.

    This is what makes "CLIENTA edits a SYSTEM app's knowledge" safe. Without
    it, either CLIENTA silently rewrites SYSTEM's knowledge for everyone, or
    CLIENTA cannot correct anything about an app they were given edit access to.
    """
    if scope.owns(entry.client_code):
        revised = await revise_entry(
            entry.id, title=title, body=body, tags=tags, confidence=confidence,
            subject=subject, updated_by=updated_by, message=message, force=True,
        )
        if revised is None:
            return {"action": "missing", "id": entry.id}
        return {"action": "revised", "id": entry.id, "version": revised["version"]}

    # Inherited. Fork it into the caller's client as an override.
    existing = await execute_query(
        f"""SELECT {_ENTRY_COLS} FROM lore_entry
             WHERE CLIENT_CODE=%s AND APP_CODE=%s AND BASE_ENTRY_ID=%s""",
        (scope.client_code, entry.app_code, entry.id),
    )
    if existing:
        # Already forked once; edit our own fork rather than making a second.
        fork_id = int(existing[0]["ID"])
        revised = await revise_entry(
            fork_id, title=title, body=body, tags=tags, confidence=confidence,
            subject=subject, updated_by=updated_by, message=message, force=True,
        )
        return {
            "action": "revised", "id": fork_id, "overrides": entry.id,
            "version": (revised or {}).get("version"),
        }

    result = await add_entry(
        scope.client_code, entry.app_code,
        kind=entry.kind,
        title=(title or entry.title),
        body=(body if body is not None else entry.body),
        subject=normalise_subject(subject or entry.subject),
        tags=(tags if tags is not None else entry.tags),
        confidence=(confidence if confidence is not None else entry.confidence),
        pinned=True,
        created_by=updated_by,
        base_entry_id=entry.id,
        # Carry the identity across the fork. Without this the override has a
        # NULL key, the next import fails to match it, and a second fork lands
        # beside the first.
        seed_key=entry.seed_key,
        seed_source=entry.seed_source,
    )
    await add_link(result["id"], entry.id, "supersedes")
    return {"action": "forked", "id": result["id"], "overrides": entry.id}


async def retire_in_scope(
    entry: Entry, scope: Any, *, updated_by: int = 0,
) -> dict[str, Any]:
    """Retire an entry as this caller.

    Own entry -> retired. Inherited entry -> a TOMBSTONE in the caller's client:
    a retired override that hides the base for this client and nobody else.
    """
    if scope.owns(entry.client_code):
        ok = await set_entry_status(entry.id, "retired", updated_by=updated_by, force=True)
        return {"action": "retired" if ok else "missing", "id": entry.id}

    existing = await execute_query(
        f"""SELECT ID FROM lore_entry
             WHERE CLIENT_CODE=%s AND APP_CODE=%s AND BASE_ENTRY_ID=%s""",
        (scope.client_code, entry.app_code, entry.id),
    )
    if existing:
        fork_id = int(existing[0]["ID"])
        await set_entry_status(fork_id, "retired", updated_by=updated_by, force=True)
        return {"action": "retired", "id": fork_id, "overrides": entry.id}

    result = await add_entry(
        scope.client_code, entry.app_code,
        kind=entry.kind, title=entry.title, body=entry.body,
        subject=entry.subject, tags=entry.tags, confidence=entry.confidence,
        status="retired", pinned=True, created_by=updated_by,
        base_entry_id=entry.id,
        seed_key=entry.seed_key, seed_source=entry.seed_source,
    )
    return {"action": "hidden", "id": result["id"], "overrides": entry.id}


async def set_entry_status(
    entry_id: int, status: str, *, superseded_by: int | None = None,
    updated_by: int = 0, force: bool = False,
) -> bool:
    """Change an entry's status. Same pinning rule as `revise_entry`: a person
    (``force=True``) may retire what they pinned; the curator may not."""
    rows = await execute_query(
        "SELECT PINNED FROM lore_entry WHERE ID=%s", (entry_id,),
    )
    if not rows:
        return False
    if rows[0].get("PINNED") and status in ("retired", "superseded") and not force:
        return False
    await execute_query(
        """UPDATE lore_entry SET STATUS=%s, SUPERSEDED_BY=%s, UPDATED_BY=%s WHERE ID=%s""",
        (status, superseded_by, updated_by, entry_id),
    )
    if superseded_by:
        await add_link(superseded_by, entry_id, "supersedes")
    return True


async def set_pinned(entry_id: int, pinned: bool, *, updated_by: int = 0) -> bool:
    affected = await execute_query(
        "UPDATE lore_entry SET PINNED=%s, UPDATED_BY=%s WHERE ID=%s",
        (1 if pinned else 0, updated_by, entry_id),
    )
    return bool(affected)


async def get_entry(entry_id: int) -> Entry | None:
    rows = await execute_query(
        f"SELECT {_ENTRY_COLS} FROM lore_entry WHERE ID=%s", (entry_id,),
    )
    return Entry.from_row(rows[0]) if rows else None


async def entries_by_id(
    client_codes: str | Sequence[str], app_code: str, ids: Sequence[int],
) -> list[Entry]:
    """Fetch named entries, scoped to what this caller may read.

    Scoped on purpose: an id is a handle the model got from an index, and an
    unscoped fetch by id would let one app read another's knowledge by
    guessing a number. Override resolution runs as usual, so a client that has
    forked an entry gets its own version rather than the base.
    """
    if not ids:
        return []
    chain = [client_codes] if isinstance(client_codes, str) else list(client_codes)
    marks = ",".join(["%s"] * len(ids))
    rows = await execute_query(
        f"""SELECT {_ENTRY_COLS} FROM lore_entry
             WHERE APP_CODE=%s AND CLIENT_CODE IN ({",".join(["%s"] * len(chain))})
               AND ID IN ({marks})""",
        (app_code, *chain, *[int(i) for i in ids]),
    )
    entries = [Entry.from_row(r) for r in (rows or [])]
    return resolve_overrides(entries, chain)


async def list_entries(
    client_codes: str | Sequence[str],
    app_code: str,
    *,
    kinds: Sequence[str] | None = None,
    subject: str | None = None,
    status: str = "active",
    limit: int = 200,
) -> list[Entry]:
    """Live entries for an app, resolved across the client inheritance chain.

    `client_codes` is the chain from `access.LoreScope.read_chain`: base client
    first, the caller's own client last. A later client's override shadows an
    earlier client's entry, and a retired override hides it entirely. Passing a
    single string reads exactly that client, with no inheritance.
    """
    chain = (client_codes,) if isinstance(client_codes, str) else tuple(client_codes)
    if not chain:
        return []
    limit = max(1, min(limit, 1000))

    where = [f"CLIENT_CODE IN ({','.join(['%s'] * len(chain))})", "APP_CODE=%s"]
    params: list[Any] = [*chain, app_code]
    # Status is filtered AFTER override resolution, not here: a retired override
    # is a tombstone and we need to see it to hide what it covers.
    if kinds:
        where.append(f"KIND IN ({','.join(['%s'] * len(kinds))})")
        params.extend(kinds)
    if subject:
        where.append("SUBJECT=%s")
        params.append(normalise_subject(subject))
    params.append(limit * max(2, len(chain)))

    rows = await execute_query(
        f"""SELECT {_ENTRY_COLS} FROM lore_entry
             WHERE {' AND '.join(where)}
          ORDER BY PINNED DESC, LAST_CONFIRMED_AT DESC LIMIT %s""",
        tuple(params),
    )
    entries = [Entry.from_row(r) for r in (rows or [])]
    resolved = resolve_overrides(entries, chain)
    if status and status != "any":
        resolved = [e for e in resolved if e.status == status]
    return resolved[:limit]


def resolve_overrides(entries: Sequence[Entry], chain: Sequence[str]) -> list[Entry]:
    """Collapse a mixed-client entry list down to what one caller should see.

    Pure, so the rule is testable without a database. The rule:

      - an entry from a LATER client in the chain that names an EARLIER entry
        via `base_entry_id` replaces it;
      - if that override is retired, the base disappears too (a tombstone);
      - entries nobody overrode survive as they are;
      - an override whose base is not in this result set still stands on its own,
        because the base may simply be outside the current filter.

    Entries are marked `inherited` when they came from a client above the
    caller, so a reader can tell "this is SYSTEM's rule" from "this is ours".
    """
    if not entries:
        return []
    rank = {code: i for i, code in enumerate(chain)}
    caller = chain[-1] if chain else ""

    by_id = {e.id: e for e in entries}
    # base id -> the winning override (highest-ranked client that overrides it)
    overrides: dict[int, Entry] = {}
    for e in entries:
        if not e.base_entry_id:
            continue
        base = by_id.get(e.base_entry_id)
        # Only accept an override written by a client BELOW the base's client.
        if base is not None and rank.get(e.client_code, -1) <= rank.get(base.client_code, -1):
            continue
        current = overrides.get(e.base_entry_id)
        if current is None or rank.get(e.client_code, -1) > rank.get(current.client_code, -1):
            overrides[e.base_entry_id] = e

    out: list[Entry] = []
    for e in entries:
        if e.id in overrides:
            continue                       # shadowed; the override is emitted below
        if e.base_entry_id and overrides.get(e.base_entry_id) is not e:
            continue                       # a losing override
        e.inherited = e.client_code != caller
        out.append(e)
    return out


async def search_entries(
    client_codes: str | Sequence[str], app_code: str, query: str, *,
    limit: int = 15, status: str = "active",
) -> list[tuple[Entry, float]]:
    """Full-text search over an app's live entries, across the client chain.

    Falls back to a LIKE scan when the fulltext index returns nothing, which it
    does for short words and for anything below the InnoDB token length: a real
    problem when someone searches for a page name like "sla".

    Override resolution runs after the match, so a client that has overridden a
    base entry gets their own version of it in the results, not the owner's.
    """
    chain = (client_codes,) if isinstance(client_codes, str) else tuple(client_codes)
    if not chain:
        return []
    limit = max(1, min(limit, 100))
    in_clause = ",".join(["%s"] * len(chain))
    # Overrides are resolved afterwards, so pull enough rows that a shadowed
    # base plus its override both make the window.
    fetch = limit * max(2, len(chain))

    rows = await execute_query(
        f"""SELECT {_ENTRY_COLS},
                   MATCH(TITLE, BODY) AGAINST(%s IN NATURAL LANGUAGE MODE) AS score
              FROM lore_entry
             WHERE CLIENT_CODE IN ({in_clause}) AND APP_CODE=%s
               AND MATCH(TITLE, BODY) AGAINST(%s IN NATURAL LANGUAGE MODE)
          ORDER BY score DESC LIMIT %s""",
        (query, *chain, app_code, query, fetch),
    )
    scored: dict[int, float] = {}
    if rows:
        scored = {int(r["ID"]): float(r.get("score") or 0.0) for r in rows}
    else:
        like = f"%{query.strip()}%"
        rows = await execute_query(
            f"""SELECT {_ENTRY_COLS} FROM lore_entry
                 WHERE CLIENT_CODE IN ({in_clause}) AND APP_CODE=%s
                   AND (TITLE LIKE %s OR BODY LIKE %s OR SUBJECT LIKE %s)
              ORDER BY PINNED DESC, LAST_CONFIRMED_AT DESC LIMIT %s""",
            (*chain, app_code, like, like, like, fetch),
        )

    entries = [Entry.from_row(r) for r in (rows or [])]
    if not entries:
        return []

    # An override may not itself have matched the query while its base did.
    # Pull the missing halves so resolve_overrides can see both sides.
    entries = await _with_override_partners(entries, chain, app_code)
    resolved = resolve_overrides(entries, chain)
    if status and status != "any":
        resolved = [e for e in resolved if e.status == status]
    # A fork inherits its base's relevance when it did not match on its own.
    def score_of(e: Entry) -> float:
        return scored.get(e.id, scored.get(e.base_entry_id or -1, 0.0))
    resolved.sort(key=score_of, reverse=True)
    return [(e, score_of(e)) for e in resolved[:limit]]


async def _with_override_partners(
    entries: Sequence[Entry], chain: Sequence[str], app_code: str,
) -> list[Entry]:
    """Add the overrides of any matched base entries, and vice versa.

    Without this a search can return an owner's entry that this client has
    already replaced or hidden, which is exactly the bug the override model
    exists to prevent.
    """
    known = {e.id for e in entries}
    wanted_bases = {e.base_entry_id for e in entries if e.base_entry_id and e.base_entry_id not in known}
    extra: list[Entry] = []

    if known:
        in_ids = ",".join(["%s"] * len(known))
        in_chain = ",".join(["%s"] * len(chain))
        rows = await execute_query(
            f"""SELECT {_ENTRY_COLS} FROM lore_entry
                 WHERE APP_CODE=%s AND CLIENT_CODE IN ({in_chain})
                   AND BASE_ENTRY_ID IN ({in_ids})""",
            (app_code, *chain, *known),
        )
        extra += [Entry.from_row(r) for r in (rows or []) if int(r["ID"]) not in known]

    if wanted_bases:
        in_ids = ",".join(["%s"] * len(wanted_bases))
        rows = await execute_query(
            f"SELECT {_ENTRY_COLS} FROM lore_entry WHERE ID IN ({in_ids})",
            tuple(wanted_bases),
        )
        extra += [Entry.from_row(r) for r in (rows or [])]

    return list(entries) + extra


async def entry_history(entry_id: int, limit: int = 20) -> list[dict[str, Any]]:
    rows = await execute_query(
        """SELECT VERSION, TITLE, BODY, CONFIDENCE, STATUS, CHANGED_BY, CHANGED_AT, MESSAGE
             FROM lore_entry_history WHERE ENTRY_ID=%s
         ORDER BY VERSION DESC LIMIT %s""",
        (entry_id, max(1, min(limit, 100))),
    )
    return rows or []


# ── Provenance and links ─────────────────────────────────────────────────


async def link_sources(entry_id: int, source_ids: Sequence[int]) -> int:
    linked = 0
    for oid in source_ids or ():
        try:
            await execute_query(
                """INSERT IGNORE INTO lore_entry_source (ENTRY_ID, OBSERVATION_ID)
                   VALUES (%s, %s)""",
                (int(entry_id), int(oid)),
            )
            linked += 1
        except Exception:
            logger.debug("lore: could not link observation %s to entry %s", oid, entry_id)
    return linked


async def entry_sources(entry_id: int, limit: int = 20) -> list[Observation]:
    rows = await execute_query(
        f"""SELECT o.ID, o.CLIENT_CODE, o.APP_CODE, o.KIND, o.SOURCE, o.SUBJECT,
                   o.BODY, o.META, o.SEEN_COUNT, o.OBSERVED_BY, o.OBSERVED_AT,
                   o.LAST_SEEN_AT, o.CURATED_AT
              FROM lore_entry_source s
              JOIN lore_observation o ON o.ID = s.OBSERVATION_ID
             WHERE s.ENTRY_ID=%s
          ORDER BY o.OBSERVED_AT DESC LIMIT %s""",
        (entry_id, max(1, min(limit, 100))),
    )
    return [Observation.from_row(r) for r in (rows or [])]


async def add_link(from_id: int, to_id: int, rel: str) -> None:
    if from_id == to_id:
        return
    await execute_query(
        "INSERT IGNORE INTO lore_link (FROM_ID, TO_ID, REL) VALUES (%s,%s,%s)",
        (int(from_id), int(to_id), rel),
    )


async def links_of(entry_id: int) -> list[dict[str, Any]]:
    rows = await execute_query(
        """SELECT FROM_ID, TO_ID, REL FROM lore_link
            WHERE FROM_ID=%s OR TO_ID=%s""",
        (entry_id, entry_id),
    )
    return rows or []


async def contradiction_counts(entry_ids: Sequence[int]) -> dict[int, int]:
    """For each entry, how many still-active entries contradict it.

    Contradiction is symmetric in meaning but stored one-way, so both ends of
    every link count. Only links whose OTHER end is still active count: an
    entry contradicted by something since retired is no longer contested, and
    leaving it marked down would be the same silent erosion that decay was.
    """
    ids = [int(i) for i in entry_ids if i]
    if not ids:
        return {}
    placeholders = ",".join(["%s"] * len(ids))
    rows = await execute_query(
        f"""SELECT l.FROM_ID, l.TO_ID
              FROM lore_link l
              JOIN lore_entry a ON a.ID = l.FROM_ID AND a.STATUS = 'active'
              JOIN lore_entry b ON b.ID = l.TO_ID   AND b.STATUS = 'active'
             WHERE l.REL = 'contradicts'
               AND (l.FROM_ID IN ({placeholders}) OR l.TO_ID IN ({placeholders}))""",
        tuple(ids) * 2,
    )
    wanted = set(ids)
    counts: dict[int, int] = {}
    for row in rows or []:
        for end in (int(row["FROM_ID"]), int(row["TO_ID"])):
            if end in wanted:
                counts[end] = counts.get(end, 0) + 1
    return counts


async def annotate_standing(entries: Sequence[Entry]) -> list[Entry]:
    """Fill in the read-time evidence that replaced time decay.

    Sets `contradicted_by` on every entry, and `subject_changed_at` from the
    most recent `edit` observation about the same subject. The second is a
    cheap stand-in for asking each service when its object last changed: the
    edit observations ARE that record, and they are already local.

    Mutates and returns the same list. Best-effort — an entry with no standing
    information reads exactly as it did before, which is the safe default and
    the reason every failure here is swallowed: a briefing that renders without
    the marks beats a briefing that does not render.
    """
    if not entries:
        return list(entries)

    try:
        counts = await contradiction_counts([e.id for e in entries])
        for entry in entries:
            entry.contradicted_by = counts.get(entry.id, 0)
    except Exception:  # noqa: BLE001
        logger.debug("lore: contradiction counts unavailable", exc_info=True)

    subjects = {e.subject for e in entries if e.subject and e.subject != "app"}
    if not subjects:
        return list(entries)

    try:
        app_code = entries[0].app_code
        client_codes = sorted({e.client_code for e in entries})
        placeholders = ",".join(["%s"] * len(subjects))
        client_placeholders = ",".join(["%s"] * len(client_codes))
        rows = await execute_query(
            f"""SELECT SUBJECT, MAX(LAST_SEEN_AT) AS changed_at
                  FROM lore_observation
                 WHERE APP_CODE=%s AND CLIENT_CODE IN ({client_placeholders})
                   AND KIND='edit' AND SUBJECT IN ({placeholders})
              GROUP BY SUBJECT""",
            (app_code, *client_codes, *sorted(subjects)),
        )
        changed = {r["SUBJECT"]: r["changed_at"] for r in (rows or [])}
        for entry in entries:
            entry.subject_changed_at = changed.get(entry.subject)
    except Exception:  # noqa: BLE001
        logger.debug("lore: subject change times unavailable", exc_info=True)
    return list(entries)


# ── Curation runs ────────────────────────────────────────────────────────


async def open_run(client_code: str, app_code: str, trigger_source: str) -> int:
    run_id = await execute_query(
        """INSERT INTO lore_curation_run (CLIENT_CODE, APP_CODE, TRIGGER_SOURCE)
           VALUES (%s,%s,%s)""",
        (client_code, app_code, trigger_source[:64]),
    )
    return int(run_id)


async def close_run(
    run_id: int, counters: dict[str, int], error: str = "",
    *, diagnostics: dict[str, Any] | None = None,
) -> None:
    """Finish a run row, including why it produced what it produced.

    `rejected` and `contradicted` used to be counted by apply_operations and
    then dropped here, which is what made a zero-entry pass indistinguishable
    from an all-rejected one. Everything the caller counts is now stored.
    """
    d = diagnostics or {}
    await execute_query(
        """UPDATE lore_curation_run
              SET FINISHED_AT=CURRENT_TIMESTAMP, OBS_CONSIDERED=%s, OBS_RENDERED=%s,
                  OPS_RETURNED=%s, ENTRIES_ADDED=%s, ENTRIES_REVISED=%s,
                  ENTRIES_CONFIRMED=%s, ENTRIES_RETIRED=%s, ENTRIES_REJECTED=%s,
                  ENTRIES_CONTRADICTED=%s, RESPONSE_CHARS=%s, REASONING_CHARS=%s,
                  STOP_REASON=%s, MODEL=%s, ATTEMPTS=%s, RAW_RESPONSE=%s, ERROR=%s
            WHERE ID=%s""",
        (counters.get("considered", 0), d.get("rendered", 0),
         d.get("ops_returned", 0), counters.get("added", 0),
         counters.get("revised", 0), counters.get("confirmed", 0),
         counters.get("retired", 0), counters.get("rejected", 0),
         counters.get("contradicted", 0), d.get("response_chars", 0),
         d.get("reasoning_chars", 0), (d.get("stop_reason") or None),
         (d.get("model") or None), d.get("attempts", 1),
         (d.get("raw_response") or None),
         (error or None) and error[:1024], run_id),
    )


async def has_open_run(client_code: str, app_code: str, stale_minutes: int = 15) -> bool:
    """Is a curation pass already running for this app?

    A run older than `stale_minutes` is treated as dead (the process died
    mid-pass) so one crash cannot block curation for that app forever.
    """
    rows = await execute_query(
        """SELECT ID FROM lore_curation_run
            WHERE CLIENT_CODE=%s AND APP_CODE=%s AND FINISHED_AT IS NULL
              AND STARTED_AT > (NOW() - INTERVAL %s MINUTE)
            LIMIT 1""",
        (client_code, app_code, stale_minutes),
    )
    return bool(rows)


async def recent_runs(client_code: str, app_code: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = await execute_query(
        """SELECT ID, TRIGGER_SOURCE, OBS_CONSIDERED, OBS_RENDERED, OPS_RETURNED,
                  ENTRIES_ADDED, ENTRIES_REVISED, ENTRIES_CONFIRMED, ENTRIES_RETIRED,
                  ENTRIES_REJECTED, ENTRIES_CONTRADICTED, RESPONSE_CHARS,
                  REASONING_CHARS, STOP_REASON, MODEL, ATTEMPTS,
                  STARTED_AT, FINISHED_AT, ERROR
             FROM lore_curation_run
            WHERE CLIENT_CODE=%s AND APP_CODE=%s
         ORDER BY ID DESC LIMIT %s""",
        (client_code, app_code, max(1, min(limit, 50))),
    )
    return rows or []


# ── Stats ────────────────────────────────────────────────────────────────


async def stats(client_code: str, app_code: str) -> dict[str, Any]:
    by_kind = await execute_query(
        """SELECT KIND, STATUS, COUNT(*) AS n FROM lore_entry
            WHERE CLIENT_CODE=%s AND APP_CODE=%s GROUP BY KIND, STATUS""",
        (client_code, app_code),
    )
    obs = await execute_query(
        """SELECT KIND, COUNT(*) AS n, SUM(CURATED_AT IS NULL) AS pending
             FROM lore_observation
            WHERE CLIENT_CODE=%s AND APP_CODE=%s GROUP BY KIND""",
        (client_code, app_code),
    )
    subjects = await execute_query(
        """SELECT SUBJECT, COUNT(*) AS n FROM lore_entry
            WHERE CLIENT_CODE=%s AND APP_CODE=%s AND STATUS='active'
         GROUP BY SUBJECT ORDER BY n DESC LIMIT 25""",
        (client_code, app_code),
    )
    return {
        "entries_by_kind": {
            f"{r['KIND']}/{r['STATUS']}": int(r["n"]) for r in (by_kind or [])
        },
        "observations_by_kind": {
            r["KIND"]: {"total": int(r["n"]), "pending": int(r["pending"] or 0)}
            for r in (obs or [])
        },
        "top_subjects": [
            {"subject": r["SUBJECT"], "entries": int(r["n"])} for r in (subjects or [])
        ],
    }


async def known_apps(limit: int = 200) -> list[dict[str, Any]]:
    """Every (client, app) lore knows anything about, busiest first."""
    rows = await execute_query(
        """SELECT CLIENT_CODE, APP_CODE, COUNT(*) AS entries,
                  MAX(LAST_CONFIRMED_AT) AS last_activity
             FROM lore_entry WHERE STATUS='active'
         GROUP BY CLIENT_CODE, APP_CODE
         ORDER BY entries DESC LIMIT %s""",
        (max(1, min(limit, 500)),),
    )
    return [
        {
            "client_code": r["CLIENT_CODE"],
            "app_code": r["APP_CODE"],
            "entries": int(r["entries"]),
            "last_activity": str(r["last_activity"]) if r["last_activity"] else None,
        }
        for r in (rows or [])
    ]


async def apps_needing_curation(min_pending: int = 5, limit: int = 25) -> list[dict[str, Any]]:
    """Apps with enough uncurated observations to be worth a pass."""
    rows = await execute_query(
        """SELECT CLIENT_CODE, APP_CODE, COUNT(*) AS pending
             FROM lore_observation WHERE CURATED_AT IS NULL
         GROUP BY CLIENT_CODE, APP_CODE
           HAVING pending >= %s
         ORDER BY pending DESC LIMIT %s""",
        (max(1, min_pending), max(1, min(limit, 100))),
    )
    return [
        {"client_code": r["CLIENT_CODE"], "app_code": r["APP_CODE"], "pending": int(r["pending"])}
        for r in (rows or [])
    ]
