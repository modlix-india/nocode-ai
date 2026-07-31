"""Live-API SSE driver for the CFA chat endpoint.

Runs a multi-turn scenario YAML against the real /api/ai/appbuilder/chat,
streams SSE, writes a per-run JSONL transcript, and refreshes the JWT when
the SaaS-side signing key rotates (auto-relogin on 401).

Usage:
    python scripts/cfa_drive.py run scripts/cfa_scenarios/taskmate.yaml
    python scripts/cfa_drive.py all              # runs every yaml in scripts/cfa_scenarios/

JWT is read from ~/.cfa-jwt (chmod 600). Dev creds for the auto-refresh path
come from ~/.cfa-creds (json: {username, password, clientCode}). The CFA base
URL and SaaS gateway URL come from local defaults; override via
CFA_BASE_URL / SAAS_BASE_URL / CFA_FORWARDED_HOST / CFA_FORWARDED_PORT.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

CFA_BASE_URL = os.environ.get("CFA_BASE_URL", "http://localhost:5001")
SAAS_BASE_URL = os.environ.get("SAAS_BASE_URL", "http://localhost:8080")
JWT_PATH = Path(os.environ.get("CFA_JWT_PATH", os.path.expanduser("~/.cfa-jwt")))
CREDS_PATH = Path(os.environ.get("CFA_CREDS_PATH", os.path.expanduser("~/.cfa-creds")))
SCENARIO_DIR = Path(__file__).parent / "cfa_scenarios"
RUN_DIR = Path(__file__).parent / "cfa_runs"
FORWARDED_HOST = os.environ.get("CFA_FORWARDED_HOST", "localhost:8080")
FORWARDED_PORT = os.environ.get("CFA_FORWARDED_PORT", "8080")
# Local Docker container password shared by redis-cache-1, mysqldev8, and mongo.
# Not a secret — these containers only accept connections from localhost on a dev
# machine. Overridable via CFA_LOCAL_DOCKER_PASSWORD if you re-shape your stack.
_LOCAL_DB_PASSWORD = os.environ.get("CFA_LOCAL_DOCKER_PASSWORD", "Kiran@123")  # noqa: S105 - local docker only


# -------------------------- auth + headers --------------------------


def _read_jwt() -> str:
    if not JWT_PATH.exists():
        sys.exit(f"JWT not found at {JWT_PATH}. Save a token or set up {CREDS_PATH} for auto-login.")
    return JWT_PATH.read_text().strip()


def _read_creds() -> dict[str, str] | None:
    if not CREDS_PATH.exists():
        return None
    return json.loads(CREDS_PATH.read_text())


async def _login_fresh(creds: dict[str, str]) -> str:
    body = {
        "userName": creds["username"],
        "password": creds["password"],
        "identifierType": creds.get("identifierType", "EMAIL_ID"),
        "loggedInClientCode": creds.get("clientCode", "SYSTEM"),
    }
    headers = {
        "Content-Type": "application/json",
        "X-Forwarded-Host": FORWARDED_HOST,
        "X-Forwarded-Port": FORWARDED_PORT,
        "clientCode": creds.get("clientCode", "SYSTEM"),
        "appCode": creds.get("appCode", "appbuilder"),
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{SAAS_BASE_URL}/api/security/authenticate", json=body, headers=headers)
    r.raise_for_status()
    token = r.json()["accessToken"]
    JWT_PATH.write_text(token)
    JWT_PATH.chmod(0o600)
    return token


def _auth_headers(jwt: str, client_code: str, app_code_access: str = "appbuilder") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {jwt}",
        "clientCode": client_code,
        "appCode": app_code_access,
        "X-Forwarded-Host": FORWARDED_HOST,
        "X-Forwarded-Port": FORWARDED_PORT,
        "Content-Type": "application/json",
    }


# -------------------------- setup-actions runner --------------------------


async def _saas_delete_app_if_exists(
    client: httpx.AsyncClient, headers: dict[str, str], app_code: str
) -> dict[str, Any]:
    """Best-effort cleanup of both storage layers for an app.

    The UI doc id (Mongo ObjectId) is different from the security row id
    (numeric). DELETE on /api/ui/applications/<numeric_id> silently 404s, so we
    look up the Mongo id separately via the index endpoint. Both endpoints
    soft-delete (the security side flips status to ARCHIVED; the UI side drops
    the active version but leaves history). That's enough for re-creation to
    proceed.
    """
    out: dict[str, Any] = {"action": "delete_app", "app_code": app_code}
    sec_id = await _lookup_security_id(client, headers, app_code)
    ui_id = await _lookup_ui_doc_id(client, headers, app_code)
    if sec_id is None and ui_id is None:
        out["status"] = "not_found"
        return out
    out["security_app_id"] = sec_id
    out["ui_doc_id"] = ui_id
    if ui_id is not None:
        ui_del = await client.delete(f"{SAAS_BASE_URL}/api/ui/applications/{ui_id}", headers=headers)
        out["ui_status"] = ui_del.status_code
    if sec_id is not None:
        sec_del = await client.delete(
            f"{SAAS_BASE_URL}/api/security/applications/{sec_id}", headers=headers
        )
        out["security_status"] = sec_del.status_code
    out["status"] = "deleted"
    return out


async def _lookup_security_id(
    client: httpx.AsyncClient, headers: dict[str, str], app_code: str
) -> int | None:
    r = await client.get(
        f"{SAAS_BASE_URL}/api/security/applications",
        params={"appCode": app_code, "size": 5},
        headers=headers,
    )
    if r.status_code != 200:
        return None
    rows = (r.json() or {}).get("content") or []
    return rows[0].get("id") if rows else None


async def _lookup_ui_doc_id(
    client: httpx.AsyncClient, headers: dict[str, str], app_code: str
) -> str | None:
    r = await client.get(
        f"{SAAS_BASE_URL}/api/ui/applications",
        params={"appCode": app_code, "size": 5},
        headers=headers,
    )
    if r.status_code != 200:
        return None
    rows = (r.json() or {}).get("content") or []
    return rows[0].get("id") if rows else None


async def _mongo_purge_ui_artifacts(
    _client: httpx.AsyncClient, _headers: dict[str, str], app_code: str
) -> dict[str, Any]:
    """Drop ALL ui-collection docs that belong to a given app_code.

    Collections in the `ui` mongo DB that have an `appCode` field include:
    application, page, theme, function, schema, storage, style, uriPath, fillerValue,
    transport, role, customComponent, action, eventDefinition, eventAction. Wiping
    them gives a clean slate for re-runs. Idempotent — empty collections are no-ops.
    """
    out: dict[str, Any] = {"action": "mongo_purge", "app_code": app_code}
    js = (
        "['application','page','theme','function','schema','storage','style','uriPath',"
        "'fillerValue','transport','role','customComponent','action','eventDefinition',"
        "'eventAction'].forEach(c => db.getSiblingDB('ui').getCollection(c)"
        f".deleteMany({{appCode: '{app_code}'}}));"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "mongo", "mongosh",
            "-u", "root", "-p", _LOCAL_DB_PASSWORD,
            "--authenticationDatabase", "admin",
            "--quiet", "--eval", js,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=15.0)
        out["status"] = "ok" if proc.returncode == 0 else "mongo_error"
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        out["status"] = "skipped"
        out["detail"] = repr(e)
    return out


async def _mysql_purge_archived_security_app(
    _client: httpx.AsyncClient, _headers: dict[str, str], app_code: str
) -> dict[str, Any]:
    """Hard-delete ARCHIVED security_app rows for this appCode, including FK-cascaded children.

    The platform's UK1_APPCODE unique index covers appCode alone, so an ARCHIVED
    row blocks re-INSERT with the same appCode. The SaaS API doesn't expose
    "resurrect" or "hard-delete archived" — we go around it via direct MySQL.
    Idempotent (zero matches = zero rows deleted).

    Children deleted first to satisfy FK constraints:
      - security_app_property (REGISTRATION_TYPE etc.)
      - security_app_reg_user_profile / user_role / user_role_v2 / file_access /
        access / designation / user_designation / department / package / integration
        / integration_tokens / profile_restriction
      - security_app_access (per-client grants)
      - security_app_dependency
    """
    out: dict[str, Any] = {"action": "mysql_purge_archived", "app_code": app_code}
    # Tables collected via:
    #   SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE
    #   WHERE REFERENCED_TABLE_NAME='security_app' AND TABLE_SCHEMA='security';
    # Order matters: children that themselves have children (e.g. security_profile,
    # security_role) must be deleted only after THEIR children. The simplest safe
    # order: drop every FK child of security_app first.
    sql = f"""
        SET @app_ids = (SELECT GROUP_CONCAT(id) FROM security.security_app
                        WHERE APP_CODE='{app_code}' AND STATUS='ARCHIVED');
        DELETE FROM security.security_app_property WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_app_reg_user_profile WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_app_reg_user_role WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_app_reg_user_role_v2 WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_app_reg_file_access WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_app_reg_access WHERE FIND_IN_SET(APP_ID, @app_ids) OR FIND_IN_SET(ALLOW_APP_ID, @app_ids);
        DELETE FROM security.security_app_reg_designation WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_app_reg_user_designation WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_app_reg_department WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_app_reg_package WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_app_reg_integration_tokens WHERE INTEGRATION_ID IN (SELECT ID FROM security.security_app_reg_integration WHERE FIND_IN_SET(APP_ID, @app_ids));
        DELETE FROM security.security_app_reg_integration WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_app_reg_profile_restriction WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_app_access WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_app_dependency WHERE FIND_IN_SET(APP_ID, @app_ids) OR FIND_IN_SET(DEP_APP_ID, @app_ids);
        DELETE FROM security.security_client_otp_policy WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_client_password_policy WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_client_pin_policy WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_otp WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_package WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_permission WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_plan WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_profile WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_role WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_v2_role WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_user_request WHERE FIND_IN_SET(APP_ID, @app_ids);
        DELETE FROM security.security_client_url WHERE APP_CODE='{app_code}';
        DELETE FROM security.security_app WHERE FIND_IN_SET(id, @app_ids);
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", "mysqldev8",
            "mysql", "-uroot", f"-p{_LOCAL_DB_PASSWORD}", "-N", "-e", sql,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.wait(), timeout=15.0)
        out["status"] = "ok" if proc.returncode == 0 else "mysql_error"
        if proc.returncode != 0:
            _, stderr = await proc.communicate()
            out["detail"] = stderr.decode(errors="replace")[:300]
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        out["status"] = "skipped"
        out["detail"] = repr(e)
    return out


