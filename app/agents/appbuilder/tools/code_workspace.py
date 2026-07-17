"""Code-reading tools — Claude-Code-style file access scoped to a local workspace.

Replaces the "Platform KB in MySQL" idea from the plan: the agent reads code
itself (Java, TypeScript, Python sources of `nocode-saas`, `nocode-ui`,
`nocode-kirun`) when it needs to understand an API or a Kirun primitive. No
curated docs to maintain — code is the source of truth.

Workspace layout (per CFA instance, on a mounted volume):

    {CFA_WORKSPACE_DIR}/
      nocode-saas/
      nocode-ui/
      nocode-kirun/

`CFA_WORKSPACE_DIR` defaults to `/var/cfa/workspace`. For dev convenience,
if that doesn't exist we fall back to sibling repos next to the nocode-ai
install root (`../nocode-saas`, etc.). This lets a developer run nocode-ai
locally without setting up a mount.

Tools exposed:
  - code_list_repos     — name, current commit SHA, last-fetched ts per repo
  - code_ls             — directory listing
  - code_glob           — files matching a glob pattern
  - code_grep           — substring/regex search across files (via git grep)
  - code_read           — read a file slice with line numbers

All tools are READ-ONLY and PATH-SAFE: every path is resolved to an absolute
filesystem path and required to stay inside the configured repo root. A
relative path like `../../etc/passwd` is rejected.

Refresh of the local checkout happens out-of-band via the deploy pipeline
(see Phase 4 of the plan) — these tools never call `git fetch` or `git pull`.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


# ── workspace resolution ──────────────────────────────────────────────────


# The three repos the agent is allowed to read. Hardcoded to keep the surface
# tight — adding a new repo means adding it here AND deploying the clone.
_ALLOWED_REPOS: tuple[str, ...] = ("nocode-saas", "nocode-ui", "nocode-kirun")


def _workspace_root() -> Path:
    """Resolve the workspace root.

    Priority:
      1. `CFA_WORKSPACE_DIR` env / setting (explicit, wins). Default is
         `/var/cfa/workspace` which is created during container provisioning.
      2. Fallback for local dev: parent of nocode-ai (so sibling clones of
         nocode-saas / nocode-ui / nocode-kirun are found).
    """
    from app.config import settings

    explicit = getattr(settings, "CFA_WORKSPACE_DIR", "") or "/var/cfa/workspace"
    p = Path(explicit).expanduser()
    if p.exists():
        return p
    # Dev fallback: nocode-ai/../  (siblings of nocode-ai)
    install_root = Path(__file__).resolve().parents[4]
    return install_root.parent


def _resolve_repo(repo: str) -> tuple[Path | None, str | None]:
    """Return (repo_root_path, error). Validates the repo name is allowed
    AND that the directory exists; error string surfaces what's missing."""
    if repo not in _ALLOWED_REPOS:
        return None, (
            f"Unknown repo '{repo}'. Allowed: {', '.join(_ALLOWED_REPOS)}. "
            "Add to _ALLOWED_REPOS + deploy a checkout to extend."
        )
    root = _workspace_root() / repo
    if not root.exists() or not root.is_dir():
        return None, (
            f"Repo '{repo}' not found at {root}. The checkout may be missing "
            "on this CFA instance. Run the admin pull endpoint or contact ops."
        )
    return root.resolve(), None


def _safe_path(repo_root: Path, relative: str) -> Path | None:
    """Resolve `relative` against repo_root, guarding against traversal.

    Returns the resolved absolute path if it stays inside repo_root, else None.
    """
    if not relative or relative == ".":
        return repo_root
    candidate = (repo_root / relative).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError:
        return None
    return candidate


