"""Turning definitions into plan entries, and definitions into prompt context.

Three operations that both the HTTP surface and the sweep job need, kept here
so neither has to import the other. They were in router.py until the job wanted
them; a job importing a router's private helpers is a circular import waiting
for the first person who adds a second caller.

None of this talks to a model. It is the arithmetic either side of one: what to
put in front of it, and where its answer belongs on the plan.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.services.blueprint import objects
from app.services.blueprint.objects import BlueprintObjectError
from app.services.blueprint.validate import ORDER_GAP, mint_uid, next_order

logger = logging.getLogger(__name__)

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

    brand = await brand_facts(app_code, headers)
    if brand:
        context["brand"] = brand
    # Who signs in and what the app talks to. Neither is an object, so nothing
    # in the lists above would ever mention either — and a plan written without
    # them describes an app with no users and no outside world.
    access = await access_facts(app_code, headers)
    if access:
        context["access"] = access
    return context


async def access_facts(app_code: str, headers: dict[str, str]) -> dict[str, Any]:
    """Who the app is for, and what it is wired to. Neither is an object.

    A plan built from the object lists alone cannot say whether anybody signs in
    to this app, what kinds of person it distinguishes, or that it sends mail
    through a connection somebody configured. Those are among the first things
    anybody asks and none of them is in any definition the sweep reads.

    PROFILES are the app's own words for its kinds of user — "Sales Manager",
    "CP Admin Profile" — and are the useful half of the security model here.
    Roles are not: the assignable list is the platform's whole catalogue of four
    hundred and sixty, which says nothing about this app.

    CONNECTIONS are what the app talks to outside itself. The listing gives
    names only; the type is stripped by the list projection, so a name is what
    is recorded and no type is invented to go with it.

    Every lookup is guarded on its own. This is enrichment, and an app whose
    security service is unreachable should get a plan without an access section,
    not no plan.
    """
    facts: dict[str, Any] = {}
    client = _client_for()

    try:
        listing = await client.get(
            "/api/security/applications", headers=headers,
            params={"page": 0, "size": 5, "appCode": app_code},
        )
        rows = _rows(listing)
        row = next((r for r in rows if r.get("appCode") == app_code), None)
        if row:
            facts["appType"] = str(row.get("appType") or "")
            facts["accessType"] = str(row.get("appAccessType") or "")
            app_id = row.get("id")
            if app_id is not None:
                profiles = await client.get(
                    f"/api/security/app/{app_id}/profiles", headers=headers,
                )
                named = [
                    str(p.get("name")) for p in _rows(profiles)
                    if isinstance(p, dict) and p.get("name")
                ]
                if named:
                    facts["profiles"] = named[:MAX_PROFILES]
    except Exception:  # noqa: BLE001 — a plan is worth more than a complete plan
        logger.debug("blueprint: no access facts for %s", app_code, exc_info=True)

    try:
        connections = await client.get(
            "/api/core/connections", headers=headers,
            params={"page": 0, "size": 100, "appCode": app_code},
        )
        named = [
            str(c.get("name")) for c in _rows(connections)
            if isinstance(c, dict) and c.get("name")
        ]
        if named:
            facts["connections"] = named[:MAX_CONNECTIONS]
    except Exception:  # noqa: BLE001
        logger.debug("blueprint: no connections for %s", app_code, exc_info=True)

    return facts


#: Kinds of user, and outside systems, named in a plan before the list is cut.
#: Both are short on every real app; the caps exist so a pathological one cannot
#: push the rest of the context out of the prompt.
MAX_PROFILES = 20
MAX_CONNECTIONS = 20


def _client_for():
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client()


def _rows(result: Any) -> list[dict[str, Any]]:
    """The rows out of a platform listing, whether it paged or not.

    Some of these routes answer with a page envelope and some with a bare list,
    and a caller that assumes one gets an empty result from the other with no
    error to explain it.
    """
    if not getattr(result, "success", False):
        return []
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        content = data.get("content")
        return [r for r in content if isinstance(r, dict)] if isinstance(content, list) else []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


async def brand_facts(app_code: str, headers: dict[str, str]) -> dict[str, Any]:
    """What the site actually looks like today: its typeface, colours and icon.

    None of this is in the object lists, and none of it is in any object's
    definition either. The typeface is a variable on the theme; the favicon is a
    `<link>` in the application's own properties. So a plan written from the
    lists alone could say anything at all about the brand, and did — the model
    had no way to know what the site is set in, so it invented a palette and
    nobody could tell the invention from a reading.

    Read once per sweep. The application document runs to hundreds of kilobytes
    and the theme to a couple of hundred more; a handful of values come out and
    the rest is dropped on the floor here rather than in the prompt.
    """
    facts: dict[str, Any] = {}

    try:
        app = await objects.read_object("application", app_code, "", headers)
    except BlueprintObjectError:
        logger.info("blueprint: could not read the application document for %s", app_code)
        app = {}

    properties = app.get("properties") or {}
    icons: dict[str, str] = {}
    for entry in (properties.get("links") or {}).values():
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("rel") or "")
        href = entry.get("href")
        if "icon" in rel.lower() and href:
            icons[rel] = str(href)
    # Said either way. "No favicon" is a fact worth planning against, and a key
    # that is simply absent reads as "not looked at".
    facts["icons"] = icons or "none — this site has no favicon set"

    packs = properties.get("fontPacks")
    if isinstance(packs, dict) and packs:
        facts["fontPacks"] = list(packs)[:10]

    theme = await _first_theme(app_code, headers)
    if theme:
        facts["theme"] = theme.get("name") or ""
        variables = brand_variables(theme)
        if variables:
            facts["look"] = variables

    return facts


async def _first_theme(app_code: str, headers: dict[str, str]) -> dict[str, Any] | None:
    """The app's theme document, or None. The first one: a site has one."""
    try:
        rows = await objects.list_objects("theme", app_code, headers, size=1)
        if not rows:
            return None
        return await objects.read_object("theme", app_code, rows[0]["name"], headers)
    except BlueprintObjectError:
        logger.info("blueprint: could not read a theme for %s", app_code)
        return None


