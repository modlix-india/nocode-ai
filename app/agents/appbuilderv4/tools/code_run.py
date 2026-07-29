"""`code_run` — the v4 agent's single write primitive.

Spawns a Python subprocess that imports the `modlix` SDK and executes the
script the agent supplies. Stdout, stderr, and the return code are
captured and returned to the agent in one ToolResult.

Why a subprocess (not in-process exec):
- Hard isolation from the agent's memory.
- Wall-clock timeout via `subprocess.run(timeout=...)` is reliable.
- Easy to swap for a containerised sandbox (E2B / Modal) later — the
  contract stays the same: stdin=script, env=auth, stdout=output.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult


# Cap on captured output sent back to the LLM. The full output is on disk
# (in the temp dir) but we truncate the returned summary so a runaway
# `print` loop doesn't blow the agent's context.
_MAX_STDOUT_CHARS = 6000
_MAX_STDERR_CHARS = 4000
_DEFAULT_TIMEOUT_S = 60
_ABSOLUTE_MAX_TIMEOUT_S = 180


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n... [truncated, {len(s) - limit} more chars]"


def _build_env(context: dict[str, Any]) -> dict[str, str]:
    """Compose the env vars the SDK reads. Everything auth/session-related
    flows through here so the sandbox is fully decoupled from the parent."""
    from app.config import settings
    base = os.environ.copy()
    headers = context.get("headers") or {}
    base["MODLIX_GATEWAY_URL"] = (getattr(settings, "GATEWAY_URL", "") or "").rstrip("/")
    base["MODLIX_AUTH_TOKEN"] = headers.get("Authorization", "").removeprefix("Bearer ").strip()
    base["MODLIX_APP_CODE"] = str(context.get("app_code") or "")
    base["MODLIX_CLIENT_CODE"] = str(context.get("client_code") or "")
    base["MODLIX_FORWARDED_HOST"] = headers.get("X-Forwarded-Host", "localhost:8080")
    base["MODLIX_FORWARDED_PORT"] = headers.get("X-Forwarded-Port", "8080")
    base["MODLIX_CATALOG_URL"] = getattr(settings, "COMPONENT_CATALOG_URL", "") or ""
    # PYTHONPATH so `app.agents.appbuilderv4.sdk` resolves inside the subprocess.
    repo_root = str(Path(__file__).resolve().parents[3])
    existing = base.get("PYTHONPATH", "")
    base["PYTHONPATH"] = f"{repo_root}{os.pathsep}{existing}" if existing else repo_root
    # Force unbuffered output so we see prints in real time.
    base["PYTHONUNBUFFERED"] = "1"
    return base


async def _execute_code_run(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    script = params.get("script")
    if not isinstance(script, str) or not script.strip():
        return ToolResult(success=False, error="`script` is required (a Python source string)")
    timeout = int(params.get("timeout_seconds") or _DEFAULT_TIMEOUT_S)
    timeout = max(1, min(timeout, _ABSOLUTE_MAX_TIMEOUT_S))

    env = _build_env(context)
    if not env["MODLIX_AUTH_TOKEN"]:
        return ToolResult(success=False, error="No Authorization token in context; code_run cannot run.")
    if not env["MODLIX_GATEWAY_URL"]:
        return ToolResult(success=False, error="GATEWAY_URL not configured; code_run cannot run.")

    # Write the script to a temp file so tracebacks point at a real path.
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, prefix="cfa_v4_") as fh:
        fh.write(script)
        script_path = fh.name

    python = sys.executable  # same interpreter as the agent process
    cmd = [python, "-m", "app.agents.appbuilderv4.sdk._runner", script_path]

    started = time.monotonic()
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd,
            env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - started
        return ToolResult(
            success=False,
            error=(
                f"code_run timed out after {elapsed:.1f}s (cap {timeout}s). "
                "The subprocess was killed. Partial stdout/stderr:\n"
                f"--- stdout ---\n{_truncate(e.stdout or '', _MAX_STDOUT_CHARS)}\n"
                f"--- stderr ---\n{_truncate(e.stderr or '', _MAX_STDERR_CHARS)}"
            ),
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"subprocess launch failed: {type(e).__name__}: {e}")
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass

    elapsed = time.monotonic() - started
    stdout = _truncate(proc.stdout or "", _MAX_STDOUT_CHARS)
    stderr = _truncate(proc.stderr or "", _MAX_STDERR_CHARS)
    success = (proc.returncode == 0)

    parts = [
        f"code_run finished in {elapsed:.2f}s, exit={proc.returncode}.",
    ]
    if stdout.strip():
        parts.append(f"--- stdout ---\n{stdout}")
    if stderr.strip():
        parts.append(f"--- stderr ---\n{stderr}")
    if not stdout.strip() and not stderr.strip():
        parts.append("(no output)")

    return ToolResult(
        success=success,
        summary="\n".join(parts),
        data={
            "exit_code": proc.returncode,
            "elapsed_seconds": round(elapsed, 3),
            "stdout": stdout,
            "stderr": stderr,
        },
        error=("" if success else f"script exited with code {proc.returncode}; see stderr"),
    )


code_run_tool = ToolDefinition(
    name="code_run",
    display_name="Run Python",
    description="""Execute a Python script in an isolated subprocess. The script can `import modlix` to access auth-bound HTTP helpers, the component catalog, and page/app CRUD wrappers.

This is the PRIMARY write primitive of appbuilder v4. Use it to:
- Discover the surface: `modlix.catalog.list_types()`, `modlix.catalog.get_schema(name)`, `modlix.apps.list()`, `modlix.pages.list(app_code=...)`.
- Read existing definitions: `modlix.pages.get('homeTwo', app_code='someApp')` returns the FULL page JSON including componentDefinition + properties; use this as a template to learn the canonical shape.
- Write atomically: `modlix.pages.replace('home', new_definition, app_code='clonelinear')` posts an entire page in one call. No chatty add_component loop.
- Compose page-definition dicts in Python (loops, conditionals, generated styleProperties UUIDs) and PUT them back.

Anti-patterns:
- Calling code_run for trivial queries you could batch with other work into ONE script.
- Inventing the page-definition shape from scratch. Always fetch an existing page first to learn the wrap conventions (`{value: 'x'}` for property literals, UUID-keyed styleProperties, etc.).
- Hard-coding gateway URLs or JWTs in the script — `modlix.config` already has them.

Script gets up to `timeout_seconds` (default 60, max 180). The subprocess is killed on timeout; partial stdout/stderr is returned.""",
    parameters=[
        ToolParameter(
            name="script", type="string", required=True,
            description="Python source. The first line should usually be `import modlix`. Tracebacks point at this script's temp path.",
        ),
        ToolParameter(
            name="timeout_seconds", type="integer", required=False, default=60,
            description="Subprocess kill deadline (1-180 seconds).",
        ),
    ],
    execute=_execute_code_run,
)
