"""Infra tools — environment introspection, cache eviction, log tailing.

Ported from modlix-mcp's environment.py, cache.py, logs.py. The bigger
infra category (apps, themes, styles, uri_paths, notifications, connections,
templates, events, personalization, html_compiler_tools) will land in
follow-up sessions; this module ships the operationally-critical subset that
the agent needs to confirm "where am I and is the gateway live" before
destructive operations.

Tools exposed:
  - which_environment       — env name, gateway URL, default tenant context
  - list_caches             — list cache names currently held in the UI service
  - clear_cache             — evict caches (one or all) across every service
                              via the Redis pub/sub eviction broadcast
  - list_log_services       — names of <service>.log files in the configured
                              LOG_DIR (dev-only — log files aren't shipped to
                              CFA hosts in prod)
  - tail_service_logs       — tail last N lines of <service>.log, optionally
                              filtered by exceptionId or substring
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────


def _client_and_headers(context: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    """Resolve the per-session SaasClient + request headers."""
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context.get("headers") or {}


def _resolve_log_dir() -> Path | None:
    """LOG_DIR resolution: explicit setting → sibling nocode-saas/logs → None.

    In CFA prod the log files don't exist on the agent host — this tool is
    primarily a dev convenience. Returns None when no directory is configured
    or found; callers surface a clear error to the user.
    """
    from app.config import settings
    log_dir = getattr(settings, "MODLIX_LOG_DIR", "") or ""
    if log_dir:
        p = Path(log_dir).expanduser()
        return p if p.exists() else None
    # Fallback: ../nocode-saas/logs relative to the nocode-ai install root.
    install_root = Path(__file__).resolve().parents[4]
    sibling = install_root.parent / "nocode-saas" / "logs"
    return sibling if sibling.exists() else None


def _read_tail(path: Path, line_count: int) -> list[str]:
    """Return up to `line_count` trailing lines without loading the whole file."""
    if line_count <= 0:
        return []
    chunk = 64 * 1024
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        data = b""
        pos = size
        while pos > 0 and data.count(b"\n") <= line_count:
            read = min(chunk, pos)
            pos -= read
            f.seek(pos)
            data = f.read(read) + data
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-line_count:]


# ── which_environment ────────────────────────────────────────────────────


async def _execute_which_environment(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    from app.config import settings

    headers = context.get("headers") or {}
    lines = [
        f"Environment: {getattr(settings, 'ENV_NAME', '') or '(unset)'}",
        f"  gatewayUrl:        {settings.GATEWAY_URL}",
        f"  callerClientCode:  {headers.get('clientCode') or '(unknown)'}",
        f"  accessAppCode:     {headers.get('appCode') or '(unknown)'}",
        f"  forwardedHost:     {headers.get('X-Forwarded-Host') or '(unset)'}",
        f"  targetAppCode:     {context.get('app_code') or '(unset; pass in ChatRequest)'}",
    ]
    # Catalog status: useful diagnostic when components throw "unknown property"
    try:
        from app.agents.appbuilder.catalog import get_catalog
        cat = get_catalog()
        comp_count = len(cat.get_all_types())
        lines.append(f"  componentCatalog:  {comp_count} types loaded")
    except (ImportError, AttributeError):
        lines.append("  componentCatalog:  (not yet loaded)")

    return ToolResult(success=True, summary="\n".join(lines))


which_environment_tool = ToolDefinition(
    name="which_environment",
    description=(
        "Report which environment the CFA is on plus the caller's tenant "
        "context (clientCode, appCode, gateway URL, target app). Use this "
        "before destructive operations to confirm you're hitting the right "
        "platform. Read-only."
    ),
    parameters=[],
    execute=_execute_which_environment,
)


# ── clear_cache + list_caches ────────────────────────────────────────────
#
# Why the UI's `/api/ui/internal/cache` endpoint specifically: the platform's
# `CacheService` extends `RedisPubSubAdapter`, so a DELETE on ANY one service's
# /internal/cache endpoint broadcasts the eviction over Redis pub/sub and every
# subscribing service drops the matching keys. One call covers ui+core+security.
#
# In modlix-mcp these tools had to bypass nginx via MODLIX_INTERNAL_BASE_URL
# because /internal/* is 403'd on the public host. In the CFA, the gateway IS
# behind the platform's reverse proxy too, but the CFA runs INSIDE the platform
# network and hits the gateway port directly, so the path Just Works through
# settings.GATEWAY_URL. The 403 ladder only applies if we end up routing
# through the public host — surface that case clearly in error text.

_CACHE_PATH = "/api/ui/internal/cache"


async def _execute_clear_cache(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    client, headers = _client_and_headers(context)
    cache_name = params.get("cache_name") or ""
    path = f"{_CACHE_PATH}/{cache_name}" if cache_name else _CACHE_PATH
    r = await client.delete(path, headers=headers)
    if not r.success:
        err = r.error or ""
        if "403" in err:
            return ToolResult(
                success=False,
                error=(
                    f"{err}. /internal/cache is blocked at the public host (nginx); "
                    "the CFA must reach the gateway port directly. Confirm "
                    "settings.GATEWAY_URL points at the internal gateway, not the "
                    "public domain."
                ),
            )
        return ToolResult(success=False, error=err)
    what = cache_name or "all caches (broadcast)"
    return ToolResult(success=True, summary=f"Evicted {what}.")


clear_cache_tool = ToolDefinition(
    name="clear_cache",
    description=(
        "Evict server-side caches across every Modlix service. Eviction "
        "propagates via Redis pub/sub so this single call clears ui+core+"
        "security in one shot. Useful after a raw mongo write or when the "
        "agent suspects stale-after-edit symptoms. Pass cache_name to evict "
        "a specific cache (e.g. 'PageCache_appbuilder_homeTwo'); omit to "
        "evict all caches across all services."
    ),
    parameters=[
        ToolParameter(
            name="cache_name", type="string", required=False,
            description=(
                "Optional specific cache name (e.g. "
                "'PageCache_appbuilder_homeTwo', "
                "'ApplicationCache_leadzump'). Omit to evict all caches."
            ),
        ),
    ],
    execute=_execute_clear_cache,
)


async def _execute_list_caches(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    client, headers = _client_and_headers(context)
    r = await client.get(_CACHE_PATH, headers=headers)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    names = r.data if isinstance(r.data, list) else []
    if not names:
        return ToolResult(success=True, summary="(no caches loaded)")
    body = f"{len(names)} caches:\n  " + "\n  ".join(sorted(map(str, names)))
    return ToolResult(success=True, summary=body)


list_caches_tool = ToolDefinition(
    name="list_caches",
    description=(
        "List cache names the UI service currently holds. Names look like "
        "`PageCache_<app>_<page>`, `ApplicationCache_<app>`, "
        "`UIFunctionCache_<app>_<fn>`, etc. Pass one as the `cache_name` "
        "argument to `clear_cache` for targeted eviction. Reports only the "
        "UI service's local cache manager; eviction broadcasts cross-service, "
        "so you rarely need to inspect the others."
    ),
    parameters=[],
    execute=_execute_list_caches,
)


# ── list_log_services + tail_service_logs ─────────────────────────────────


async def _execute_list_log_services(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    log_dir = _resolve_log_dir()
    if log_dir is None:
        return ToolResult(
            success=False,
            error=(
                "Log directory not found. Set MODLIX_LOG_DIR or run from a "
                "checkout where ../nocode-saas/logs exists. Logs aren't "
                "available on CFA prod hosts — this tool is a dev convenience."
            ),
        )
    rows: list[str] = []
    for p in sorted(log_dir.glob("*.log")):
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        rows.append(f"  {p.stem:24s}  {size:>12} bytes")
    if not rows:
        return ToolResult(success=True, summary=f"(no *.log files in {log_dir})")
    return ToolResult(success=True, summary=f"Services in {log_dir}:\n" + "\n".join(rows))


list_log_services_tool = ToolDefinition(
    name="list_log_services",
    description=(
        "List the <service>.log files available for tailing. Dev-only — log "
        "files don't ship to CFA prod hosts. Pair with `tail_service_logs` to "
        "read recent lines from a named service when triaging an exceptionId."
    ),
    parameters=[],
    execute=_execute_list_log_services,
)


async def _execute_tail_service_logs(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    service = params.get("service") or ""
    if not service:
        return ToolResult(success=False, error="service is required (e.g. 'core', 'ui', 'security')")

    log_dir = _resolve_log_dir()
    if log_dir is None:
        return ToolResult(
            success=False,
            error=(
                "Log directory not found. Set MODLIX_LOG_DIR or use "
                "list_log_services to discover the configured path."
            ),
        )
    path = log_dir / f"{service}.log"
    if not path.exists():
        return ToolResult(success=False, error=f"{path} does not exist")

    lines_arg = params.get("lines") or 200
    try:
        lines = max(1, min(int(lines_arg), 5000))
    except (TypeError, ValueError):
        lines = 200

    raw = _read_tail(path, lines)

    exc_id = (params.get("exception_id") or "").strip()
    search = (params.get("search") or "").strip()
    if exc_id:
        raw = [line for line in raw if exc_id in line]
    if search:
        raw = [line for line in raw if search.lower() in line.lower()]

    if not raw:
        return ToolResult(success=True, summary=f"(no matching lines in {path})")
    body = f"{path} (last {len(raw)} lines):\n" + "\n".join(raw)
    return ToolResult(success=True, summary=body)


tail_service_logs_tool = ToolDefinition(
    name="tail_service_logs",
    description=(
        "Tail the last N lines of a <service>.log file. Optional filters: "
        "exception_id (exact substring match) and search (case-insensitive "
        "substring). Used for triaging an exceptionId that came back in an "
        "API response. Dev-only — log files don't ship to CFA prod hosts."
    ),
    parameters=[
        ToolParameter(
            name="service", type="string",
            description="Service name (e.g. 'core', 'ui', 'security', 'gateway'). See list_log_services.",
        ),
        ToolParameter(
            name="lines", type="integer", required=False, default=200,
            description="How many trailing lines to read (max 5000).",
        ),
        ToolParameter(
            name="exception_id", type="string", required=False,
            description="Exact substring match (typically the exceptionId from an API response).",
        ),
        ToolParameter(
            name="search", type="string", required=False,
            description="Case-insensitive substring filter to apply after tailing.",
        ),
    ],
    execute=_execute_tail_service_logs,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    which_environment_tool,
    list_caches_tool,
    clear_cache_tool,
    list_log_services_tool,
    tail_service_logs_tool,
]
