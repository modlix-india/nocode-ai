"""Which objects reach which other objects, read off the definitions.

── Why this is the missing half of the plan ──────────────────────────────

A plan that lists forty objects and says a sentence about each one has recorded
forty facts and zero connections. It reads as a site the parts of which happen
to sit beside each other. But almost every real question about an app is a
question about an edge:

  "can we drop the order form?"      -> what else writes to `orderRequest`
  "what breaks if I rename this?"    -> who names it
  "where does this data come from?"  -> which function fills it
  "is this page reachable?"          -> does anything link to it

The reason to build through a plan at all is that a model can hold a graph of
forty nodes in its head and cannot hold forty definitions. Without the edges it
holds neither, and the plan is a glossary.

── Derived, never asked for ─────────────────────────────────────────────

Nothing here calls a model. Every edge is a reference that is literally written
in a definition: a step whose `url` addresses a storage, a Link whose `linkPath`
names a page, a step whose namespace is a core function. So this costs nothing,
runs on every sweep, and is either right or absent — which is the correct
failure for a fact a build will act on.

── Why it is structural and not a text search ───────────────────────────

The first version of this matched storage names against the whole page JSON on
a word boundary. It linked a page to the `post` storage because a heading read
"Post an enquiry". A derived edge that is wrong is worse than no edge, because
the next thing to read it cannot tell it from one somebody drew.

So references are taken only from the KEYS that carry references — a step's
`url` and `storageName`, a component's `linkPath` and `pageName` — and are then
resolved against the app's REAL object names. An unresolvable reference is
dropped in silence: it is an address outside this app, which is a true thing to
know nothing about.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterator

logger = logging.getLogger(__name__)

#: Most edges a plan will carry. A forty-page app with real logic produces a
#: few hundred, and the graph is read by a model with a context budget. Past
#: this the honest thing is to stop rather than to keep a partial tail that
#: looks complete.
MAX_EDGES = 500

#: Namespaces that are the runtime itself, not an object of this app.
#:
#: `_` is a page's own event functions — a real edge, but an internal one, and
#: the app graph is about edges BETWEEN objects.
#:
#: `CoreServices` is the one that matters most and is the least obvious.
#: `CoreServices.Storage.ReadPage` is how nearly every page in the platform
#: reaches its data, and the storage it reaches is in a PARAMETER, not in the
#: namespace. Without this entry the resolver would first go looking for an app
#: function called `Storage.ReadPage`, and on the app that eventually has one it
#: would find it and record an edge that does not exist.
PLATFORM_NAMESPACES = {"", "UIEngine", "System", "CoreServices", "_"}


@dataclass(frozen=True)
class Edge:
    """One object reaching another, and how we know."""

    from_kind: str
    from_name: str
    to_kind: str
    to_name: str
    #: The verb, in the app's own terms: "reads", "writes to", "goes to", "runs".
    how: str
    #: The statement or component the reference was found in. What makes an edge
    #: checkable by a person rather than something to take on faith.
    where: str = ""

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        """Identity for dedup. `where` is deliberately not part of it: a page
        that posts to one storage from three buttons is one relationship."""
        return (self.from_kind, self.from_name, self.to_kind, self.to_name, self.how)

    def as_entry(self, order: int) -> dict[str, Any]:
        """The plan's shape: flat, no arrays, addressable as `kind:name`."""
        return {
            "order": order,
            "from": f"{self.from_kind}:{self.from_name}",
            "to": f"{self.to_kind}:{self.to_name}",
            "how": self.how,
            "where": self.where,
        }


# ── What a step's name means ─────────────────────────────────────────────
#
# The verb comes from the primitive being called, so an edge reads the way a
# person would say it. Without this every edge said "uses", and "the order page
# uses orderRequest" does not distinguish reading a list from destroying one.

