"""Reading and writing the `blueprint` field on a platform object.

The field lives on `AbstractOverridableDTO`, so every overridable document has
one and the only thing that differs per kind is which collection to address.
That is the whole of this module: a kind table, a read, and a write.

Two things here are not obvious.

**The list route cannot be used to read a plan.** `LRO_FIELDS` in
`AbstractOverridableDataService` is an allowlist — `_id`, `name`, `description`,
`title`, `version` and the bookkeeping — and `blueprint` is excluded from it by
omission, deliberately: a list of forty pages must not carry forty plans. So a
plan is always read from the detail route by id, and listing is only ever how an
id is found.

**A write is a PATCH of the plan alone, never a PUT of the document.** A full
PUT of a page increments every per-component version, and those counters are
precisely what a plan entry fingerprints itself against, so saving a plan that
way invalidated the plan in the same write and left every card on the page
reporting as edited by hand. `PATCH {api}/{id}/blueprint` exists for this and
moves nothing but the plan and the document version.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.agents.appbuilder.tools._shared import get_saas_client
from app.core.tools.http_client import SaasClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Kind:
    """One object kind: where it lives and what its plan is anchored to."""

    name: str
    api: str
    #: The definition fields a `describe` call should be shown. Sending the whole
    #: document would put a 900-component page into a prompt that needs its
    #: shape, and the cost difference is two orders of magnitude.
    describe_fields: tuple[str, ...]
    #: Where per-entry fingerprints come from, for drift. None means this kind
    #: has no per-entry tracking and drift is whole-object or not at all.
    fingerprint_field: str | None = None


KINDS: dict[str, Kind] = {
    "application": Kind("application", "/api/ui/applications", ("properties", "languages")),
    "page": Kind(
        "page", "/api/ui/pages",
        ("rootComponent", "componentDefinition", "properties"),
        fingerprint_field="componentVersions",
    ),
    # `relations` is the storage's foreign keys and is a TOP-LEVEL field, not
    # part of the schema. Leaving it out meant a storage was described as a bag
    # of fields with no mention of the storages it links to — the one fact about
    # a storage that nobody can recover by looking at the site.
    "storage": Kind(
        "storage", "/api/core/storages",
        ("schema", "relations", "isAppLevel", "isAudited"),
    ),
    # Not a board kind and never swept. It is here so a storage's fields can be
    # resolved: a storage's `schema` is usually a REF to one of these.
    "schema": Kind("schema", "/api/core/schemas", ("properties", "required")),
    "function": Kind("function", "/api/core/functions", ("definition",)),
    "uifunction": Kind("uifunction", "/api/ui/functions", ("definition",)),
    "theme": Kind("theme", "/api/ui/themes", ("variables", "componentDefinition")),
    "style": Kind("style", "/api/ui/styles", ("styles", "variables")),
    # `pathDefinitions` — PLURAL — is a map of HTTP method to handler. The
    # singular does not exist on the document, so every URI path in the platform
    # came back with nothing to describe and no connections.
    "uripath": Kind("uripath", "/api/ui/uripaths", ("pathDefinitions", "pathString")),
    "template": Kind("template", "/api/core/templates", ("templateParts", "templateType")),
    # `channelTemplates`, not `channelDetails`. Same mistake, same result: the
    # notification band drew a card per notification saying only its own name.
    "notification": Kind(
        "notification", "/api/core/notifications",
        ("channelTemplates", "notificationType"),
    ),
}

KIND_NAMES: tuple[str, ...] = tuple(KINDS)

#: Every kind an app is MADE of, in the order the board stacks them.
#:
#: `application` is not here and must not be: it is the app itself, planned once
#: as the thing that owns all of these, not swept as one more object beside them.
#:
#: This is the one list. The sweep walks it, the board draws a band per entry,
#: the app context is built from it and `/objects` answers with it. It was
#: `("page", "storage")` in three of those places independently, which is why a
#: plan could call itself complete while saying nothing about the functions the
#: site runs, the routes it answers on, or the mail it sends — the parts of a
#: site a person is least able to reconstruct by looking at it.
BOARD_KINDS: tuple[str, ...] = (
    "page",
    "storage",
    "function",
    "uifunction",
    "uripath",
    "template",
    "notification",
    "theme",
    "style",
)


#: The fields the BOARD reads off one object's document, per kind.
#:
#: A document is fetched whole and then cut down to this before it crosses the
#: wire to a browser. A page carries its translations, its properties, its
#: permissions and a component map; the board draws cards from the component
#: map and nothing else, and shipping the rest is bytes a person waits for to
#: render nothing.
#:
#: Everything common — name, title, description, blueprint, version — is added
#: to whatever is listed here.
BOARD_FIELDS: dict[str, tuple[str, ...]] = {
    "page": ("rootComponent", "componentDefinition", "componentVersions"),
    "storage": ("schema", "relations"),
    "function": ("definition",),
    "uifunction": ("definition",),
    "uripath": ("pathDefinitions", "pathString"),
    "template": ("templateParts", "templateType"),
    "notification": ("channelTemplates", "notificationType"),
}

COMMON_BOARD_FIELDS: tuple[str, ...] = (
    "name", "title", "description", "blueprint", "version", "clientCode",
)


def board_document(document: dict[str, Any], kind_name: str) -> dict[str, Any]:
    """One object's document, cut down to what the board actually draws."""
    wanted = COMMON_BOARD_FIELDS + BOARD_FIELDS.get(resolve_kind(kind_name).name, ())
    return {key: document[key] for key in wanted if key in document}


