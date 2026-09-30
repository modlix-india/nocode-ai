"""Making a running job findable from any worker, and after a refresh.

── The two ways progress went missing ───────────────────────────────────

**A page refresh.** The board's job id lived only in the page store, written
from the POST that started the job. Refreshing cleared it, so the board had no
id, no stream, and no way to discover either — while the job carried on running
perfectly well on the server with nobody watching.

**A second worker.** Production runs gunicorn with four uvicorn workers (see the
Dockerfile), and each job registry is a module-level dict. The job therefore
lives on exactly ONE worker, and a later request lands wherever the load
balancer puts it. A "what is running?" route over the dict alone would answer
correctly about one time in four, which is worse than not having it: it would
tell three people in four that nothing was running while their build ran.

`core/stream_registry.py` solved the same problem for agent runs by republishing
through Redis, and this is the same shape — including the part that matters
most, which is that **Redis is optional**. `get_redis_client()` returns None
when it is disabled or unreachable, and everything here degrades to the local
registry. That is not a fallback for production, it is the correct behaviour for
a single-process run, which is what local is.

── What it deliberately does not do ─────────────────────────────────────

It does not move the WORK. The job still runs on the worker that started it, as
an asyncio task in that process; only its PROGRESS is shared. Moving the work
would mean a queue and a worker pool, which is a different and much larger
thing, and nothing here needs it: the work is already durable in the only sense
that matters, because every object is written the moment it is finished.

It does not survive a restart of the worker running the job. Redis keeps the
last progress that was published, so the board can say what happened and how far
it got — but the task is gone, and a job that stops mid-run is reported as
stopped rather than left saying "running" for ever.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How long a published job stays readable. Comfortably past the longest build
#: and short enough that a dead job stops being offered as "running" for ever.
TTL_SECONDS = 60 * 60

#: How often a running job republishes itself.
#:
#: One write per second per running job, and there is at most one job per app.
#: Publishing on every state change instead would mean threading a call through
#: every place a step's state moves — and the one that got forgotten would be
#: the one that mattered, because it would be a job that looked stalled.
PUBLISH_EVERY = 1.0

_KEY = "ai:blueprint:job:{job}"
_APP_KEY = "ai:blueprint:app:{app}"


async def _redis():
    try:
        from app.services.redis_client import get_redis_client

        return await get_redis_client()
    except Exception:  # noqa: BLE001 — a missing cache must never fail a job
        logger.debug("blueprint jobs: no redis", exc_info=True)
        return None


async def publish(job: Any) -> None:
    """Write one job's current progress where any worker can read it."""
    client = await _redis()
    if client is None:
        return
    try:
        payload = job.progress()
        encoded = json.dumps(payload)
        await client.set(_KEY.format(job=job.id), encoded, ex=TTL_SECONDS)
        # The app pointer is what makes "is anything running for this site?"
        # answerable without knowing a job id — which is exactly the position a
        # freshly refreshed page is in.
        await client.set(_APP_KEY.format(app=job.app_code), job.id, ex=TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.debug("blueprint jobs: could not publish %s", job.id, exc_info=True)


async def read(job_id: str) -> dict[str, Any] | None:
    """The last published progress for one job, from any worker."""
    client = await _redis()
    if client is None or not job_id:
        return None
    try:
        raw = await client.get(_KEY.format(job=job_id))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return json.loads(raw if isinstance(raw, str) else raw.decode())
    except (ValueError, AttributeError):
        return None


async def running_for(app_code: str) -> dict[str, Any] | None:
    """The job this app has in flight, as last published. None if there is none.

    A job that finished is NOT returned: the pointer is left in place until it
    expires, so this reads the payload and checks its state rather than trusting
    the pointer's existence. Otherwise a board opened an hour after a build
    would re-attach to it and show a finished run as though it had just started.
    """
    client = await _redis()
    if client is None or not app_code:
        return None
    try:
        job_id = await client.get(_APP_KEY.format(app=app_code))
    except Exception:  # noqa: BLE001
        return None
    if not job_id:
        return None
    payload = await read(job_id if isinstance(job_id, str) else job_id.decode())
    if not payload or payload.get("state") != "running":
        return None
    return payload


def watch(job: Any) -> asyncio.Task | None:
    """Republish this job until it stops. Returns the task, or None with no Redis.

    Started beside the job rather than inside it, so a job's own code says
    nothing about how it is observed and a job that is never watched behaves
    identically.
    """

    async def loop() -> None:
        try:
            while True:
                await publish(job)
                if job.state != "running":
                    return
                await asyncio.sleep(PUBLISH_EVERY)
        except asyncio.CancelledError:
            # One last write on the way out, so a cancelled job does not leave
            # its final state unpublished and look like it is still running.
            await publish(job)
            raise
        except Exception:  # noqa: BLE001 — observation must not kill the job
            logger.debug("blueprint jobs: watcher stopped for %s", job.id, exc_info=True)

    return asyncio.create_task(loop())


class RemoteJob:
    """A job held by another worker, seen through what it published.

    Exposes the two things `_progress_stream` needs — `progress()` and `state` —
    so one streaming loop serves a local job and a remote one without knowing
    the difference. The alternative was a second copy of that loop for the
    remote case, which is a second place for the keep-alive and the terminal
    condition to be wrong.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.id = str(payload.get("job") or "")
        self.app_code = str(payload.get("appCode") or "")

    @property
    def state(self) -> str:
        return str(self._payload.get("state") or "running")

    def progress(self) -> dict[str, Any]:
        return self._payload

    async def refresh(self) -> bool:
        """Re-read what the owning worker last published. False when it is gone.

        A job whose key has expired or whose worker died mid-write stops being
        readable, and that has to end the stream rather than repeat the last
        frame for ever: a progress bar that stops moving and never resolves is
        the one thing it must not do.
        """
        payload = await read(self.id)
        if payload is None:
            self._payload = {
                **self._payload,
                "state": "failed",
                "error": (
                    "Lost track of this job — the service restarted while it was "
                    "running. Anything it finished is saved on the objects "
                    "themselves."
                ),
            }
            return False
        self._payload = payload
        return True
