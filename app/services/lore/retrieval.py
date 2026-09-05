"""Reading lore: search, briefings, and everything-about-one-object.

The briefing is the point of the whole service. `brief()` answers "what does
someone need to know before they touch this app", in a fixed number of
characters, ordered so that the things which constrain what you may do come
before the history of how it got that way.

Every read takes a `LoreScope` rather than a client code, because "what this
caller can see" is not one client's rows: it is the app owner's knowledge with
this client's overrides applied on top. Resolving that is `store.list_entries`'
job; this module only ever hands it `scope.read_chain`.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from app.services.lore import store
from app.services.lore.models import (
    BRIEF_ORDER,
    UNVERIFIED_BELOW,
    Entry,
    normalise_subject,
    subject_type,
)

logger = logging.getLogger(__name__)

# Default character budget for a briefing. Sized to sit comfortably inside an
# agent's system prompt alongside everything else it carries.
BRIEF_BUDGET = 6000

# Per-kind caps in a briefing. Without these one chatty kind (usually decision)
# crowds out everything else.
BRIEF_CAPS: dict[str, int] = {
    "purpose": 3,
    "constraint": 8,
    "convention": 8,
    "glossary": 14,
    "integration": 6,
    "gotcha": 6,
    "howto": 5,
    "decision": 8,
    "owner": 5,
    "status": 4,
}

_KIND_HEADINGS: dict[str, str] = {
    "purpose": "What this app is",
    "constraint": "Rules that must hold",
    "convention": "How this app is built",
    "glossary": "Terms, as used here",
    "integration": "Talks to",
    "gotcha": "Known traps",
    "howto": "Procedures",
    "decision": "Decisions and why",
    "owner": "Who knows what",
    "status": "In flight right now",
}


def _rank(entry: Entry) -> tuple[int, int, Any]:
    """Pinned first, then by effective confidence, then by recency."""
    return (1 if entry.pinned else 0, entry.effective_confidence, entry.last_confirmed_at)


def _mark(entry: Entry) -> str:
    """The parenthetical after a briefing line: where it came from and how sure.

    Inheritance is shown first because it changes what a reader should DO about
    a line. "SYSTEM's rule" is not yours to overrule casually; your own is.

    A contested or unverified entry says so in words. The whole reason time
    decay was dropped is that a shrinking number tells a reader nothing about
    what to do; "two entries disagree" tells them to go and settle it.
    """
    bits: list[str] = []
    if entry.inherited:
        bits.append(f"from {entry.client_code}")
    if entry.pinned:
        bits.append("confirmed by a person")
    if entry.contradicted_by:
        other = "another entry" if entry.contradicted_by == 1 else f"{entry.contradicted_by} other entries"
        bits.append(f"contested by {other}")
    elif entry.standing == "unverified":
        bits.append(f"{entry.subject} changed since this was confirmed")
    conf = entry.effective_confidence
    if conf < UNVERIFIED_BELOW:
        bits.append(f"unverified, confidence {conf}")
    return f" *({', '.join(bits)})*" if bits else ""


async def brief(
    scope: Any,
    *,
    subject: str | None = None,
    budget: int = BRIEF_BUDGET,
    include_unverified: bool = True,
    kinds: Sequence[str] | None = None,
) -> dict[str, Any]:
    """A markdown briefing on an app, or on one object inside it.

    Returns {"markdown": str, "entry_count": int, "truncated": bool, ...}.
    An app with no entries yields an honest empty briefing rather than an error:
    "lore knows nothing about this app yet" is useful information.

    `kinds` narrows the briefing to those entry kinds. That exists so a caller
    can render the non-negotiable half (purpose and constraints) under its own
    budget, before anything else can crowd it out — see context.big_picture.
    """
    subject = normalise_subject(subject) if subject else None
    app_code = scope.app_code
    wanted = tuple(kinds) if kinds else None
    entries = await store.list_entries(
        scope.read_chain, app_code, subject=subject, status="active", limit=400,
    )
    await store.annotate_standing(entries)
    if wanted:
        entries = [e for e in entries if e.kind in wanted]
    if not include_unverified:
        entries = [e for e in entries if e.effective_confidence >= UNVERIFIED_BELOW]

    # Shadows the LoreScope parameter from here down, deliberately kept as a
    # separate name so a later reader does not mistake it for the scope.
    what = f"`{subject}`" if subject else f"app `{app_code}`"
    if not entries:
        return {
            "markdown": (
                f"# {app_code}\n\n"
                f"Lore has nothing recorded about {what} yet. "
                "Knowledge accumulates as the app is built and discussed; "
                "you can also write something down directly with a note."
            ),
            "entry_count": 0,
            "truncated": False,
            "subject": subject or "app",
            "kinds": [],
        }

    by_kind: dict[str, list[Entry]] = {}
    for entry in entries:
        by_kind.setdefault(entry.kind, []).append(entry)
    for kind in by_kind:
        by_kind[kind].sort(key=_rank, reverse=True)

    header = f"# {app_code}" + (f" · {subject}" if subject else "")
    parts: list[str] = [header, ""]
    used = len(header) + 2
    hit_budget = False
    rendered = 0
    kinds_present: list[str] = []

    for kind in BRIEF_ORDER:
        group = by_kind.get(kind)
        if not group:
            continue
        cap = BRIEF_CAPS.get(kind, 6)
        shown, held_back = group[:cap], max(0, len(group) - cap)
        section: list[str] = [f"## {_KIND_HEADINGS.get(kind, kind.title())}", ""]
        for entry in shown:
            # Indent EVERY line of the body, not just the first. A multi-line
            # markdown body under a bullet loses its association with the
            # bullet otherwise, which is how a briefing ends up reading as one
            # run-on paragraph.
            body = "\n  ".join(entry.body.strip().splitlines())
            subj = "" if subject or entry.subject == "app" else f" `{entry.subject}`"
            section.append(f"- **{entry.title}**{subj}{_mark(entry)}  \n  {body}")
        # Say what was left out rather than quietly presenting a partial list as
        # the whole picture — a briefing that looks complete when it is not is
        # worse than one that admits its own edges.
        if held_back:
            section.append(f"- _…{held_back} more, lower confidence. Search for specifics._")
        section.append("")
        block = "\n".join(section)
        if used + len(block) > budget:
            hit_budget = True
            break
        parts.append(block)
        used += len(block)
        rendered += len(shown)
        kinds_present.append(kind)

    omitted = len(entries) - rendered
    truncated = hit_budget or omitted > 0
    if truncated:
        parts.append(
            f"\n_{omitted} of {len(entries)} entries not shown"
            f"{' (briefing budget reached)' if hit_budget else ''}. "
            "Search lore for a specific question._"
        )

    return {
        "markdown": "\n".join(parts).strip(),
        "entry_count": len(entries),
        "rendered": rendered,
        "truncated": truncated,
        "subject": subject or "app",
        "kinds": kinds_present,
    }


def render_entry(entry: Entry) -> str:
    """One entry in full, the way a briefing renders it.

    Shared by the briefing and by lore_get so an entry reads identically
    whichever way the model reached it.
    """
    where = "" if entry.subject == "app" else f" · {entry.subject}"
    return (
        f"#{entry.id} [{entry.kind}{where}] {entry.title}{_mark(entry)}\n"
        f"{entry.body.strip()}"
    )


async def index(
    scope: Any, *, subject: str | None = None, include_unverified: bool = True,
) -> dict[str, Any]:
    """One line per entry: everything this app knows, as a table of contents.

    This exists because a briefing cannot carry an app's knowledge and the
    prompt budget at the same time. Measured on a 21-entry app: the full bodies
    are 8,274 characters and a 3,800-character briefing rendered 5 of them, so
    16 entries were invisible to the model unless it thought to ask. The same
    21 entries as index lines are 1,561 characters — under a fifth of the
    bodies, and small enough that nothing has to be dropped.

    That inversion is the point. A briefing makes the ranking decide what the
    model sees; an index makes the MODEL decide, because it can see that every
    entry exists and fetch the ones its task actually needs. It also removes
    the search-phrasing problem: picking a real title beats guessing terms
    against a fulltext index that answers "how are phone numbers stored" with
    an entry about partner onboarding.

    Entries are never truncated away here. An index that silently omits things
    is worse than no index, because the model cannot tell the difference
    between "this app does not know that" and "that did not fit".
    """
    subject = normalise_subject(subject) if subject else None
    entries = await store.list_entries(
        scope.read_chain, scope.app_code, subject=subject, status="active", limit=400,
    )
    entries = await store.annotate_standing(entries)
    if not include_unverified:
        entries = [e for e in entries if e.effective_confidence >= UNVERIFIED_BELOW]
    entries.sort(key=_rank)

    by_kind: dict[str, list[Entry]] = {}
    for entry in entries:
        by_kind.setdefault(entry.kind, []).append(entry)

    lines: list[str] = []
    for kind in BRIEF_ORDER:
        group = by_kind.get(kind)
        if not group:
            continue
        lines.append(f"\n{_KIND_HEADINGS.get(kind, kind)}")
        for entry in group:
            where = "" if entry.subject == "app" else f" [{entry.subject}]"
            lines.append(f"  #{entry.id}{where} {entry.title}{_mark(entry)}")

    return {
        "markdown": "\n".join(lines).strip(),
        "entry_count": len(entries),
        "subject": subject or "app",
        "scope": scope.to_dict() if hasattr(scope, "to_dict") else None,
    }


async def search(
    scope: Any,
    query: str,
    *,
    limit: int = 12,
    kinds: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Full-text search over an app's live knowledge.

    Results are re-ranked by decayed confidence as well as text score, so a
    stale but lexically perfect match does not beat a current one.
    """
    query = (query or "").strip()
    if not query:
        return {"query": query, "results": [], "count": 0}

    scored = await store.search_entries(scope.read_chain, scope.app_code, query, limit=limit * 3)
    await store.annotate_standing([e for e, _ in scored])
    if kinds:
        allowed = set(kinds)
        scored = [(e, s) for e, s in scored if e.kind in allowed]

    def combined(pair: tuple[Entry, float]) -> float:
        entry, text_score = pair
        # Text relevance dominates; confidence breaks ties and demotes stale hits.
        return text_score * 2.0 + entry.effective_confidence / 100.0 + (0.5 if entry.pinned else 0.0)

    scored.sort(key=combined, reverse=True)
    top = scored[:limit]

    return {
        "query": query,
        "count": len(top),
        "results": [
            {
                **entry.to_dict(),
                "text_score": round(score, 4),
            }
            for entry, score in top
        ],
    }


