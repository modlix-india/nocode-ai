"""Source adapters: how the world gets into lore.

Each function here takes something that actually happens while people build an
app and turns it into observations. They are deliberately forgiving — an
ingest failure must never break the thing that was happening. Every public
function swallows its own exceptions and logs.

Design rule: adapters record WHAT HAPPENED, never conclusions. Interpretation
is the curator's job, and keeping the two apart is what lets us change how we
interpret things later without having lost the raw record.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from app.db.connection import is_pool_available
from app.services.lore import store
from app.services.lore.curator import redact
from app.services.lore.models import normalise_subject

logger = logging.getLogger(__name__)

# Turns shorter than this are "ok", "yes", "thanks" — no durable content.
MIN_CHAT_CHARS = 40

# Nothing over this goes in whole; long turns are head-and-tail sampled so the
# ask and the outcome both survive.
MAX_BODY_CHARS = 4000


def _enabled() -> bool:
    """Lore writes are best-effort and silently no-op without a database."""
    return is_pool_available()


def _trim(text: str) -> str:
    text = redact((text or "").strip())
    if len(text) <= MAX_BODY_CHARS:
        return text
    head = text[: MAX_BODY_CHARS * 2 // 3]
    tail = text[-(MAX_BODY_CHARS // 3):]
    return f"{head}\n…[{len(text) - len(head) - len(tail)} chars omitted]…\n{tail}"


# ── Agent conversation ───────────────────────────────────────────────────


async def from_chat_turn(
    client_code: str,
    app_code: str,
    *,
    session_id: str,
    agent_name: str,
    user_message: str = "",
    assistant_message: str = "",
    subject: str = "app",
    user_id: int = 0,
) -> dict[str, Any]:
    """Record one exchange in an agent session.

    Both halves go in as separate observations: what was asked and what was
    reported are different kinds of evidence, and the curator weights an
    explicit user instruction more heavily than the agent's own narration.
    """
    if not _enabled():
        return {"recorded": 0}

    source = f"{agent_name}:session:{session_id}"[:160]
    payload: list[dict[str, Any]] = []

    if user_message and len(user_message.strip()) >= MIN_CHAT_CHARS:
        payload.append({
            "kind": "chat",
            "source": source,
            "subject": subject,
            "body": f"The person building the app said:\n{_trim(user_message)}",
            "meta": {"role": "user", "session_id": session_id, "agent": agent_name},
            "observed_by": user_id,
        })

    if assistant_message and len(assistant_message.strip()) >= MIN_CHAT_CHARS:
        payload.append({
            "kind": "chat",
            "source": source,
            "subject": subject,
            "body": f"The agent reported:\n{_trim(assistant_message)}",
            "meta": {"role": "assistant", "session_id": session_id, "agent": agent_name},
            "observed_by": user_id,
        })

    if not payload:
        return {"recorded": 0}

    try:
        counts = await store.record_observations(client_code, app_code, payload)
        return {"recorded": counts["created"], "repeated": counts["repeated"]}
    except Exception:
        logger.debug("lore: chat ingest failed for %s/%s", client_code, app_code, exc_info=True)
        return {"recorded": 0}


# ── Definition edits ─────────────────────────────────────────────────────


async def from_edit(
    client_code: str,
    app_code: str,
    *,
    object_type: str,
    object_name: str,
    action: str,
    detail: str = "",
    actor: str = "agent",
    user_id: int = 0,
    subject: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record that a definition changed.

    `action` is create|update|delete|rename. `detail` is a human sentence about
    what changed, not a diff — lore is not a version-control system, and the
    platform already keeps versions. What lore wants is the intent that a
    diff cannot carry.

    `subject` overrides the derived `<type>:<name>`. Callers that already know
    the canonical subject should pass it, so an app-level change lands in the
    "app" bucket rather than creating an `application:<code>` bucket beside it.
    """
    if not _enabled():
        return {"recorded": 0}

    subject = normalise_subject(subject or f"{object_type.lower()}:{object_name}")
    body = f"{actor} {action}d {object_type} `{object_name}`"
    if detail:
        body += f": {_trim(detail)}"

    try:
        result = await store.record_observation(
            client_code, app_code,
            kind="edit",
            source=f"{actor}:{object_type}:{object_name}"[:160],
            subject=subject,
            body=body,
            meta={"action": action, "object_type": object_type,
                  "object_name": object_name, **(meta or {})},
            observed_by=user_id,
        )
        return {"recorded": 1 if result["created"] else 0, "observation_id": result["id"]}
    except Exception:
        logger.debug("lore: edit ingest failed for %s/%s", client_code, app_code, exc_info=True)
        return {"recorded": 0}


