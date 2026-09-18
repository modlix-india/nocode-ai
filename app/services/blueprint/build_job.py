"""Executing a plan: turning what was written down into objects that exist.

── The two verbs, and why they are two ──────────────────────────────────

A plan is a document. Building it is not one act but two, and collapsing them
is what makes a build screen untrustworthy.

  MAKE ROOM   Create the row. A page nobody has created has no `blueprint`
              field to hold its own plan, so a planned page's sections live in
              the app plan under `spec` (see compose.index_objects). Making the
              object moves that spec onto the object where it belongs. No model
              call, nothing authored: it is a create and a move, it takes a
              second per object, and it is the half that can be trusted.

  FILL IT IN  Author the components. This is the expensive, fallible half — a
              model reading one section's brief and writing the thing. It runs
              per object and writes only that object.

Separating them means the first half either works or fails loudly and cheaply,
and the second half's failures are per card. It also means a plan can be
REALISED without being filled: the objects exist, the board is real, and
somebody can look at the shape of the site before spending anything on content.

── Why the failure model is per card ────────────────────────────────────

The mockup's promise, in its own words: "If one card fails it is one card, not
the site." So every step is one object or one section, each is written the
moment it lands, and a failure marks that step and the sweep carries on. A
build that dies halfway has still built half a site, and the board says which
half.

── What this deliberately does not do ───────────────────────────────────

It never publishes. Everything it creates is created unpublished where the
platform supports it, because "the objects exist" and "the public can see them"
are different decisions and the second one is the customer's.

It also never deletes. A plan entry that disappeared is not a licence to remove
a page somebody may have edited by hand; that is a conversation, not a sweep.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.session import BaseSession
from app.services.blueprint import objects
from app.services.blueprint.compose import collection_for
from app.services.blueprint import job_store
from app.services.blueprint.objects import BlueprintObjectError

logger = logging.getLogger(__name__)

#: Kinds this can create, in the order they must be created.
#:
#: Storages first, always. A page's form posts into a storage, so a page filled
#: before its storage exists is a form wired to nothing — and the wiring is the
#: part nobody notices is missing until a customer's first enquiry vanishes.
MAKEABLE_KINDS: tuple[str, ...] = ("storage", "page")

KEEP_FINISHED_SECONDS = 30 * 60
JOB_TIMEOUT_SECONDS = 60 * 60

WAITING, WORKING, DONE, FAILED, SKIPPED = "waiting", "working", "done", "failed", "skipped"

MAKE, FILL = "make", "fill"


@dataclass
class BuildStep:
    """One object to create, or one object's sections to author."""

    phase: str
    kind: str
    name: str
    label: str
    state: str = WAITING
    detail: str = ""
    seconds: float = 0.0
    #: The plan entry this step came from, so the board can mark the right card.
    uid: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "kind": self.kind,
            "name": self.name,
            "label": self.label,
            "state": self.state,
            "detail": self.detail,
            "uid": self.uid,
            "seconds": round(self.seconds, 1),
        }


@dataclass
class BuildJob:
    id: str
    app_code: str
    state: str = "running"
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    steps: list[BuildStep] = field(default_factory=list)
    task: asyncio.Task | None = None
    #: Off means MAKE ROOM only: create every planned object and stop. The
    #: cheap half on its own is a useful act — the site's shape becomes real
    #: and nothing has been spent on content.
    fill: bool = True

    def progress(self) -> dict[str, Any]:
        """The same shape `plan_job` answers with, so one renderer draws both."""
        done = sum(1 for s in self.steps if s.state in (DONE, FAILED, SKIPPED))
        working = next((s for s in self.steps if s.state == WORKING), None)
        return {
            "job": self.id,
            "appCode": self.app_code,
            "kind": "build",
            "state": self.state,
            "done": done,
            "total": len(self.steps),
            "failed": sum(1 for s in self.steps if s.state == FAILED),
            "current": working.as_dict() if working else None,
            "steps": [s.as_dict() for s in self.steps],
            "error": self.error,
            "seconds": round((self.finished_at or time.time()) - self.started_at, 1),
        }