async def about(
    scope: Any, subject: str, *, include_related: bool = True,
) -> dict[str, Any]:
    """Everything lore knows about one object.

    `include_related` also pulls entries whose subject shares a type and whose
    body mentions this object's name, which is how a page picks up the
    conventions written against its storage.
    """
    subject = normalise_subject(subject)
    direct = await store.list_entries(
        scope.read_chain, scope.app_code, subject=subject, status="active", limit=200,
    )
    await store.annotate_standing(direct)
    direct.sort(key=_rank, reverse=True)

    related: list[Entry] = []
    if include_related and ":" in subject:
        name = subject.partition(":")[2]
        if len(name) >= 3:
            found = await store.search_entries(scope.read_chain, scope.app_code, name, limit=30)
            seen = {e.id for e in direct}
            related = [e for e, _ in found if e.id not in seen][:12]
            await store.annotate_standing(related)
            related.sort(key=_rank, reverse=True)

    return {
        "subject": subject,
        "subject_type": subject_type(subject),
        "direct": [e.to_dict() for e in direct],
        "related": [e.to_dict() for e in related],
        "count": len(direct) + len(related),
    }


async def provenance(entry_id: int) -> dict[str, Any]:
    """Where one entry came from: its sources, its history, and its links.

    This is what makes a lore entry arguable rather than an oracle. If someone
    disagrees with an entry, they can see exactly what produced it.
    """
    entry = await store.get_entry(entry_id)
    if not entry:
        return {"error": f"No lore entry {entry_id}"}
    sources = await store.entry_sources(entry_id)
    history = await store.entry_history(entry_id)
    links = await store.links_of(entry_id)
    return {
        "entry": entry.to_dict(),
        "sources": [o.to_dict() for o in sources],
        "history": [
            {
                "version": int(h["VERSION"]),
                "title": h["TITLE"],
                "body": h["BODY"],
                "confidence": int(h["CONFIDENCE"]),
                "status": h["STATUS"],
                "changed_at": str(h["CHANGED_AT"]),
                "message": h.get("MESSAGE"),
            }
            for h in history
        ],
        "links": [
            {"from": int(l["FROM_ID"]), "to": int(l["TO_ID"]), "rel": l["REL"]}
            for l in links
        ],
    }