_VERBS: dict[str, str] = {
    "fetchdata": "reads",
    "senddata": "writes to",
    "deletedata": "deletes from",
    "create": "writes to",
    "update": "writes to",
    "delete": "deletes from",
    "read": "reads",
    "readpage": "reads",
    "navigate": "goes to",
    "getdata": "reads",
}

DEFAULT_VERB = "uses"


def _verb(step_name: str, fallback: str = DEFAULT_VERB) -> str:
    name = (step_name or "").split(".")[-1].lower()
    return _VERBS.get(name, fallback)


# ── Which keys carry a reference, and to what ────────────────────────────
#
# Lowercased on lookup because the platform is not consistent about it across
# step parameters and component properties.

_KEY_TARGETS: dict[str, tuple[str, str]] = {
    "storagename": ("storage", "uses"),
    "linkpath": ("page", "goes to"),
    "pagename": ("page", "includes"),
    "subpagename": ("page", "includes"),
    "templatename": ("template", "sends"),
    "notificationname": ("notification", "sends"),
    "themename": ("theme", "drawn with"),
    "stylename": ("style", "drawn with"),
    "functionname": ("function", "runs"),
}

#: `/api/core/data/{appCode}/{storage}` and its `/api/core/data/{storage}` short
#: form. The one URL shape in the platform that names an object.
_DATA_URL = re.compile(r"/api/core/data/(?:[A-Za-z0-9_-]+/)?([A-Za-z][A-Za-z0-9_]*)")
#: `/api/core/function/execute/{namespace}/{name}` — a server function by address.
_FUNCTION_URL = re.compile(
    r"/api/core/function/(?:execute/)?([A-Za-z][A-Za-z0-9_]*)/([A-Za-z][A-Za-z0-9_]*)"
)


# ── Reading values out of a definition ───────────────────────────────────


def _literals(node: Any) -> Iterator[str]:
    """Every literal string a parameter's value map holds.

    A KIRun parameter is `{uid: {type, value, expression}}`, and only a VALUE
    carries a name we can resolve — an EXPRESSION is computed at run time and
    its text is not an address. Reading expressions here is what would put a
    guess into a derived fact.
    """
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        if node.get("type") == "EXPRESSION":
            return
        if isinstance(node.get("value"), str):
            yield node["value"]
            return
        for value in node.values():
            yield from _literals(value)


def _referenced_keys(node: Any) -> Iterator[tuple[str, str]]:
    """(key, literal) for every key in a map that might carry a reference.

    Walks the whole parameter map rather than a fixed path, because the same
    key sits at a different depth in a step's parameters, a component's
    properties and a template's channel details.
    """
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if not isinstance(key, str):
            continue
        lowered = key.lower()
        if lowered in _KEY_TARGETS or lowered == "url":
            for literal in _literals(value):
                if literal:
                    yield lowered, literal
        elif isinstance(value, dict):
            yield from _referenced_keys(value)


def _page_of(link_path: str) -> str:
    """The page a `linkPath` addresses, or "".

    `/blog/{id}` is the blog page. `#`, `#anchor`, `/` and an absolute URL are
    not page references and must not be guessed into one.
    """
    path = (link_path or "").strip()
    if not path or path.startswith("#") or "://" in path:
        return ""
    first = path.lstrip("/").split("/")[0].split("?")[0].split("#")[0]
    return first if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", first or "") else ""


# ── Resolving a reference against what the app actually has ──────────────


