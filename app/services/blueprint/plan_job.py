"""Planning a whole app, as a job you can watch.

── Why this is a job and not a request ──────────────────────────────────

`POST /generate` writes ONE object's plan. Pointed at an application that is
the app-level plan: what the site is for, which objects exist, which features
group them. It says nothing about what is inside any of them, so a board built
from it renders every card reading "nothing describes this yet". That is the
whole of the "shallow" complaint, and it is not a prompt problem: no single
call was ever going to describe forty pages well, and one that tried would be
a forty-page context answered in one shot, wrong in ways nobody reads closely
enough to catch.

So the work is a sweep: the app plan first, then one derivation per object,
each reading only its own definition. That is N model calls, and N model calls
do not fit in a request — the gateway gives up at sixty seconds, and even a
small site is past that. The job therefore outlives its request, and the
caller watches it.

── What watching buys, beyond not timing out ────────────────────────────

The step list is built BEFORE any model call, from the object lists, so the
first poll already answers "what is being processed" with real names rather
than a spinner. Every object is written the moment its own derivation lands,
so a sweep that dies halfway has still improved the plan by half, and a failed
object is one object: it is marked failed with its reason and the sweep
carries on, which is the same promise the build screen makes about cards.

── What this deliberately does not have ─────────────────────────────────

No queue, no persistence, no worker. The registry is a dict in this process,
so a restart loses the PROGRESS of a running sweep — never the work, which is
already on the objects. A durable job store is worth building when a second
process exists to read it; today it would be ceremony around a dict.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.services.blueprint import objects, relations, service
from app.services.blueprint.compose import (
    access_facts,
    apply_describes,
    brand_facts,
    build_app_context,
    index_access,
    index_assets,
    index_brand,
    index_objects,
    seed_entries,
    set_uses,
)
from app.services.blueprint.relations import Edge, Known
from app.services.blueprint import job_store
from app.services.blueprint.objects import BlueprintObjectError
from app.services.blueprint.service import BlueprintGenerationError
from app.services.billing import OUT_OF_TOKENS, CallMeter

logger = logging.getLogger(__name__)

#: Kinds swept after the app plan, in board order.
#:
#: Everything an app is made of, because a plan that covers the pages and the
#: storages and nothing else is not a plan of the app: it is a plan of the two
#: parts that are easiest to picture. The functions are where the work happens,
#: the uripaths are the addresses other systems call, the templates are what a
#: customer actually receives — and those are precisely the parts nobody can
#: reconstruct by looking at the site.
#:
#: A kind with no objects contributes no steps, so this costs nothing on an app
#: that has none. `kinds` on the request still overrides it, which is what a
#: caller drawing fewer bands should use.
SWEPT_KINDS: tuple[str, ...] = objects.BOARD_KINDS

#: Finished jobs kept for this long so a poll that arrives after the last step
#: still sees how it ended rather than "no such job".
KEEP_FINISHED_SECONDS = 15 * 60

#: Hard ceiling on a single sweep, so a provider that hangs cannot pin a task
#: for the life of the process.
JOB_TIMEOUT_SECONDS = 30 * 60

WAITING, WORKING, DONE, FAILED = "waiting", "working", "done", "failed"


@dataclass
class Step:
    """One unit of work, named before it runs."""

    kind: str
    name: str
    label: str
    state: str = WAITING
    detail: str = ""
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "label": self.label,
            "state": self.state,
            "detail": self.detail,
            "seconds": round(self.seconds, 1),
        }


@dataclass
class PlanJob:
    id: str
    app_code: str
    prompt: str
    state: str = "running"
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    steps: list[Step] = field(default_factory=list)
    task: asyncio.Task | None = None
    #: (kind, name, summary, pending, parts) per object read, for the index.
    seen: list[tuple[str, str, str, int, int]] = field(default_factory=list)
    #: Every storage the app has, known before the first page is read, so a
    #: page's plan can record which of them its own logic names.
    storage_names: list[str] = field(default_factory=list)
    #: Every object name the app has, by kind, listed before the sweep starts.
    #: A reference only becomes an edge when it resolves to something in here,
    #: which is what keeps a derived graph from containing guesses.
    known: Known = field(default_factory=lambda: Known({}))
    #: Every edge found while reading the objects, accumulated so the app plan
    #: can be written with the whole graph at the end. Incoming edges cannot be
    #: computed by an object about itself, so they can only be written here.
    edges: list[Edge] = field(default_factory=list)
    #: Gates and charges every model call this sweep makes.
    #:
    #: A sweep talks to the provider directly rather than through the agent
    #: loop, so nothing else in the path meters it — and for a while nothing
    #: did: forty calls against a suspended wallet, billed for none, while the
    #: chat on the same screen correctly refused to answer.
    meter: CallMeter | None = None

    def progress(self) -> dict[str, Any]:
        """What a poll answers. Flat, and a whole answer every time.

        The step list goes out in full rather than as a delta: it is at most a
        few dozen short rows, and a client that has to accumulate deltas gets
        it wrong the first time it misses a poll.
        """
        done = sum(1 for s in self.steps if s.state in (DONE, FAILED))
        working = next((s for s in self.steps if s.state == WORKING), None)
        failed = [s for s in self.steps if s.state == FAILED]
        return {
            "job": self.id,
            "appCode": self.app_code,
            "state": self.state,
            "done": done,
            "total": len(self.steps),
            "failed": len(failed),
            "current": working.as_dict() if working else None,
            "steps": [s.as_dict() for s in self.steps],
            "error": self.error,
            "seconds": round((self.finished_at or time.time()) - self.started_at, 1),
            # What it cost, so a sweep is never a silent charge. Weighted
            # tokens, the same unit the wallet is debited in.
            "calls": self.meter.calls if self.meter else 0,
            "tokens": round(self.meter.tokens) if self.meter else 0,
        }


_JOBS: dict[str, PlanJob] = {}


def get(job_id: str) -> PlanJob | None:
    _sweep_old()
    return _JOBS.get(job_id)


def running_for(app_code: str) -> PlanJob | None:
    """The sweep already under way for this app, if there is one.

    Pressing Regenerate twice is one sweep, not two. Two would race each other
    on the same objects and bill for it.
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


