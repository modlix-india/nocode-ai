"""Turning definitions into plan entries, and definitions into prompt context.

Three operations that both the HTTP surface and the sweep job need, kept here
so neither has to import the other. They were in router.py until the job wanted
them; a job importing a router's private helpers is a circular import waiting
for the first person who adds a second caller.

None of this talks to a model. It is the arithmetic either side of one: what to
put in front of it, and where its answer belongs on the plan.
"""

from __future__ import annotations

from typing import Any

from app.services.blueprint import objects
from app.services.blueprint.objects import BlueprintObjectError
from app.services.blueprint.validate import ORDER_GAP, mint_uid, next_order

#: Objects of one kind put into a generation prompt.
#:
#: Capped, and low. Generation has to answer inside the gateway's 60s window,
#: and the cost runs both ways: sixty-four page names in the prompt produce a
#: plan with sixty-four entries in the reply, which is the half that is slow.
#: Past this the honest answer is a background job, not a bigger prompt — so
#: the cap is a limit on what a REQUEST will attempt, not on what a plan may
#: contain.
CONTEXT_OBJECT_CAP = 40

#: Where one kind's parts live in its plan, and which field links an entry back
#: to the definition it describes.
#:
#: The link field is never an index or a position. A section is matched by
#: `componentKey`, a storage field by `name`, a function step by its statement
#: name — each of which survives the thing being moved, where "the third one"
#: does not.
#:
#: A kind with no entry here plans its parts under `parts`, keyed by `part`.
#: That is the shape a theme and a style get, and both have exactly one part:
#: themselves.
PLAN_COLLECTION: dict[str, tuple[str, str]] = {
    "page": ("sections", "componentKey"),
    "storage": ("fields", "name"),
    "function": ("steps", "step"),
    "uifunction": ("steps", "step"),
    "uripath": ("steps", "step"),
    "template": ("parts", "part"),
    "notification": ("channels", "channel"),
}

DEFAULT_COLLECTION: tuple[str, str] = ("parts", "part")


def collection_for(kind: str) -> tuple[str, str]:
    """(collection name, link field) for one kind's plan entries."""
    return PLAN_COLLECTION.get((kind or "").lower(), DEFAULT_COLLECTION)


#: Every (collection, link field) pair, deduplicated, for a caller that has a
#: blueprint in hand but does not know which kind of object it came off.
_ALL_COLLECTIONS: tuple[tuple[str, str], ...] = tuple(
    dict.fromkeys([*PLAN_COLLECTION.values(), DEFAULT_COLLECTION])
)