class Known:
    """The app's real object names, by kind. An edge exists only if both ends do.

    Also indexes functions by their bare name, because a step calls
    `sitezump.contactUsEmail` as namespace `sitezump` + name `contactUsEmail`
    while the object is listed under the dotted form — and on an app whose
    functions were created without a namespace, the other way round.
    """

    def __init__(self, names_by_kind: dict[str, list[str] | set[str]]) -> None:
        self.by_kind: dict[str, set[str]] = {
            kind: {str(n) for n in names if n} for kind, names in names_by_kind.items()
        }
        self.function_tails: dict[str, str] = {}
        for kind in ("function", "uifunction"):
            for full in self.by_kind.get(kind, ()):
                tail = full.split(".")[-1]
                # First writer wins: two functions with the same tail in
                # different namespaces cannot be told apart from a call site
                # that gives only the tail, and inventing a winner silently is
                # worse than leaving the second one unlinked.
                self.function_tails.setdefault(tail, full)

    def has(self, kind: str, name: str) -> bool:
        return bool(name) and name in self.by_kind.get(kind, ())

    def resolve_function(self, namespace: str, name: str) -> tuple[str, str]:
        """(kind, name) for a called function, or ("", "")."""
        dotted = f"{namespace}.{name}".strip(".")
        for kind in ("function", "uifunction"):
            if self.has(kind, dotted):
                return kind, dotted
            if self.has(kind, name):
                return kind, name
        resolved = self.function_tails.get(name)
        if resolved:
            kind = "function" if resolved in self.by_kind.get("function", ()) else "uifunction"
            return kind, resolved
        return "", ""

    def resolve_path(self, path: str) -> tuple[str, str]:
        """A uripath whose `pathString` this URL matches, or ("", "")."""
        for name in self.by_kind.get("uripath", ()):
            if name and name in path:
                return "uripath", name
        return "", ""


# ── Per-kind extraction ──────────────────────────────────────────────────


def edges_of(
    document: dict[str, Any], kind: str, known: Known,
) -> list[Edge]:
    """Every object this one reaches. Empty is a normal, honest answer."""
    name = str(document.get("name") or "")
    if not name:
        return []
    reader = _READERS.get(kind)
    if not reader:
        return []
    try:
        return dedup(reader(document, kind, name, known))
    except Exception:  # noqa: BLE001 — a bad definition must not fail a sweep
        logger.debug("blueprint relations: could not read %s '%s'", kind, name, exc_info=True)
        return []


def dedup(edges: list[Edge]) -> list[Edge]:
    seen: dict[tuple[str, str, str, str, str], Edge] = {}
    for edge in edges:
        seen.setdefault(edge.key, edge)
    return list(seen.values())


def _steps_edges(
    steps: Any, known: Known, from_kind: str, from_name: str, where_prefix: str = "",
) -> list[Edge]:
    """Edges out of one KIRun definition's steps.

    Two independent sources per step, and both matter. The NAMESPACE says which
    function is being called — that is how a page reaches a server function. The
    PARAMETERS say which object is being addressed — that is how it reaches a
    storage, and it is invisible from the namespace, since every data call is
    `UIEngine.FetchData` regardless of what it fetches.
    """
    if not isinstance(steps, dict):
        return []
    found: list[Edge] = []

    for statement, step in steps.items():
        if not isinstance(step, dict):
            continue
        where = f"{where_prefix}{step.get('statementName') or statement}"
        namespace = str(step.get("namespace") or "")
        step_name = str(step.get("name") or "")

        if namespace not in PLATFORM_NAMESPACES:
            to_kind, to_name = known.resolve_function(namespace, step_name)
            if to_name:
                found.append(Edge(from_kind, from_name, to_kind, to_name, "runs", where))

        for key, literal in _referenced_keys(step.get("parameterMap") or {}):
            found.extend(
                _resolve_reference(key, literal, known, from_kind, from_name, step_name, where)
            )

    return found


def _resolve_reference(
    key: str,
    literal: str,
    known: Known,
    from_kind: str,
    from_name: str,
    step_name: str,
    where: str,
) -> list[Edge]:
    """One referencing key and its literal, turned into at most one edge."""
    if key == "url":
        return _url_edges(literal, known, from_kind, from_name, step_name, where)

    target_kind, default_verb = _KEY_TARGETS[key]
    value = _page_of(literal) if target_kind == "page" else literal.strip()
    if target_kind == "function":
        to_kind, to_name = known.resolve_function("", value)
        if to_name:
            return [Edge(from_kind, from_name, to_kind, to_name, "runs", where)]
        return []
    if known.has(target_kind, value):
        return [Edge(
            from_kind, from_name, target_kind, value,
            _verb(step_name, default_verb), where,
        )]
    return []