_JOBS: dict[str, BuildJob] = {}


def get(job_id: str) -> BuildJob | None:
    _sweep_old()
    return _JOBS.get(job_id)


def running_for(app_code: str) -> BuildJob | None:
    """The build already under way for this app, if there is one.

    Two builds on one app would race each other on the same objects, and the
    second would create what the first had just created.
    """
    _sweep_old()
    for job in _JOBS.values():
        if job.app_code == app_code and job.state == "running":
            return job
    return None


def _sweep_old() -> None:
    cutoff = time.time() - KEEP_FINISHED_SECONDS
    for job_id, job in list(_JOBS.items()):
        if job.state != "running" and job.finished_at and job.finished_at < cutoff:
            _JOBS.pop(job_id, None)


# ── Reading the plan for work ────────────────────────────────────────────


def planned_objects(app_blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    """Every entry in the app plan that names something not yet built.

    `status` is the test, and absence of it is not "planned": a plan written
    before status existed describes a site that already stands, and treating
    those entries as work would try to create every page the site already has.
    So only an explicit "planned" counts.
    """
    plan = (app_blueprint or {}).get("plan") or {}
    entries = plan.get("objects")
    if not isinstance(entries, dict):
        return []
    out = []
    for uid, entry in entries.items():
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        if entry.get("status") != "planned":
            continue
        out.append({**entry, "uid": uid})
    out.sort(key=lambda e: (MAKEABLE_KINDS.index(e["kind"])
                            if e.get("kind") in MAKEABLE_KINDS else 99,
                            e.get("order") or 0))
    return out


def pending_sections(blueprint: dict[str, Any], kind: str) -> list[tuple[str, dict[str, Any]]]:
    """Plan entries with nothing built answering to them, in order.

    Two ways to be pending, and both mean the same thing: the plan is ahead of
    the build, so building is what resolves it.

      NOTHING BUILT   — for a page, a section whose `componentKey` is null. The
                        one link from a plan back into a definition.
      THE PLAN MOVED  — built once, and its intent has been changed since. The
                        fingerprint says so (`objects.plan_moved`).

    The second is what makes changing an existing page expressible at all.
    Without it a section with a `componentKey` was permanently done, so a
    conversation about reworking a page could produce a plan and never anything
    a build could act on.

    Drift is the opposite direction and is deliberately NOT here: a definition
    that moved ahead of its plan is resolved by a conversation, never by
    building over the top of somebody's edit.
    """
    collection, match_on = collection_for(kind)
    entries = ((blueprint or {}).get("plan") or {}).get(collection)
    if not isinstance(entries, dict):
        return []
    pending = [
        (uid, entry) for uid, entry in entries.items()
        if isinstance(entry, dict)
        and (not entry.get(match_on) or objects.plan_moved(blueprint, uid, entry))
    ]
    pending.sort(key=lambda pair: pair[1].get("order") or 0)
    return pending


# ── Starting ────────────────────────────────────────────────────────────


async def start(
    *,
    app_code: str,
    headers: dict[str, str],
    auth: Any,
    fill: bool = True,
) -> BuildJob:
    """Name the work from the plan, then do it in the background.

    The step list is read off the plan before anything runs, so the first poll
    already says what is going to be built. That is not a nicety: the plan IS
    the checklist, which is what lets the same board be the progress view, the
    retry unit and the failure report.
    """
    app = await objects.read_blueprint("application", app_code, "", headers)
    blueprint = app.get("blueprint") or {}

    steps: list[BuildStep] = []
    for entry in planned_objects(blueprint):
        kind = entry.get("kind") or "page"
        if kind not in MAKEABLE_KINDS:
            # An asset, a theme, a uripath. Named so a person can see it was
            # left out rather than silently dropped, and skipped rather than
            # failed, because nothing went wrong — this simply cannot make one.
            steps.append(BuildStep(
                phase=MAKE, kind=kind, name=entry["name"], uid=entry["uid"],
                label=entry["name"], state=SKIPPED,
                detail=f"a {kind} is not something this can create yet",
            ))
            continue
        steps.append(BuildStep(
            phase=MAKE, kind=kind, name=entry["name"], uid=entry["uid"],
            label=entry.get("purpose") or entry["name"],
        ))

    if fill:
        # One fill step per page the plan names, whether it is being created now
        # or has been standing for a year: a page that exists can still have
        # sections nobody has built. What is actually pending is read when the
        # step runs, off the page's own plan.
        for kind in ("page",):
            try:
                rows = await objects.list_objects(kind, app_code, headers)
            except BlueprintObjectError:
                rows = []
            known = {r["name"] for r in rows}
            planned_names = {
                e["name"] for e in planned_objects(blueprint) if e.get("kind") == kind
            }
            for name in sorted(known | planned_names):
                steps.append(BuildStep(
                    phase=FILL, kind=kind, name=name, label=name,
                ))

    job = BuildJob(id=uuid.uuid4().hex, app_code=app_code, steps=steps, fill=fill)
    _JOBS[job.id] = job
    job.task = asyncio.create_task(_run(job, headers, auth))
    # Published beside the job so a board that refreshed, or a request that
    # landed on another of the four workers, can still find it. A no-op when
    # Redis is off, which is the single-process local case.
    job_store.watch(job)
    return job


async def _run(job: BuildJob, headers: dict[str, str], auth: Any) -> None:
    try:
        await asyncio.wait_for(_build(job, headers, auth), timeout=JOB_TIMEOUT_SECONDS)
        job.state = "done"
    except asyncio.TimeoutError:
        job.state = "failed"
        job.error = "The build ran past its time limit and was stopped."
        for step in job.steps:
            if step.state == WORKING:
                step.state, step.detail = FAILED, "stopped when the build timed out"
    except asyncio.CancelledError:
        job.state = "failed"
        job.error = "Stopped."
        raise
    except Exception as exc:  # noqa: BLE001 - a job must record why it died
        logger.exception("blueprint build failed for %s", job.app_code)
        job.state = "failed"
        job.error = str(exc)
    finally:
        job.finished_at = time.time()


async def _build(job: BuildJob, headers: dict[str, str], auth: Any) -> None:
    client_code = getattr(auth, "client_code", "") or ""
    for step in job.steps:
        if step.state == SKIPPED:
            continue
        step.state = WORKING
        began = time.time()
        try:
            if step.phase == MAKE:
                await _make(job, step, headers, client_code)
            else:
                await _fill(job, step, headers, auth)
            if step.state == WORKING:
                step.state = DONE
        except BlueprintObjectError as exc:
            step.state, step.detail = FAILED, exc.message
        except Exception as exc:  # noqa: BLE001 - one card, not the site
            logger.exception("build step failed: %s %s %s", step.phase, step.kind, step.name)
            step.state, step.detail = FAILED, str(exc)
        finally:
            step.seconds = time.time() - began


# ── Make room ───────────────────────────────────────────────────────────


async def _make(
    job: BuildJob, step: BuildStep, headers: dict[str, str], client_code: str,
) -> None:
    """Create one object and move its plan onto it.

    Idempotent on purpose. Pressing Build twice, or building after a half-failed
    run, must not create a second page called `blog` — so an object that already
    exists is adopted rather than remade, and the step says so.
    """
    from app.agents.appbuilder.tools._shared import get_saas_client

    client = get_saas_client()
    entry = await _plan_entry(job.app_code, step.uid, headers)
    spec = entry.get("spec") if isinstance(entry.get("spec"), dict) else {}

    existed = await _exists(client, step.kind, job.app_code, step.name, headers)
    if existed:
        step.detail = "already there, adopted"
    elif step.kind == "page":
        await _create_page(client, job.app_code, client_code, step, entry, headers)
    else:
        await _create_storage(client, job.app_code, client_code, step, entry, spec, headers)

    # The spec becomes the object's own plan. This is the whole point of the
    # spec: one shape, moved rather than rewritten, so nothing has to be kept
    # in step with anything.
    if spec:
        blueprint = {"schemaVersion": 1, "plan": spec}
        purpose = (entry.get("purpose") or "").strip()
        if purpose:
            blueprint["intent"] = purpose
        await objects.write_blueprint(
            step.kind, job.app_code, step.name, blueprint,
            headers, client_code, message="plan moved onto the object it describes",
        )

    await _mark_built(job.app_code, step.uid, headers, client_code)


async def _exists(
    client: Any, kind: str, app_code: str, name: str, headers: dict[str, str],
) -> bool:
    api = objects.resolve_kind(kind).api
    result = await client.get(
        api, headers=headers, params={"page": 0, "size": 5, "appCode": app_code, "name": name},
    )
    if not result.success or not isinstance(result.data, dict):
        return False
    return any(
        r.get("name") == name for r in (result.data.get("content") or [])
        if isinstance(r, dict)
    )


async def _create_page(
    client: Any, app_code: str, client_code: str, step: BuildStep,
    entry: dict[str, Any], headers: dict[str, str],
) -> None:
    """A page skeleton: a root Grid and nothing in it.

    Unpublished. `createUnpublished` is the platform's own answer to "exists but
    has never gone live" — a real row with a real id, so every id-addressed
    route keeps working, and nothing renders to the public until somebody
    publishes on purpose.
    """
    from app.agents.appbuilder.tools.modlix import _page_ops as p_ops

    body = p_ops.new_page_skeleton(
        step.name, app_code, client_code, title=entry.get("purpose") or step.name,
    )
    body["message"] = "created from the plan"
    result = await client.post(f"{p_ops.API_PREFIX}?draft=true", headers=headers, json=body)
    if not result.success:
        # The platform reads back through a security-access cache that a fresh
        # write can race, so a create that says "not found" may well have
        # written the row. Checking is cheaper than a wrong failure: one run
        # reported building nothing while every page existed.
        if await _exists(client, "page", app_code, step.name, headers):
            step.detail = "created"
            return
        raise BlueprintObjectError(f"Could not create the page '{step.name}': {result.error}")
    step.detail = "created"


#: How a planned field's type reaches a storage schema.
#:
#: A plan says "a date" because a person said "a date". A schema needs a KIRun
#: type, and guessing wrongly is worse than defaulting: a number stored as a
#: string sorts 10 before 9 and nobody sees it until a customer does.
_FIELD_TYPES: dict[str, str] = {
    "string": "STRING", "text": "STRING", "email": "STRING", "phone": "STRING",
    "number": "INTEGER", "integer": "INTEGER", "int": "INTEGER", "count": "INTEGER",
    "decimal": "DOUBLE", "double": "DOUBLE", "float": "DOUBLE", "price": "DOUBLE",
    "money": "DOUBLE", "amount": "DOUBLE",
    "boolean": "BOOLEAN", "bool": "BOOLEAN", "flag": "BOOLEAN",
    "date": "STRING", "datetime": "STRING", "timestamp": "STRING",
}


def schema_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """A storage schema from a plan's `fields` map.

    Inline rather than a referenced schema document. A storage can carry either
    (see `objects._inline_storage_schema`), and inline means one object to
    create instead of two, and one to delete if this was a mistake.
    """
    fields = spec.get("fields") if isinstance(spec.get("fields"), dict) else {}
    ordered = sorted(
        (e for e in fields.values() if isinstance(e, dict) and e.get("name")),
        key=lambda e: e.get("order") or 0,
    )
    properties: dict[str, Any] = {}
    required: list[str] = []
    for entry in ordered:
        name = str(entry["name"])
        stated = str(entry.get("type") or entry.get("kind") or "string").strip().lower()
        properties[name] = {"type": [_FIELD_TYPES.get(stated, "STRING")]}
        if entry.get("required"):
            required.append(name)
    schema: dict[str, Any] = {"type": ["OBJECT"], "properties": properties}
    if required:
        schema["required"] = required
    return schema


async def _create_storage(
    client: Any, app_code: str, client_code: str, step: BuildStep,
    entry: dict[str, Any], spec: dict[str, Any], headers: dict[str, str],
) -> None:
    schema = schema_from_spec(spec)
    if not schema.get("properties"):
        # A storage with no fields is a table with no columns. Refusing is
        # kinder than creating one somebody then has to find and delete.
        raise BlueprintObjectError(
            f"The plan for '{step.name}' names no fields, so there is nothing to create."
        )
    body = {
        "name": step.name,
        "appCode": app_code,
        "clientCode": client_code,
        "schema": schema,
        "isAudited": True,
        "isVersioned": False,
        "isAppLevel": False,
        "onlyThruKIRun": False,
        "message": "created from the plan",
    }
    purpose = (entry.get("purpose") or "").strip()
    if purpose:
        body["description"] = purpose
    result = await client.post(objects.resolve_kind("storage").api, headers=headers, json=body)
    if not result.success:
        if await _exists(client, "storage", app_code, step.name, headers):
            step.detail = "created"
            return
        raise BlueprintObjectError(
            f"Could not create the storage '{step.name}': {result.error}"
        )
    step.detail = f"{len(schema['properties'])} fields"


# ── The app plan's own bookkeeping ──────────────────────────────────────


async def _plan_entry(
    app_code: str, uid: str, headers: dict[str, str],
) -> dict[str, Any]:
    app = await objects.read_blueprint("application", app_code, "", headers)
    entries = (((app.get("blueprint") or {}).get("plan")) or {}).get("objects") or {}
    entry = entries.get(uid)
    return entry if isinstance(entry, dict) else {}


async def _mark_built(
    app_code: str, uid: str, headers: dict[str, str], client_code: str,
) -> None:
    """Flip one entry to built and drop its spec.

    The spec is REMOVED rather than left behind. It now lives on the object as
    that object's own plan, and two copies of a plan is two plans: the next edit
    lands on one of them and the other goes quietly stale, with nothing saying
    which is real.

    Re-read immediately before writing. Each object's make step writes the app
    document, so holding a copy from the start of the build would overwrite
    every entry flipped since.
    """
    app = await objects.read_blueprint("application", app_code, "", headers)
    blueprint = app.get("blueprint") or {}
    entries = ((blueprint.get("plan")) or {}).get("objects") or {}
    entry = entries.get(uid)
    if not isinstance(entry, dict):
        return
    entry["status"] = "built"
    entry.pop("spec", None)
    await objects.write_blueprint(
        "application", app_code, "", blueprint, headers, client_code,
        message="built from the plan",
    )


# ── Fill it in ──────────────────────────────────────────────────────────


#: What the builder is told, per page. Short on purpose: the plan is the brief,
#: and repeating it in prose here would give the model two briefs to reconcile.
_FILL_PROMPT = """Work on the page `{name}` in app `{app_code}`. Two lists \
follow and they are not the same job.
{to_build}{to_rework}
Rules for this task:
- Do ONLY what these two lists ask, in the order given. Touch no other page and
  no other object.
- For anything to ADD, name the section's top-level component EXACTLY the name
  given in brackets. That name is how the plan finds what you built; a
  different one leaves the card reading "not on the site yet" forever.
- For anything to CHANGE, edit the component that is already there, named in
  brackets. Do NOT add a second one beside it. The page already shows this
  section to real visitors; the plan for it has been rewritten and the built
  version has to catch up.
- Use the app's own theme variables for every colour and every font. Do not
  invent hex values.
- Write real copy from the brief, not placeholder text.

When you are done, stop. Do not publish anything."""

_TO_BUILD = """
ADD these sections to the page's root component. Nothing on the page answers to
them yet:

{sections}
"""

_TO_REWORK = """
CHANGE these sections. Each one already exists on the page under the component
name in brackets, and its plan has been rewritten since it was built. Bring the
built version in line with the brief — edit in place, do not duplicate:

{sections}
"""


async def _fill(
    job: BuildJob, step: BuildStep, headers: dict[str, str], auth: Any,
) -> None:
    """Author one page's pending sections, then record what was built.

    The authoring is the AppBuilder agent's job and is deliberately not
    reimplemented here: it already knows the component catalogue, the theme
    floor and the style gate, and a second, thinner authoring path would drift
    from all three.
    """
    try:
        document = await objects.read_object("page", job.app_code, step.name, headers)
    except BlueprintObjectError as exc:
        step.state, step.detail = SKIPPED, exc.message
        return

    blueprint = document.get("blueprint") or {}
    pending = pending_sections(blueprint, "page")
    if not pending:
        step.state, step.detail = SKIPPED, "nothing planned that is not built"
        return

    # Adding and changing are different instructions, and giving one for both
    # is how a reworked hero becomes two heroes. A pending entry that already
    # carries a `componentKey` exists on the page: its plan was rewritten after
    # it was built, so the job is to edit what is there, not to append a second.
    fresh = [(uid, e) for uid, e in pending if not e.get("componentKey")]
    rework = [(uid, e) for uid, e in pending if e.get("componentKey")]

    step.detail = f"{len(fresh)} to add, {len(rework)} to change"
    errors = await _run_builder(
        _FILL_PROMPT.format(
            name=step.name, app_code=job.app_code,
            to_build=_TO_BUILD.format(sections=_brief_list(fresh)) if fresh else "",
            to_rework=_TO_REWORK.format(sections=_brief_list(rework)) if rework else "",
        ),
        job.app_code, headers, auth,
    )

    # Matched, not written. Writing the plan here bumps the page's version and
    # makes the draft the builder just created unpublishable — see
    # `_reconcile_built`. The link is made when the work goes on the site.
    built, untouched = await _reconcile_built(
        job.app_code, step.name, headers, getattr(auth, "client_code", "") or "",
        write=False,
    )
    step.detail = f"{built} of {len(pending)} done"
    if untouched:
        # Named, because "3 of 4 done" does not say which one the visitor is
        # still looking at the old version of.
        step.state = FAILED
        step.detail += " — unchanged: " + ", ".join(untouched[:4])
        return
    if built == 0:
        step.state = FAILED
        # The builder's own words when it had any. "Nothing answers to the
        # planned sections" is the symptom; the reason is in the run, and
        # without it somebody reruns the same build and watches it fail the
        # same way.
        step.detail = errors[-1] if errors else (
            "the builder finished without putting anything on the page"
        )


def _brief_list(entries: list[tuple[str, dict[str, Any]]]) -> str:
    """One bulleted line per section, named by what the plan calls it."""
    return "\n".join(
        f"- [{(entry.get('name') or uid)}] {_section_brief(entry)}"
        for uid, entry in entries
    )


def _section_brief(entry: dict[str, Any]) -> str:
    """One line of brief per section: what it is for, and what goes in it."""
    parts = [
        (entry.get("purpose") or entry.get("describes") or "").strip(),
    ]
    content = entry.get("content")
    if isinstance(content, dict):
        for key in ("eyebrow", "heading", "body"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(f"{key}: {value.strip()}")
    variant = entry.get("variant")
    if isinstance(variant, str) and variant.strip():
        parts.append(f"arranged as {variant.strip()}")
    return " — ".join(p for p in parts if p) or "no brief was written for this one"


async def _run_builder(
    prompt: str, app_code: str, headers: dict[str, str], auth: Any,
) -> list[str]:
    """One headless AppBuilder run.

    Headless, so there is no SSE stream and no session for a person to attach
    to: the build's own progress is the thing being watched, and a second stream
    per page would be two progress reports disagreeing with each other.
    """
    from app.core.streaming import AgentEventStream

    agent = await _builder_agent()
    session = BaseSession(agent_name="appbuilder")
    session.context["app_code"] = app_code
    session.context["headers"] = headers
    # Nothing to confirm: a person pressed Build, which IS the confirmation, and
    # a headless run has nobody to ask. Without this every create pauses for an
    # answer that is never coming.
    session.context["auto_confirm"] = True
    # The REQUEST's own auth, never a fabricated one. Everything the builder
    # touches is written as the person who pressed Build, which is what keeps
    # the platform's access rules meaning something during a background job.
    await session.get_or_create(None, auth)

    # The stream is DRAINED, not discarded.
    #
    # `BaseAgent.run` catches everything and reports it as an event, so a run
    # that fails outright returns normally — and a caller that throws the events
    # away cannot tell "built nothing because it refused" from "built nothing
    # because it crashed on the first tool call". That is exactly what happened:
    # a fill step spent 228 seconds, reported success, and the page was still an
    # empty skeleton with nothing anywhere saying why.
    stream = AgentEventStream()
    errors: list[str] = []

    async def drain() -> None:
        async for event in stream.events():
            kind = getattr(event, "type", "") or getattr(event, "event", "")
            if "error" in str(kind).lower():
                text = getattr(event, "message", None) or getattr(event, "data", "")
                errors.append(str(text)[:400])

    drainer = asyncio.create_task(drain())
    try:
        await agent.run(prompt, session, stream)
    finally:
        # The run emits its own done sentinel; give the drainer a moment to see
        # it, then stop waiting. A stuck drainer must not hold up the build.
        try:
            await asyncio.wait_for(drainer, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            drainer.cancel()

    return errors


async def _builder_agent():
    """The AppBuilderAgent the service built at startup.

    Reused, never constructed here. It is made once in `main.py` with the
    component catalogue, the API catalogue and a loaded context — roughly 10K
    tokens of prefix rendered once and cached for the life of the process. A
    second instance re-downloads both catalogues and can then disagree with the
    first about which components exist, which is a difference that shows up as
    a page the agent swears it built correctly.

    Constructing one here was the first attempt and it failed on its first real
    run: `AppBuilderAgent.__init__` requires the context, so every fill step
    died with "missing 1 required positional argument". The build reported it
    per card and carried on, which is the failure model working — but the fill
    phase had never authored anything.
    """
    from app.agents.appbuilder.router import get_appbuilder_agent

    agent = get_appbuilder_agent()
    if agent is None:
        raise BlueprintObjectError(
            "The builder is not available in this process, so nothing can be "
            "authored. The objects themselves were still created.",
            status=503,
        )
    return agent


async def _reconcile_built(
    app_code: str, name: str, headers: dict[str, str], client_code: str,
    *, write: bool = True,
) -> tuple[int, list[str]]:
    """Match what is now on the page to the sections that asked for it.

    Matched on the component's NAME against the plan entry's name, which is why
    the prompt insists on the exact name. A position would not survive the next
    section being inserted above it, and a key cannot be agreed in advance
    because the platform mints it.

    Each match writes `componentKey` and stamps `reconciled` at the component's
    current version — it was just built from exactly this entry, so it agrees
    with it. Without the stamp every freshly built card would render as
    "edited on the site", which reads as an accusation about work nobody did.

    Returns (matched, untouched): how many sections were settled, and the names
    of any REWORK the builder was asked for and did not do. The second is not a
    detail. A rework that quietly re-stamps is worse than one that fails, since
    the card goes clean while the page still shows the old thing and nothing on
    screen ever says so again.

    `write=False` matches and reports WITHOUT touching the plan, and the fill
    step uses it. This is not an optimisation, it is the difference between a
    build you can publish and one you cannot:

    a blueprint write is a `PATCH /{id}/blueprint`, which moves the plan **and
    the document version**. The builder's draft carries the version it was taken
    from, and publish refuses a draft whose base has moved — "Please reload to
    get the new version before making changes". So stamping the plan at the end
    of a fill step silently made the work it had just verified unpublishable:

        draft baseVersion = 2   live version = 4   published = false

    The plan is therefore linked at PUBLISH time, once the components are on the
    site. That is also the more honest moment: a section existing only in an
    unpublished draft is not yet built, whatever the draft says.
    """
    # The DEFINITION comes from the DRAFT, because that is where the builder
    # writes. The PLAN comes from the live object, because that is where a plan
    # lives and a draft of the content is not a draft of the intent.
    built = await objects.read_object("page", app_code, name, headers, draft=True)
    document = await objects.read_object("page", app_code, name, headers)
    blueprint = document.get("blueprint") or {}
    sections = ((blueprint.get("plan")) or {}).get("sections") or {}
    definition = built.get("componentDefinition") or {}
    versions = built.get("componentVersions") or {}
    root = built.get("rootComponent")
    children = (definition.get(root) or {}).get("children") or {} if root else {}

    # Keyed by the component's KEY as well as its NAME, and the key is the one
    # that actually works.
    #
    # The prompt asks the builder to name each section exactly what the plan
    # calls it, and it very nearly does: asked for `header` it produced a
    # component KEYED `header` and NAMED `headerGrid`. Two of three sections
    # matched by luck and the third did not, so a page that was correctly and
    # completely built still reported a failure.
    #
    # Matching on both costs nothing and the key is the better identifier
    # anyway: it is what `componentKey` stores, and it is what the plan points
    # back at for the rest of the object's life.
    by_name: dict[str, str] = {}
    for key, on in children.items():
        if not on:
            continue
        by_name.setdefault(key.strip().lower(), key)
        component_name = ((definition.get(key) or {}).get("name") or "").strip()
        if component_name:
            by_name.setdefault(component_name.lower(), key)

    claimed = {
        e.get("componentKey") for e in sections.values()
        if isinstance(e, dict) and e.get("componentKey")
    }
    reconciled = blueprint.get("reconciled")
    if not isinstance(reconciled, dict):
        reconciled = {}
        blueprint["reconciled"] = reconciled
    # Two stamps, two different questions, and keeping them apart is what lets
    # the plan and the definition each move without being mistaken for the
    # other. `reconciled` holds the component version, so a later hand edit
    # reads as drift. `agreed` holds the plan's own fingerprint, so a later plan
    # change reads as pending.
    agreed = blueprint.get("agreed")
    if not isinstance(agreed, dict):
        agreed = {}
        blueprint["agreed"] = agreed

    matched = 0
    untouched: list[str] = []
    for uid, entry in sections.items():
        if not isinstance(entry, dict):
            continue
        # A section built before and re-planned since arrives here with a key
        # already on it. It still needs re-stamping, or it would stay pending
        # for ever and every build would do it again.
        already = entry.get("componentKey")
        if already:
            if not objects.plan_moved(blueprint, uid, entry):
                continue
            # A rework is only done when the COMPONENT moved. The plan changing
            # is what asked for the work; the component version changing is the
            # only evidence any was done. Re-stamping on the strength of having
            # asked would mark a section agreed that the builder never touched,
            # and the card would go quiet while the page still showed the old
            # thing — the worst of the possible outcomes, because nothing on
            # screen would ever say so again.
            current = versions.get(already)
            if current is None or current == reconciled.get(uid):
                untouched.append(str(entry.get("name") or already))
                continue
            reconciled[uid] = current
            agreed[uid] = objects.plan_fingerprint(entry)
            matched += 1
            continue
        wanted = (entry.get("name") or "").strip().lower()
        key = by_name.get(wanted)
        if not key or key in claimed:
            continue
        entry["componentKey"] = key
        claimed.add(key)
        if key in versions:
            reconciled[uid] = versions[key]
        agreed[uid] = objects.plan_fingerprint(entry)
        matched += 1

    if matched and write:
        await objects.write_blueprint(
            "page", app_code, name, blueprint, headers, client_code,
            message="plan linked to what was built",
        )
    return matched, untouched
