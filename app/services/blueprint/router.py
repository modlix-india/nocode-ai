"""HTTP surface for blueprints, mounted at /api/ai/blueprint.

Consumers:
  - the BlueprintEditor board in nocode-ui (read, describe, suggest, save)
  - SiteZump's AI Studio page, which is that board plus a prompt
  - anything that wants a plan generated without opening a chat session

`create_common_routes` is deliberately NOT called here. That is agent session
and run plumbing — /chat, /sessions, /runs — and none of these endpoints is an
agent. Mounting it would advertise a conversation surface that does not exist.

Access is the app's own: read access on the app to read a plan, edit access to
write one, resolved against the security service on every call by the same
module lore uses. A plan is a description of somebody's business and is not less
sensitive than the definitions it describes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.base_auth import require_auth_context
from app.core.base_router import stream_agent_response
from app.core.session import AuthContext, BaseSession, session_title
from app.services.billing import CallMeter
from app.services.blueprint import build_job, job_store, objects, plan_job, publish, service
from app.services.blueprint.objects import BlueprintObjectError
from app.services.blueprint.service import BlueprintGenerationError
from app.services.blueprint.compose import (
    apply_describes,
    build_app_context,
    seed_entries,
)
from app.services.blueprint.validate import validate_blueprint
from app.services.lore import access
from app.services.lore.access import LoreAccessError

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Access ───────────────────────────────────────────────────────────────


async def _scope(auth: AuthContext, app_code: str, *, write: bool):
    """Resolve what this caller may do with this app, or refuse.

    Reuses lore's resolver rather than growing a second one. The questions are
    identical — does this client have read/edit access to this app — and two
    implementations of an access check is one more than anybody can keep right.
    """
    try:
        scope = await access.resolve_scope(auth, app_code)
        if write:
            scope.require_write()
        else:
            scope.require_read()
        return scope
    except LoreAccessError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


def _headers(auth: AuthContext) -> dict[str, str]:
    """Platform headers for a call made on the caller's behalf.

    `appCode` here is the ACCESS app — the product the caller is using, such as
    sitezump — and not the app being planned. Sending the planned app's code
    would make the gateway resolve authorities against an app the user may have
    no registration on.
    """
    headers = {"Authorization": auth.token, "clientCode": auth.client_code}
    if auth.access_app_code:
        headers["appCode"] = auth.access_app_code
    if auth.forwarded_host:
        headers["X-Forwarded-Host"] = auth.forwarded_host
    if auth.forwarded_port:
        headers["X-Forwarded-Port"] = auth.forwarded_port
    return headers


def _object_error(exc: BlueprintObjectError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


def _generation_error(exc: BlueprintGenerationError) -> HTTPException:
    """502 for a model that failed, 402 for a wallet that is empty.

    They are not the same thing and a client has to be able to tell them apart:
    one is "try again", the other is "add money", and a screen that offers
    Retry for an empty wallet teaches people that Retry does nothing.

    402 rather than 403 because nothing here is forbidden — the caller has every
    right to this and has run out of credit, which is precisely what 402 is for.
    """
    if exc.reason == "out-of-tokens":
        return HTTPException(status_code=402, detail=exc.message)
    return HTTPException(status_code=502, detail=exc.message)


# ── Reads ────────────────────────────────────────────────────────────────


@router.get("/object")
async def get_blueprint(
    app_code: str = Query(..., description="The app the object belongs to"),
    kind: str = Query("application", description=f"One of: {', '.join(objects.KIND_NAMES)}"),
    name: str = Query("", description="Object name. Ignored for kind=application."),
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """One object's plan. `blueprint` is `{}` when it has none, which is normal."""
    await _scope(auth, app_code, write=False)
    try:
        return await objects.read_blueprint(kind, app_code, name, _headers(auth))
    except BlueprintObjectError as exc:
        raise _object_error(exc) from exc


