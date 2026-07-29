"""Platform knowledge base — file-backed, service-organised.

The KB lives as markdown files under `app/agents/appbuilderv4/kb/`,
grouped by service: security / ui / core / entity-processor / shared /
workflows. Files are refreshed on deploy (via scripts/build_v4_kb.py)
and read into memory once on first access. No DB, no online editing.

Three tools:
  - platform_kb_list(service?)         — services list, or files in one service
  - platform_kb_get(service, slug)     — full file body
  - platform_kb_search(query, service?) — substring search across files,
                                          returns top hits with excerpts

Search is plain case-insensitive substring (no FTS index) — the corpus
is small enough (~250 files, ~6KB avg) that scanning is sub-50ms.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult


KB_ROOT = Path(__file__).resolve().parents[1] / "kb"
SERVICES = ("security", "ui", "core", "entity-processor", "shared", "workflows")

# Tiny in-process cache: {service: {slug: body}}.
_CACHE: dict[str, dict[str, str]] | None = None


def _load_all() -> dict[str, dict[str, str]]:
    """Read every .md under kb/<service>/ once and cache by (service, slug)."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    cache: dict[str, dict[str, str]] = {svc: {} for svc in SERVICES}
    if not KB_ROOT.exists():
        _CACHE = cache
        return _CACHE
    for svc in SERVICES:
        svc_dir = KB_ROOT / svc
        if not svc_dir.exists():
            continue
        for md in sorted(svc_dir.glob("*.md")):
            try:
                cache[svc][md.stem] = md.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
    _CACHE = cache
    return _CACHE


