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
from app.core.session import AuthContext, BaseSession
from app.services.session_manager import get_session_manager
from app.services.blueprint import objects, plan_job, service
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
            prompt=body.prompt, kind=body.kind, app_code=body.app_code,
            context=context, existing=existing,
        )
    except BlueprintGenerationError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc

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
        )
    except BlueprintGenerationError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc

    if body.save and result["describes"]:
        blueprint = document.get("blueprint") or {}
        if body.seed:
            blueprint = seed_entries(blueprint, document, body.kind, result["describes"])
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
    name: str
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
        return await service.suggest_features(objects=listed, app_code=body.app_code)
    except BlueprintGenerationError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc


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

    results = await asyncio.gather(
        *(listing(kind) for kind in wanted), return_exceptions=True,
    )

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


@router.get("/object")
async def get_object(
    app_code: str,
    kind: str,
    name: str,
    auth: AuthContext = Depends(require_auth_context),
) -> dict[str, Any]:
    """One object's document, cut down to what the board draws from it.

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
            seed_app_plan=body.app_plan,
            kinds=tuple(k.strip() for k in body.kinds.split(",") if k.strip()) or None,
        )
    except BlueprintObjectError as exc:
        raise _object_error(exc) from exc
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
    job = plan_job.get(job_id)
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
    job = plan_job.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such planning job.")
    await _scope(auth, job.app_code, write=False)

    async def events():
        last = ""
        idle = 0.0
        while True:
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
                # A comment, which SSE ignores and every proxy counts as traffic.
                yield ": keep-alive\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx buffers a response body by default, which for a stream means
            # the browser sees nothing until the sweep ends.
            "X-Accel-Buffering": "no",
        },
    )


#: How often the stream looks for a change. Not how often it writes: it writes
#: when the progress differs, which on a sweep is once per object.
_STREAM_TICK = 0.5

#: Silence after which the stream writes a comment, to keep an idle proxy from
#: closing a connection that is working perfectly well.
_STREAM_KEEPALIVE = 20.0


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

    session = BaseSession(agent_name="blueprint")
    if body.app_code:
        session.context["app_code"] = body.app_code
    if body.editor_context:
        session.context["editor_context"] = body.editor_context
    # Nothing here writes a definition, so there is nothing to confirm and
    # nothing to hold back behind a draft.
    session.context["auto_confirm"] = True
    await session.get_or_create(body.session_id, auth)

    if not body.session_id:
        title = body.message[:100].strip()
        if title:
            await get_session_manager().update_session_title(
                session.session_id, title, auth.user_id
            )

    return await stream_agent_response(await plan_agent(), body.message, session)