# ── Object inventory ─────────────────────────────────────────────────────


async def from_inventory(
    client_code: str,
    app_code: str,
    *,
    objects: dict[str, Sequence[str]],
    user_id: int = 0,
) -> dict[str, Any]:
    """Record a snapshot of what the app contains.

    `objects` maps an object type to the names present, e.g.
    {"page": ["home", "jobsToday"], "storage": ["job", "customer"]}.

    Deliberately ONE observation for the whole snapshot: the shape of an app is
    a single fact, and fingerprinting the whole list means a snapshot that has
    not changed collapses into a repeat sighting instead of a new row every day.
    """
    if not _enabled() or not objects:
        return {"recorded": 0}

    lines = []
    for object_type in sorted(objects):
        names = sorted(str(n) for n in (objects[object_type] or []))
        if not names:
            continue
        shown = names[:60]
        suffix = f" …and {len(names) - 60} more" if len(names) > 60 else ""
        lines.append(f"{object_type} ({len(names)}): {', '.join(shown)}{suffix}")

    if not lines:
        return {"recorded": 0}

    try:
        result = await store.record_observation(
            client_code, app_code,
            kind="inventory",
            source="inventory:snapshot",
            subject="app",
            body="The app currently contains:\n" + "\n".join(lines),
            meta={"counts": {k: len(v or []) for k, v in objects.items()}},
            observed_by=user_id,
        )
        return {
            "recorded": 1 if result["created"] else 0,
            "changed": result["created"],
            "observation_id": result["id"],
        }
    except Exception:
        logger.debug("lore: inventory ingest failed for %s/%s", client_code, app_code, exc_info=True)
        return {"recorded": 0}


# ── Documents ────────────────────────────────────────────────────────────


async def from_document(
    client_code: str,
    app_code: str,
    *,
    title: str,
    content: str,
    origin: str,
    subject: str = "app",
    user_id: int = 0,
) -> dict[str, Any]:
    """Record a document: a KB section, a blog post, a README, a spec.

    Long documents are split on markdown headings so each section can be
    fingerprinted and curated independently — editing one section of a spec
    should not re-curate the whole thing.
    """
    if not _enabled() or not content:
        return {"recorded": 0}

    sections = _split_markdown(content)
    payload = [
        {
            "kind": "doc",
            "source": f"doc:{origin}"[:160],
            "subject": subject,
            "body": f"From “{title}”{(' · ' + heading) if heading else ''}:\n{_trim(section)}",
            "meta": {"origin": origin, "title": title, "heading": heading},
            "observed_by": user_id,
        }
        for heading, section in sections
        if len(section.strip()) >= MIN_CHAT_CHARS
    ]
    if not payload:
        return {"recorded": 0}

    try:
        counts = await store.record_observations(client_code, app_code, payload)
        return {"recorded": counts["created"], "repeated": counts["repeated"], "sections": len(payload)}
    except Exception:
        logger.debug("lore: doc ingest failed for %s/%s", client_code, app_code, exc_info=True)
        return {"recorded": 0}


def _split_markdown(content: str, max_sections: int = 40) -> list[tuple[str, str]]:
    """Split on ## / ### headings. Returns [(heading, body), …]."""
    lines = (content or "").splitlines()
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") and len(stripped.split(" ", 1)) > 1:
            if len(sections) >= max_sections:
                break
            sections.append((stripped.lstrip("#").strip(), []))
        else:
            sections[-1][1].append(line)
    return [(heading, "\n".join(body).strip()) for heading, body in sections if "".join(body).strip()]


