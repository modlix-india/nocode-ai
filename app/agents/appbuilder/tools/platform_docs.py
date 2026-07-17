"""Platform-doc reading tools — on-demand access to bundled CFA reference.

The CFA ships with curated markdown under `app/agents/appbuilder/aicontext/`
covering platform conventions, design system, Kirun primitives, and a corpus
of by-task recipes. The system prompt lists the doc names + one-line
summaries; the agent pulls full content on demand via these tools (same
deferred-content pattern as the modlix tool catalog).

Tools:
  - platform_doc_list()         — index of available docs with summaries
  - platform_doc_read(name)     — read one doc by short name
  - pattern_search(query)       — substring search over the by-task corpus
                                   (under aicontext/patterns/ or corpus/)
  - pattern_read(task_name)     — read one task README by slug

Path resolution:
  - reference docs live under aicontext/ or aicontext/reference/
  - patterns live under aicontext/patterns/<task>/README.md
                 or aicontext/corpus/by-task/<task>/README.md (legacy layout)
  - the existing 6 numbered files (01-critical-rules.md, …) at the
    aicontext/ root are surfaced under their canonical short name (the file
    stem without the numeric prefix and .md suffix).

Read-only. Edits happen via PR to the nocode-ai repo by Modlix devs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


# Shared constants — keeps the linter happy and makes layout changes easy.
_PATTERN_README = "README.md"
_PATTERN_PARENTS = ("patterns", "corpus/by-task")


def _aicontext_root() -> Path:
    """Resolve <nocode-ai>/app/agents/appbuilder/aicontext/.

    Co-located with this module so the resolution stays stable regardless of
    where the process is launched from.
    """
    return Path(__file__).resolve().parent.parent / "aicontext"


def _is_pattern_dir(p: Path) -> bool:
    return p.is_dir() and (p / _PATTERN_README).exists()


def _strip_numeric_prefix(stem: str) -> str:
    """`01-critical-rules` → `critical-rules`. Leaves non-prefixed names alone."""
    parts = stem.split("-", 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1]
    return stem


def _first_heading(path: Path, max_len: int = 120) -> str:
    """First non-blank H1/H2 line of a markdown file, capped for compactness.

    Skips YAML frontmatter (delimited by `---` at the top of the file) so
    files that start with `name: foo` metadata still surface their real
    heading. Falls back to the first content line only if no heading appears.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            in_frontmatter = False
            saw_frontmatter_open = False
            for line in f:
                s = line.strip()
                if s == "---":
                    if not saw_frontmatter_open:
                        # Opening delimiter — only count if it's the file's
                        # first non-blank line (or we've only seen blanks).
                        saw_frontmatter_open = True
                        in_frontmatter = True
                    else:
                        in_frontmatter = False
                    continue
                if in_frontmatter:
                    continue
                if s.startswith("#"):
                    text = s.lstrip("#").strip()
                    return text if len(text) <= max_len else text[:max_len - 3] + "..."
                if s:
                    # Fall back to first content line if no heading appears.
                    return s if len(s) <= max_len else s[:max_len - 3] + "..."
    except OSError:
        pass
    return ""


def _resolve_doc_path(name: str, root: Path) -> Path | None:
    """Find a reference doc by short name. Search order:
      1. <root>/reference/<name>.md
      2. <root>/<name>.md
      3. <root>/<NN>-<name>.md (numeric-prefixed legacy layout)
    Returns the path if found, else None."""
    if not name:
        return None
    candidates = [
        root / "reference" / f"{name}.md",
        root / f"{name}.md",
    ]
    for c in candidates:
        if c.is_file():
            return c
    # Numeric-prefix fallback for the existing 6 docs (01-critical-rules.md …).
    if root.exists():
        for p in root.iterdir():
            if p.is_file() and p.suffix == ".md" and _strip_numeric_prefix(p.stem) == name:
                return p
    return None


