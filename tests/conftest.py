"""Shared pytest fixtures.

Two mock surfaces cover the bulk of the test suite:

  - ``MockSaasClient`` — drop-in replacement for
    ``app.core.tools.http_client.SaasClient``. Records every call (method,
    path, headers, params, json body) and returns a programmed ``ToolResult``.
    The ``get_saas_client`` shared helper is monkey-patched so any ported
    modlix tool transparently picks up the mock.

  - ``mock_execute_query`` — monkey-patches
    ``app.db.connection.execute_query`` (used by ``app/services/app_kb.py``)
    with a recording stub. Lets KB-lifecycle tests cover propose-then-commit
    without a live MySQL.

Both fixtures are function-scoped so each test starts with a fresh call
log and fresh response queue.

A small ``call_log`` helper namespace is exposed so tests assert against
captured calls in a readable way (e.g. ``calls.last().path``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from app.core.tools.base import ToolResult


# ── Recorded call shape ──────────────────────────────────────────────────


@dataclass
class RecordedCall:
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] | None = None
    json: Any = None


@dataclass
class CallLog:
    calls: list[RecordedCall] = field(default_factory=list)

    def record(self, call: RecordedCall) -> None:
        self.calls.append(call)

    def __len__(self) -> int:
        return len(self.calls)

    def __iter__(self):
        return iter(self.calls)

    def last(self) -> RecordedCall:
        assert self.calls, "no calls recorded yet"
        return self.calls[-1]

    def by_method(self, method: str) -> list[RecordedCall]:
        return [c for c in self.calls if c.method == method.upper()]

    def by_path(self, substring: str) -> list[RecordedCall]:
        return [c for c in self.calls if substring in c.path]


# ── MockSaasClient ───────────────────────────────────────────────────────


class MockSaasClient:
    """Drop-in replacement for SaasClient with a programmable response queue.

    Programming responses:
      - ``client.set_default(ToolResult(success=True, data={...}))`` — return
        the same response for every call. Default: ``ToolResult(success=True,
        data={"content": [], "totalElements": 0})``.
      - ``client.enqueue(ToolResult(...))`` — queue a per-call response.
        Consumed in FIFO order. Falls back to the default once exhausted.
      - ``client.respond_to(method, path_substring, ToolResult(...))`` —
        route-style: return the given result the first time a call matches.

    Tests then assert against ``client.calls`` (a ``CallLog``).
    """

    def __init__(self) -> None:
        self.calls: CallLog = CallLog()
        self._queue: list[ToolResult] = []
        self._default: ToolResult = ToolResult(
            success=True,
            data={"content": [], "totalElements": 0},
            summary="(mock default)",
        )
        self._routes: list[tuple[str, str, ToolResult]] = []

    def set_default(self, result: ToolResult) -> None:
        self._default = result

    def enqueue(self, *results: ToolResult) -> None:
        self._queue.extend(results)

    def respond_to(self, method: str, path_substring: str, result: ToolResult) -> None:
        self._routes.append((method.upper(), path_substring, result))

    def _resolve(self, method: str, path: str) -> ToolResult:
        # Routes win first.
        for i, (m, frag, res) in enumerate(self._routes):
            if m == method and frag in path:
                self._routes.pop(i)
                return res
        if self._queue:
            return self._queue.pop(0)
        return self._default

    async def _record(
        self, method: str, path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> ToolResult:
        self.calls.record(RecordedCall(
            method=method, path=path,
            headers=dict(headers or {}),
            params=dict(params) if params is not None else None,
            json=json,
        ))
        return self._resolve(method, path)

    async def get(self, path, headers=None, params=None) -> ToolResult:
        return await self._record("GET", path, headers, params, None)

    async def post(self, path, headers=None, json=None, params=None) -> ToolResult:
        return await self._record("POST", path, headers, params, json)

    async def put(self, path, headers=None, json=None, params=None) -> ToolResult:
        return await self._record("PUT", path, headers, params, json)

    async def patch(self, path, headers=None, json=None, params=None) -> ToolResult:
        return await self._record("PATCH", path, headers, params, json)

    async def delete(self, path, headers=None, params=None) -> ToolResult:
        return await self._record("DELETE", path, headers, params, None)

    async def close(self) -> None:
        return None


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def mock_client(monkeypatch) -> MockSaasClient:
    """Replace SaasClient anywhere ported modlix tools resolve it.

    The ported tools all go through ``_shared.get_saas_client()`` (or a
    module-local ``_client_and_headers`` helper that calls it). Monkey-patching
    that one resolver flips every tool to the mock.
    """
    client = MockSaasClient()

    def _resolver():
        return client

    monkeypatch.setattr(
        "app.agents.appbuilder.tools._shared.get_saas_client", _resolver,
    )
    # Some modlix modules import get_saas_client lazily inside their
    # _client_and_headers helper — the monkeypatch on the source module is
    # what they'll resolve. No per-module patching needed.
    return client


@pytest.fixture
def tool_context() -> dict[str, Any]:
    """A canonical tool-call context.

    Tools read app_code / client_code / headers from this. Tests that need a
    different shape construct one inline; this is the default.
    """
    return {
        "app_code": "testapp",
        "client_code": "SYSTEM",
        "headers": {
            "Authorization": "Bearer test-token",
            "appCode": "testapp",
            "clientCode": "SYSTEM",
        },
        "get_app_user_token": lambda: "app-user-token-stub",
    }


# ── KB / DB layer mock ───────────────────────────────────────────────────


class MockExecuteQuery:
    """Records ``execute_query`` calls and serves canned rows.

    Programming:
      - ``mock.enqueue_rows([{...}, {...}])`` — next SELECT-style call returns
        these rows. INSERT/UPDATE calls return an empty list by default.
      - ``mock.set_default([])`` — what to return when the queue is empty.

    Captured calls are in ``mock.calls`` (list of ``(query, args)``).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._row_queue: list[list[dict[str, Any]]] = []
        self._default: list[dict[str, Any]] = []
        self._handler: Callable[[str, tuple], list[dict[str, Any]]] | None = None

    def enqueue_rows(self, rows: list[dict[str, Any]]) -> None:
        self._row_queue.append(rows)

    def set_default(self, rows: list[dict[str, Any]]) -> None:
        self._default = rows

    def set_handler(self, handler: Callable[[str, tuple], list[dict[str, Any]]]) -> None:
        """For tests that need query-aware behaviour (e.g. compute next version
        from a running counter). Handler signature: (query, args) → rows."""
        self._handler = handler

    async def __call__(self, query: str, params: tuple | None = None) -> Any:
        # execute_query(query, params) — params is a flat tuple of bind values.
        # We store it flat so tests can assert against args[0], args[1]... directly
        # against the params tuple (not against a nested 1-tuple).
        recorded = params if params is not None else ()
        self.calls.append((query, recorded))
        if self._handler is not None:
            return self._handler(query, recorded)
        if self._row_queue:
            return self._row_queue.pop(0)
        return list(self._default)


@pytest.fixture
def mock_execute_query(monkeypatch) -> MockExecuteQuery:
    """Replace ``app.db.connection.execute_query`` with a recording stub.

    Patches at the source module AND at the import site in
    ``app.services.app_kb`` so the KB service picks up the mock regardless of
    how it imported the symbol.
    """
    mock = MockExecuteQuery()
    monkeypatch.setattr("app.db.connection.execute_query", mock)
    monkeypatch.setattr("app.services.app_kb.execute_query", mock, raising=False)
    return mock