async def gaps(scope: Any) -> dict[str, Any]:
    """What lore does NOT know, so someone can fill it in.

    A knowledge base that cannot say what is missing looks complete when it is
    not, which is the failure mode that makes people stop trusting it.
    """
    entries = await store.list_entries(scope.read_chain, scope.app_code, status="active", limit=500)
    present = {e.kind for e in entries}

    missing_kinds = [k for k in ("purpose", "constraint", "convention", "owner") if k not in present]

    stale = sorted(
        (e for e in entries if e.effective_confidence < UNVERIFIED_BELOW),
        key=lambda e: e.effective_confidence,
    )[:15]

    thin_subjects: list[str] = []
    by_subject: dict[str, int] = {}
    for entry in entries:
        by_subject[entry.subject] = by_subject.get(entry.subject, 0) + 1
    for subj, count in by_subject.items():
        if subj != "app" and count == 1:
            thin_subjects.append(subj)

    return {
        "missing_kinds": missing_kinds,
        "unverified": [
            {"id": e.id, "kind": e.kind, "title": e.title, "confidence": e.effective_confidence}
            for e in stale
        ],
        "thinly_covered_subjects": sorted(thin_subjects)[:25],
        "pending_observations": await store.count_pending(scope.client_code, scope.app_code),
        "total_entries": len(entries),
    }