def _url_edges(
    url: str, known: Known, from_kind: str, from_name: str, step_name: str, where: str,
) -> list[Edge]:
    """What a `url` parameter addresses. The single richest reference there is.

    `/api/core/data/<app>/<storage>` is how every page in the platform reaches
    its data, and it is the edge that answers "what writes here". The verb comes
    from the step — FetchData against the same URL as SendData is a completely
    different relationship and collapsing them loses the only part that matters.
    """
    match = _DATA_URL.search(url)
    if match and known.has("storage", match.group(1)):
        return [Edge(
            from_kind, from_name, "storage", match.group(1),
            _verb(step_name, "uses"), where,
        )]

    match = _FUNCTION_URL.search(url)
    if match:
        to_kind, to_name = known.resolve_function(match.group(1), match.group(2))
        if to_name:
            return [Edge(from_kind, from_name, to_kind, to_name, "runs", where)]

    to_kind, to_name = known.resolve_path(url)
    if to_name:
        return [Edge(from_kind, from_name, to_kind, to_name, "calls", where)]
    return []


def _page_edges(
    document: dict[str, Any], kind: str, name: str, known: Known,
) -> list[Edge]:
    """A page reaches things two ways, and only one of them is its logic.

    Its event functions name storages and server functions. Its COMPONENTS name
    pages — a Link's `linkPath`, a SubPage's `pageName` — and those are the
    edges that answer whether a page is reachable at all, which no amount of
    reading its logic would ever show.
    """
    found: list[Edge] = []

    for function_key, function in (document.get("eventFunctions") or {}).items():
        if not isinstance(function, dict):
            continue
        label = str(function.get("name") or function_key)
        found.extend(_steps_edges(
            function.get("steps"), known, kind, name, where_prefix=f"{label}/",
        ))

    for component_key, component in (document.get("componentDefinition") or {}).items():
        if not isinstance(component, dict):
            continue
        where = str(component.get("name") or component_key)
        for key, literal in _referenced_keys(component.get("properties") or {}):
            found.extend(
                _resolve_reference(key, literal, known, kind, name, "", where)
            )

    return found


def _function_edges(
    document: dict[str, Any], kind: str, name: str, known: Known,
) -> list[Edge]:
    """What a function or a URI path reaches, which are two different shapes.

    A core or UI function holds its logic inline at `definition.steps`.

    A URI PATH holds none. It is a map of HTTP METHOD to a handler, at
    `pathDefinitions` — plural — and the handler's `kiRunFxDefinition` is not a
    definition at all but a REFERENCE: `{namespace, name, outputEventName}`
    naming the function that actually runs. So a URI path's only edge is the
    function it delegates to, and that edge is among the most valuable in the
    app: these are the addresses the outside world calls, and what happens when
    it does is invisible from the site, from the pages, and from the path.

    Reading the singular `pathDefinition` — which does not exist on the
    document — is why every URI path came back with no steps and no edges.

    The method is carried into `where`, because GET and POST on one path can
    delegate to different functions, and on a real app they do.
    """
    definition = document.get("definition")
    if isinstance(definition, dict) and definition.get("steps"):
        return _steps_edges(definition.get("steps"), known, kind, name)

    found: list[Edge] = []
    for method, handler in (document.get("pathDefinitions") or {}).items():
        if not isinstance(handler, dict):
            continue
        fx = handler.get("kiRunFxDefinition")
        if not isinstance(fx, dict):
            continue
        # An inline definition is accepted too. Nothing writes one today, but
        # the key is spelled as though it could be one, and a reader that
        # assumes otherwise breaks in silence on the first that is.
        if fx.get("steps"):
            found.extend(_steps_edges(
                fx.get("steps"), known, kind, name, where_prefix=f"{method}/",
            ))
            continue
        to_kind, to_name = known.resolve_function(
            str(fx.get("namespace") or ""), str(fx.get("name") or ""),
        )
        if to_name:
            found.append(Edge(kind, name, to_kind, to_name, "answers with", str(method)))
    return found