async def _redis_evict_app_caches(
    _client: httpx.AsyncClient, _headers: dict[str, str], app_code: str, client_code: str = "SYSTEM"
) -> dict[str, Any]:
    """Drop the SaaS commons cache entries that pin hasReadAccess(appCode, clientCode).

    Each SaaS service runs an in-JVM CaffeineCache fronting the shared Redis hash;
    HDEL alone leaves the JVM copies stale. CacheService.java listens on the
    `evictionChannel` pubsub topic for `cacheName:key` messages and evicts the
    JVM-local entry. We do BOTH: HDEL the Redis hash field, then PUBLISH the
    eviction message so every service's local Caffeine drops its copy.
    Skipped silently if Docker isn't available.
    """
    out: dict[str, Any] = {"action": "evict_cache", "app_code": app_code, "evicted": []}
    targets = [
        ("cmn-appReadAccess", f"{app_code}:{client_code}"),
        ("cmn-appWriteAccess", f"{app_code}:{client_code}"),
        ("cmn-byAppCode", app_code),
        ("cmn-byAppCodeExplicit", app_code),
        ("cmn-appDependencies", app_code),
    ]
    for hkey, field in targets:
        await _redis_one_evict(hkey, field, out)
    out["status"] = "ok"
    return out


async def _redis_one_evict(hkey: str, field: str, out: dict[str, Any]) -> None:
    """HDEL + pubsub PUBLISH for one (hash, field) cache entry."""
    try:
        del_proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "redis-cache-1", "redis-cli",
            "-a", _LOCAL_DB_PASSWORD, "HDEL", hkey, field,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(del_proc.wait(), timeout=5.0)
        pub_proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "redis-cache-1", "redis-cli",
            "-a", _LOCAL_DB_PASSWORD, "PUBLISH", "evictionChannel", f"{hkey}:{field}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(pub_proc.wait(), timeout=5.0)
        out["evicted"].append(f"{hkey}/{field}")
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        out["error"] = repr(e)