def _list_reference_docs(root: Path) -> list[tuple[str, str, Path]]:
    """Return (short_name, summary, path) for every reference doc."""
    out: list[tuple[str, str, Path]] = []
    seen_names: set[str] = set()

    # 1. <root>/reference/*.md
    ref_dir = root / "reference"
    if ref_dir.is_dir():
        for p in sorted(ref_dir.glob("*.md")):
            name = p.stem
            if name not in seen_names:
                out.append((name, _first_heading(p), p))
                seen_names.add(name)

    # 2. <root>/*.md (the legacy numbered docs and anything top-level)
    if root.is_dir():
        for p in sorted(root.glob("*.md")):
            name = _strip_numeric_prefix(p.stem)
            if name not in seen_names:
                out.append((name, _first_heading(p), p))
                seen_names.add(name)
    return out


def _list_pattern_dirs(root: Path) -> list[tuple[str, str, Path]]:
    """Return (slug, summary, path-to-README) for every pattern recipe."""
    out: list[tuple[str, str, Path]] = []
    for parent_name in _PATTERN_PARENTS:
        parent = root / parent_name
        if not parent.is_dir():
            continue
        for sub in sorted(parent.iterdir()):
            if not _is_pattern_dir(sub):
                continue
            readme = sub / _PATTERN_README
            out.append((sub.name, _first_heading(readme), readme))
    return out


# ── platform_doc_list ───────────────────────────────────────────────────