def brand_variables(theme: dict[str, Any]) -> dict[str, str]:
    """The handful of theme variables that describe the LOOK.

    A theme carries a couple of hundred variables and all but a few dozen of
    them are per-component tokens — `textBoxBorderRadiusDefaultTertiary` and two
    hundred of its relatives. Those say nothing about what the site looks like;
    they say how one widget is drawn.

    The filter is on the NAME and it is anchored, which is what keeps the
    component tokens out: `colorOne` matches, `textBoxActiveRightIconColor...`
    does not, because it does not START with a brand word.
    """
    variables = theme.get("variables")
    if not isinstance(variables, dict):
        return {}
    # ALL is the base breakpoint. A per-breakpoint override of the typeface is
    # not what somebody means by "what font is this".
    base = variables.get("ALL")
    if not isinstance(base, dict):
        return {}

    found: dict[str, str] = {}
    for name, value in base.items():
        if not isinstance(name, str) or not isinstance(value, (str, int, float)):
            continue
        if _BRAND_VARIABLE.match(name):
            found[name] = str(value)
        if len(found) >= BRAND_VARIABLE_CAP:
            break
    return found


#: How many theme variables are worth showing. Past this it stops being "what
#: does this site look like" and becomes the theme editor with fewer features.
BRAND_VARIABLE_CAP = 24