async def start(
    *,
    app_code: str,
    prompt: str,
    headers: dict[str, str],
    client_code: str,
    auth: Any = None,
    seed_app_plan: bool = True,
    kinds: tuple[str, ...] | None = None,
) -> PlanJob:
    """Name every piece of work, then run it in the background.

    The listing happens HERE, awaited, rather than inside the task: the caller
    gets back a job whose steps are already real, so the first thing on screen
    is the list of what is about to be read. It also means a site that cannot
    be listed fails the request instead of failing invisibly a second later.
    """
    steps: list[Step] = []
    if seed_app_plan:
        steps.append(Step(kind="application", name="", label="the site as a whole"))

    # Names first, ALL of them, before any object is read. A reference found in
    # a page can only become an edge if it resolves to a real object, and the
    # page being read second must be able to resolve a name belonging to a kind
    # that is swept ninth. Listing as we go would mean the first half of the
    # sweep silently found fewer edges than the second.
    names_by_kind: dict[str, list[str]] = {}
    for kind in objects.BOARD_KINDS:
        try:
            rows = await objects.list_objects(kind, app_code, headers)
        except BlueprintObjectError as exc:
            # A kind that cannot be listed is not a reason to abandon the rest.
            logger.warning("blueprint sweep: cannot list %s in %s: %s", kind, app_code, exc)
            continue
        names_by_kind[kind] = [row["name"] for row in rows]
        if kind not in (kinds or SWEPT_KINDS):
            continue
        for row in rows:
            steps.append(
                Step(kind=kind, name=row["name"], label=row.get("title") or row["name"])
            )

    # Last, and only when there is something to index. Each object's summary is
    # written into the app plan in ONE write at the end rather than a write per
    # object: they all land on the same document, and forty PATCHes to one
    # document is forty chances for two of them to race.
    if len(steps) > (1 if seed_app_plan else 0):
        steps.append(Step(kind="index", name="", label="the list of what it is made of"))

    job = PlanJob(
        id=uuid.uuid4().hex, app_code=app_code, prompt=prompt, steps=steps,
        storage_names=list(names_by_kind.get("storage") or ()),
        known=Known(names_by_kind),
    )
    # The job id as the billing session, because it is the thing a person can
    # point at afterwards and say "that is the sweep I ran".
    if auth is not None:
        job.meter = CallMeter(auth, session_id=f"blueprint-plan:{job.id}")
        # Asked here, before the task is created, so an empty wallet refuses the
        # REQUEST. A job that starts and then fails on its first step reads as a
        # broken feature; a refused request reads as what it is, and the refusal
        # is the same sentence the chat uses so the two halves of the screen
        # agree about whether there is any money.
        if not await job.meter.allowed():
            raise BlueprintGenerationError(OUT_OF_TOKENS, reason="out-of-tokens")
    _JOBS[job.id] = job
    job.task = asyncio.create_task(_run(job, headers, client_code))
    # Published beside the job so a board that refreshed, or a request that
    # landed on another of the four workers, can still find it. A no-op when
    # Redis is off, which is the single-process local case.
    job_store.watch(job)
    return job