async def _run_git(cwd: Path, *args: str, timeout: float = 10.0) -> tuple[int, str, str]:
    """Run `git <args>` in cwd. Returns (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", f"git {' '.join(shlex.quote(a) for a in args)} timed out after {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


# ── code_list_repos ───────────────────────────────────────────────────────


async def _execute_code_list_repos(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    rows: list[str] = []
    workspace = _workspace_root()
    rows.append(f"Workspace root: {workspace}")
    rows.append("")
    for repo in _ALLOWED_REPOS:
        root = workspace / repo
        if not root.exists():
            rows.append(f"- {repo}: (not checked out)")
            continue
        rc, sha, _ = await _run_git(root, "rev-parse", "HEAD")
        rc2, branch, _ = await _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        rc3, last, _ = await _run_git(root, "log", "-1", "--format=%cI %s")
        sha_s = sha.strip()[:12] if rc == 0 else "?"
        branch_s = branch.strip() if rc2 == 0 else "?"
        last_s = last.strip() if rc3 == 0 else "?"
        rows.append(f"- {repo}: branch={branch_s} sha={sha_s} last={last_s}")
    return ToolResult(success=True, summary="\n".join(rows))


code_list_repos_tool = ToolDefinition(
    name="code_list_repos",
    description=(
        "List the local code workspace: which repos are checked out, their "
        "current branch + commit SHA, and the timestamp of the latest commit. "
        "Use this before code_read / code_grep to confirm the workspace is "
        "current. Repos: nocode-saas, nocode-ui, nocode-kirun."
    ),
    parameters=[],
    execute=_execute_code_list_repos,
)


# ── code_ls ───────────────────────────────────────────────────────────────


async def _execute_code_ls(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    repo = params.get("repo") or ""
    rel = params.get("path") or "."
    root, err = _resolve_repo(repo)
    if err:
        return ToolResult(success=False, error=err)
    target = _safe_path(root, rel)
    if target is None:
        return ToolResult(success=False, error=f"Path '{rel}' escapes repo root")
    if not target.exists():
        return ToolResult(success=False, error=f"Path '{rel}' does not exist in {repo}")
    if target.is_file():
        return ToolResult(success=False, error=f"'{rel}' is a file; use code_read")
    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    rows = [f"{repo}/{rel}:"]
    for p in entries:
        if p.name.startswith(".") and p.name not in (".github", ".claude"):
            continue
        rel_to_root = p.relative_to(root)
        mark = "/" if p.is_dir() else ""
        rows.append(f"  {rel_to_root}{mark}")
    return ToolResult(success=True, summary="\n".join(rows))


code_ls_tool = ToolDefinition(
    name="code_ls",
    description=(
        "List directory contents inside a checked-out repo. Excludes hidden "
        "dot-files (keeps .github/.claude). Use to navigate before code_read. "
        "Repo must be one of nocode-saas, nocode-ui, nocode-kirun."
    ),
    parameters=[
        ToolParameter(name="repo", type="string", description="Repo name (nocode-saas | nocode-ui | nocode-kirun)."),
        ToolParameter(name="path", type="string", required=False, default=".", description="Directory relative to repo root. Default '.' (repo root)."),
    ],
    execute=_execute_code_ls,
)


# ── code_glob ─────────────────────────────────────────────────────────────


async def _execute_code_glob(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    repo = params.get("repo") or ""
    pattern = params.get("pattern") or ""
    if not pattern:
        return ToolResult(success=False, error="`pattern` is required, e.g. 'src/**/*.java'")
    root, err = _resolve_repo(repo)
    if err:
        return ToolResult(success=False, error=err)

    # rglob handles ** correctly. Bound the result count so the response stays
    # under the LLM's tool-result cap.
    try:
        max_results = int(params.get("max_results") or 200)
    except (TypeError, ValueError):
        max_results = 200
    max_results = max(1, min(max_results, 2000))

    matches: list[str] = []
    truncated = False
    for p in root.rglob(pattern.lstrip("/")):
        if p.is_file():
            matches.append(str(p.relative_to(root)))
            if len(matches) >= max_results:
                truncated = True
                break
    matches.sort()
    if not matches:
        return ToolResult(success=True, summary=f"(no files match '{pattern}' in {repo})")
    body = "\n".join(matches)
    if truncated:
        body += f"\n\n... [truncated at {max_results} matches; tighten the pattern]"
    return ToolResult(success=True, summary=f"{repo} files matching '{pattern}' ({len(matches)}):\n{body}")


code_glob_tool = ToolDefinition(
    name="code_glob",
    description=(
        "Find files inside a repo by glob pattern (e.g. 'src/**/*.java', "
        "'**/AuthService.*'). Returns relative paths sorted alphabetically. "
        "Capped at max_results matches. Use before code_read to discover the "
        "file path you want."
    ),
    parameters=[
        ToolParameter(name="repo", type="string", description="Repo name (nocode-saas | nocode-ui | nocode-kirun)."),
        ToolParameter(name="pattern", type="string", description="Glob pattern (Python pathlib rglob semantics)."),
        ToolParameter(name="max_results", type="integer", required=False, default=200, description="Max matches to return (capped at 2000)."),
    ],
    execute=_execute_code_glob,
)


# ── code_grep ─────────────────────────────────────────────────────────────


async def _execute_code_grep(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    repo = params.get("repo") or ""
    pattern = params.get("pattern") or ""
    if not pattern:
        return ToolResult(success=False, error="`pattern` is required")
    root, err = _resolve_repo(repo)
    if err:
        return ToolResult(success=False, error=err)

    path_glob = (params.get("path_glob") or "").strip()
    try:
        max_results = int(params.get("max_results") or 200)
    except (TypeError, ValueError):
        max_results = 200
    max_results = max(1, min(max_results, 1000))

    # git grep is faster than `grep -r` and respects .gitignore.
    args = ["grep", "-n", "-I", "--no-color", "--", pattern]
    if path_glob:
        args.extend(["--", path_glob])
    rc, out, err_text = await _run_git(root, *args, timeout=15.0)
    # git grep returns 1 when no matches (NOT an error condition here).
    if rc not in (0, 1):
        return ToolResult(success=False, error=f"git grep failed (rc={rc}): {err_text[:300]}")

    lines = [line for line in out.splitlines() if line.strip()]
    if not lines:
        return ToolResult(success=True, summary=f"(no matches for '{pattern}' in {repo})")
    truncated = len(lines) > max_results
    shown = lines[:max_results]
    body = "\n".join(shown)
    if truncated:
        body += f"\n\n... [{len(lines) - max_results} more matches truncated; tighten the pattern or path_glob]"
    return ToolResult(success=True, summary=f"{repo} grep '{pattern}' ({len(shown)} of {len(lines)}):\n{body}")


code_grep_tool = ToolDefinition(
    name="code_grep",
    description=(
        "Search across a repo for a pattern via `git grep -n` (faster than "
        "grep -r and respects .gitignore). Returns `path:line:content` rows. "
        "Optional path_glob narrows by file pattern (e.g. '*.java', "
        "'src/**/*.ts'). Capped at max_results rows."
    ),
    parameters=[
        ToolParameter(name="repo", type="string", description="Repo name (nocode-saas | nocode-ui | nocode-kirun)."),
        ToolParameter(name="pattern", type="string", description="Pattern (passed to git grep — fixed string by default; prefix with '-E' yourself for extended regex via the pattern arg)."),
        ToolParameter(name="path_glob", type="string", required=False, description="Optional path glob to narrow the search (e.g. '*.java')."),
        ToolParameter(name="max_results", type="integer", required=False, default=200, description="Max result rows to return (capped at 1000)."),
    ],
    execute=_execute_code_grep,
)


# ── code_read ─────────────────────────────────────────────────────────────


async def _execute_code_read(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    repo = params.get("repo") or ""
    rel = params.get("path") or ""
    if not rel:
        return ToolResult(success=False, error="`path` is required")
    root, err = _resolve_repo(repo)
    if err:
        return ToolResult(success=False, error=err)
    target = _safe_path(root, rel)
    if target is None:
        return ToolResult(success=False, error=f"Path '{rel}' escapes repo root")
    if not target.exists() or not target.is_file():
        return ToolResult(success=False, error=f"'{rel}' is not a file in {repo}")

    try:
        offset = max(0, int(params.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = max(1, min(int(params.get("limit") or 2000), 5000))
    except (TypeError, ValueError):
        limit = 2000

    try:
        with target.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as e:
        return ToolResult(success=False, error=f"Failed to read '{rel}': {e}")

    total = len(all_lines)
    end = min(offset + limit, total)
    selected = all_lines[offset:end]

    # Numbered output matches the FileReadTool format.
    numbered = [f"{i+1:6d}\t{line.rstrip()}" for i, line in enumerate(selected, start=offset)]
    header = f"{repo}/{rel}  (lines {offset + 1}-{end} of {total})"
    body = header + "\n" + "\n".join(numbered)
    if end < total:
        body += f"\n\n... [{total - end} more lines below; re-call with offset={end}]"
    return ToolResult(success=True, summary=body)


code_read_tool = ToolDefinition(
    name="code_read",
    description=(
        "Read a file from a checked-out repo with line numbers. Returns at "
        "most `limit` lines starting at `offset` (1-based after the header). "
        "Use offset to page through large files. Always pair the result with "
        "the file path + line numbers when citing code in your responses."
    ),
    parameters=[
        ToolParameter(name="repo", type="string", description="Repo name (nocode-saas | nocode-ui | nocode-kirun)."),
        ToolParameter(name="path", type="string", description="File path relative to repo root."),
        ToolParameter(name="offset", type="integer", required=False, default=0, description="0-based line offset to start reading from."),
        ToolParameter(name="limit", type="integer", required=False, default=2000, description="Max lines to return (capped at 5000)."),
    ],
    execute=_execute_code_read,
)


# ── Module export ────────────────────────────────────────────────────────


CODE_WORKSPACE_TOOLS: list[ToolDefinition] = [
    code_list_repos_tool,
    code_ls_tool,
    code_glob_tool,
    code_grep_tool,
    code_read_tool,
]