def _notification_edges(
    document: dict[str, Any], kind: str, name: str, known: Known,
) -> list[Edge]:
    """What a notification reaches, per channel.

    The field is `channelTemplates`, keyed by channel — inapp, email, sms — and
    the usual case is that the wording is written INLINE under `templateParts`
    rather than pointing at a template object. So most notifications have no
    outgoing edge at all, and that is the true answer rather than a gap.

    The reference keys are still resolved, for the notifications that do name a
    template object. `where` is the channel, which is the useful grain: a
    notification whose email wording moved and whose in-app wording did not is a
    real and common state.
    """
    found: list[Edge] = []
    for channel, detail in (document.get("channelTemplates") or {}).items():
        if not isinstance(detail, dict):
            continue
        for key, literal in _referenced_keys(detail):
            found.extend(_resolve_reference(key, literal, known, kind, name, "", str(channel)))
    return found


def _storage_edges(
    document: dict[str, Any], kind: str, name: str, known: Known,
) -> list[Edge]:
    """A storage's foreign keys — the one relation nobody can see on the site.

    The platform keeps them in a top-level `relations` map on the storage, keyed
    by the field that holds the link:

        "relations": {"category": {"storageName": "blogCategories",
                                   "relationType": "TO_MANY", ...}}

    Not in `schema.properties.<field>.ref`, which is what this looked for first
    and which is a reference to a SCHEMA document, a completely different thing.

    The relation type goes into the verb, because "one blog has many categories"
    and "one blog has one category" are different facts and a plan that records
    the wrong one will have something built against it.
    """
    found: list[Edge] = []
    for field, relation in (document.get("relations") or {}).items():
        if not isinstance(relation, dict):
            continue
        target = str(relation.get("storageName") or "")
        if not known.has("storage", target) or target == name:
            continue
        kind_of = str(relation.get("relationType") or "").upper()
        how = "links to many" if kind_of == "TO_MANY" else "links to one"
        found.append(Edge(kind, name, "storage", target, how, str(field)))
    return found


def _application_edges(
    document: dict[str, Any], kind: str, name: str, known: Known,
) -> list[Edge]:
    """The app's own references: its shell, its landing page, its theme.

    Read by key name rather than from a fixed list of property names, because
    the application document's property set differs by app type and a fixed list
    would quietly stop finding the login page on the first app that names it
    something else. A value that resolves to a real page IS a reference to that
    page; one that does not is dropped.
    """
    found: list[Edge] = []
    for key, value in (document.get("properties") or {}).items():
        if not isinstance(key, str):
            continue
        for literal in _literals(value):
            page = _page_of(literal)
            lowered = key.lower()
            if "page" in lowered and known.has("page", page):
                found.append(Edge(kind, name, "page", page, "opens as its " + _plain(key), key))
            elif "theme" in lowered and known.has("theme", literal):
                found.append(Edge(kind, name, "theme", literal, "drawn with", key))
    return found


def _plain(key: str) -> str:
    """`forgotPasswordPage` -> "forgot password page". A property name a person
    can read, since it lands in a sentence on the board."""
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", key).lower()
    return spaced.strip() or key


_READERS: dict[str, Any] = {
    "page": _page_edges,
    "function": _function_edges,
    "uifunction": _function_edges,
    "uripath": _function_edges,
    "notification": _notification_edges,
    "storage": _storage_edges,
    "application": _application_edges,
}