SETUP_RUNNERS = {
    "delete_app": lambda c, h, a: _saas_delete_app_if_exists(c, h, a["app_code"]),
    "evict_app_cache": lambda c, h, a: _redis_evict_app_caches(c, h, a["app_code"], a.get("client_code", "SYSTEM")),
    "mysql_purge_archived": lambda c, h, a: _mysql_purge_archived_security_app(c, h, a["app_code"]),
    "mongo_purge_ui": lambda c, h, a: _mongo_purge_ui_artifacts(c, h, a["app_code"]),
}


async def _run_setup_actions(
    actions: list[dict[str, Any]],
    jwt: str,
    client_code: str,
    log_path: Path,
) -> None:
    if not actions:
        return
    headers = _auth_headers(jwt, client_code)
    async with httpx.AsyncClient(timeout=30.0) as client:
        for action in actions:
            atype = action.get("type")
            runner = SETUP_RUNNERS.get(atype)
            if runner is None:
                _append_jsonl(log_path, {"event": "setup_skip", "reason": "unknown_type", "action": action})
                continue
            try:
                result = await runner(client, headers, action)
            except Exception as e:  # noqa: BLE001
                result = {"action": atype, "status": "error", "detail": repr(e)}
            _append_jsonl(log_path, {"event": "setup_action", "result": result})
            print(f"  setup {atype}: {result.get('status', '?')}")