def _slug_or_filename(stem: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


def _excerpt(body: str, query: str, span: int = 160) -> str:
    """Return ~span chars around the first case-insensitive match of query.
    Falls back to the file's first 200 chars if no match."""
    if not query:
        return body[:200].strip()
    idx = body.lower().find(query.lower())
    if idx == -1:
        return body[:200].strip()
    start = max(0, idx - span // 2)
    end = min(len(body), idx + len(query) + span // 2)
    text = body[start:end].strip().replace("\n", " ")
    prefix = "... " if start > 0 else ""
    suffix = " ..." if end < len(body) else ""
    return f"{prefix}{text}{suffix}"


# ── platform_kb_list ─────────────────────────────────────────────────────


async def _execute_platform_kb_list(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    cache = _load_all()
    svc = (params.get("service") or "").strip()
    if not svc:
        # Top-level list of services.
        lines = ["Platform KB services:"]
        for s in SERVICES:
            n = len(cache.get(s, {}))
            lines.append(f"  {s:<18s} {n:>3d} entries")
        return ToolResult(success=True, summary="\n".join(lines),
                          data={"services": {s: len(cache.get(s, {})) for s in SERVICES}})
    if svc not in cache:
        return ToolResult(success=False,
                          error=f"unknown service {svc!r}; valid: {list(SERVICES)}")
    entries = sorted(cache[svc])
    lines = [f"Platform KB — {svc} ({len(entries)} entries):"]
    for slug in entries:
        # First non-empty line is usually a markdown H1 — use it as title.
        first = next((l.strip() for l in cache[svc][slug].splitlines()
                      if l.strip()), slug)
        first = first.lstrip("# ").strip()
        lines.append(f"  {slug:<40s} {first[:80]}")
    return ToolResult(success=True, summary="\n".join(lines),
                      data={"service": svc, "entries": entries})


platform_kb_list_tool = ToolDefinition(
    name="platform_kb_list",
    description=(
        "List platform KB services, or list the entries within one service. "
        "Pass no `service` to see all services + their entry counts. Pass a "
        "service to see every entry's slug + title."
    ),
    parameters=[
        ToolParameter(name="service", type="string", required=False,
                      description="One of: security, ui, core, entity-processor, shared, workflows. Omit to list services."),
    ],
    execute=_execute_platform_kb_list,
)


# ── platform_kb_get ──────────────────────────────────────────────────────


async def _execute_platform_kb_get(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    svc = (params.get("service") or "").strip()
    slug = (params.get("slug") or "").strip()
    if not svc or not slug:
        return ToolResult(success=False, error="both `service` and `slug` are required")
    cache = _load_all()
    if svc not in cache:
        return ToolResult(success=False, error=f"unknown service {svc!r}; valid: {list(SERVICES)}")
    # Try exact, then slugified, then case-insensitive match.
    candidate = (slug if slug in cache[svc]
                 else _slug_or_filename(slug) if _slug_or_filename(slug) in cache[svc]
                 else next((k for k in cache[svc] if k.lower() == slug.lower()), None))
    if candidate is None:
        # Suggest close matches.
        suggestions = [k for k in cache[svc] if slug.lower() in k.lower()][:5]
        msg = f"no entry {slug!r} under service {svc!r}."
        if suggestions:
            msg += f" Did you mean: {suggestions}?"
        return ToolResult(success=False, error=msg)
    body = cache[svc][candidate]
    return ToolResult(
        success=True,
        summary=body,
        data={"service": svc, "slug": candidate, "chars": len(body)},
    )


platform_kb_get_tool = ToolDefinition(
    name="platform_kb_get",
    description=(
        "Fetch one platform KB entry verbatim. `service` is one of the 6 "
        "services; `slug` is the entry filename without the .md extension "
        "(use `platform_kb_list(service)` to discover slugs). On a near-miss "
        "the error message suggests close matches."
    ),
    parameters=[
        ToolParameter(name="service", type="string", description="security | ui | core | entity-processor | shared | workflows"),
        ToolParameter(name="slug", type="string", description="Entry slug, e.g. 'auth-lifecycle'"),
    ],
    execute=_execute_platform_kb_get,
)


# ── platform_kb_search ───────────────────────────────────────────────────


async def _execute_platform_kb_search(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    query = (params.get("query") or "").strip()
    if not query:
        return ToolResult(success=False, error="`query` is required")
    svc_filter = (params.get("service") or "").strip() or None
    max_hits = max(1, min(int(params.get("max_hits") or 8), 30))

    cache = _load_all()
    services = [svc_filter] if svc_filter else list(SERVICES)
    if svc_filter and svc_filter not in cache:
        return ToolResult(success=False, error=f"unknown service {svc_filter!r}")

    q_lower = query.lower()
    hits: list[tuple[str, str, int, str]] = []  # (service, slug, count, excerpt)
    for svc in services:
        for slug, body in cache.get(svc, {}).items():
            count = body.lower().count(q_lower)
            if count == 0:
                # Also match against the slug itself
                if q_lower in slug.lower():
                    count = 1
                else:
                    continue
            hits.append((svc, slug, count, _excerpt(body, query)))
    # Rank: higher count first.
    hits.sort(key=lambda h: -h[2])
    hits = hits[:max_hits]
    if not hits:
        return ToolResult(success=True,
                          summary=f"No matches for {query!r}"
                                  + (f" in service={svc_filter}" if svc_filter else ""),
                          data={"hits": []})
    lines = [f"{len(hits)} hit(s) for {query!r}"
             + (f" (service={svc_filter}):" if svc_filter else " (all services):")]
    for svc, slug, count, excerpt in hits:
        lines.append(f"  [{svc}/{slug}]  ({count} match{'es' if count != 1 else ''})")
        lines.append(f"    {excerpt}")
    return ToolResult(
        success=True,
        summary="\n".join(lines),
        data={"hits": [{"service": s, "slug": sl, "count": c, "excerpt": e}
                       for s, sl, c, e in hits]},
    )


platform_kb_search_tool = ToolDefinition(
    name="platform_kb_search",
    description=(
        "Substring search across the platform KB. Returns the top hits with "
        "service, slug, match-count, and a short excerpt. Use this BEFORE "
        "guessing platform values (permission strings, app types, endpoint "
        "paths, etc.). Optionally scope to one service via `service=` for "
        "tighter results."
    ),
    parameters=[
        ToolParameter(name="query", type="string", description="Text to search for (case-insensitive substring)."),
        ToolParameter(name="service", type="string", required=False,
                      description="Limit search to one service. Omit to search all 6."),
        ToolParameter(name="max_hits", type="integer", required=False, default=8,
                      description="Cap on returned hits (1-30)."),
    ],
    execute=_execute_platform_kb_search,
)


TOOLS = [
    platform_kb_list_tool,
    platform_kb_get_tool,
    platform_kb_search_tool,
]
