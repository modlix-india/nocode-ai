"""LeadZump agent LIVE smoke test — drives the real tools against a running stack.

The counterpart to `tests/agents/leadzump/test_agent.py`, which is offline and
proves the shapes. This proves the shapes are the ones the backend actually
accepts: the condition tree against `AbstractConditionDeserializer`, the epoch
timestamps against `CommonsSerializationModule`, and the read-modify-write PUT
against `updatableEntity`. Those were derived by reading Java, and reading Java
is not the same as a 200.

It talks to the gateway, so it needs nocode-saas up and a real user in a
`CLIENT`-level LeadZump tenant.

Reads only, by default. `--write` additionally exercises `note_add` against a
deal it just found — a real row in a real tenant, which is why it is opt-in and
never touches `deal_move_stage` (that one queues customer messages and ad-platform
conversions; drive it from the panel, where a human approves it).

Run:
    cd nocode-ai && ./venv/bin/python -m scripts.leadzump.smoke \\
        --user kiran@modlix.com --password '...' --client FIN
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import httpx

from app.agents.leadzump.tools.registry import ALL_TOOLS, MUTATING_TOOLS
from app.core.session import AuthContext
from app.core.tools.base import ToolResult

TOOLS = {t.name: t for t in ALL_TOOLS}


async def login(
    gateway: str, user: str, password: str, client_code: str, app_code: str, forwarded: str
) -> str:
    """Authenticate and return the bearer token.

    The `hostName` claim is stamped from `X-Forwarded-Host`, and later calls are
    checked against it — so the same value has to be used everywhere, which is
    why it is one argument rather than a default in three places.
    """
    headers = {
        "Content-Type": "application/json",
        "X-Forwarded-Host": forwarded,
        "X-Forwarded-Port": forwarded.split(":")[-1] if ":" in forwarded else "80",
        "clientCode": client_code,
        "appCode": app_code,
    }
    body = {
        "userName": user,
        "password": password,
        "identifierType": "EMAIL_ID" if "@" in user else "USER_NAME",
        "loggedInClientCode": client_code,
    }
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.post(
            f"{gateway}/api/security/authenticate", json=body, headers=headers
        )
    if response.status_code != 200:
        # The error body carries a multi-line stackTrace, which is not legal
        # JSON string content — parse it leniently rather than dying on it.
        try:
            detail = json.loads(response.text, strict=False).get("message", response.text[:200])
        except ValueError:
            detail = response.text[:200]
        raise SystemExit(f"login failed ({response.status_code}): {detail}")
    body_out = json.loads(response.text, strict=False)
    user = body_out.get("user") or {}
    return body_out["accessToken"], int(user.get("id") or 0), _display_name(user)


def _display_name(user: dict[str, Any]) -> str:
    parts = [(user.get("firstName") or "").strip(), (user.get("lastName") or "").strip()]
    return " ".join(p for p in parts if p) or (user.get("userName") or "")


def tool_context(auth: AuthContext) -> dict[str, Any]:
    """The subset of `BaseAgent.build_tool_context` the tools actually read.

    Not the real one, because that needs a `BaseSession` and therefore MySQL.
    It must still carry `auth`, though: the real `build_tool_context` sets it,
    and `task_list(mine=True)` reads `user_id` off it. A harness that omitted it
    would report a failure the running agent does not have — which it did, once.
    """
    return {
        "session_id": "smoke",
        "headers": auth.to_headers(),
        "client_code": auth.client_code,
        "app_code": auth.app_code,
        "auth": auth,
        "session_context": {},
    }


def render(name: str, result: ToolResult) -> bool:
    """One line per tool.

    Only the first line of `summary`: the rest is the JSON payload, because a
    read tool has to render its rows into `summary` for the model to see them
    at all (`ToolResult.to_tool_result_content` uses `summary` *instead of*
    `data`). Printing all of it would bury the result of every other check.

    The size and any truncation are worth showing, though — a read the model
    only half receives is a silent failure, and this is where it surfaces.
    """
    if not result.success:
        print(f"  [FAIL] {name}: {result.error}")
        return False
    seen = result.to_tool_result_content()
    headline = (result.summary or "").splitlines()[0] if result.summary else "ok"
    flag = "  ** TRUNCATED **" if "truncated" in seen else ""
    note = (result.data or {}).get("note") if isinstance(result.data, dict) else None
    print(f"  [ok  ] {name}: {headline}  ({len(seen)} chars to model){flag}")
    if note:
        print(f"         note: {note}")
    return "truncated" not in seen


async def run(args: argparse.Namespace) -> int:
    token, user_id, user_name = await login(
        args.gateway, args.user, args.password, args.client, args.app, args.forwarded
    )
    auth = AuthContext(
        token=token,
        client_code=args.client,
        client_id=0,
        user_id=user_id,
        user_name=user_name,
        app_code=args.app,
        access_app_code=args.app,
        client_level_type="CLIENT",
        forwarded_host=args.forwarded,
        forwarded_port=args.forwarded.split(":")[-1] if ":" in args.forwarded else "80",
    )
    context = tool_context(auth)
    failures: list[str] = []

    def check(name: str, result: ToolResult) -> ToolResult:
        if not render(name, result):
            failures.append(name)
        return result

    print(f"\n== reads against {args.gateway} as {args.user} @ {args.client}/{args.app} ==")

    leads = check("lead_search", await TOOLS["lead_search"].execute({"size": 5}, context))
    deals = check("deal_search", await TOOLS["deal_search"].execute({"size": 5}, context))
    products = check("product_list", await TOOLS["product_list"].execute({"size": 5}, context))
    check("assignee_list", await TOOLS["assignee_list"].execute({}, context))
    check(
        "stage_counts",
        await TOOLS["stage_counts"].execute({"group_by": "product", "size": 5}, context),
    )

    # Date filtering is the epoch-second contract's real test: an ISO string in
    # the filter would come back as a parse failure, not as fewer rows.
    check(
        "deal_search(created_after)",
        await TOOLS["deal_search"].execute(
            {"created_after": "2020-01-01", "size": 3}, context
        ),
    )
    # And free-text, which is the STRING_LOOSE_EQUAL / OR branch of the tree.
    check("deal_search(q)", await TOOLS["deal_search"].execute({"q": "a", "size": 3}, context))

    print("\n== single-record reads ==")
    lead_code = _first_code(leads)
    deal_code = _first_code(deals)
    if lead_code:
        check("lead_get", await TOOLS["lead_get"].execute({"code": lead_code}, context))
    else:
        print("  [skip] lead_get: no lead visible to this user")
    if deal_code:
        check("deal_get", await TOOLS["deal_get"].execute({"code": deal_code}, context))
    else:
        print("  [skip] deal_get: no deal visible to this user")

    product_id = _first_product_id(products)
    if product_id:
        check(
            "pipeline_describe",
            await TOOLS["pipeline_describe"].execute({"product_id": product_id}, context),
        )
    else:
        print("  [skip] pipeline_describe: no product visible to this user")

    print("\n== discovery, history and follow-ups ==")
    check("source_list", await TOOLS["source_list"].execute({}, context))
    check("task_list", await TOOLS["task_list"].execute({"size": 10}, context))
    check("task_list(mine)", await TOOLS["task_list"].execute({"mine": True, "size": 10}, context))

    if deal_code:
        deal = await TOOLS["deal_get"].execute({"code": deal_code}, context)
        deal_id = (deal.data or {}).get("id") if deal.success else None
        check("deal_activity", await TOOLS["deal_activity"].execute({"code": deal_code}, context))
        if deal_id:
            # Also the guard against the id/epoch confusion: a deal id that came
            # back as a 1970 timestamp would fail here rather than in production.
            if not isinstance(deal_id, int):
                failures.append("deal_get.id")
                print(f"  [FAIL] deal_get.id is {deal_id!r}, not a number")
            check("note_list", await TOOLS["note_list"].execute({"deal_id": deal_id}, context))
        else:
            print("  [skip] note_list: deal_get returned no id")

    notifications = await TOOLS["notification_list"].execute({"size": 5}, context)
    if not notifications.success and "503" in (notifications.error or ""):
        # The notification service is optional in a local stack; a 503 here says
        # it is not running, not that the tool is wrong.
        print("  [skip] notification_list: notification service not reachable (503)")
    else:
        check("notification_list", notifications)

    if args.write and deal_code:
        print("\n== write (real row) ==")
        check(
            "note_add",
            await TOOLS["note_add"].execute(
                {"deal_code": deal_code, "content": "LeadZump agent smoke test."}, context
            ),
        )
    elif args.write:
        print("\n  [skip] note_add: no deal to attach it to")
    else:
        print(f"\n  [skip] writes ({', '.join(sorted(MUTATING_TOOLS))}) — pass --write to include")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


def _first_code(result: ToolResult) -> str:
    rows = (result.data or {}).get("rows") if isinstance(result.data, dict) else None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return str(rows[0].get("code") or "")
    return ""


def _first_product_id(result: ToolResult) -> Any:
    products = (result.data or {}).get("products") if isinstance(result.data, dict) else None
    if isinstance(products, list) and products and isinstance(products[0], dict):
        return products[0].get("id")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default="http://localhost:8080")
    parser.add_argument("--forwarded", default="localhost:8080", help="X-Forwarded-Host")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--client", required=True, help="clientCode of a CLIENT-level tenant")
    parser.add_argument("--app", default="leadzump")
    parser.add_argument(
        "--write",
        action="store_true",
        help="also add a real note to the first deal found (never moves a stage)",
    )
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