@router.get("/drift")
async def get_drift(
    app_code: str = Query(...),
    kind: str = Query("page"),
    name: str = Query(...),
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Which plan entries agree with the definition, and which do not.

    Three states, and the two that are not `clean` move in opposite directions:
    `pending` means the plan is ahead and resolving it changes the definition;
    `drifted` means the definition is ahead and resolving it changes the plan.
    """
    await _scope(auth, app_code, write=False)
    try:
        document = await objects.read_object(kind, app_code, name, _headers(auth))
    except BlueprintObjectError as exc:
        raise _object_error(exc) from exc
    return {"kind": kind, "name": name, "app_code": app_code, **objects.drift_of(document)}


# ── Writes ───────────────────────────────────────────────────────────────


class SaveRequest(BaseModel):
    app_code: str
    kind: str = "application"
    name: str = ""
    blueprint: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


@router.post("/object")
async def save_blueprint(
    body: SaveRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Replace one object's plan. Refused whole if it breaks a rule.

    Refused, not repaired. A plan that is quietly fixed on the way in stops
    matching what the caller believes it wrote, and the next read surprises
    them. Coercion belongs to generation, where the alternative is discarding a
    model's whole answer over a shape that converts mechanically.
    """
    await _scope(auth, body.app_code, write=True)

    issues = validate_blueprint(body.blueprint)
    if issues:
        raise HTTPException(status_code=400, detail={
            "message": "The plan was refused. Nothing was written.",
            "issues": [{"path": i.path, "message": i.message} for i in issues],
        })

    try:
        return await objects.write_blueprint(
            body.kind, body.app_code, body.name, body.blueprint,
            _headers(auth), auth.client_code, message=body.message,
        )
    except BlueprintObjectError as exc:
        raise _object_error(exc) from exc


# ── Derivation ───────────────────────────────────────────────────────────


class GenerateRequest(BaseModel):
    prompt: str
    app_code: str = ""
    kind: str = "application"
    name: str = ""
    #: Refine the plan the object already has, rather than starting fresh.
    refine: bool = True
    #: Write the result straight to the object. Off by default: a generated plan
    #: is a proposal, and a person should see it before it becomes what the app
    #: claims to be.
    save: bool = False
    message: str = ""


@router.post("/generate")
async def post_generate(
    body: GenerateRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Turn a prompt into a plan. Returns it; only writes when asked to."""
    headers = _headers(auth)
    if body.app_code:
        await _scope(auth, body.app_code, write=body.save)

    existing: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    if body.app_code and body.refine:
        try:
            current = await objects.read_blueprint(body.kind, body.app_code, body.name, headers)
            existing = current.get("blueprint") or None
        except BlueprintObjectError:
            # Planning something that does not exist yet is the normal first
            # case, not an error: a plan can precede its object.
            existing = None
        context = await build_app_context(body.app_code, headers)

    try:
        result = await service.generate(
            meter=CallMeter(auth, session_id=f"blueprint-generate:{body.app_code}"),
            prompt=body.prompt, kind=body.kind, app_code=body.app_code,
            context=context, existing=existing,
        )
    except BlueprintGenerationError as exc:
        raise _generation_error(exc) from exc

    if body.save and result["valid"] and body.app_code:
        try:
            saved = await objects.write_blueprint(
                body.kind, body.app_code, body.name, result["blueprint"],
                headers, auth.client_code, message=body.message or "plan generated",
            )
            result["saved"] = True
            result["version"] = saved.get("version")
        except BlueprintObjectError as exc:
            raise _object_error(exc) from exc
    else:
        result["saved"] = False
    return result


class DescribeRequest(BaseModel):
    app_code: str
    kind: str = "page"
    name: str
    #: Write the lines onto the plan's entries as `describes`. Never touches
    #: `purpose`, which only a person writes.
    save: bool = False
    #: Create a plan entry for a described part that has none.
    #:
    #: Off by default, and named separately from `save` rather than folded into
    #: it, because it is a different act: `save` annotates a plan, `seed` makes
    #: one. But without it "Explain this" does nothing visible on the case that
    #: matters most — an existing site, which is every site on day one, has no
    #: plan at all, so there are no entries for a description to land on and the
    #: derivation is computed, charged for, and thrown away.
    seed: bool = False


@router.post("/describe")
async def post_describe(
    body: DescribeRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """One line per part of one object, derived from its definition.

    This is the metered call, and it is why nothing here runs on page open. The
    board renders its titles from definitions for free; a description costs
    tokens, so it happens when somebody asks for it.
    """
    await _scope(auth, body.app_code, write=body.save)
    headers = _headers(auth)
    try:
        document = await objects.read_object(body.kind, body.app_code, body.name, headers)
    except BlueprintObjectError as exc:
        raise _object_error(exc) from exc

    try:
        result = await service.describe(
            document=document, kind=body.kind, app_code=body.app_code,
            meter=CallMeter(auth, session_id=f"blueprint-describe:{body.name}"),
        )
    except BlueprintGenerationError as exc:
        raise _generation_error(exc) from exc

    if body.save and result["describes"]:
        blueprint = document.get("blueprint") or {}
        if body.seed:
            blueprint = seed_entries(
                blueprint, document, body.kind, result["describes"],
                result.get("names") or {},
            )
        blueprint = apply_describes(blueprint, result["describes"], body.kind)
        try:
            saved = await objects.write_blueprint(
                body.kind, body.app_code, body.name, blueprint,
                headers, auth.client_code, message="descriptions derived",
            )
            result["saved"] = True
            result["version"] = saved.get("version")
        except BlueprintObjectError as exc:
            raise _object_error(exc) from exc
    else:
        result["saved"] = False
    return result


class ReconcileRequest(BaseModel):
    app_code: str
    kind: str = "page"
    #: Which object. EMPTY MEANS THE WHOLE APP.
    #:
    #: "Update the plan from the site" is an app-level act — somebody has been
    #: editing pages and the plan is behind across several of them — and making
    #: it a required field meant the board's own banner could not call its own
    #: endpoint. It sent no name, the request failed validation, and the button
    #: reported an unknown error.
    name: str = ""
    #: The plan entries to accept. Empty means every drifted entry on the object.
    uids: str = ""
    message: str = ""


@router.post("/reconcile")
async def post_reconcile(
    body: ReconcileRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Accept what was built as what was meant. Moves the PLAN, never the build.

    This is the whole of "Update the plan", and the direction is the point.
    A drifted entry means somebody edited the page by hand after the plan was
    agreed; the edit is the newer fact and the plan is the stale one. So this
    restamps the entry's fingerprint against the definition as it stands now and
    touches nothing else. It cannot revert anybody's work, because it never
    writes a definition.

    It also does not re-derive the description, though the section it describes
    has changed. That would spend the customer's tokens on a button that does not
    say it costs anything. "Explain again" is the button that does.

    The opposite direction — a plan entry nothing was built for — is NOT handled
    here and must not be: resolving that means building, which is the agent's
    ordinary work. Collapsing the two into one "sync" is how an update overwrites
    the thing it was meant to record.
    """
    await _scope(auth, body.app_code, write=True)
    headers = _headers(auth)

    if not body.name:
        return await _reconcile_app(body, auth, headers)

    try:
        document = await objects.read_object(body.kind, body.app_code, body.name, headers)
    except BlueprintObjectError as exc:
        raise _object_error(exc) from exc

    blueprint = document.get("blueprint") or {}
    plan = blueprint.get("plan") if isinstance(blueprint.get("plan"), dict) else {}
    sections = plan.get("sections") if isinstance(plan.get("sections"), dict) else {}
    if not sections:
        raise HTTPException(
            status_code=400,
            detail=f"{body.kind} '{body.name}' has no plan entries to reconcile.",
        )

    wanted = {u.strip() for u in body.uids.split(",") if u.strip()}
    drift = objects.drift_of(document)
    if not wanted:
        wanted = {uid for uid, state in drift["status"].items() if state == "drifted"}

    versions = document.get("componentVersions") or {}
    reconciled = blueprint.get("reconciled")
    if not isinstance(reconciled, dict):
        reconciled = {}
        blueprint["reconciled"] = reconciled

    stamped: list[str] = []
    for uid in sorted(wanted):
        entry = sections.get(uid)
        if not isinstance(entry, dict):
            continue
        component_key = entry.get("componentKey")
        if component_key and component_key in versions:
            reconciled[uid] = versions[component_key]
            stamped.append(uid)

    if not stamped:
        # Nothing to do is not a failure, and reporting it as one would have the
        # board show an error for a board that is already right.
        return {"kind": body.kind, "name": body.name, "app_code": body.app_code,
                "reconciled": [], "saved": False}

    try:
        saved = await objects.write_blueprint(
            body.kind, body.app_code, body.name, blueprint,
            headers, auth.client_code,
            message=body.message or f"plan accepts {len(stamped)} built section(s)",
        )
    except BlueprintObjectError as exc:
        raise _object_error(exc) from exc

    return {
        "kind": body.kind, "name": body.name, "app_code": body.app_code,
        "reconciled": stamped, "saved": True, "version": saved.get("version"),
    }


async def _reconcile_app(
    body: ReconcileRequest, auth: AuthContext, headers: dict[str, str],
) -> dict[str, Any]:
    """Accept what was built, across every object of the kind, in one press.

    Sequential and one write per object that actually moved. A page with no
    drift is read and left alone rather than written with its own contents,
    which would bump its version and, worse, show up in its history as an edit
    nobody made.

    One object failing is one object: it is named in `failed` and the rest are
    still reconciled. The alternative — refusing the whole thing because the
    fourth page could not be read — throws away three pages of correct work and
    leaves the person with no way to make progress on the other three.
    """
    try:
        rows = await objects.list_objects(body.kind, body.app_code, headers)
    except BlueprintObjectError as exc:
        raise _object_error(exc) from exc

    updated: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    for row in rows:
        one = ReconcileRequest(
            app_code=body.app_code, kind=body.kind, name=row["name"],
            message=body.message,
        )
        try:
            result = await post_reconcile(one, auth)
        except HTTPException as exc:
            # A page with no plan entries answers 400, and that is not a
            # failure of this sweep: most pages on most sites have no plan.
            if exc.status_code != 400:
                failed[row["name"]] = str(exc.detail)
            continue
        except BlueprintObjectError as exc:
            failed[row["name"]] = exc.message
            continue
        if result.get("saved"):
            updated.append({"name": row["name"], "reconciled": result.get("reconciled") or []})

    return {
        "app_code": body.app_code,
        "kind": body.kind,
        "objects": updated,
        "reconciled": sum(len(u["reconciled"]) for u in updated),
        "saved": bool(updated),
        "failed": failed,
    }


class SuggestRequest(BaseModel):
    app_code: str
    kinds: Optional[str] = Field(
        default=None,
        description="Comma-separated kinds to consider. Defaults to pages and storages.",
    )


@router.post("/suggest-features")
async def post_suggest_features(
    body: SuggestRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Group an app's objects into named capabilities. A proposal, never saved."""
    await _scope(auth, body.app_code, write=False)
    headers = _headers(auth)
    wanted = [
        k.strip() for k in (body.kinds or ",".join(objects.BOARD_KINDS)).split(",") if k.strip()
    ]

    listed: list[dict[str, Any]] = []
    for kind in wanted:
        try:
            listed.extend(await objects.list_objects(kind, body.app_code, headers))
        except BlueprintObjectError as exc:
            logger.info("blueprint: skipping %s for %s: %s", kind, body.app_code, exc.message)

    try:
        return await service.suggest_features(
            objects=listed, app_code=body.app_code,
            meter=CallMeter(auth, session_id=f"blueprint-features:{body.app_code}"),
        )
    except BlueprintGenerationError as exc:
        raise _generation_error(exc) from exc


# ── Everything the app is made of, in one call ───────────────────────────


@router.get("/objects")
async def get_objects(
    app_code: str,
    kinds: str = "",
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Name, title and description for every object of every kind.

    The board needs this before it can draw a single column, and the alternative
    was one platform call per kind from the page itself: nine FetchData steps
    and nine SetStores hand-authored in KIRun, which is nine places for the list
    of kinds to fall out of step with the sweep that plans them. One call, one
    place, and the kind list lives next to the code that uses it.

    Listed in parallel because they are independent reads and a person is
    waiting for all of them; sequentially this is nine round trips of latency
    before the first column appears.

    A kind that cannot be listed is reported under `unavailable` rather than
    failing the request. An older core build with no notifications route must
    not be the reason a board shows no pages.
    """
    await _scope(auth, app_code, write=False)
    headers = _headers(auth)
    wanted = tuple(k.strip() for k in kinds.split(",") if k.strip()) or objects.BOARD_KINDS

    async def listing(kind: str) -> list[dict[str, Any]]:
        return await objects.list_objects(kind, app_code, headers)

    # The FIRST one alone, then the rest together.
    #
    # Every one of these makes the platform ask the security service whether
    # this caller may read this app, and that answer is cached. Firing nine at
    # once against a cold cache is nine concurrent identical questions, which is
    # the shape that lets a negative answer get cached and then refuse every
    # read of the app until something evicts it. One call first settles the
    # answer; the other eight then read it.
    results: list[Any] = []
    if wanted:
        try:
            results.append(await listing(wanted[0]))
        except Exception as exc:  # noqa: BLE001 — reported per kind below
            results.append(exc)
    results.extend(await asyncio.gather(
        *(listing(kind) for kind in wanted[1:]), return_exceptions=True,
    ))

    found: dict[str, Any] = {}
    unavailable: dict[str, str] = {}
    for kind, result in zip(wanted, results):
        if isinstance(result, BaseException):
            message = getattr(result, "message", str(result))
            logger.info("blueprint: cannot list %s in %s: %s", kind, app_code, message)
            unavailable[kind] = message
        elif result:
            found[kind] = result

    return {
        "appCode": app_code,
        "objects": found,
        "counts": {kind: len(rows) for kind, rows in found.items()},
        "unavailable": unavailable,
    }


@router.get("/document")
async def get_object(
    app_code: str,
    kind: str,
    name: str,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """One object's document, cut down to what the board draws from it.

    `/document`, NOT `/object`. `/object` was already taken, by the route that
    reads a plan — and FastAPI matches the first declaration, so this one was
    dead from the moment it was written. Everything asking for a page got the
    plan-only payload back: no component map, no schema, no steps. The board
    then drew every column from its plan alone, so a page's cards were all
    "planned, not built" while the page stood there fully built, and a function
    column was simply empty. One shadowed route, and the whole board lied.

    The board opens a column by fetching the object behind it, and doing that
    from the page meant knowing which platform route each kind lives on — a
    nine-way branch in KIRun, in a second place, kept in step by hand. The kind
    table is already here (`objects.KINDS`); this is that table answering.

    Trimmed rather than proxied whole. A page document carries its translations,
    its permissions and its properties alongside the component map, and the
    board draws cards from the component map alone.
    """
    await _scope(auth, app_code, write=False)
    try:
        document = await objects.read_object(kind, app_code, name, _headers(auth))
    except BlueprintObjectError as exc:
        raise _object_error(exc) from exc
    return objects.board_document(document, kind)


# ── Planning the whole app, as a watchable job ───────────────────────────


class PlanRequest(BaseModel):
    app_code: str
    #: What the person said they wanted, when they said anything. Empty means
    #: "read what is built and write down what it is", which is the case for
    #: every site that existed before plans did.
    prompt: str = ""
    #: Rewrite the app-level plan as well as the per-object ones. Off when
    #: somebody only wants the objects described against a plan they have
    #: already edited, since generation would overwrite their wording.
    app_plan: bool = True
    #: Which kinds to sweep, comma separated. Empty means what the board draws:
    #: pages and storages. A caller that renders more can ask for more, and one
    #: that renders less should ask for less, because a derivation nothing
    #: displays is tokens spent on something nobody reads.
    kinds: str = ""


@router.post("/plan")
async def post_plan(
    body: PlanRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Start a sweep over every object, and answer with what it will do.

    Returns immediately, before any model call. The reply already carries the
    full step list, so the board can show what is about to be read instead of a
    spinner with nothing behind it.

    An app with a sweep already running gets that one back rather than a second:
    two sweeps would race on the same objects and bill twice for it.
    """
    await _scope(auth, body.app_code, write=True)

    running = plan_job.running_for(body.app_code)
    if running:
        return {**running.progress(), "joined": True}

    headers = _headers(auth)
    try:
        job = await plan_job.start(
            app_code=body.app_code,
            prompt=body.prompt or _DEFAULT_PLAN_PROMPT,
            headers=headers,
            client_code=auth.client_code,
            auth=auth,
            seed_app_plan=body.app_plan,
            kinds=tuple(k.strip() for k in body.kinds.split(",") if k.strip()) or None,
        )
    except BlueprintObjectError as exc:
        raise _object_error(exc) from exc
    except BlueprintGenerationError as exc:
        # The wallet gate, asked before the task was created. A sweep refused
        # here has spent nothing and started nothing.
        raise _generation_error(exc) from exc
    return {**job.progress(), "joined": False}


@router.get("/plan/{job_id}")
async def get_plan(
    job_id: str,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Where the sweep has got to.

    404 once the job has aged out of the registry, which a client should read as
    "finished a while ago", not as failure: every step's work was written to its
    own object as that step completed.
    """
    job = await _find_job(job_id, plan_job.get)
    if job is None:
        raise HTTPException(status_code=404, detail="No such planning job.")
    await _scope(auth, job.app_code, write=False)
    return job.progress()


_DEFAULT_PLAN_PROMPT = (
    "Write the plan for this site by reading what is already built. The pages "
    "and storages in the context are what exists; say what the site is for, who "
    "it is for, and what each object is there to do. Group objects into features "
    "where several of them only mean anything together. Do not invent objects "
    "that are not listed."
)


@router.get("/plan/{job_id}/stream")
async def stream_plan(
    job_id: str,
    auth: AuthContext = Depends(require_auth_context),
) -> StreamingResponse:
    """The same progress, pushed instead of asked for.

    A sweep takes minutes and changes a handful of times, so polling it spends
    a request every couple of seconds to be told nothing has moved — which is
    what it looked like in the network panel, a wall of identical GETs. This
    holds one connection open and writes only when the progress actually
    changes, plus a keep-alive comment so an idle proxy does not close it.

    The poll endpoint stays. A stream can be refused by something between here
    and the browser, and a client that cannot open one should degrade to asking
    rather than lose the ability to watch.
    """
    job = await _find_job(job_id, plan_job.get)
    if job is None:
        raise HTTPException(status_code=404, detail="No such planning job.")
    await _scope(auth, job.app_code, write=False)

    return _progress_stream(job)


#: How often the stream looks for a change. Not how often it writes: it writes
#: when the progress differs, which on a sweep is once per object.
_STREAM_TICK = 0.5

#: Silence after which the stream writes a comment, to keep an idle proxy from
#: closing a connection that is working perfectly well.
_STREAM_KEEPALIVE = 20.0


# ── Building: turning the plan into objects that exist ───────────────────


class BuildRequest(BaseModel):
    app_code: str
    #: Author the content as well as creating the objects.
    #:
    #: Off is a real and useful choice: creating every planned object is fast,
    #: cheap and reliable, and it makes the shape of the site real so somebody
    #: can look at it before anything is spent writing copy into it.
    fill: bool = True


@router.post("/build")
async def post_build(
    body: BuildRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Build what the plan says, and answer with the checklist.

    Returns before anything is created, carrying the full step list, because
    the plan IS the checklist — which is what lets one board be the progress
    view, the retry unit and the failure report at the same time.

    An app already building gets that build back rather than a second one. Two
    would race on the same objects and the loser would create what the winner
    had just created.
    """
    await _scope(auth, body.app_code, write=True)

    running = build_job.running_for(body.app_code)
    if running:
        return {**running.progress(), "joined": True}

    try:
        job = await build_job.start(
            app_code=body.app_code,
            headers=_headers(auth),
            auth=auth,
            fill=body.fill,
        )
    except BlueprintObjectError as exc:
        raise _object_error(exc) from exc
    return {**job.progress(), "joined": False}


@router.get("/build/{job_id}")
async def get_build(
    job_id: str,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Where the build has got to."""
    job = await _find_job(job_id, build_job.get)
    if job is None:
        raise HTTPException(status_code=404, detail="No such build.")
    await _scope(auth, job.app_code, write=False)
    return job.progress()


@router.get("/build/{job_id}/stream")
async def stream_build(
    job_id: str,
    auth: AuthContext = Depends(require_auth_context),
) -> StreamingResponse:
    """The build's progress, pushed. Same contract as the planning stream."""
    job = await _find_job(job_id, build_job.get)
    if job is None:
        raise HTTPException(status_code=404, detail="No such build.")
    await _scope(auth, job.app_code, write=False)
    return _progress_stream(job)


async def _find_job(job_id: str, local: Any) -> Any | None:
    """A job by id, from this worker or from whichever one is running it.

    Production runs four workers and each registry is a dict in one process, so
    a poll or a stream lands on the right worker about one time in four. Falling
    back to what the owning worker published turns "no such job" — which is what
    three people in four were told about a job running fine — into an answer.
    """
    job = local(job_id)
    if job is not None:
        return job
    payload = await job_store.read(job_id)
    return job_store.RemoteJob(payload) if payload else None


def _progress_stream(job: Any) -> StreamingResponse:
    """One SSE body for any job that can describe its own progress.

    Written once rather than per job type: a build and a planning sweep report
    the same shape on purpose, so that one renderer draws both and a second
    copy of this loop would be a second place for the keep-alive to be wrong.
    """

    async def events():
        last = ""
        idle = 0.0
        while True:
            # A job held by ANOTHER worker only changes when it is re-read, so
            # the remote case refreshes before each frame. `RemoteJob` exists to
            # make that the only difference between the two: everything below
            # is identical, because a second copy of this loop would be a second
            # place for the keep-alive and the terminal condition to be wrong.
            if hasattr(job, "refresh"):
                await job.refresh()
            current = job.progress()
            encoded = json.dumps(current, sort_keys=True)
            if encoded != last:
                last = encoded
                idle = 0.0
                yield f"event: progress\ndata: {encoded}\n\n"
            if job.state != "running":
                yield "event: done\ndata: {}\n\n"
                return
            await asyncio.sleep(_STREAM_TICK)
            idle += _STREAM_TICK
            if idle >= _STREAM_KEEPALIVE:
                idle = 0.0
                yield ": keep-alive\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── The planning conversation ────────────────────────────────────────────


class PlanChatRequest(BaseModel):
    message: str
    app_code: str = ""
    session_id: Optional[str] = None
    editor_context: Optional[dict[str, Any]] = None


_plan_agent = None


async def plan_agent():
    """One agent for the process, built on first use.

    Cheap to construct — four tools and a persona, no catalogs and nothing off
    the network — so there is no startup wiring to keep in step with main.py.

    The `load()` is what makes the persona the cached static prefix, and it is
    not optional: without it the first message comes back as "Call load() before
    building system prompt", which is a RuntimeError the customer reads.
    """
    global _plan_agent
    if _plan_agent is None:
        from app.services.blueprint.agent import PlanAgent

        agent = PlanAgent()
        await agent.context_builder.load()
        _plan_agent = agent
    return _plan_agent


@router.post("/chat")
async def plan_chat(
    body: PlanChatRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> StreamingResponse:
    """Talk about the plan. Streams SSE, exactly as the build agent does.

    Separate from `/api/ai/appbuilder/chat` because the two conversations are
    about different things and, more to the point, are ABLE to do different
    things. The board used to post here's sibling, and a question about adding a
    blog came back with a storage, two server functions and two pages built on
    the customer's live site. That is what an agent holding build tools does with
    a build-shaped request; the fix is not a sterner prompt, it is a different
    agent with a different toolbox.

    Write access is required even to ask: the answer to a planning question is
    usually a plan written down, and finding out at the end that the caller may
    not save it wastes the turn.
    """
    if body.app_code:
        await _scope(auth, body.app_code, write=True)
        auth.app_code = body.app_code

    # Named at creation — see the appbuilder route. A new session goes in with
    # its name; a resumed one keeps the one it has.
    session = BaseSession(agent_name="blueprint", title=session_title(body.message))
    if body.app_code:
        session.context["app_code"] = body.app_code
    if body.editor_context:
        session.context["editor_context"] = body.editor_context
    # Nothing here writes a definition, so there is nothing to confirm and
    # nothing to hold back behind a draft.
    session.context["auto_confirm"] = True
    await session.get_or_create(body.session_id, auth)

    return await stream_agent_response(await plan_agent(), body.message, session)


# ── Putting it on the site ───────────────────────────────────────────────


class PublishRequest(BaseModel):
    app_code: str


@router.get("/pending")
async def get_pending(
    app_code: str = Query(..., description="The app to check"),
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """What is built and not yet on the site.

    Read access is enough: knowing how much is waiting is not a change, and the
    board asks for it on every load to decide whether Publish has anything to
    do. A button that offers to publish nothing is how people learn to distrust
    the next one.
    """
    await _scope(auth, app_code, write=False)
    return await publish.pending(app_code, _headers(auth), auth.client_code)


@router.post("/publish")
async def post_publish(
    body: PublishRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Put everything drafted for this app on the site.

    The one route in this service that changes what the public can see, and it
    exists only because a person pressed a button. Nothing calls it: not the
    build, not the sweep, not an agent. The build deliberately creates
    unpublished and authors onto a draft, which is right — and left the person
    who pressed Build unable to see the result, because a page created
    unpublished 404s on the live surface and on the draft host alike.
    """
    await _scope(auth, body.app_code, write=True)
    try:
        return await publish.publish_all(body.app_code, _headers(auth), auth.client_code)
    except publish.PublishError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@router.get("/running")
async def get_running(
    app_code: str = Query(..., description="The app to ask about"),
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """Whatever job this app has in flight, or `{}`.

    What a freshly loaded board asks, and the answer to a question it could not
    previously ask at all. The job id lived only in the page store, written from
    the POST that started it, so a refresh lost the id — and with it any way to
    find a job that was still running perfectly well on the server.

    Both registries are asked because a board does not know which kind it is
    looking for: it refreshed, so it knows nothing. The payload carries `kind`,
    which is what tells it which stream to open afterwards.

    Local first, then what another worker published. Production runs four
    workers and each registry is a dict in one process, so the local answer is
    right about one time in four — and a wrong "nothing is running" is worse
    than no route, because it says the build is over when it is not.

    `{}` is the ordinary answer and not an error: most of the time nothing is
    running, and a 404 for the normal case makes every caller handle a failure
    that is not one.
    """
    await _scope(auth, app_code, write=False)

    for registry in (build_job, plan_job):
        job = registry.running_for(app_code)
        if job is not None:
            return job.progress()

    published = await job_store.running_for(app_code)
    return published or {}