# -------------------------- io helpers --------------------------


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# -------------------------- SSE plumbing --------------------------


@dataclass
class _TurnState:
    turn: int
    message: str
    log_path: Path
    session_id: str | None = None
    events: dict[str, int] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    started_at: str = field(default_factory=_utc_ts)
    elapsed_s: float | None = None


def _parse_sse_payload(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}


def _handle_event(state: _TurnState, name: str, payload: dict[str, Any]) -> None:
    state.events[name] = state.events.get(name, 0) + 1
    if name == "tool_start":
        state.tool_calls.append(
            {
                "name": payload.get("tool_name") or payload.get("tool"),
                "display_name": payload.get("display_name"),
                "id": payload.get("tool_use_id"),
                "args": payload.get("tool_input") or payload.get("input"),
            }
        )
    elif name == "tool_result":
        _record_tool_result(state.tool_calls, payload)
    elif name == "error":
        state.errors.append({"sse_error": payload})
    elif name == "done":
        state.session_id = payload.get("session_id") or state.session_id
        state.usage = payload.get("usage")
    _append_jsonl(state.log_path, {"event": name, "turn": state.turn, "payload": payload})


def _record_tool_result(tool_calls: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    for call in reversed(tool_calls):
        if call.get("id") == payload.get("tool_use_id"):
            call["success"] = payload.get("success")
            call["summary"] = (payload.get("summary") or "")[:300]
            call["error"] = payload.get("error")
            return


# -------------------------- chat turn --------------------------


@dataclass
class _ChatBody:
    message: str
    app_code: str
    session_id: str | None = None
    app_user: dict[str, Any] | None = None
    model: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"message": self.message, "app_code": self.app_code}
        if self.session_id:
            out["session_id"] = self.session_id
        if self.app_user:
            out["app_user"] = self.app_user
        if self.model:
            out["model"] = self.model
        return out


async def _stream_chat_into_state(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    body: _ChatBody,
    state: _TurnState,
) -> int:
    """POST chat, stream SSE into state. Returns HTTP status code."""
    async with client.stream(
        "POST",
        f"{CFA_BASE_URL}/api/ai/appbuilder/chat",
        headers=headers,
        json=body.to_json(),
        timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0),
    ) as resp:
        if resp.status_code != 200:
            err = (await resp.aread()).decode(errors="replace")[:500]
            state.errors.append({"http": resp.status_code, "body": err})
            _append_jsonl(state.log_path, {"event": "http_error", "turn": state.turn, "status": resp.status_code, "body": err})
            return resp.status_code

        async for name, payload in _aiter_sse(resp):
            _handle_event(state, name, payload)
        return 200