class BlueprintObjectError(Exception):
    """The object could not be read or written. Carries an HTTP-ish status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def resolve_kind(kind: str) -> Kind:
    normalised = (kind or "").strip().lower()
    if normalised in KINDS:
        return KINDS[normalised]
    raise BlueprintObjectError(
        f"Unknown object kind '{kind}'. One of: {', '.join(KIND_NAMES)}.", status=400,
    )


def _client() -> SaasClient:
    return get_saas_client()


async def _find_id(
    kind: Kind, app_code: str, name: str, headers: dict[str, str],
) -> tuple[str, str]:
    """(id, clientCode) for one named object, or raise.

    Filtered server-side by name first, because when a name exists in several
    clients of an override chain the SERVER is the thing that knows which one
    this caller should see. The unfiltered fallback is for older builds that
    ignore the `name` parameter.
    """
    client = _client()

    async def listing(params: dict[str, Any]) -> list[dict[str, Any]]:
        result = await client.get(kind.api, headers=headers, params=params)
        if not result.success:
            raise BlueprintObjectError(
                f"Could not list {kind.name}s in '{app_code}': {result.error}", status=502,
            )
        data = result.data or {}
        return data.get("content", []) if isinstance(data, dict) else []

    rows = await listing({"page": 0, "size": 5, "appCode": app_code, "name": name})
    match = next((r for r in rows if r.get("name") == name), None)
    if match is None:
        rows = await listing({"page": 0, "size": 1000, "appCode": app_code})
        match = next((r for r in rows if r.get("name") == name), None)
    if match is None:
        raise BlueprintObjectError(
            f"No {kind.name} called '{name}' in app '{app_code}'.", status=404,
        )
    return str(match.get("id") or ""), str(match.get("clientCode") or "")


async def read_object(
    kind_name: str, app_code: str, name: str, headers: dict[str, str],
    *, draft: bool = False,
) -> dict[str, Any]:
    """The full document for one object, plan included.

    For `application` the name IS the app code: there is one UI document per
    app and asking for it by any other name is a mistake worth naming.

    `draft=True` reads the DRAFT surface, and something has to read it.

    The AppBuilder agent is draft-first: everything it authors lands on a draft
    and nothing is published until a person says so. That is correct and it is
    the whole point of a draft. But the build's reconciliation read the LIVE
    object, found an empty page, and reported "the builder finished without
    putting anything on the page" — about a page carrying eleven components and
    all three of its planned sections on the draft.

    So every fill run looked like a total failure while the work sat one query
    parameter away, and the conclusion drawn from it — that the authoring half
    had never once worked — was false.

    `GET ?draft=true` returns the object itself (only PUT returns a wrapper), so
    this is the same shape either way.
    """
    kind = resolve_kind(kind_name)
    if kind.name == "application":
        name = app_code

    object_id, _ = await _find_id(kind, app_code, name, headers)
    params = {"draft": "true"} if draft else None
    result = await _client().get(f"{kind.api}/{object_id}", headers=headers, params=params)
    if not result.success:
        raise BlueprintObjectError(
            f"Could not read {kind.name} '{name}': {result.error}", status=502,
        )
    document = result.data if isinstance(result.data, dict) else {}
    if not document:
        raise BlueprintObjectError(f"{kind.name} '{name}' came back empty.", status=502)
    if kind.name == "storage":
        document = await _inline_storage_schema(document, app_code, headers)
    return document


async def _inline_storage_schema(
    document: dict[str, Any], app_code: str, headers: dict[str, str],
) -> dict[str, Any]:
    """Put a storage's real fields where anything reading it expects them.

    A storage's `schema` is usually not a schema. It is a REFERENCE to one:

        "schema": {"ref": "sitezump.contactUsDetails", "type": ["OBJECT"]}

    with the fields in a separate document of their own. Only a storage built
    with an inline schema carries `properties` here.

    Everything reading a storage read `schema.properties` and found nothing, so
    a referenced storage looked like a storage with NO FIELDS. Two things then
    went wrong and neither announced itself: the sweep described nothing, and
    the board — which calls a planned field with no matching property "not on
    the site yet" — marked every field of a working form as unbuilt while the
    live site was busy collecting them.

    Resolved on read so that both sides get it, rather than at either call site
    where the next caller would have to remember.

    A ref that cannot be read leaves the document exactly as it came. A storage
    whose fields we failed to look up is a storage we know nothing about, and
    that is better said by silence than by a confident empty list.
    """
    schema = document.get("schema")
    if not isinstance(schema, dict) or schema.get("properties"):
        return document
    ref = schema.get("ref")
    if not ref:
        return document

    try:
        referenced = await read_object("schema", app_code, str(ref), headers)
    except BlueprintObjectError as exc:
        logger.info("blueprint: could not resolve schema '%s': %s", ref, exc.message)
        return document

    properties = referenced.get("properties")
    if isinstance(properties, dict) and properties:
        document["schema"] = {
            **schema,
            "properties": properties,
            "required": referenced.get("required"),
        }
    return document


async def read_blueprint(
    kind_name: str, app_code: str, name: str, headers: dict[str, str],
) -> dict[str, Any]:
    """{kind, name, blueprint, version} — blueprint is {} when there is no plan."""
    document = await read_object(kind_name, app_code, name, headers)
    return {
        "kind": resolve_kind(kind_name).name,
        "name": document.get("name") or name,
        "app_code": app_code,
        "blueprint": document.get("blueprint") or {},
        "version": document.get("version"),
        "client_code": document.get("clientCode") or "",
    }


async def write_blueprint(
    kind_name: str,
    app_code: str,
    name: str,
    blueprint: dict[str, Any],
    headers: dict[str, str],
    user_client_code: str,
    # Accepted and ignored. The blueprint route writes no version-history row,
    # so there is nothing for a commit message to appear on. Kept in the
    # signature because every caller writes one and dropping it would read as
    # though the message had gone somewhere.
    message: str = "",
) -> dict[str, Any]:
    """Replace one object's plan, leaving its definition alone.

    Goes through `PATCH {api}/{id}/blueprint`, which exists precisely so this
    does not have to be a full-document PUT. A PUT of a page increments every
    per-component version, and those counters are exactly what a plan entry
    fingerprints itself against — so writing a plan that way invalidated the
    plan in the same write, and every card on the page then reported as edited
    by hand. The dedicated route touches the plan and the document version and
    nothing else.

    An object the caller cannot write refuses with 403 rather than silently
    creating an override. Forking somebody else's object is a decision, not a
    side effect of saving a description.
    """
    kind = resolve_kind(kind_name)
    if kind.name == "application":
        name = app_code

    document = await read_object(kind.name, app_code, name, headers)
    object_id = str(document.get("id") or "")

    result = await _client().patch(
        f"{kind.api}/{object_id}/blueprint", headers=headers, json=blueprint,
    )
    if not result.success:
        raise BlueprintObjectError(
            f"Could not save the plan for {kind.name} '{name}': {result.error}", status=502,
        )
    # The pushed brief caches a rendered plan for a minute. Dropping it here is
    # what stops an agent that just wrote a plan from being briefed with the one
    # it replaced for the rest of that minute.
    try:
        from app.services.blueprint import context as brief_cache
        brief_cache.invalidate(user_client_code, app_code)
    except Exception:  # noqa: BLE001 — a stale brief must not fail a write
        logger.debug("blueprint: could not invalidate the brief cache", exc_info=True)

    saved = result.data if isinstance(result.data, dict) else {}
    return {
        "kind": kind.name,
        "name": name,
        "app_code": app_code,
        # Handed back because the caller announcing this write needs it, and
        # it was already read above to address the PATCH. Looking it up a
        # second time would be a listing round trip for something in hand.
        "id": object_id,
        "version": saved.get("version"),
        "blueprint": saved.get("blueprint") or blueprint,
    }


async def list_objects(
    kind_name: str, app_code: str, headers: dict[str, str], *, size: int = 1000,
) -> list[dict[str, Any]]:
    """Name, title and description for every object of one kind.

    This is the LRO projection, so it carries no plan and no definition. It is
    what `suggest_features` reads: grouping is a judgement about names and
    intent, and it would be absurd to load forty full pages to make it.
    """
    kind = resolve_kind(kind_name)
    result = await _client().get(
        kind.api, headers=headers, params={"page": 0, "size": size, "appCode": app_code},
    )
    if not result.success:
        raise BlueprintObjectError(
            f"Could not list {kind.name}s in '{app_code}': {result.error}", status=502,
        )
    data = result.data or {}
    rows = data.get("content", []) if isinstance(data, dict) else []
    return [
        {
            "kind": kind.name,
            "name": row.get("name") or "",
            "title": row.get("title") or "",
            "description": row.get("description") or "",
        }
        for row in rows
        if isinstance(row, dict) and row.get("name")
    ]


# ── Drift ────────────────────────────────────────────────────────────────


#: The fields of a plan entry that say what the thing is MEANT to be.
#:
#: `describes` is not among them and must never be: it is derived from the
#: definition and rewritten on every sweep, so including it would make every
#: sweep look like the plan had changed and put the whole site up for rebuild.
#: `order`, `componentKey` and `name` are bookkeeping — moving a section up the
#: page is not a change to what it is for.
INTENT_FIELDS: tuple[str, ...] = ("purpose", "spec", "content", "role", "layout", "uses")


def plan_fingerprint(entry: dict[str, Any]) -> str:
    """A short hash of what one plan entry ASKS FOR.

    This is the counterpart to `componentVersions` and it exists because the
    plan had no way to express a change to something that already exists.
    `pending` meant `componentKey` was null, so the moment a section was built
    it could never be planned again: you could add to a page and you could
    describe a page, and you could not CHANGE one. Every conversation about
    changing an existing page ended in a plan nobody could act on.

    With a fingerprint the two directions stay symmetric and separate:

        componentVersions moved  ->  the DEFINITION changed  ->  drifted
        this fingerprint moved   ->  the PLAN changed        ->  pending

    An entry that has never been stamped is NOT pending. That is the whole
    installed base — every section seeded from an existing site — and treating
    an unstamped entry as changed would put every card on every existing site
    up for rebuild the first time anybody looked at it.
    """
    import hashlib
    import json as _json

    intent = {
        field: entry.get(field)
        for field in INTENT_FIELDS
        if entry.get(field) not in (None, "", {}, [])
    }
    if not intent:
        return ""
    canonical = _json.dumps(intent, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode()).hexdigest()[:16]


def plan_moved(blueprint: dict[str, Any], uid: str, entry: dict[str, Any]) -> bool:
    """Has this entry's intent changed since whatever was built for it?

    False when nothing was ever stamped, which is the safe default and the
    common case.
    """
    agreed = (blueprint or {}).get("agreed")
    if not isinstance(agreed, dict):
        return False
    stamped = agreed.get(uid)
    if not stamped:
        return False
    return stamped != plan_fingerprint(entry)


def drift_of(document: dict[str, Any]) -> dict[str, Any]:
    """Which plan entries agree with the definition, and which do not.

    Three states, and the two that are not `clean` move in OPPOSITE directions:

      clean    the plan and the definition agree.
      pending  the plan is ahead. Somebody planned a section nobody has built.
               Resolving it moves the DEFINITION.
      drifted  the definition is ahead. Somebody edited the page by hand.
               Resolving it moves the PLAN.

    Conflating those two is how an "update" button ends up overwriting the work
    it was meant to record, so they are never collapsed into one "out of sync".

    The fingerprint is per ENTRY, never the document version. A blueprint is a
    field ON the object it describes, so editing the plan bumps the same
    `version` counter that editing the page bumps, and a document-level
    comparison cannot tell the two apart. `Page.componentVersions` already
    tracks per-component versions, which is exactly the right grain.
    """
    plan = ((document.get("blueprint") or {}).get("plan")) or {}
    sections = plan.get("sections") if isinstance(plan.get("sections"), dict) else {}
    reconciled = (document.get("blueprint") or {}).get("reconciled") or {}
    component_versions = document.get("componentVersions") or {}
    definition = document.get("componentDefinition") or {}

    blueprint = document.get("blueprint") or {}
    status: dict[str, str] = {}
    for uid, entry in (sections or {}).items():
        if not isinstance(entry, dict):
            continue
        component_key = entry.get("componentKey")
        if not component_key or component_key not in definition:
            # Planned, and nothing in the definition answers to it.
            status[uid] = "pending"
            continue
        if plan_moved(blueprint, uid, entry):
            # Built once, and the plan has been changed since. The plan is
            # ahead, so this is `pending` in exactly the same sense as something
            # never built: it is resolved by building, not by rewriting the
            # plan. Without this a built section could never be planned again
            # and changing an existing page was not expressible.
            status[uid] = "pending"
            continue
        agreed = reconciled.get(uid)
        current = component_versions.get(component_key)
        if agreed is None:
            # Never reconciled is the same as drifted and needs no third word:
            # in both cases the definition is what is real and the plan has not
            # been checked against it.
            status[uid] = "drifted"
        elif current is not None and agreed != current:
            status[uid] = "drifted"
        else:
            status[uid] = "clean"

    # Anything built that no plan entry claims. Not drift on an entry, but the
    # same question asked the other way round, and the board draws it the same.
    claimed = {
        e.get("componentKey") for e in (sections or {}).values()
        if isinstance(e, dict) and e.get("componentKey")
    }
    root = document.get("rootComponent")
    root_children = (definition.get(root) or {}).get("children") or {} if root else {}
    unplanned = [
        key for key, on in root_children.items()
        if on and key not in claimed
    ]

    return {
        "status": status,
        "unplanned": sorted(unplanned),
        "counts": {
            state: sum(1 for v in status.values() if v == state)
            for state in ("clean", "pending", "drifted")
        },
    }
