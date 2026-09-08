"""Objects the user has open, unsaved, in front of them.

The sidekick used to be blind in both directions. It read through the platform
APIs, so whatever the user had unsaved in a workspace pane or on the page editor
canvas was invisible to it, and it wrote through the platform APIs, so by the time
its answer appeared the change was already committed. There was no state in which
the user could look at what it did and decide.

This module is the fix, and it is one rule:

    Hold writes for exactly the objects the client declares open
    AND that the server cannot draft for us.
    Write everything else straight through.

The second line is the 2026-09-03 change, and it is worth being precise about
why. Holding a write in the browser buys review, but it costs three things: the
change is not in the database, so `screenshot_page` cannot see it and the agent
looking at its own work sees nothing; it lives in one tab, so it is gone if that
tab is; and the surgical part-of-an-object endpoints have to be re-implemented
here, against a copy, to apply it. The server's draft surface has none of those
costs. So anything the server will draft is now written there instead, and the
open tab REFETCHES the draft, which is exactly what the page editor already does.

What is left holding is what the server has no draft for: the security objects
behind the org console. A profile, a role, a department has no `Draft` row
available at any price, so the browser is the only place a change to one can wait
for someone to look at it.

Either way the principle underneath is unchanged: a surface declares only the
objects it has a review UI for. A surface that declares nothing (the plain `ai`
chat page) gets exactly today's behaviour, with no special case anywhere.

Two independent decisions compose in `resolve()` and `entry()`, and neither infers
intent from a URL:

    resolve(method, path) -> (kind, id)   which object is this call about
    entry(kind, id)       -> DraftEntry?  did the client say that one is open

The intercept sits in `SaasClient._request` rather than in the ~50 call sites that
mutate. Every mutating tool in the service has the same shape (fetch the doc by
name, mutate the whole doc, PUT it back by id), so 50 edits would be 50 chances to
miss one, and a missed one writes silently to the database, which is the precise
failure this exists to remove. A choke point with an explicit allow-list fails the
other way: a path nobody listed behaves exactly as it does today.
"""

from __future__ import annotations

import copy
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Set for the duration of one agent turn. A ContextVar rather than a parameter
# because the intercept is in the HTTP client, which is reached through twenty
# layers of tool code that has no business knowing this exists.
#
# The value is a mutable registry, never reassigned mid-turn: the harness runs a
# tool batch through asyncio.gather, and each task gets a COPY of the context, so
# a rebind inside one task would be invisible to its siblings. Mutating the object
# they all share is the only thing that works.
open_drafts: ContextVar["DraftRegistry | None"] = ContextVar("open_drafts", default=None)

# True when this turn's writes to draftable objects should go to the server's
# draft surface. Decided once, in the agent, from what the caller asked for AND
# what the deployment actually supports, because a deployment that predates the
# draft work accepts `?draft=true` and performs an ordinary live update.
#
# It lives here rather than in the appbuilder agent's `_draft_surface` because
# the choke point that has to read it is in core, and core must not import from
# an agent. `_draft_surface.active()` reads the same flag, so the tools and the
# choke point cannot disagree about where a write went.
drafting: ContextVar[bool] = ContextVar("drafting", default=False)


# API path prefix -> object kind. Explicit rather than pattern-matched: the whole
# objection to deriving behaviour from URLs is that URLs are a weak signal, and the
# answer to that is to enumerate rather than to guess. A prefix absent from this
# table is never diverted and never reported, which is correct for the calls that
# are actions rather than object edits: function invocation, message send, role
# assignment, app access grants, transports, cache clear, file upload,
# personalization.
#
# Longest prefix wins, because /api/core/functions and /api/core/functions/execute
# would otherwise both match and the second must not be treated as an object.
PATH_KINDS: dict[str, str] = {
    "/api/ui/pages": "page",
    "/api/ui/themes": "theme",
    "/api/ui/styles": "style",
    "/api/ui/uripaths": "uripath",
    "/api/ui/functions": "function",
    "/api/ui/schemas": "schema",
    "/api/ui/applications": "application",
    "/api/core/storages": "storage",
    "/api/core/connections": "connection",
    "/api/core/templates": "template",
    "/api/core/notifications": "notification",
    "/api/core/functions": "serverfunction",
    "/api/core/eventDefinitions": "eventdefinition",
    "/api/core/eventActions": "eventaction",
    # Security objects. Present so the org console's open records can be held
    # and reported, NOT because they draft: see DRAFTABLE_KINDS below.
    "/api/security/app/profiles": "profile",
    "/api/security/profiles": "profile",
    "/api/security/rolev2": "role",
    "/api/security/users": "user",
    "/api/security/clients": "client",
    "/api/security/departments": "department",
    "/api/security/designations": "designation",
}