async def _run(job: PlanJob, headers: dict[str, str], client_code: str) -> None:
    try:
        await asyncio.wait_for(_sweep(job, headers, client_code), timeout=JOB_TIMEOUT_SECONDS)
        job.state = "done"
    except asyncio.TimeoutError:
        job.state = "failed"
        job.error = "The sweep ran past its time limit and was stopped."
        for step in job.steps:
            if step.state == WORKING:
                step.state, step.detail = FAILED, "stopped when the sweep timed out"
    except asyncio.CancelledError:
        job.state = "failed"
        job.error = "Stopped."
        raise
    except Exception as exc:  # noqa: BLE001 - a job must record why it died
        logger.exception("blueprint sweep failed for %s", job.app_code)
        job.state = "failed"
        job.error = str(exc)
    finally:
        job.finished_at = time.time()


async def _sweep(job: PlanJob, headers: dict[str, str], client_code: str) -> None:
    """One step at a time, on purpose.

    Sequential because the point of this job is that somebody is watching it:
    "reading Home" is a true statement about one thing, where four at once is a
    list of four half-finished names. It is also gentler on the provider, and
    the sweep is measured in minutes either way.
    """
    for step in job.steps:
        step.state = WORKING
        began = time.time()
        try:
            if step.kind == "application":
                await _plan_the_app(job, headers, client_code)
            elif step.kind == "index":
                await _write_the_index(job, step, headers, client_code)
            else:
                await _describe_one(job, step, headers, client_code)
            step.state = DONE
        except (BlueprintGenerationError, BlueprintObjectError) as exc:
            step.state = FAILED
            step.detail = getattr(exc, "message", str(exc))
        except Exception as exc:  # noqa: BLE001 - one object, not the sweep
            logger.exception("blueprint sweep step failed: %s %s", step.kind, step.name)
            step.state = FAILED
            step.detail = str(exc)
        finally:
            step.seconds = time.time() - began


async def _plan_the_app(job: PlanJob, headers: dict[str, str], client_code: str) -> None:
    """The app-level plan: what it is for, which objects, which features."""
    existing: dict[str, Any] | None = None
    try:
        current = await objects.read_blueprint("application", job.app_code, "", headers)
        existing = current.get("blueprint") or None
    except BlueprintObjectError:
        existing = None

    context = await build_app_context(job.app_code, headers)
    result = await service.generate(
        prompt=job.prompt, kind="application", app_code=job.app_code,
        context=context, existing=existing, meter=job.meter,
    )
    if not result.get("valid"):
        raise BlueprintGenerationError(
            "; ".join(result.get("issues") or ["the plan came back unusable"])
        )
    await objects.write_blueprint(
        "application", job.app_code, "", result["blueprint"],
        headers, client_code, message="plan generated",
    )


def _counts(blueprint: dict[str, Any], kind: str) -> tuple[int, int]:
    """(pending, parts) for one object's plan, as the app index records them.

    In one place because the sweep reaches the index down three paths — a
    described object, one with nothing new to say, and one whose description
    failed — and two of those reported zero parts for a page whose plan held
    three. That is the exact failure the count exists to prevent: a board
    reading "0" under a page with three sections in its plan, which is a
    confident wrong answer where a blank would at least have been honest.
    """
    from app.services.blueprint.build_job import pending_sections
    from app.services.blueprint.compose import collection_for

    collection, _ = collection_for(kind)
    return (
        len(pending_sections(blueprint, kind)),
        len(((blueprint or {}).get("plan") or {}).get(collection) or {}),
    )