# ── Writing the graph into a plan ────────────────────────────────────────


def index_relations(
    app_blueprint: dict[str, Any], edges: list[Edge],
) -> dict[str, Any]:
    """Put the whole graph on the application's plan, under `plan.relations`.

    On the APPLICATION and nowhere else, for one reason: `usedBy` is the half
    that answers the questions worth asking, and no object can compute its own
    incoming edges — only something holding every document at once can. Writing
    `uses` to each object and `usedBy` nowhere would record the easy direction
    and drop the useful one.

    So the graph lives in one place, once, and a board or an agent reads both
    directions off it by matching `from` and `to`. One write instead of N, and
    no chance of the two halves disagreeing.

    Keys are stable across sweeps: the uid is derived from the edge itself, so
    re-running a sweep on an unchanged app rewrites the same map rather than
    minting forty new uids and orphaning every note anybody attached.
    """
    plan = app_blueprint.get("plan")
    if not isinstance(plan, dict):
        plan = {}
        app_blueprint["plan"] = plan

    relations: dict[str, Any] = {}
    order = 0
    for edge in sorted(
        dedup(edges), key=lambda e: (e.from_kind, e.from_name, e.to_kind, e.to_name)
    )[:MAX_EDGES]:
        order += 1000
        relations[stable_uid(edge)] = edge.as_entry(order)

    if relations:
        plan["relations"] = relations
    else:
        # An app with no edges at all is a real state — a brochure site of flat
        # pages — and an empty map says it better than a stale one from the
        # sweep before somebody deleted the logic.
        plan.pop("relations", None)
    return app_blueprint


def stable_uid(edge: Edge) -> str:
    """A letter-first uid that is the same every sweep for the same edge.

    A hash rather than a mint, because `mint_uid` is random and the plan is
    diffed by the override machinery: a fresh uid per sweep makes every sweep
    look like it deleted the whole graph and wrote a new one, which detaches
    every tenant override of it.
    """
    import hashlib

    digest = hashlib.sha1(
        f"{edge.from_kind}:{edge.from_name}>{edge.to_kind}:{edge.to_name}:{edge.how}".encode()
    ).hexdigest()
    return "r" + digest[:11]


def uses_of(edges: list[Edge], kind: str, name: str) -> dict[str, Any]:
    """One object's outgoing edges, in the shape `plan.uses` takes.

    Kept on the object as well as in the app graph, and it is the one
    duplication here worth having: an agent reading a single page's plan to
    change that page needs to know what it touches, and making it read the whole
    app plan for that is a round trip and a thousand tokens for four lines.
    """
    out: dict[str, Any] = {}
    order = 0
    for edge in sorted(
        dedup([e for e in edges if e.from_kind == kind and e.from_name == name]),
        key=lambda e: (e.to_kind, e.to_name),
    ):
        order += 1000
        out[stable_uid(edge)] = {
            "order": order,
            "kind": edge.to_kind,
            "name": edge.to_name,
            "how": edge.how,
            "where": edge.where,
        }
    return out


def summarise(edges: list[Edge], kind: str, name: str) -> str:
    """One line a person can read: what this object touches and what touches it.

    Goes in front of a model at the top of a describe call, so the description
    of a page is written by something that knows the page posts to a storage.
    Before this the describer saw a component tree and nothing else, and wrote
    "a form with four fields" about a form whose whole purpose was the storage
    it filled.
    """
    out = sorted({f"{e.how} {e.to_kind} {e.to_name}"
                  for e in edges if e.from_kind == kind and e.from_name == name})
    incoming = sorted({f"{e.from_kind} {e.from_name}"
                       for e in edges if e.to_kind == kind and e.to_name == name})
    parts = []
    if out:
        parts.append("It " + ", ".join(out[:8]) + ".")
    if incoming:
        parts.append("Reached from " + ", ".join(incoming[:8]) + ".")
    return " ".join(parts)