# Which of those the backend will hold in a `Draft` row for review.
#
# `isDraftable()` is true for all eight `ui` services and all eleven
# `commons-core` ones, and false for everything in `security`: a profile, a role
# or a user has no draft surface at all. That split is the whole reason this set
# exists, because it decides WHERE a write goes:
#
#   draftable     -> the server's draft, and the open tab refetches it. The
#                    change is real, reviewable, and visible to a screenshot.
#   not draftable -> held in the browser and streamed back as a patch, which is
#                    the only review step available when the server has none.
#
# Getting this wrong in either direction is a data-loss bug. Adding a kind that
# does not really draft sends the user's work live while the agent reports it as
# pending; leaving out one that does keeps it trapped in a browser tab where a
# screenshot cannot see it.
DRAFTABLE_KINDS: frozenset[str] = frozenset({
    "page", "theme", "style", "uripath", "function", "schema", "application",
    "storage", "connection", "template", "notification", "serverfunction",
    "eventdefinition", "eventaction",
})


def is_draftable(kind: str | None) -> bool:
    return bool(kind) and kind in DRAFTABLE_KINDS

# Trailing segments that are operations on a collection, not object ids. A path
# ending in one of these is not an object call at all, so it resolves to nothing:
# POST /api/core/functions/execute runs a function, and reading it as "create a
# serverfunction" would put it in front of the override-save match below.
_OPERATIONS = {"execute", "search", "query", "filter", "count", "internal", "copy"}


@dataclass
class DraftEntry:
    """One object the user has open and unsaved.

    `doc` is the live document: what the client had when the turn started, plus
    every change the agent has made since. `sent` is the copy the client has
    already been given, and exists only so a patch can name what actually changed
    rather than shipping the whole document again.
    """

    kind: str
    id: str
    name: str = ""
    app_code: str = ""
    doc: dict[str, Any] = field(default_factory=dict)
    sent: dict[str, Any] = field(default_factory=dict)
    # The user's unsaved edits, as a difference from the saved object. Set for a
    # page, where sending the whole thing every message would mean megabytes on
    # the wire to say "nothing has changed". Consumed once, when the object is
    # first read, by laying it over the saved version.
    overlay: dict[str, Any] | None = None
    # False until the saved object has been fetched and the overlay applied. The
    # fetch is deferred to the first read so a turn that never touches the open
    # object costs nothing.
    loaded: bool = False
    # True once the agent has written to it this turn. Distinct from the client's
    # own dirty flag, which says the *user* had unsaved edits on arrival.
    touched: bool = False


class DraftRegistry:
    """What the client declared open, for one agent turn."""

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        self._entries: dict[tuple[str, str], DraftEntry] = {}
        # Set by the agent when the turn starts. Absent in headless runs, where
        # nothing can be held anyway because no client is listening.
        self.stream: Any = None

    # ── Population ───────────────────────────────────────────────

    def declare(self, entry: DraftEntry) -> None:
        self._entries[(entry.kind, str(entry.id))] = entry

    def entry(self, kind: str, obj_id: str) -> DraftEntry | None:
        return self._entries.get((kind, str(obj_id)))

    def entries(self) -> list[DraftEntry]:
        return list(self._entries.values())

    def __bool__(self) -> bool:
        return bool(self._entries)

    # ── Routing ──────────────────────────────────────────────────

    @staticmethod
    def resolve(path: str) -> tuple[str | None, str | None, str | None]:
        """Map a request path to (kind, id, sub).

        `sub` is whatever follows the id, and it is the part that matters most
        here. The platform has surgical endpoints that write PART of an object
        (`/pages/{id}/components/{key}`), and an earlier version of this resolver
        returned nothing for those because they had an extra segment. That made
        the intercept fail OPEN: the most common editing tool in the service
        wrote straight to the database while the agent told the user it had not.
        Reporting the sub-path instead lets the caller fail closed.

        (kind, None, None) is a collection path; (None, None, None) is a path
        this mechanism has no opinion about.
        """
        clean = path.split("?", 1)[0].rstrip("/")
        if not clean.startswith("/"):
            clean = "/" + clean

        # Longest prefix wins: /api/core/functions/execute must not resolve
        # against /api/core/functions as if "execute" were an id.
        best: str | None = None
        for prefix in PATH_KINDS:
            if clean == prefix or clean.startswith(prefix + "/"):
                if best is None or len(prefix) > len(best):
                    best = prefix
        if best is None:
            return None, None, None

        kind = PATH_KINDS[best]
        rest = clean[len(best):].strip("/")
        if not rest:
            return kind, None, None
        obj_id, _, sub = rest.partition("/")
        if obj_id in _OPERATIONS:
            return None, None, None
        return kind, obj_id, (sub or None)


    # ── Filling an entry ─────────────────────────────────────────

    @staticmethod
    def hydrate(entry: DraftEntry, saved: dict[str, Any] | None) -> None:
        """Reconstruct what the user is actually looking at.

        For most kinds the client sent the document whole and there is nothing to
        do. For a page it sent only its unsaved difference, so the live version is
        the saved one with that difference laid over it. Both `doc` and `sent`
        start equal, because `sent` is the baseline a later patch is measured
        against and the client already has exactly this.
        """
        if entry.loaded:
            return

        base = snapshot(saved) if saved else {}
        overlay = entry.overlay or {}

        if overlay:
            comps = base.setdefault("componentDefinition", {})
            comps.update(overlay.get("changed") or {})
            for key in overlay.get("removed") or []:
                comps.pop(key, None)
            base.update(overlay.get("fields") or {})

        # A client that sent the document whole wins outright: it is the live copy.
        if entry.doc and not overlay:
            base = entry.doc

        entry.doc = base
        entry.sent = snapshot(base)
        entry.loaded = True

    # ── Staging a write ──────────────────────────────────────────

    async def stage(self, entry: DraftEntry, doc: dict[str, Any]) -> None:
        """Record a write against an open draft and tell the client what changed.

        Nothing goes to the database. The client applies the patch into the draft
        it already has on screen, so the change shows up on the canvas or in the
        form, becomes an undo step, and waits there for the user's Save.
        """
        patch = _build_patch(entry.kind, entry.sent, doc)
        entry.doc = doc
        entry.sent = snapshot(doc)
        entry.touched = True

        if self.stream is None:
            return
        await self.stream.emit_draft_patch(
            kind=entry.kind,
            obj_id=entry.id,
            name=entry.name or doc.get("name", ""),
            app_code=entry.app_code,
            patch=patch,
        )