async def _describe_one(
    job: PlanJob, step: Step, headers: dict[str, str], client_code: str,
) -> None:
    """One object: read what it connects to, then derive a line per part.

    `seed` is on because this is the case it exists for. A site that has never
    been planned has no entries for a description to land on, so deriving
    without seeding would spend the tokens and write nothing.

    THE CONNECTIONS ARE TAKEN FIRST AND KEPT WHATEVER HAPPENS NEXT. They are
    read off the definition, so they cost nothing and cannot fail the way a
    model call can — and a sweep whose provider is down, out of budget or simply
    wrong should still leave the app knowing what reaches what. Letting a failed
    description discard them would throw away the free half of the work because
    the paid half broke.
    """
    document = await objects.read_object(step.kind, job.app_code, step.name, headers)

    # Before the model call, not after. What a page touches is the single most
    # useful thing to know about it, and a describer that has not been told
    # writes "a form with four fields" about a form whose entire purpose is the
    # storage it fills. Derived, so it costs nothing and cannot be wrong in the
    # way a guess can.
    found = relations.edges_of(document, step.kind, job.known)
    job.edges.extend(found)

    try:
        result = await service.describe(
            document=document, kind=step.kind, app_code=job.app_code,
            connections=relations.summarise(found, step.kind, step.name),
            meter=job.meter,
        )
    except BlueprintGenerationError as exc:
        if not found:
            raise
        # The description failed and the connections did not. Write them, then
        # let the step report the failure honestly: half the work landed, and
        # saying "failed" with nothing written would be the wrong half of true.
        blueprint = set_uses(
            document.get("blueprint") or {},
            relations.uses_of(found, step.kind, step.name),
        )
        await objects.write_blueprint(
            step.kind, job.app_code, step.name, blueprint,
            headers, client_code, message="connections derived from the definition",
        )
        job.seen.append((step.kind, step.name, "", *_counts(blueprint, step.kind)))
        raise BlueprintGenerationError(
            f"{exc.message} ({len(found)} connections were still recorded)",
            reason=exc.reason,
        ) from exc

    describes: dict[str, str] = result.get("describes") or {}
    if not describes and not found:
        # Recorded even so: an object with no parts worth describing is still an
        # object the app is made of, and leaving it out of the index would say
        # the site does not have it.
        job.seen.append((
            step.kind, step.name, result.get("summary") or "",
            *_counts(document.get("blueprint") or {}, step.kind),
        ))
        step.detail = "nothing to describe"
        return

    blueprint = document.get("blueprint") or {}
    if describes:
        blueprint = seed_entries(
            blueprint, document, step.kind, describes, result.get("names") or {},
        )
        blueprint = apply_describes(blueprint, describes, step.kind)
    # What this object reaches, for every kind rather than only storages. An
    # object's own plan carries its outgoing edges so an agent working on one
    # page does not have to read the whole app plan to learn what it touches.
    blueprint = set_uses(blueprint, relations.uses_of(found, step.kind, step.name))

    # Counted here, where the object's plan is already in hand, and carried to
    # the index so a board can see outstanding work without opening anything.
    from app.services.blueprint.build_job import pending_sections

    job.seen.append((
        step.kind, step.name, result.get("summary") or "",
        *_counts(blueprint, step.kind),
    ))
    await objects.write_blueprint(
        step.kind, job.app_code, step.name, blueprint,
        headers, client_code, message="plan derived from the definition",
    )
    step.detail = f"{len(describes)} described"
    if found:
        step.detail += f", {len(found)} connections"


async def _write_the_index(
    job: PlanJob, step: Step, headers: dict[str, str], client_code: str,
) -> None:
    """One write, at the end: what the app is made of, with a line each.

    The app plan's `objects` map is the INDEX the board reads. Without this it
    holds whatever the first model call guessed the app contains, which is a
    plausible list rather than the real one — and every column's second line
    stays blank until somebody opens it, because an object's own plan is only
    reachable through its full document.

    It re-reads the app blueprint rather than reusing what `_plan_the_app`
    wrote, because the objects were described after that and this has to land on
    top of it, not beside it.
    """
    if not job.seen:
        step.detail = "nothing to index"
        return

    # The application's own references — its shell, its landing page, its theme.
    # Read here because the application document was never one of the swept
    # steps, so nothing else in the sweep ever opens it.
    try:
        app_document = await objects.read_object("application", job.app_code, "", headers)
        job.edges.extend(relations.edges_of(app_document, "application", job.known))
    except BlueprintObjectError as exc:
        logger.info("blueprint sweep: no application references for %s: %s", job.app_code, exc)

    current = await objects.read_blueprint("application", job.app_code, "", headers)
    blueprint = current.get("blueprint") or {}
    blueprint = index_objects(blueprint, job.seen)
    # The graph, on the application and nowhere else. `usedBy` is the half that
    # answers the questions worth asking and no object can compute its own
    # incoming edges — only something holding every document at once can.
    blueprint = relations.index_relations(blueprint, job.edges)
    # The look and the icon, neither of which is in any object's definition or
    # in any list: the typeface is a variable on the theme and the icon is a
    # `<link>` on the application's own properties, so nothing else in the sweep
    # would ever have seen either one.
    brand = await brand_facts(job.app_code, headers)
    blueprint = index_brand(blueprint, brand)
    blueprint = index_assets(blueprint, brand)
    # Who the app is for and what it talks to. Neither is an object, so nothing
    # the sweep read would ever have mentioned either.
    blueprint = index_access(blueprint, await access_facts(job.app_code, headers))
    await objects.write_blueprint(
        "application", job.app_code, "", blueprint,
        headers, client_code, message="what the app is made of",
    )
    step.detail = f"{len(job.seen)} objects listed"
    if job.edges:
        step.detail += f", {len(relations.dedup(job.edges))} connections mapped"