async def _aiter_sse(resp: httpx.Response):
    """Yield (event_name, json_payload) tuples from an httpx streaming response."""
    cur_event: str | None = None
    cur_data: list[str] = []
    async for raw in resp.aiter_lines():
        if raw == "":
            if cur_event:
                yield cur_event, _parse_sse_payload("\n".join(cur_data))
            cur_event = None
            cur_data = []
            continue
        if raw.startswith(":"):
            continue
        if raw.startswith("event:"):
            cur_event = raw[6:].strip()
        elif raw.startswith("data:"):
            cur_data.append(raw[5:].strip())
    if cur_event:
        yield cur_event, _parse_sse_payload("\n".join(cur_data))


async def _run_turn(
    client: httpx.AsyncClient,
    *,
    body: _ChatBody,
    headers: dict[str, str],
    log_path: Path,
    turn_index: int,
) -> _TurnState:
    state = _TurnState(turn=turn_index, message=body.message, log_path=log_path, session_id=body.session_id)
    _append_jsonl(
        log_path,
        {"event": "turn_start", "turn": turn_index, "message": body.message, "session_id": body.session_id},
    )
    started = time.monotonic()
    try:
        await _stream_chat_into_state(client, headers, body, state)
    except httpx.HTTPError as e:
        state.errors.append({"network": repr(e)})
        _append_jsonl(log_path, {"event": "network_error", "turn": turn_index, "detail": repr(e)})
    state.elapsed_s = round(time.monotonic() - started, 2)
    _append_jsonl(log_path, {"event": "turn_end", "turn": turn_index, "summary": _state_slim(state)})
    return state


def _state_slim(state: _TurnState) -> dict[str, Any]:
    return {
        "turn": state.turn,
        "session_id": state.session_id,
        "elapsed_s": state.elapsed_s,
        "events": state.events,
        "tool_call_count": len(state.tool_calls),
        "error_count": len(state.errors),
    }


# -------------------------- scenario orchestration --------------------------


@dataclass
class _Scenario:
    name: str
    client_code: str
    app_code: str
    turns: list[str]
    app_user: dict[str, Any] | None
    setup_actions: list[dict[str, Any]]
    model: str | None
    spec: dict[str, Any]


def _load_scenario(path: Path) -> _Scenario:
    spec = yaml.safe_load(path.read_text())
    turns = spec.get("turns") or []
    if not turns:
        raise ValueError(f"{path} has no turns")
    return _Scenario(
        name=spec.get("name") or path.stem,
        client_code=spec.get("client_code", "SYSTEM"),
        app_code=spec["app_code"],
        turns=turns,
        app_user=spec.get("app_user"),
        setup_actions=spec.get("setup_actions") or [],
        model=spec.get("model"),
        spec=spec,
    )


def _is_auth_expired_state(state: _TurnState) -> bool:
    """True if this turn's errors look like an expired-JWT 401."""
    for err in state.errors:
        if not isinstance(err, dict):
            continue
        if err.get("http") == 401:
            return True
        sse = err.get("sse_error")
        if isinstance(sse, dict) and "401" in str(sse.get("message", "")):
            return True
    return False


async def _ensure_jwt(refresh: bool) -> str:
    creds = _read_creds()
    if refresh and creds:
        return await _login_fresh(creds)
    jwt = _read_jwt() if JWT_PATH.exists() else None
    if jwt:
        return jwt
    if creds:
        return await _login_fresh(creds)
    sys.exit("No JWT and no creds — save a token at ~/.cfa-jwt or creds at ~/.cfa-creds.")


def _scenario_run_dir(name: str) -> tuple[Path, Path]:
    run_root = RUN_DIR / name / _utc_ts()
    run_root.mkdir(parents=True, exist_ok=True)
    return run_root, run_root / "turns.jsonl"


def _update_latest_link(scenario_name: str, run_root: Path) -> None:
    latest_link = RUN_DIR / scenario_name / "latest"
    if latest_link.exists() or latest_link.is_symlink():
        latest_link.unlink()
    latest_link.symlink_to(run_root.name)