_BRAND_VARIABLE = re.compile(
    r"^(?:"
    r"fontFamily"
    r"|[a-z]+Font"
    r"|color[A-Za-z0-9]*"
    r"|customColor\d+"
    r"|textColor\d+"
    r"|mainFontColor"
    r"|fontColor[A-Za-z]+"
    r"|backgroundColor[A-Za-z]+"
    r"|borderColor[A-Za-z]+"
    r"|(?:error|success|warning|information|primary|secondary)Color"
    r")$"
)


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
    names: dict[str, str] | None = None,
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

    # Fill in a missing name on an entry that already exists, before seeding.
    #
    # The naming rule arrived after entries had already been seeded without one,
    # and nothing went back for them: a re-sweep skips anything already claimed,
    # so those cards read "Untitled" for ever no matter how many times the site
    # was swept. Only an ABSENT name is filled — a name somebody typed is theirs
    # and is never recomputed.
    by_link = {
        e.get(match_on): e for e in entries.values()
        if isinstance(e, dict) and e.get(match_on)
    }
    for key, entry in by_link.items():
        if not entry.get("name"):
            entry["name"] = (names or {}).get(key) or _entry_name(document, kind, str(key))

    order = next_order(entries)
    for key in described:
        if key in claimed:
            continue
        uid = mint_uid()
        entries[uid] = {
            "order": order,
            match_on: key,
            # The name the BOARD shows. Seeded entries carried only a link and a
            # derived line, so every card on every column read "Untitled" while
            # its description sat underneath it — a whole board of anonymous
            # cards, each confidently describing itself.
            #
            # A name the derivation offered wins, because the page editor leaves
            # most sections called "Grid" and a board of "Grid 1" through
            # "Grid 9" is exactly as useless as a board of "Untitled". Failing
            # that, the component's own name, then its type, then the key: an
            # ugly name beats no name.
            "name": (names or {}).get(key) or _entry_name(document, kind, key),
            "describes": described[key],
        }
        if key in versions:
            reconciled[uid] = versions[key]
        order += ORDER_GAP
    return blueprint


def _entry_name(document: dict[str, Any], kind: str, key: str) -> str:
    """What to call one seeded entry on the board.

    Deliberately dumb. Naming a section WELL is a judgement — "the bit that
    convinces you to book" — and that costs a model call and belongs to the
    person or to a derivation, not to a seeding pass that is meant to be free.
    """
    if kind == "page":
        component = (document.get("componentDefinition") or {}).get(key) or {}
        name = str(component.get("name") or "").strip()
        # A generated key, or a name the editor left as the bare type. Neither
        # is worth showing over the key itself.
        if name and not _UNHELPFUL_NAME.match(name) and name != key:
            return name
        # The KEY before the type, and this order matters more than it looks.
        # A hand-built page names every section `grid`, so falling to the type
        # titled twelve different cards "Grid" and the board read as though it
        # had nothing to say about any of them — while the keys sitting right
        # there were `nav`, `hero`, `features`, `testimonial`.
        if _READABLE_KEY.match(key) and not _UNHELPFUL_NAME.match(key):
            return key
        kind_of = str(component.get("type") or "").strip()
        return kind_of or key
    return key


#: Names and keys the page editor leaves behind when nobody has chosen one.
#:
#: The leading single letter matters: the editor mints keys like `cGrid2` and
#: `cText7`, which are as uninformative as `grid2` and would otherwise pass for
#: a name somebody picked.
_UNHELPFUL_NAME = re.compile(
    r"^[_a-z]?(grid|comp|component|div|section|text|box|container)\d*$", re.IGNORECASE,
)

#: A key somebody chose, as opposed to one the editor minted. A `shortUUID` is
#: 22 characters of base62, so the length bound alone separates `hero` from
#: `4jZpbyLqrS2TQIBDuQdQby` without having to guess at its alphabet.
_READABLE_KEY = re.compile(r"^[a-z][A-Za-z0-9_]{1,19}$")