async def _execute_platform_doc_list(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    root = _aicontext_root()
    if not root.exists():
        return ToolResult(
            success=False,
            error=(
                f"aicontext directory not found at {root}. The bundled platform "
                "docs ship inside the agent. Check the deployment."
            ),
        )
    docs = _list_reference_docs(root)
    patterns = _list_pattern_dirs(root)
    lines = [f"Platform reference docs ({len(docs)}):"]
    for name, summary, _path in docs:
        lines.append(f"  - {name} — {summary or '(no heading)'}")
    if patterns:
        lines.append("")
        lines.append(f"By-task pattern recipes ({len(patterns)} — fetch with pattern_read):")
        # Cap the listing to keep the response readable. The agent uses
        # pattern_search to find slugs by keyword for the long tail.
        for slug, summary, _path in patterns[:40]:
            lines.append(f"  - {slug} — {summary or '(no heading)'}")
        if len(patterns) > 40:
            lines.append(f"  ... and {len(patterns) - 40} more. Use pattern_search(query=...) to find by keyword.")
    return ToolResult(success=True, summary="\n".join(lines))


platform_doc_list_tool = ToolDefinition(
    name="platform_doc_list",
    description=(
        "List the bundled platform reference docs (Modlix conventions, "
        "design system, Kirun primitives, etc.) and the by-task pattern "
        "recipes. Each entry includes a one-line summary. Use this when you "
        "need to pick which doc to read with platform_doc_read or which "
        "pattern slug to fetch with pattern_read."
    ),
    parameters=[],
    execute=_execute_platform_doc_list,
)


# ── platform_doc_read ───────────────────────────────────────────────────


async def _execute_platform_doc_read(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required (use platform_doc_list to discover)")
    root = _aicontext_root()
    path = _resolve_doc_path(name, root)
    if path is None:
        return ToolResult(
            success=False,
            error=f"Unknown doc '{name}'. Use platform_doc_list to see available short names.",
        )
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ToolResult(success=False, error=f"Failed to read {path}: {e}")
    rel = path.relative_to(root) if path.is_relative_to(root) else path
    return ToolResult(success=True, summary=f"# {name}  ({rel})\n\n{body}")


platform_doc_read_tool = ToolDefinition(
    name="platform_doc_read",
    description=(
        "Read one bundled platform reference doc by short name. Returns the "
        "full markdown body. Use after platform_doc_list to fetch the "
        "specific doc you need (e.g. 'design-system', 'critical-rules', "
        "'kirun-primitives')."
    ),
    parameters=[
        ToolParameter(name="name", type="string", description="Doc short name (from platform_doc_list)."),
    ],
    execute=_execute_platform_doc_read,
)


# ── pattern_search ──────────────────────────────────────────────────────


async def _execute_pattern_search(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    query = (params.get("query") or "").strip().lower()
    if not query:
        return ToolResult(success=False, error="`query` is required")
    try:
        max_results = max(1, min(int(params.get("max_results") or 10), 30))
    except (TypeError, ValueError):
        max_results = 10
    root = _aicontext_root()
    patterns = _list_pattern_dirs(root)
    if not patterns:
        return ToolResult(success=True, summary="(no patterns directory found; nothing to search)")

    matches: list[tuple[str, str, str]] = []  # (score-as-string-for-display, slug, snippet)
    for slug, summary, path in patterns:
        slug_l = slug.lower()
        try:
            body = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            body = ""
        score = 0
        if query == slug_l:
            score = 100
        elif slug_l.startswith(query):
            score = 80
        elif query in slug_l:
            score = 60
        elif query in summary.lower():
            score = 40
        elif query in body:
            score = 20
        if score > 0:
            matches.append((f"{score:03d}", slug, summary))

    matches.sort(key=lambda r: (-int(r[0]), r[1]))
    if not matches:
        return ToolResult(success=True, summary=f"(no patterns match '{query}')")
    rows = matches[:max_results]
    lines = [f"Pattern matches for '{query}' ({len(rows)} of {len(matches)}):"]
    for _score, slug, summary in rows:
        lines.append(f"  - {slug} — {summary or '(no heading)'}")
    lines.append("")
    lines.append("Fetch with: pattern_read(task_name=\"<slug>\")")
    return ToolResult(success=True, summary="\n".join(lines))


pattern_search_tool = ToolDefinition(
    name="pattern_search",
    description=(
        "Search the by-task pattern recipes by keyword (matches against slug "
        "+ summary + body). Returns ranked slugs. Use to find a recipe like "
        "'login page' or 'crud list' before fetching the full README with "
        "pattern_read."
    ),
    parameters=[
        ToolParameter(name="query", type="string", description="Keyword(s) to search (case-insensitive substring)."),
        ToolParameter(name="max_results", type="integer", required=False, default=10, description="Max matches (capped at 30)."),
    ],
    execute=_execute_pattern_search,
)


# ── pattern_read ────────────────────────────────────────────────────────


_SAMPLE_EXTENSIONS = (".json", ".dsl", ".tree.txt")


def _resolve_pattern_dir(slug: str, root: Path) -> Path | None:
    """Return the directory holding <slug>/README.md, or None."""
    for parent_name in _PATTERN_PARENTS:
        candidate = root / parent_name / slug
        if (candidate / _PATTERN_README).is_file():
            return candidate
    return None


def _list_pattern_samples(pattern_dir: Path) -> list[str]:
    """Return sample filenames (sorted) sitting beside README.md.

    Anything that isn't the README and matches one of the known sample
    extensions is surfaced. The agent fetches one with pattern_sample.
    """
    out: list[str] = []
    for p in sorted(pattern_dir.iterdir()):
        if not p.is_file() or p.name == _PATTERN_README:
            continue
        name_lower = p.name.lower()
        if any(name_lower.endswith(ext) for ext in _SAMPLE_EXTENSIONS):
            out.append(p.name)
    return out


async def _execute_pattern_read(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    slug = (params.get("task_name") or "").strip()
    if not slug:
        return ToolResult(success=False, error="`task_name` is required (use pattern_search to find slugs)")
    root = _aicontext_root()
    pattern_dir = _resolve_pattern_dir(slug, root)
    if pattern_dir is None:
        return ToolResult(
            success=False,
            error=f"Pattern '{slug}' not found under aicontext/patterns/ or aicontext/corpus/by-task/. Use pattern_search to discover slugs.",
        )
    readme = pattern_dir / _PATTERN_README
    try:
        body = readme.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ToolResult(success=False, error=f"Failed to read {readme}: {e}")
    samples = _list_pattern_samples(pattern_dir)
    suffix = ""
    if samples:
        suffix = (
            f"\n\n---\n\n**Available sample files** (fetch with `pattern_sample(task_name=\"{slug}\", file_name=...)`):\n\n"
            + "\n".join(f"  - {name}" for name in samples)
        )
    return ToolResult(success=True, summary=f"# pattern: {slug}\n\n{body}{suffix}")


pattern_read_tool = ToolDefinition(
    name="pattern_read",
    description=(
        "Read one by-task pattern recipe by slug. Returns the README plus a "
        "list of sample files (page JSONs, decompiled Kirun DSL, component "
        "trees) sitting alongside it. Fetch any sample with pattern_sample. "
        "Use after pattern_search finds a candidate slug (e.g. 'login-page', "
        "'crud-list-page')."
    ),
    parameters=[
        ToolParameter(name="task_name", type="string", description="Pattern slug (from pattern_search)."),
    ],
    execute=_execute_pattern_read,
)


# ── pattern_sample ──────────────────────────────────────────────────────


async def _execute_pattern_sample(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    slug = (params.get("task_name") or "").strip()
    file_name = (params.get("file_name") or "").strip()
    if not slug or not file_name:
        return ToolResult(success=False, error="`task_name` and `file_name` are required")
    # Path-traversal guard: file_name must be a bare filename, not a path.
    if "/" in file_name or "\\" in file_name or ".." in file_name:
        return ToolResult(
            success=False,
            error="`file_name` must be a bare filename (no path separators or '..'). Use pattern_read to see available samples.",
        )
    root = _aicontext_root()
    pattern_dir = _resolve_pattern_dir(slug, root)
    if pattern_dir is None:
        return ToolResult(
            success=False,
            error=f"Pattern '{slug}' not found. Use pattern_search to discover slugs.",
        )
    sample_path = (pattern_dir / file_name).resolve()
    # Defence-in-depth: ensure the resolved path stays inside the pattern dir.
    try:
        sample_path.relative_to(pattern_dir.resolve())
    except ValueError:
        return ToolResult(success=False, error="Resolved path escapes the pattern directory; refusing to read.")
    if not sample_path.is_file():
        return ToolResult(
            success=False,
            error=f"Sample '{file_name}' not found in pattern '{slug}'. Use pattern_read to see available samples.",
        )
    name_lower = sample_path.name.lower()
    if not any(name_lower.endswith(ext) for ext in _SAMPLE_EXTENSIONS):
        return ToolResult(
            success=False,
            error=f"'{file_name}' is not a recognized sample type. Supported: {', '.join(_SAMPLE_EXTENSIONS)}",
        )
    try:
        body = sample_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ToolResult(success=False, error=f"Failed to read {sample_path}: {e}")
    rel = sample_path.relative_to(root) if sample_path.is_relative_to(root) else sample_path
    return ToolResult(success=True, summary=f"# sample: {slug}/{file_name}  ({rel})\n\n{body}")


pattern_sample_tool = ToolDefinition(
    name="pattern_sample",
    description=(
        "Read one sample file from a pattern recipe (page JSON, decompiled "
        "Kirun DSL, or component-tree summary). These are the concrete "
        "reference cards backing each by-task recipe — use them to see what "
        "good looks like for a given pattern. Discover the available filenames "
        "via pattern_read (which lists them at the end of its response)."
    ),
    parameters=[
        ToolParameter(name="task_name", type="string", description="Pattern slug (e.g. 'login-page')."),
        ToolParameter(
            name="file_name",
            type="string",
            description="Sample filename from the pattern_read listing (e.g. 'leadzump.login.json', 'cxapp.login.event.loginFunction.dsl').",
        ),
    ],
    execute=_execute_pattern_sample,
)


# ── Module export ────────────────────────────────────────────────────────


PLATFORM_DOC_TOOLS: list[ToolDefinition] = [
    platform_doc_list_tool,
    platform_doc_read_tool,
    pattern_search_tool,
    pattern_read_tool,
    pattern_sample_tool,
]
