"""Subprocess entrypoint for code_run.

Invoked as:
    venv/bin/python -m app.agents.appbuilderv4.sdk._runner <script_path>

Inside, we register the `modlix` SDK on `sys.modules['modlix']` so the
user's script can do `import modlix` and get the auth-bound helpers.

Stdout and stderr stream to the parent process; the parent reads them
verbatim. The exit code is 0 on success, 1 on any uncaught exception in
the user script.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m app.agents.appbuilderv4.sdk._runner <script_path>", file=sys.stderr)
        return 2

    script_path = Path(sys.argv[1])
    if not script_path.exists():
        print(f"script not found: {script_path}", file=sys.stderr)
        return 2

    # Register the SDK as the top-level `modlix` module so `import modlix`
    # works from the user's script.
    import app.agents.appbuilderv4.sdk as _sdk
    sys.modules["modlix"] = _sdk

    source = script_path.read_text()
    # Compile so tracebacks point at the script path, not "<string>".
    try:
        code = compile(source, str(script_path), "exec")
    except SyntaxError:
        traceback.print_exc()
        return 1

    # Plain exec inside a fresh module-like namespace. We deliberately do
    # NOT block builtins or imports — the sandbox is process-level, not
    # in-process. The subprocess itself is the boundary.
    glb: dict = {"__name__": "__main__", "__file__": str(script_path)}
    try:
        exec(code, glb)
    except SystemExit as e:
        return int(e.code or 0)
    except BaseException:  # noqa: BLE001 — capture everything for the agent to read
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
