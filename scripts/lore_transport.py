#!/usr/bin/env python3
"""Export, plan and import lore for one app.

Three jobs, one format and one merge engine (app/services/lore/transport.py):

  export   read what a client knows about an app into a portable document
  plan     say what importing a document would do, and write nothing
  import   apply it

The committed seed files under app/services/lore/seeds/ are transport
documents that a person wrote by hand, so seeding an app is just an import.
That is deliberate — it means the seeds go through the same validation as every
import, are exercised by the import tests, and survive a database reset.

    ./venv/bin/python scripts/lore_transport.py plan \
        --client SYSTEM --app appbuilder --file app/services/lore/seeds/appbuilder.yaml

    ./venv/bin/python scripts/lore_transport.py import \
        --client SYSTEM --app appbuilder --file app/services/lore/seeds/appbuilder.yaml --yes

On authentication: this talks to the database directly, the way the other
migration scripts do, because it is not a request and has no token holder.
It DOES still resolve the real inheritance chain through
`access.resolve_scope` — the security service's `applications/internal/**`
routes are permitAll, which is what unattended curation already relies on — so
the CLI and the HTTP import make the identical base-versus-override decision.
Only `require_write()` is skipped, and `--client` is mandatory to compensate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.db.connection import init_db_pool, close_db_pool  # noqa: E402
from app.services.lore import access, transport  # noqa: E402

SEEDS_DIR = _REPO_ROOT / "app" / "services" / "lore" / "seeds"


class _Caller:
    """The minimum `resolve_scope` needs: a verified client code."""

    def __init__(self, client_code: str):
        self.client_code = client_code


async def _scope(client_code: str, app_code: str):
    scope = await access.resolve_scope(_Caller(client_code), app_code)
    scope.require_read()
    # require_write is deliberately NOT called: an operator at a terminal has
    # no JWT to check. --client being mandatory is the compensating control.
    return scope


def _resolve_file(args) -> Path:
    if args.file:
        return Path(args.file)
    candidate = SEEDS_DIR / f"{args.app}.yaml"
    if candidate.exists():
        return candidate
    raise SystemExit(
        f"No --file given and no committed seed at {candidate}. "
        f"Available: {', '.join(sorted(p.stem for p in SEEDS_DIR.glob('*.yaml'))) or '(none)'}"
    )


def _print_plan(plan_obj: transport.ImportPlan) -> None:
    t = plan_obj.totals
    print(f"\nPlan for {plan_obj.client_code}/{plan_obj.app_code} "
          f"from {plan_obj.source} (mode={plan_obj.mode})")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(t.items()) if v))

    groups: dict[str, list] = {}
    for a in plan_obj.actions:
        groups.setdefault(a.action, []).append(a)
    for action in ("add", "revise", "fork", "retire", "skip"):
        rows = groups.get(action) or []
        if not rows:
            continue
        print(f"\n  {action.upper()} ({len(rows)})")
        for a in rows:
            suffix = f"  [{a.reason}]" if a.reason and action in ("fork", "retire", "skip") else ""
            print(f"    {a.kind:11} {a.title[:66]}{suffix}")

    if plan_obj.shadowed:
        print(f"\n  SHADOWED ({len(plan_obj.shadowed)}) — your override is kept, "
              f"the base moves under it")
        for a in plan_obj.shadowed:
            print(f"    {a.kind:11} {a.title[:66]}")

    keep = [a for a in plan_obj.orphans if a.action == "keep"]
    drop = [a for a in plan_obj.orphans if a.action == "retire"]
    if keep:
        print(f"\n  HERE BUT NOT IN THE FILE — left alone ({len(keep)})")
        for a in keep[:12]:
            print(f"    {a.kind:11} {a.title[:56]}  [{a.reason}]")
    if drop:
        print(f"\n  WOULD BE RETIRED by sync mode ({len(drop)})")
        for a in drop:
            print(f"    {a.kind:11} {a.title[:66]}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("export", "plan", "import"):
        p = sub.add_parser(name)
        p.add_argument("--client", required=True, help="Client code. No default, on purpose.")
        p.add_argument("--app", required=True, help="App code")
        if name == "export":
            p.add_argument("--out", default="", help="Write here instead of stdout")
            p.add_argument("--resolved", action="store_true",
                           help="Flatten the inheritance chain. Importing such a file "
                                "elsewhere turns inherited rows into owned copies.")
            p.add_argument("--status", default="active")
        else:
            p.add_argument("--file", default="",
                           help="Transport document. Defaults to the committed seed for --app.")
            p.add_argument("--mode", default="merge", choices=("merge", "sync"))
        if name == "import":
            p.add_argument("--yes", action="store_true", help="Do not ask")
            p.add_argument("--updated-by", type=int, default=0)

    args = ap.parse_args()

    await init_db_pool()
    try:
        scope = await _scope(args.client, args.app)
        print(f"scope: client={scope.client_code} app={scope.app_code} "
              f"chain={'>'.join(scope.read_chain)} "
              f"owner={scope.base_client or scope.client_code} "
              f"override={scope.is_override}")

        if args.cmd == "export":
            doc = await transport.export(scope, resolved=args.resolved, status=args.status)
            text = json.dumps(doc, indent=2, ensure_ascii=False, default=str)
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
                print(f"wrote {len(doc['entries'])} entries to {args.out}")
            else:
                print(text)
            return 0

        path = _resolve_file(args)
        doc = transport.parse(path.read_text(encoding="utf-8"))
        if doc.app_code != args.app:
            raise SystemExit(
                f"{path.name} is for app {doc.app_code!r} but --app is {args.app!r}"
            )
        if doc.client_code != args.client:
            print(f"note: the file says client {doc.client_code!r}; importing as "
                  f"{args.client!r}")
        print(f"document: {path} — {len(doc.entries)} entries")

        plan_obj = await transport.plan(scope, doc, mode=args.mode)
        _print_plan(plan_obj)

        if args.cmd == "plan":
            print("\n(plan only — nothing was written)")
            return 0

        if args.mode == "sync" and not args.yes:
            raise SystemExit("\nsync mode retires rows; re-run with --yes")
        if not args.yes:
            reply = input(f"\nApply this to {scope.client_code}/{args.app}? [y/N] ")
            if reply.strip().lower() not in ("y", "yes"):
                print("nothing written")
                return 1

        counters = await transport.apply(scope, doc, plan_obj, updated_by=args.updated_by)
        print("\napplied: " + "  ".join(f"{k}={v}" for k, v in counters.items() if v))
        return 1 if counters.get("failed") else 0

    finally:
        await close_db_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