def snapshot(doc: dict[str, Any]) -> dict[str, Any]:
    """Copy a document so later mutation of the original cannot alter it."""
    return copy.deepcopy(doc)


# Page-level keys that are metadata rather than content. Echoing them back would
# make every patch look like it changed the page even when it only moved a button.
_PAGE_META = {"componentDefinition", "updatedAt", "updatedBy", "createdAt", "createdBy"}


def _build_patch(kind: str, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """What the client must apply to turn `old` into `new`.

    A page is diffed per component, because a real one reaches 1.4MB and shipping
    it whole on every tool call would put megabytes on the wire to move one button.
    Every other kind is a form's worth of fields, so the whole document is both
    smaller and simpler than describing the difference.
    """
    if kind != "page":
        return {"doc": new}

    old_comps = old.get("componentDefinition") or {}
    new_comps = new.get("componentDefinition") or {}

    changed = {k: v for k, v in new_comps.items() if old_comps.get(k) != v}
    removed = [k for k in old_comps if k not in new_comps]

    fields = {
        k: v
        for k, v in new.items()
        if k not in _PAGE_META and old.get(k) != v
    }

    return {"changed": changed, "removed": removed, "fields": fields}


def registry() -> DraftRegistry | None:
    """The registry for the turn in flight, if a client declared anything.

    An empty declaration reads as no registry, so the intercept leaves early
    rather than walking an empty table on every call.
    """
    reg = open_drafts.get()
    return reg if reg else None


async def announce_change(
    kind: str,
    obj_id: str = "",
    name: str = "",
    app_code: str = "",
    operation: str = "UPDATE",
) -> None:
    """Report a write the choke point could not recognise as one.

    The intercept decides "was this a write?" from the HTTP verb, which is right
    for the definition APIs and wrong for a corner of the security service that
    performs mutations over GET: `/users/{id}/assignRole/{roleId}` changes the
    user and answers 200 to a GET. Nothing about the request says so, so the
    surface showing that user was never told, and the org console sat on a list
    that no longer matched the database.

    Tools that reach one of those call this instead. Deliberately explicit: a
    guess based on the path would have to encode which GETs are really writes,
    and being wrong in either direction is worse than being asked.
    """
    reg = announcer()
    if reg is None or reg.stream is None or not kind:
        return
    await reg.stream.emit_object_changed(
        kind=kind, obj_id=obj_id, name=name, app_code=app_code,
        operation=operation, draft=False,
    )


def announcer() -> DraftRegistry | None:
    """The turn's registry whether or not anything was declared.

    Two different questions were sharing one accessor, and the emptiness rule
    is right for only one of them. "Is this object being held?" wants an empty
    registry to read as absent. "Who do I tell that a write really happened?"
    does not, and the case where nothing is declared is not an edge: it is the
    page editor in draft mode, where every write goes to the server precisely
    so the editor can show it. Under the shared accessor those turns emitted no
    object_changed at all, and the canvas sat on a definition the agent had
    already replaced until the user reloaded the page by hand.
    """
    return open_drafts.get()
