"""Moving lore between places: committed seed files, and one env to another.

There is one format and one merge engine, used by three callers — the seed
loader, the CLI promoter, and the HTTP import the AppBuilder screen drives.
That unification is deliberate: seeding an app and promoting an app's knowledge
from dev to prod are the same operation with different sources, and a seed file
is simply a transport document that a person wrote by hand. Keeping them
separate would mean two parsers, two validation gates, and a seed corpus that
no import test ever exercises.

The delta resolution people ask for is NOT implemented here. It already exists:
`store.edit_in_scope` forks-or-revises depending on whether the caller owns the
row, `store.retire_in_scope` writes a tombstone versus a retirement, and
`store.resolve_overrides` walks the chain base-first. All of it is unit-tested.
This module's only job is to match a document row to a database row and decide
which of those to call. It must never write merge SQL of its own.

    plan()  decides everything and writes nothing
    apply() carries out a plan

That split is what lets a screen show someone what an upload would do — how
many entries land as new, how many revise, and crucially how many fork into
their own client as an override — before anything changes.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.services.lore import store
from app.services.lore.curator import redact
from app.services.lore.models import (
    ENTRY_KINDS,
    ENTRY_STATUSES,
    Entry,
    MAX_BODY,
    MAX_TITLE,
    normalise_subject,
)

logger = logging.getLogger(__name__)

FORMAT = "lore_transport/v1"

# Relations that mean the same thing on another instance. `supersedes` does
# not: it records a local supersession event and carries a SUPERSEDED_BY
# pointer, so importing one would assert history that never happened here.
PORTABLE_RELATIONS: frozenset[str] = frozenset({
    "relates_to", "depends_on", "example_of", "contradicts",
})

# Read off a document and thrown away. Every one of these is local to the
# instance that produced it. `sources` is the important one: observation ids
# mean nothing here, and honouring them would attach an entry to an unrelated
# observation — the provenance corruption `curator._clean_sources` exists to
# prevent.
_NEVER_IMPORTED: frozenset[str] = frozenset({
    "id", "version", "source_count", "sources", "created_by", "updated_by",
    "first_seen_at", "last_confirmed_at", "updated_at", "standing",
    "contradicted_by", "subject_changed_at", "effective_confidence", "inherited",
})

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9\-_.]{1,118}$")


class TransportError(ValueError):
    """The document cannot be used. Always carries a reason a person can act on."""


@dataclass
class TransportEntry:
    key: str
    kind: str
    subject: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    confidence: int = 75
    pinned: bool = False
    status: str = "active"
    overrides_key: str | None = None


@dataclass
class TransportDoc:
    app_code: str
    client_code: str
    source: str
    entries: list[TransportEntry] = field(default_factory=list)
    links: list[tuple[str, str, str]] = field(default_factory=list)
    resolved: bool = False


@dataclass
class PlannedAction:
    key: str
    action: str          # add | revise | fork | retire | skip
    title: str
    kind: str
    reason: str = ""
    entry_id: int | None = None
    base_entry_id: int | None = None


@dataclass
class ImportPlan:
    app_code: str
    client_code: str
    source: str
    mode: str
    actions: list[PlannedAction] = field(default_factory=list)
    # Rows this client has overridden whose base the document would move. The
    # fork is deliberately left alone — that is what an override means — but a
    # base shifting silently under someone's correction is how corrections get
    # lost, so it is reported.
    shadowed: list[PlannedAction] = field(default_factory=list)
    # Local rows the document does not mention.
    orphans: list[PlannedAction] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.actions:
            out[a.action] = out.get(a.action, 0) + 1
        out["shadowed"] = len(self.shadowed)
        out["orphans"] = len(self.orphans)
        return out

    def to_dict(self) -> dict[str, Any]:
        def row(a: PlannedAction) -> dict[str, Any]:
            d = {"key": a.key, "action": a.action, "kind": a.kind, "title": a.title}
            if a.reason:
                d["reason"] = a.reason
            if a.entry_id:
                d["entry_id"] = a.entry_id
            if a.base_entry_id:
                d["overrides"] = a.base_entry_id
            return d

        return {
            "app_code": self.app_code,
            "client_code": self.client_code,
            "source": self.source,
            "mode": self.mode,
            "totals": self.totals,
            "actions": [row(a) for a in self.actions],
            "shadowed": [row(a) for a in self.shadowed],
            "orphans": [row(a) for a in self.orphans],
        }


# ── Keys ─────────────────────────────────────────────────────────────────


def derive_key(kind: str, subject: str, title: str) -> str:
    """A stable identity for a claim.

    Deliberately NOT the body hash. BODY_HASH is the dedupe key and changes
    every time the wording changes, so matching on it would import an edited
    entry as a new row and leave the stale one standing beside it. Keyed on
    (kind, subject, title) instead, and a document may override it with an
    explicit `key:` so a title can be rewritten without orphaning the row.
    """
    norm = re.sub(r"\s+", " ", (title or "").strip().lower())
    raw = f"{kind}|{subject}|{norm}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ── Parsing ──────────────────────────────────────────────────────────────


def parse(document: Any) -> TransportDoc:
    """Validate a transport document. Pure: no I/O, no database, no LLM.

    Accepts an already-parsed mapping or a YAML/JSON string. `yaml.safe_load`
    reads JSON unchanged, which is why there is one parser for the
    hand-authored files and the wire format.
    """
    if isinstance(document, (str, bytes)):
        import yaml
        try:
            document = yaml.safe_load(document)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"could not parse the document: {exc}") from exc

    if not isinstance(document, dict):
        raise TransportError("a transport document must be a mapping at the top level")

    fmt = str(document.get("format") or "")
    if fmt != FORMAT:
        raise TransportError(
            f"unrecognised format {fmt!r}; this build reads {FORMAT!r}"
        )

    app_code = str(document.get("app_code") or "").strip()
    client_code = str(document.get("client_code") or "").strip()
    if not app_code:
        raise TransportError("app_code is required")
    if not client_code:
        raise TransportError("client_code is required")

    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise TransportError("entries must be a non-empty list")

    doc = TransportDoc(
        app_code=app_code,
        client_code=client_code,
        source=str(document.get("source") or f"import:{app_code}")[:120],
        resolved=bool(document.get("resolved")),
    )

    seen: set[str] = set()
    for i, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise TransportError(f"entry {i} is not a mapping")
        where = f"entry {i}"

        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in ENTRY_KINDS:
            raise TransportError(
                f"{where}: kind {kind!r} is not one of {', '.join(ENTRY_KINDS)}"
            )

        title = str(raw.get("title") or "").strip()
        if not 4 <= len(title) <= MAX_TITLE:
            raise TransportError(f"{where}: title must be 4-{MAX_TITLE} characters")

        body = str(raw.get("body") or "").strip()
        if len(body) < 10:
            raise TransportError(f"{where}: body must be at least 10 characters")

        given = str(raw.get("subject") or "app").strip()
        subject = normalise_subject(given)
        if subject == "app" and given.lower() not in ("", "app"):
            # Silent degradation is how knowledge ends up filed where nothing
            # looks for it. In a hand-authored file it is a typo, so say so.
            raise TransportError(
                f"{where}: subject {given!r} is not a recognised subject; it would be "
                f"filed as app-level. Use app, or <type>:<name> with a known type."
            )

        status = str(raw.get("status") or "active").strip().lower()
        if status not in ENTRY_STATUSES:
            raise TransportError(f"{where}: status {status!r} is not valid")

        key = str(raw.get("key") or "").strip().lower() or derive_key(kind, subject, title)
        if not _KEY_RE.match(key):
            raise TransportError(f"{where}: key {key!r} must be lowercase alphanumeric")
        if key in seen:
            raise TransportError(f"{where}: duplicate key {key!r}")
        seen.add(key)

        try:
            confidence = int(raw.get("confidence", 75))
        except (TypeError, ValueError):
            raise TransportError(f"{where}: confidence must be a number") from None

        doc.entries.append(TransportEntry(
            key=key, kind=kind, subject=subject,
            title=title[:MAX_TITLE], body=redact(body)[:MAX_BODY],
            tags=[str(t)[:40] for t in (raw.get("tags") or [])][:8],
            confidence=max(0, min(100, confidence)),
            pinned=bool(raw.get("pinned")),
            status=status,
            overrides_key=(str(raw["overrides_key"]).strip().lower()
                           if raw.get("overrides_key") else None),
        ))

    for e in doc.entries:
        if e.overrides_key and e.overrides_key not in seen:
            raise TransportError(
                f"entry {e.key!r} overrides {e.overrides_key!r}, which is not in this document"
            )

    for raw in (document.get("links") or []):
        if not isinstance(raw, dict):
            continue
        a, b = str(raw.get("from_key") or ""), str(raw.get("to_key") or "")
        rel = str(raw.get("rel") or "relates_to")
        if rel not in PORTABLE_RELATIONS:
            raise TransportError(
                f"link relation {rel!r} is not portable; use one of "
                f"{', '.join(sorted(PORTABLE_RELATIONS))}"
            )
        if a not in seen or b not in seen:
            raise TransportError(f"link {a!r}->{b!r} names a key not in this document")
        doc.links.append((a, b, rel))

    return doc


# ── Export ───────────────────────────────────────────────────────────────


async def export(scope: Any, *, resolved: bool = False, status: str = "active") -> dict[str, Any]:
    """A portable snapshot of what this client knows about this app.

    Own-client rows only by default. Exporting the inherited chain and
    importing it elsewhere would turn every inherited row into an owned copy
    and destroy the override model for that app, so `resolved` has to be asked
    for and the importer refuses such a file unless the operator confirms.
    """
    chain = (scope.client_code,) if not resolved else tuple(scope.read_chain)
    entries = await store.list_entries(chain, scope.app_code, status=status, limit=500)

    by_id = {e.id: e for e in entries}
    out: list[dict[str, Any]] = []
    for e in entries:
        key = e.seed_key or derive_key(e.kind, e.subject, e.title)
        row: dict[str, Any] = {
            "key": key,
            "kind": e.kind,
            "subject": e.subject,
            "title": e.title,
            "body": e.body,
            "confidence": e.confidence,
        }
        if e.tags:
            row["tags"] = e.tags
        if e.pinned:
            row["pinned"] = True
        if e.status != "active":
            row["status"] = e.status
        base = by_id.get(e.base_entry_id) if e.base_entry_id else None
        if base is not None:
            row["overrides_key"] = base.seed_key or derive_key(
                base.kind, base.subject, base.title,
            )
        out.append(row)

    return {
        "format": FORMAT,
        "app_code": scope.app_code,
        "client_code": scope.client_code,
        "source": f"export:{scope.app_code}/{scope.client_code}",
        "resolved": resolved,
        "entries": out,
    }


# ── Planning ─────────────────────────────────────────────────────────────


async def _index(chain: Sequence[str], app_code: str) -> tuple[dict, dict, list[Entry]]:
    """Every entry in the chain, indexed by key for the caller and for above."""
    entries = await store.list_entries(
        tuple(chain), app_code, status="any", limit=500,
    )
    mine: dict[str, Entry] = {}
    inherited: dict[str, Entry] = {}
    for e in entries:
        key = e.seed_key or derive_key(e.kind, e.subject, e.title)
        target = mine if e.client_code == chain[-1] else inherited
        # First writer wins for inherited, so base-first ordering is respected.
        if key not in target:
            target[key] = e
    return mine, inherited, entries


async def plan(scope: Any, doc: TransportDoc, *, mode: str = "merge") -> ImportPlan:
    """Decide what an import would do. Reads only; writes nothing."""
    if mode not in ("merge", "sync"):
        raise TransportError(
            f"mode {mode!r} is not supported. 'replace' is deliberately not "
            f"implemented: one import could wipe a client's curated knowledge "
            f"and the only undo is the entry history."
        )
    if doc.resolved:
        raise TransportError(
            "this document has resolved=true, meaning its inheritance chain was "
            "flattened. Importing it would turn every inherited entry into an "
            "owned copy for this client. Re-export with resolved=false, or pass "
            "the explicit flatten option if that is genuinely what you want."
        )

    chain = tuple(scope.read_chain) if getattr(scope, "read_chain", None) else (scope.client_code,)
    if chain[-1] != scope.client_code:
        chain = chain + (scope.client_code,)
    mine, inherited, _all = await _index(chain, doc.app_code)

    out = ImportPlan(
        app_code=doc.app_code, client_code=scope.client_code,
        source=doc.source, mode=mode,
    )

    for e in doc.entries:
        mine_row = mine.get(e.key)
        base_row = inherited.get(e.key)

        if e.status == "retired":
            target = mine_row or base_row
            if target is None:
                out.actions.append(PlannedAction(
                    e.key, "skip", e.title, e.kind,
                    reason="retires an entry that is not here; nothing to hide",
                ))
            else:
                out.actions.append(PlannedAction(
                    e.key, "retire", e.title, e.kind,
                    reason=("hidden for this client only" if base_row is not None
                            and mine_row is None else "retired"),
                    entry_id=target.id,
                    base_entry_id=(base_row.id if base_row is not None else None),
                ))
            continue

        if mine_row is not None:
            if mine_row.body.strip() == e.body.strip() and mine_row.title.strip() == e.title.strip():
                out.actions.append(PlannedAction(
                    e.key, "skip", e.title, e.kind,
                    reason="already identical", entry_id=mine_row.id,
                ))
            else:
                out.actions.append(PlannedAction(
                    e.key, "revise", e.title, e.kind,
                    reason="you own this row", entry_id=mine_row.id,
                    base_entry_id=mine_row.base_entry_id,
                ))
            if base_row is not None:
                out.shadowed.append(PlannedAction(
                    e.key, "shadowed", e.title, e.kind,
                    reason=(f"you override {base_row.client_code}'s version; your fork is "
                            f"kept and theirs is not touched"),
                    entry_id=mine_row.id, base_entry_id=base_row.id,
                ))
            continue

        if base_row is not None:
            owned = scope.owns(base_row.client_code) if hasattr(scope, "owns") else False
            same = (base_row.body.strip() == e.body.strip()
                    and base_row.title.strip() == e.title.strip())
            if same and not owned:
                # Nothing to override. Forking an identical body would give this
                # client a private copy of a row it already inherits, and from
                # then on the owner's corrections would stop reaching it. That
                # is how importing a shared seed into every client quietly
                # destroys the inheritance it was supposed to use.
                out.actions.append(PlannedAction(
                    e.key, "skip", e.title, e.kind,
                    reason=f"already inherited from {base_row.client_code}, unchanged",
                    entry_id=base_row.id, base_entry_id=base_row.id,
                ))
                continue
            if same and owned:
                out.actions.append(PlannedAction(
                    e.key, "skip", e.title, e.kind,
                    reason="already identical", entry_id=base_row.id,
                ))
                continue
            out.actions.append(PlannedAction(
                e.key, "revise" if owned else "fork", e.title, e.kind,
                reason=("you own the base row" if owned else
                        f"differs from {base_row.client_code}'s version, so it forks "
                        f"into {scope.client_code} as an override"),
                entry_id=base_row.id,
                base_entry_id=(None if owned else base_row.id),
            ))
            continue

        out.actions.append(PlannedAction(e.key, "add", e.title, e.kind, reason="new"))

    # Local rows the document does not mention.
    doc_keys = {e.key for e in doc.entries}
    for key, row in mine.items():
        if key in doc_keys or row.status != "active":
            continue
        if not row.seed_source:
            out.orphans.append(PlannedAction(
                key, "keep", row.title, row.kind,
                reason="written here, not from a transport document; never touched",
                entry_id=row.id,
            ))
        elif mode == "sync" and row.seed_source == doc.source:
            out.orphans.append(PlannedAction(
                key, "retire", row.title, row.kind,
                reason=f"came from {row.seed_source} and is no longer in it",
                entry_id=row.id,
            ))
        else:
            out.orphans.append(PlannedAction(
                key, "keep", row.title, row.kind,
                reason=f"from {row.seed_source}; merge mode leaves it alone",
                entry_id=row.id,
            ))

    return out


# ── Applying ─────────────────────────────────────────────────────────────


async def apply(
    scope: Any, doc: TransportDoc, plan_obj: ImportPlan, *, updated_by: int = 0,
) -> dict[str, Any]:
    """Carry out a plan. Every write goes through store's scope-aware helpers."""
    by_key = {e.key: e for e in doc.entries}
    counters: dict[str, int] = {
        "added": 0, "revised": 0, "forked": 0, "retired": 0,
        "skipped": 0, "failed": 0, "linked": 0,
    }
    ids: dict[str, int] = {}

    for action in plan_obj.actions:
        e = by_key.get(action.key)
        if e is None:
            continue
        try:
            if action.action == "skip":
                counters["skipped"] += 1
                if action.entry_id:
                    ids[action.key] = action.entry_id
                continue

            if action.action == "add":
                result = await store.add_entry(
                    scope.client_code, doc.app_code,
                    kind=e.kind, title=e.title, body=e.body, subject=e.subject,
                    tags=e.tags, confidence=e.confidence, pinned=e.pinned,
                    created_by=updated_by, seed_key=e.key, seed_source=doc.source,
                )
                ids[action.key] = int(result["id"])
                counters["added" if result.get("created") else "skipped"] += 1
                continue

            target = await store.get_entry(action.entry_id) if action.entry_id else None
            if target is None:
                counters["failed"] += 1
                continue

            if action.action == "retire":
                await store.retire_in_scope(target, scope, updated_by=updated_by)
                counters["retired"] += 1
                continue

            # revise and fork are the SAME call. Which one happens is decided by
            # store.edit_in_scope from whether the caller owns the row, and that
            # is the whole of the per-client delta resolution.
            result = await store.edit_in_scope(
                target, scope, title=e.title, body=e.body, tags=e.tags,
                confidence=e.confidence, subject=e.subject,
                updated_by=updated_by,
                message=f"imported from {doc.source}",
            )
            ids[action.key] = int(result.get("id") or 0)
            counters["forked" if result.get("action") == "forked" else "revised"] += 1

        except Exception:  # noqa: BLE001 — one bad row must not abort the import
            logger.exception(
                "lore transport: %s failed for key %s", action.action, action.key,
            )
            counters["failed"] += 1

    for action in plan_obj.orphans:
        if action.action != "retire" or not action.entry_id:
            continue
        try:
            target = await store.get_entry(action.entry_id)
            if target is not None:
                await store.retire_in_scope(target, scope, updated_by=updated_by)
                counters["retired"] += 1
        except Exception:  # noqa: BLE001
            logger.exception("lore transport: sync retire failed for %s", action.key)
            counters["failed"] += 1

    # Links last: both ends must exist before one can be drawn.
    for a, b, rel in doc.links:
        if a in ids and b in ids and ids[a] and ids[b]:
            try:
                await store.add_link(ids[a], ids[b], rel)
                counters["linked"] += 1
            except Exception:  # noqa: BLE001
                logger.exception("lore transport: link %s->%s failed", a, b)

    logger.info(
        "lore transport: %s/%s imported from %s — %s",
        scope.client_code, doc.app_code, doc.source,
        ", ".join(f"{k}={v}" for k, v in counters.items() if v),
    )
    return counters