# ── Runs and failures ────────────────────────────────────────────────────


async def from_run(
    client_code: str,
    app_code: str,
    *,
    what: str,
    outcome: str,
    subject: str = "app",
    failed: bool = False,
    user_id: int = 0,
) -> dict[str, Any]:
    """Record the outcome of running something: a function, a job, an import.

    Failures matter more than successes here — a repeated failure is exactly
    the kind of thing that should become a `gotcha` entry, and the SEEN_COUNT
    on the observation is what tells the curator it keeps happening.
    """
    if not _enabled():
        return {"recorded": 0}
    try:
        result = await store.record_observation(
            client_code, app_code,
            kind="run",
            source=f"run:{what}"[:160],
            subject=subject,
            body=(f"Running {what} failed: " if failed else f"Ran {what}: ") + _trim(outcome),
            meta={"failed": failed, "what": what},
            observed_by=user_id,
        )
        return {"recorded": 1 if result["created"] else 0, "seen_count": result["seen_count"]}
    except Exception:
        logger.debug("lore: run ingest failed for %s/%s", client_code, app_code, exc_info=True)
        return {"recorded": 0}


# ── People writing things down ───────────────────────────────────────────


async def note(
    client_code: str,
    app_code: str,
    *,
    text: str,
    subject: str = "app",
    author: str = "user",
    user_id: int = 0,
) -> dict[str, Any]:
    """Someone deliberately wrote something down.

    Given higher standing than any other source: a manual note is the one kind
    of observation a person chose to make, so it always reaches the curator and
    is never trimmed away as noise.
    """
    if not _enabled() or not (text or "").strip():
        return {"recorded": 0}
    result = await store.record_observation(
        client_code, app_code,
        kind="manual",
        source=f"note:{author}"[:160],
        subject=subject,
        body=_trim(text),
        meta={"author": author},
        observed_by=user_id,
    )
    return {
        "recorded": 1 if result["created"] else 0,
        "observation_id": result["id"],
        "seen_count": result["seen_count"],
    }


# ── Backfill from the existing per-app KB ────────────────────────────────


async def from_app_kb(client_code: str, app_code: str, *, user_id: int = 0) -> dict[str, Any]:
    """Seed lore from cfa_app_kb, so an app with a hand-written KB starts warm.

    Read-only on app_kb. Safe to run repeatedly: unchanged sections fingerprint
    to the same observation and collapse.
    """
    if not _enabled():
        return {"recorded": 0}
    try:
        from app.services import app_kb
    except ImportError:
        return {"recorded": 0, "reason": "app_kb unavailable"}

    payload: list[dict[str, Any]] = []
    try:
        sections = await app_kb.list_sections_present(client_code, app_code)
        for section in sections:
            row = await app_kb.get_latest(client_code, app_code, section)
            if not row or not row.get("BODY"):
                continue
            for heading, chunk in _split_markdown(row["BODY"]):
                if len(chunk.strip()) < MIN_CHAT_CHARS:
                    continue
                payload.append({
                    "kind": "doc",
                    "source": f"app_kb:{section}"[:160],
                    "subject": "app",
                    "body": f"From the app knowledge base ({section}"
                            f"{' · ' + heading if heading else ''}):\n{_trim(chunk)}",
                    "meta": {"section": section, "heading": heading,
                             "kb_version": row.get("VERSION")},
                    "observed_by": user_id,
                })
    except Exception:
        logger.debug("lore: app_kb backfill failed for %s/%s", client_code, app_code, exc_info=True)
        return {"recorded": 0}

    if not payload:
        return {"recorded": 0, "sections": 0}
    counts = await store.record_observations(client_code, app_code, payload)
    return {"recorded": counts["created"], "repeated": counts["repeated"], "chunks": len(payload)}