async def build_app_context(
    app_code: str, headers: dict[str, str], kinds: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """What already exists in the app, so generation does not re-invent it.

    Names and titles only. This goes into a prompt, and the whole point of the
    LRO projection is that listing forty pages should not carry forty
    definitions.

    Every kind, not just the two that are easy to picture. An app plan written
    against pages and storages alone names no functions, so it describes a site
    that does nothing — and the next thing that reads the plan is a model, which
    will happily fill the silence by inventing the work again.
    """
    context: dict[str, Any] = {}
    for kind in kinds or objects.BOARD_KINDS:
        try:
            rows = await objects.list_objects(kind, app_code, headers)
        except BlueprintObjectError:
            continue
        if not rows:
            continue
        context[f"{kind}s"] = [
            {"name": r["name"], "title": r["title"]} for r in rows[:CONTEXT_OBJECT_CAP]
        ]
        if len(rows) > CONTEXT_OBJECT_CAP:
            # Said out loud rather than silently truncated. A model handed forty
            # of sixty-four pages with no note writes a confident plan for a
            # site two thirds the size of the real one.
            context[f"{kind}sOmitted"] = len(rows) - CONTEXT_OBJECT_CAP
    return context


def apply_describes(
    blueprint: dict[str, Any], described: dict[str, str], kind: str = "",
) -> dict[str, Any]:
    """Write derived lines onto the plan entries they belong to.

    `describes` and `purpose` are different fields on purpose and this only
    touches the first. `purpose` is what a person said the thing is for; a
    derivation that could overwrite it would eventually erase the only record of
    intent anybody wrote down, and it would do it during a routine refresh.

    Each kind's entries are matched on the field that links them back into the
    definition — `componentKey` for a page section, `name` for a storage field,
    `step` for a function step (see `PLAN_COLLECTION`). A described part with no
    plan entry is skipped rather than invented: creating entries here would
    silently turn a description pass into a planning pass.

    With no `kind` every known collection is walked. That is what a caller who
    holds a blueprint but not the kind it came from needs, and walking a
    collection the object does not have costs nothing.
    """
    plan = blueprint.get("plan")
    if not isinstance(plan, dict):
        return blueprint

    pairs = [collection_for(kind)] if kind else _ALL_COLLECTIONS
    for collection, match_on in pairs:
        _describe_entries(plan.get(collection), match_on, described)
    return blueprint


def seed_entries(
    blueprint: dict[str, Any],
    document: dict[str, Any],
    kind: str,
    described: dict[str, str],
) -> dict[str, Any]:
    """Create a plan entry for each described part that has none.

    This is how a plan is born for a site that already exists, which is every
    site. The entries carry only what can be known without asking anybody:
    `order`, the link back into the definition, and the derived line. They carry
    NO `purpose` — that is what a person says the thing is for, and inventing one
    here would put words in their mouth that later read as their own decision.

    Each new entry is stamped as reconciled at the definition's CURRENT
    fingerprint. It was derived from exactly that, so it agrees with it; without
    the stamp every freshly described card would render as drifted the moment it
    appeared, which reads as "somebody changed this" about a page nobody touched.
    """
    plan = blueprint.get("plan")
    if not isinstance(plan, dict):
        plan = {}
        blueprint["plan"] = plan
    blueprint.setdefault("schemaVersion", 1)

    collection, match_on = collection_for(kind)
    entries = plan.get(collection)
    if not isinstance(entries, dict):
        entries = {}
        plan[collection] = entries

    claimed = {
        e.get(match_on) for e in entries.values()
        if isinstance(e, dict) and e.get(match_on)
    }
    versions = document.get("componentVersions") or {}
    reconciled = blueprint.get("reconciled")
    if not isinstance(reconciled, dict):
        reconciled = {}
        blueprint["reconciled"] = reconciled

    order = next_order(entries)
    for key in described:
        if key in claimed:
            continue
        uid = mint_uid()
        entries[uid] = {"order": order, match_on: key, "describes": described[key]}
        if key in versions:
            reconciled[uid] = versions[key]
        order += ORDER_GAP
    return blueprint


def index_objects(
    app_blueprint: dict[str, Any], seen: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """Write each object's one-line summary into the app plan's `objects` map.

    `seen` is (kind, name, summary), one per object the sweep read.

    ── Why the app plan and not the object ──────────────────────────────

    The object already holds the detail: what each of its sections is for, what
    each field is. What it cannot hold is a line a BOARD can read cheaply — the
    only route to an object's own plan is its detail document, and drawing a
    second line under forty columns would be forty full documents fetched to
    show forty sentences. So the summary is written twice on purpose: once where
    it is derived, and once in the index that is read.

    ── What this must never do ──────────────────────────────────────────

    It writes `summary` and `status` and nothing else. `purpose` is what a
    person said the thing is for and `spec` is what they want a thing that does
    not exist yet to be; both are theirs, and a routine re-read that could
    overwrite either would eventually erase the only record of intent anybody
    wrote down — during a refresh nobody was watching.

    An object the app plan never mentioned gets an entry rather than being
    dropped. A manifest that silently omits what the app is made of is worse
    than no manifest: it reads as complete.
    """
    plan = app_blueprint.get("plan")
    if not isinstance(plan, dict):
        plan = {}
        app_blueprint["plan"] = plan
    app_blueprint.setdefault("schemaVersion", 1)

    entries = plan.get("objects")
    if not isinstance(entries, dict):
        entries = {}
        plan["objects"] = entries

    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries.values():
        if isinstance(entry, dict) and entry.get("name"):
            by_identity[(entry.get("kind") or "page", entry["name"])] = entry

    order = next_order(entries)
    for kind, name, summary in seen:
        entry = by_identity.get((kind, name))
        if entry is None:
            entry = {"order": order, "kind": kind, "name": name}
            entries[mint_uid()] = entry
            by_identity[(kind, name)] = entry
            order += ORDER_GAP
        if summary:
            entry["summary"] = summary
        # It was read off a real document, so it exists, whatever the plan said
        # about it a moment ago.
        entry["status"] = "built"
    return app_blueprint


def _describe_entries(
    entries: Any, match_on: str, described: dict[str, str],
) -> None:
    if not isinstance(entries, dict):
        return
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        key = entry.get(match_on)
        if key in described:
            entry["describes"] = described[key]