def _print_turn_summary(state: _TurnState) -> None:
    ev = state.events or {}
    print(
        f"    -> tool_calls={len(state.tool_calls)}  "
        f"text={ev.get('text', 0)}  errors={len(state.errors)}  {state.elapsed_s}s"
    )
    for e in state.errors[:3]:
        print(f"      ! {json.dumps(e)[:200]}")


async def _execute_scenario(scenario: _Scenario, jwt: str) -> dict[str, Any]:
    run_root, log_path = _scenario_run_dir(scenario.name)
    print(f"\n=== scenario {scenario.name} :: app_code={scenario.app_code} :: {len(scenario.turns)} turns ===")
    print(f"    run dir: {run_root}")

    await _run_setup_actions(scenario.setup_actions, jwt, scenario.client_code, log_path)

    headers = _auth_headers(jwt, scenario.client_code)
    session_id: str | None = None
    turn_states: list[_TurnState] = []

    async with httpx.AsyncClient() as client:
        for i, message in enumerate(scenario.turns, start=1):
            print(f"  turn {i}/{len(scenario.turns)}: {message[:80]!r}")
            body = _ChatBody(
                message=message,
                app_code=scenario.app_code,
                session_id=session_id,
                app_user=scenario.app_user,
                model=scenario.model,
            )
            state = await _run_turn(client, body=body, headers=headers, log_path=log_path, turn_index=i)
            # JWT auto-refresh on mid-scenario 401. Long scenarios (>30 min) outlive
            # the 30-min token; without this every turn after the expiry returns 401
            # and the build silently halts. We try once: refresh, swap headers, replay
            # the turn. Persistent 401 (bad creds) still falls through as an error.
            if _is_auth_expired_state(state):
                creds = _read_creds()
                if creds:
                    print("    JWT expired — refreshing and retrying turn")
                    jwt = await _login_fresh(creds)
                    headers = _auth_headers(jwt, scenario.client_code)
                    state = await _run_turn(client, body=body, headers=headers, log_path=log_path, turn_index=i)
            turn_states.append(state)
            session_id = state.session_id or session_id
            _print_turn_summary(state)

    overall = _build_overall(scenario, session_id, turn_states)
    (run_root / "summary.json").write_text(json.dumps(overall, indent=2))
    _update_latest_link(scenario.name, run_root)
    print(
        f"  done. session_id={session_id}  "
        f"total_tool_calls={overall['total_tool_calls']}  total_errors={overall['total_errors']}"
    )
    return overall


def _build_overall(
    scenario: _Scenario, session_id: str | None, states: list[_TurnState]
) -> dict[str, Any]:
    return {
        "scenario": scenario.name,
        "app_code": scenario.app_code,
        "session_id": session_id,
        "turns": [_state_slim(s) for s in states],
        "total_tool_calls": sum(len(s.tool_calls) for s in states),
        "total_errors": sum(len(s.errors) for s in states),
        "started_at": states[0].started_at if states else _utc_ts(),
        "finished_at": _utc_ts(),
    }


def _collect_scenarios(arg: str) -> list[Path]:
    if arg == "all":
        return sorted(p for p in SCENARIO_DIR.glob("*.yaml") if not p.name.startswith("_"))
    p = Path(arg)
    if not p.is_absolute():
        candidate = SCENARIO_DIR / arg
        if candidate.exists():
            p = candidate
        elif (SCENARIO_DIR / f"{arg}.yaml").exists():
            p = SCENARIO_DIR / f"{arg}.yaml"
    if not p.exists():
        sys.exit(f"scenario not found: {arg}")
    return [p]


async def _amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "all"])
    parser.add_argument("target", nargs="?", default="all")
    parser.add_argument("--refresh", action="store_true", help="Force-refresh JWT via ~/.cfa-creds before each scenario")
    args = parser.parse_args(argv)

    scenarios = _collect_scenarios(args.target if args.command == "run" else "all")
    overall_ok = True

    for s in scenarios:
        jwt = await _ensure_jwt(refresh=args.refresh)
        try:
            scenario = _load_scenario(s)
            result = await _execute_scenario(scenario, jwt)
            if result.get("total_errors", 0) > 0:
                overall_ok = False
        except Exception as e:  # noqa: BLE001
            print(f"  scenario {s.name} crashed: {e!r}")
            overall_ok = False
    return 0 if overall_ok else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