def index_objects(
    app_blueprint: dict[str, Any], seen: list[tuple[str, str, str, int, int]],
) -> dict[str, Any]:
    """Write each object's summary, part count and outstanding count into the app plan.

    `seen` is (kind, name, summary, pending, parts), one per object read.
    `pending` is how many of that object's plan entries nothing built answers
    to — which is the number Build has to act on.

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
    for kind, name, summary, pending, parts in seen:
        entry = by_identity.get((kind, name))
        if entry is None:
            entry = {"order": order, "kind": kind, "name": name}
            entries[mint_uid()] = entry
            by_identity[(kind, name)] = entry
            order += ORDER_GAP
        if summary:
            entry["summary"] = summary
        # How much of this object's own plan is still unbuilt.
        #
        # Recorded HERE, in the index, because it is the only place a board can
        # read it cheaply. A column's cards come from that object's own document,
        # which is fetched when the column is opened — so before anybody opens
        # anything the board can see that a page exists and cannot see that
        # three of its sections were never built. The Build button then reads
        # "nothing to build" over a plan with nine outstanding sections, which is
        # the worst thing that button can say.
        entry["pending"] = pending
        # How many parts this object HAS, which is a different question from how
        # many are outstanding and is answered nowhere else cheaply.
        #
        # A column nobody has opened has not been read, so the board showed a
        # blank where its count goes — deliberately, because printing 0 there
        # would say the page is empty. The effect was a board where only the
        # column you had clicked said anything about its size. One integer per
        # object in the index fixes that for every column at once, and it is an
        # integer rather than the parts themselves on purpose: copying each
        # object's sections in here would be a second copy of every plan, going
        # quietly stale, for something the column already fetches when opened.
        entry["parts"] = parts
        # It was read off a real document, so it exists, whatever the plan said
        # about it a moment ago.
        entry["status"] = "built"
    return app_blueprint


def index_assets(app_blueprint: dict[str, Any], brand: dict[str, Any]) -> dict[str, Any]:
    """Put the site's real favicon on the plan, as the asset it is.

    A picture is the one thing a plan can describe that is never a document, so
    an asset only ever exists as an entry in the app's own plan. That cuts both
    ways: a favicon somebody ASKED for has nowhere else to live, and a favicon
    the site already HAS appears nowhere at all unless something puts it there.
    The second is this. Without it a plan can be swept, described and indexed and
    still not answer "what is the icon", which is among the first things anybody
    looks for.

    `assetId` carries the href of the file that exists, which is what makes the
    board draw it as settled rather than as something still to be made. An asset
    a person asked for keeps `assetId: null` until it is, and this never touches
    those: it only ever fills in an icon that is already on the site.
    """
    icons = brand.get("icons")
    if not isinstance(icons, dict) or not icons:
        return app_blueprint

    plan = app_blueprint.get("plan")
    if not isinstance(plan, dict):
        plan = {}
        app_blueprint["plan"] = plan
    entries = plan.get("objects")
    if not isinstance(entries, dict):
        entries = {}
        plan["objects"] = entries

    existing = {
        e.get("name"): e for e in entries.values()
        if isinstance(e, dict) and e.get("kind") == "asset"
    }

    order = next_order(entries)
    for rel, href in icons.items():
        name = rel.strip() or "icon"
        entry = existing.get(name)
        if entry is None:
            entry = {"order": order, "kind": "asset", "name": name}
            entries[mint_uid()] = entry
            order += ORDER_GAP
        asset = entry.get("asset")
        if not isinstance(asset, dict):
            asset = {}
            entry["asset"] = asset
        asset["use"] = asset.get("use") or name
        # Never over a stated intent. "A cinnamon roll in a circle, flat, two
        # colours" is what somebody asked for, and the file that exists today is
        # not an answer to it — it may be exactly what they want replaced.
        asset.setdefault("intent", "")
        asset["assetId"] = href
        entry["status"] = "built"
    return app_blueprint


def set_uses(blueprint: dict[str, Any], uses: dict[str, Any]) -> dict[str, Any]:
    """Record what this object reaches, replacing whatever was derived before.

    Replaced whole rather than merged, and that is the point. `uses` is DERIVED
    from the definition on every sweep, so a page that stopped posting to a
    storage must stop claiming it — a merge would keep the dead edge forever and
    a plan that lists a connection nobody can find is worse than one that lists
    none.

    The uids are stable per edge (`relations.stable_uid`), so an unchanged app
    rewrites the identical map. That matters more than it sounds: the override
    machinery diffs this field, and a fresh uid per sweep would make every sweep
    look like it deleted the whole set and wrote a new one, detaching every
    tenant override of it.

    This superseded a version that matched storage names against the whole page
    JSON on a word boundary, which linked a page to the `post` storage because a
    heading read "Post an enquiry". A derived edge that is wrong is worse than
    no edge: the next thing to read it cannot tell it from one somebody drew.
    """
    plan = blueprint.get("plan")
    if not isinstance(plan, dict):
        plan = {}
        blueprint["plan"] = plan
    if uses:
        plan["uses"] = uses
    else:
        plan.pop("uses", None)
    return blueprint



def index_access(app_blueprint: dict[str, Any], access: dict[str, Any]) -> dict[str, Any]:
    """Write who the app is for, and what it connects to, into `plan.access`.

    Derived, so it is refreshed every sweep and replaces what was there. The
    alternative was leaving the model to say who an app is for, which it will
    do confidently and from nothing — a plan claiming an app has administrators
    and customers when its security says it has one profile called "Owner" is
    worse than a plan silent on the subject, because the next thing to read it
    builds a role switcher.

    Written as keyed maps, like everything else, so a person can later attach a
    `purpose` to a profile without the whole block being replaced under them.
    """
    if not access:
        return app_blueprint
    plan = app_blueprint.get("plan")
    if not isinstance(plan, dict):
        plan = {}
        app_blueprint["plan"] = plan

    block: dict[str, Any] = {}
    for field in ("appType", "accessType"):
        if access.get(field):
            block[field] = access[field]
    for field, kind in (("profiles", "profile"), ("connections", "connection")):
        names = access.get(field)
        if not isinstance(names, list) or not names:
            continue
        keyed: dict[str, Any] = {}
        order = 0
        for name in names:
            order += ORDER_GAP
            keyed[_stable_key(kind, str(name))] = {"order": order, "name": str(name)}
        block[field] = keyed

    if block:
        plan["access"] = block
    else:
        plan.pop("access", None)
    return app_blueprint


def _stable_key(kind: str, name: str) -> str:
    """A letter-first uid that is the same for the same thing every sweep.

    Same reason as the relation graph: these maps are diffed by the override
    machinery, and a freshly minted uid per sweep reads as the whole block being
    deleted and rewritten, which detaches every tenant override of it.
    """
    import hashlib

    return "a" + hashlib.sha1(f"{kind}:{name}".encode()).hexdigest()[:11]


def index_brand(app_blueprint: dict[str, Any], brand: dict[str, Any]) -> dict[str, Any]:
    """Write the site's real typeface and palette into `plan.brand`.

    The app plan already had a `brand` block and it was pure invention: nothing
    ever read the theme, so the model filled it with a palette that looked
    plausible beside one that was actually in use. A plan that states the wrong
    colours is worse than one that states none, because the next thing to read
    it builds against them.

    Derived keys are refreshed every sweep, because the theme is the truth about
    them and a stale copy is the whole problem. Stated keys — `tone`, `motion`,
    anything a person wrote — are left exactly alone: they are a judgement about
    the brand, not a reading of it, and no amount of re-reading the theme
    produces or corrects them.
    """
    look = brand.get("look")
    if not isinstance(look, dict) or not look:
        return app_blueprint

    plan = app_blueprint.get("plan")
    if not isinstance(plan, dict):
        plan = {}
        app_blueprint["plan"] = plan
    block = plan.get("brand")
    if not isinstance(block, dict):
        block = {}
        plan["brand"] = block

    typeface = look.get("fontFamily")
    if typeface:
        block["typeface"] = typeface
    if brand.get("theme"):
        block["theme"] = brand["theme"]

    # A colour is a value beginning with '#'. The type ramp is everything whose
    # name ends in Font — nine entries reading "14px/14px <fontFamily>", which
    # is the scale somebody would otherwise have to open the theme to see.
    palette = {n: v for n, v in look.items() if isinstance(v, str) and v.startswith("#")}
    if palette:
        block["palette"] = palette
    scale = {n: v for n, v in look.items() if n.endswith("Font")}
    if scale:
        block["typeScale"] = scale

    # `fontPacks` deliberately not copied. It is a list of web-font packs the
    # index HTML loads, and a keyed map of them would need minted uids to satisfy
    # the no-arrays rule for something `typeface` already answers.
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
