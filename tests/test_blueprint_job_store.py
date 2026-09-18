"""Finding a running job after a refresh, and from another worker.

Two ways progress went missing, and only one of them is about the browser.

**A page refresh.** The job id lived only in the page store, written from the
POST that started it. Refreshing cleared it, so the board had no id, no stream,
and no way to discover either — while the job ran on happily with nobody
watching.

**A second worker.** Production runs gunicorn with four uvicorn workers and each
registry is a module-level dict, so a job lives on exactly one worker and a
later request lands wherever the balancer puts it. A "what is running?" route
over the dict alone answers correctly about one time in four, which is worse
than having no route: it tells three people in four that nothing is running
while their build runs.

Redis is optional throughout. With it off — which is the single-process local
case — everything degrades to the local registry, and that is correct rather
than a fallback.
"""

from __future__ import annotations

import asyncio
import json

from app.services.blueprint import job_store


class _FakeRedis:
    """Just enough Redis to exercise the store, with TTLs recorded."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.values[key] = value
        if ex:
            self.ttls[key] = ex

    async def get(self, key):
        return self.values.get(key)


class _FakeJob:
    def __init__(self, job_id="j1", app_code="crumbco", state="running") -> None:
        self.id = job_id
        self.app_code = app_code
        self.state = state
        self.done = 0

    def progress(self):
        return {
            "job": self.id, "appCode": self.app_code, "kind": "build",
            "state": self.state, "done": self.done, "total": 4,
        }


def _use(monkeypatch, client):
    async def fake():
        return client
    monkeypatch.setattr(job_store, "_redis", fake)
    return client


def test_a_published_job_is_findable_by_app_alone(monkeypatch):
    # What a refreshed board has: the app code and nothing else.
    redis = _use(monkeypatch, _FakeRedis())
    asyncio.run(job_store.publish(_FakeJob()))

    found = asyncio.run(job_store.running_for("crumbco"))
    assert found["job"] == "j1"
    assert found["kind"] == "build"
    # Both keys carry a TTL, so a job nobody cleaned up stops being offered as
    # running rather than haunting the board for ever.
    assert set(redis.ttls.values()) == {job_store.TTL_SECONDS}


def test_a_finished_job_is_not_offered_as_running(monkeypatch):
    """The pointer outlives the job, so the payload is what decides.

    Otherwise a board opened an hour after a build would re-attach to it and
    show a finished run as though it had just started.
    """
    _use(monkeypatch, _FakeRedis())
    job = _FakeJob()
    asyncio.run(job_store.publish(job))
    job.state = "done"
    asyncio.run(job_store.publish(job))

    assert asyncio.run(job_store.running_for("crumbco")) is None
    # Still readable by id, because "how did it end" is a real question.
    assert asyncio.run(job_store.read("j1"))["state"] == "done"


def test_everything_is_quiet_when_redis_is_off(monkeypatch):
    # The local single-process case. Not a degraded mode: with one worker the
    # registry dict is the whole truth and publishing is pure overhead.
    async def none():
        return None
    monkeypatch.setattr(job_store, "_redis", none)

    asyncio.run(job_store.publish(_FakeJob()))  # must not raise
    assert asyncio.run(job_store.read("j1")) is None
    assert asyncio.run(job_store.running_for("crumbco")) is None


def test_a_broken_redis_never_reaches_the_job(monkeypatch):
    class Broken:
        async def set(self, *a, **k):
            raise RuntimeError("redis is down")

        async def get(self, *a, **k):
            raise RuntimeError("redis is down")

    _use(monkeypatch, Broken())
    # Observation must never be able to kill the work it is observing.
    asyncio.run(job_store.publish(_FakeJob()))
    assert asyncio.run(job_store.read("j1")) is None
    assert asyncio.run(job_store.running_for("crumbco")) is None


def test_a_remote_job_looks_like_a_local_one(monkeypatch):
    """`_progress_stream` must not know the difference.

    One loop serves both, because a second copy of it would be a second place
    for the keep-alive and the terminal condition to be wrong.
    """
    _use(monkeypatch, _FakeRedis())
    job = _FakeJob()
    asyncio.run(job_store.publish(job))

    remote = job_store.RemoteJob(asyncio.run(job_store.read("j1")))
    assert remote.state == "running"
    assert remote.progress()["done"] == 0

    # The owning worker moves on; the remote view follows on refresh.
    job.done = 3
    asyncio.run(job_store.publish(job))
    assert asyncio.run(remote.refresh()) is True
    assert remote.progress()["done"] == 3


def test_a_job_whose_worker_died_ends_rather_than_repeating(monkeypatch):
    """A progress bar that stops moving and never resolves is the one thing it
    must never do."""
    redis = _use(monkeypatch, _FakeRedis())
    asyncio.run(job_store.publish(_FakeJob()))
    remote = job_store.RemoteJob(asyncio.run(job_store.read("j1")))

    redis.values.clear()  # the key expired, or the worker went away mid-write
    assert asyncio.run(remote.refresh()) is False
    assert remote.state == "failed"
    # And it says which of the two it is, rather than a bare failure.
    assert "restarted" in remote.progress()["error"]


def test_the_watcher_stops_when_the_job_does(monkeypatch):
    redis = _use(monkeypatch, _FakeRedis())
    job = _FakeJob()

    async def run():
        task = job_store.watch(job)
        await asyncio.sleep(0)          # let it publish once
        job.state = "done"
        await asyncio.wait_for(task, timeout=5)

    monkeypatch.setattr(job_store, "PUBLISH_EVERY", 0.01)
    asyncio.run(run())

    # The final state was published, so nothing is left claiming to be running.
    assert json.loads(redis.values[job_store._KEY.format(job="j1")])["state"] == "done"
