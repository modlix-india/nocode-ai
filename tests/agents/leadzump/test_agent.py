"""Unit: app/agents/leadzump — gates, wire contract, and the write path.

No network, no LLM, no DB. ``SaasClient._request`` is patched at the CLASS
level so it intercepts every instance regardless of how a tools module holds
its client.

These tests are aimed at the four things that would actually go wrong here,
not at coverage for its own sake:

  (a) **The gates.** A SiteZump user must not reach the CRM agent, a
      business-partner user must not reach the owner assistant, and widening
      the AppBuilder agent's own gate must stay off the table.
  (b) **The read-modify-write contract.** `TicketService.updatableEntity` and
      `OwnerService.updatableEntity` re-read the row and then assign `email`,
      `assignedUserId`, `subSource`, `tag`, `phoneNumber` and `dialCode` from
      the request body with no null check. A partial PUT therefore blanks the
      fields it omits and still answers 200. Every update tool must GET first
      and send the whole object back.
  (c) **Epoch-second timestamps.** commons serialises `LocalDateTime` as
      `toEpochSecond(UTC)`. A tool that puts an ISO string in a filter matches
      nothing; one that hands an epoch integer to the model reports nonsense.
  (d) **No tenant argument anywhere.** Tenancy comes from the caller's token.
      A tool that accepted a client code would let a model widen its own reach.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.leadzump.test_agent -v
"""

from __future__ import annotations

import asyncio
import json
import types
import unittest
from typing import Any
from unittest import mock

from fastapi import HTTPException

from app.config import settings

# Defensive: provider-key checks must never bite an offline unit test.
for _key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY"):
    if not getattr(settings, _key, ""):
        setattr(settings, _key, "offline-test-key")

from app.agents.leadzump.agent import LeadZumpAgent
from app.agents.leadzump.router import (
    ALLOWED_LEADZUMP_APPS,
    OWNER_LEVEL_TYPES,
    require_leadzump_auth_context,
)
from app.agents.leadzump.tools import _client as wire
from app.agents.leadzump.tools.deals import _deal_condition
from app.agents.leadzump.tools.leads import _lead_condition
from app.agents.leadzump.tools.registry import ALL_TOOLS, MUTATING_TOOLS
from app.core.session import AuthContext
from app.core.tools.base import ToolDefinition, ToolResult
from app.core.tools.http_client import SaasClient

# A real 22-character code, the length `BaseUpdatableDto.CODE_LENGTH` demands.
DEAL_CODE = "abcdefghij0123456789KL"
LEAD_CODE = "zyxwvutsrq9876543210AB"

AUTH_HEADERS = {
    "Authorization": "Bearer offline-test",
    "clientCode": "ACME",
    "appCode": "leadzump",
}


def _tool(name: str) -> ToolDefinition:
    by_name = {t.name: t for t in ALL_TOOLS}
    if name not in by_name:
        raise AssertionError(f"tool {name!r} not in ALL_TOOLS: {sorted(by_name)}")
    return by_name[name]


def _context() -> dict[str, Any]:
    return {
        "session_id": "ACME_test0001",
        "headers": dict(AUTH_HEADERS),
        "client_code": "ACME",
        "app_code": "leadzump",
        "session_context": {},
    }


def _auth(app_code: str = "leadzump", level: str = "CLIENT") -> AuthContext:
    return AuthContext(
        token="Bearer offline-test",
        client_code="ACME",
        client_id=7,
        user_id=42,
        app_code=app_code,
        access_app_code=app_code,
        client_level_type=level,
    )


class FakeSaasRequests:
    """Class-level ``SaasClient._request`` replacement.

    Patched as a class attribute; a callable object is not a descriptor, so
    ``client._request(method, path, ...)`` calls ``__call__`` WITHOUT the
    client instance — the signature starts at ``method`` on purpose.
    """

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        method: str,
        path: str,
        headers: dict | None = None,
        json: Any = None,
        params: dict | None = None,
        bypass_drafts: bool = False,
    ) -> ToolResult:
        self.calls.append(
            {"method": method, "path": path, "json": json, "params": params, "headers": headers}
        )
        for (m, suffix), payload in self.routes.items():
            if method == m and path.endswith(suffix):
                if isinstance(payload, ToolResult):
                    return payload
                return ToolResult(success=True, data=payload)
        return ToolResult(success=True, data={})

    def call(self, method: str, suffix: str) -> dict[str, Any]:
        for entry in self.calls:
            if entry["method"] == method and entry["path"].endswith(suffix):
                return entry
        raise AssertionError(
            f"no {method} call ending {suffix!r}; saw "
            f"{[(c['method'], c['path']) for c in self.calls]}"
        )


def _page(rows: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    """A Spring `Page` envelope around some rows."""
    return {
        "content": rows,
        "totalElements": total if total is not None else len(rows),
        "number": 0,
        "size": len(rows),
    }


def _run(tool_name: str, params: dict[str, Any], fake: FakeSaasRequests) -> ToolResult:
    with mock.patch.object(SaasClient, "_request", fake):
        return asyncio.run(_tool(tool_name).execute(params, _context()))


# ── (a) the gates ──────────────────────────────────────────────────────────
class GateTests(unittest.TestCase):
    def test_leadzump_owner_user_is_admitted(self):
        auth = asyncio.run(require_leadzump_auth_context(_auth()))
        self.assertEqual(auth.access_app_code, "leadzump")

    def test_other_app_is_refused(self):
        for app_code in ("sitezump", "appbuilder", "adzump"):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(require_leadzump_auth_context(_auth(app_code=app_code)))
            self.assertEqual(caught.exception.status_code, 403, app_code)

    def test_platform_operator_is_admitted(self):
        """`SYSTEM` is a real value of the column and must not be locked out.

        `security_client.LEVEL_TYPE` is enum('SYSTEM','CLIENT','CUSTOMER',
        'CONSUMER') — not the Java `ClientLevelType` enum, which has OWNER and
        no SYSTEM. Gating on the Java enum would refuse every platform admin.
        """
        auth = asyncio.run(require_leadzump_auth_context(_auth(level="SYSTEM")))
        self.assertEqual(auth.client_level_type, "SYSTEM")

    def test_business_partner_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(require_leadzump_auth_context(_auth(level="CUSTOMER")))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("business-partner", caught.exception.detail)

    def test_consumer_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(require_leadzump_auth_context(_auth(level="CONSUMER")))
        self.assertEqual(caught.exception.status_code, 403)

    def test_gate_covers_every_value_the_column_can_hold(self):
        """The column is a NOT NULL enum, so the allow-list is exhaustive."""
        column_enum = {"SYSTEM", "CLIENT", "CUSTOMER", "CONSUMER"}
        self.assertTrue(
            OWNER_LEVEL_TYPES <= column_enum,
            f"{OWNER_LEVEL_TYPES - column_enum} is not a value LEVEL_TYPE can hold",
        )

    def test_missing_level_type_fails_closed(self):
        """An absent levelType must refuse, not default to admitting.

        Defaulting open here would silently hand the partner portal an
        owner-scoped assistant the moment anything upstream stopped populating
        the field.
        """
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(require_leadzump_auth_context(_auth(level="")))
        self.assertEqual(caught.exception.status_code, 403)

    def test_the_customer_gate_is_security_relevant_not_cosmetic(self):
        """Measured on local: a partner reads the OWNER ORG's whole lead list.

        `OwnerDAO` declares no `userAccessField`, so `processorAccessCondition`
        applies no user filter, and a partner's `getEffectiveClientCode()`
        resolves to the managed client. A user in `VIVOB` therefore sees all 164
        of `PALLA7`'s leads, and `lead_get` returns `clientCode: PALLA7` to
        them. Deals ARE scoped (25 of 169) because `Ticket` has
        `ASSIGNED_USER_ID`; leads are not.

        No `bp*` page calls `owners/*`, so only a tool like `lead_search` makes
        this reachable — which makes this gate the control, not a preference.
        Anyone tempted to widen `OWNER_LEVEL_TYPES` should read the router
        docstring first.
        """
        self.assertNotIn("CUSTOMER", OWNER_LEVEL_TYPES)
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(require_leadzump_auth_context(_auth(level="CUSTOMER")))
        self.assertEqual(caught.exception.status_code, 403)

    def test_appbuilder_gate_is_untouched(self):
        """`ALLOWED_AI_APPS` is the AppBuilder agent's gate and stays as it is.

        Adding `leadzump` there would hand CRM users a tool that authors
        storages, pages and schemas. This agent has its own list.
        """
        from app.services.security import ALLOWED_AI_APPS

        self.assertEqual(ALLOWED_AI_APPS, {"sitezump", "appbuilder"})
        self.assertNotIn("leadzump", ALLOWED_AI_APPS)
        self.assertEqual(ALLOWED_LEADZUMP_APPS, {"leadzump"})
        self.assertNotIn("CUSTOMER", OWNER_LEVEL_TYPES)


# ── construction and registry ──────────────────────────────────────────────
class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = LeadZumpAgent.get_instance()

    def test_singleton_named_leadzump(self):
        self.assertIs(LeadZumpAgent.get_instance(), self.agent)
        self.assertEqual(self.agent.name, "leadzump")

    def test_tool_names_unique_and_registered(self):
        names = [t.name for t in ALL_TOOLS]
        self.assertEqual(len(names), len(set(names)), f"duplicates: {names}")
        for name in names:
            self.assertIn(name, self.agent.tools)

    def test_every_tool_documented(self):
        for tool in ALL_TOOLS:
            self.assertTrue(tool.description.strip(), f"{tool.name}: empty description")
            for param in tool.parameters:
                self.assertTrue(
                    param.description.strip(), f"{tool.name}.{param.name}: empty description"
                )

    def test_every_mutating_tool_confirms(self):
        """A write must pause for the user, and must be declared as doing so.

        `BaseAgent` lints the second half at construction; asserting it here
        turns a log warning into a failing test.
        """
        self.assertEqual(self.agent.CONFIRMATION_TOOLS, MUTATING_TOOLS)
        for name in MUTATING_TOOLS:
            tool = _tool(name)
            self.assertEqual(tool.kind, "elicitation", f"{name} must be kind='elicitation'")
            self.assertEqual(tool.elicit_mode, "blocking", f"{name} must be elicit_mode='blocking'")

    def test_no_tool_accepts_a_tenant_argument(self):
        """Tenancy comes from the token, never from an argument.

        `ProcessorAccess.of(ca, …)` derives appCode and clientCode from the
        caller's own security context. A tool taking either would let a model
        ask for a tenant it was not signed in to.
        """
        forbidden = {"client_code", "clientcode", "app_code", "appcode", "client_id", "tenant"}
        for tool in ALL_TOOLS:
            for param in tool.parameters:
                self.assertNotIn(
                    param.name.lower().replace("-", "_"),
                    forbidden,
                    f"{tool.name} exposes tenant argument {param.name!r}",
                )


# ── (c) the epoch-second wire contract ─────────────────────────────────────
class TimeContractTests(unittest.TestCase):
    def test_bare_date_is_utc_midnight(self):
        self.assertEqual(wire.to_epoch("2026-09-05"), 1788566400)

    def test_z_suffix_and_explicit_offset_agree(self):
        self.assertEqual(
            wire.to_epoch("2026-09-05T05:30:00Z"),
            wire.to_epoch("2026-09-05T11:00:00+05:30"),
        )

    def test_naive_datetime_is_read_as_utc(self):
        self.assertEqual(wire.to_epoch("2026-09-05T00:00:00"), wire.to_epoch("2026-09-05"))

    def test_epoch_passes_through(self):
        self.assertEqual(wire.to_epoch(1788566400), 1788566400)
        self.assertEqual(wire.to_epoch("1788566400"), 1788566400)

    def test_unparseable_raises_rather_than_matching_everything(self):
        with self.assertRaises(ValueError):
            wire.to_epoch("last tuesday")

    def test_from_epoch_renders_utc_iso(self):
        self.assertEqual(wire.from_epoch(1788566400), "2026-09-05T00:00:00Z")

    def test_from_epoch_leaves_non_numbers_alone(self):
        self.assertEqual(wire.from_epoch("Ravi Kumar"), "Ravi Kumar")
        self.assertEqual(wire.from_epoch(None), None)
        self.assertEqual(wire.from_epoch(True), True)


# ── the condition tree ─────────────────────────────────────────────────────
class ConditionTreeTests(unittest.TestCase):
    def test_free_text_becomes_a_substring_or_over_three_fields(self):
        """STRING_LOOSE_EQUAL, not LIKE.

        The backend renders LIKE as the raw pattern and STRING_LOOSE_EQUAL as
        `LIKE '%value%'`. A model will not write the wildcards.
        """
        condition = _lead_condition({"q": "Ravi"})
        self.assertEqual(condition["operator"], "OR")
        fields = {c["field"] for c in condition["conditions"]}
        self.assertEqual(fields, {"name", "phoneNumber", "email"})
        for clause in condition["conditions"]:
            self.assertEqual(clause["operator"], "STRING_LOOSE_EQUAL")
            self.assertEqual(clause["value"], "Ravi")

    def test_multiple_filters_are_anded(self):
        condition = _deal_condition({"product_id": 12, "source": "Meta"})
        self.assertEqual(condition["operator"], "AND")
        self.assertEqual(len(condition["conditions"]), 2)

    def test_single_filter_is_not_wrapped(self):
        condition = _deal_condition({"product_id": 12})
        self.assertEqual(condition, {"field": "productId", "operator": "EQUALS", "value": 12})

    def test_no_filters_means_no_condition(self):
        self.assertIsNone(_deal_condition({}))

    def test_date_bounds_become_epoch_comparisons(self):
        condition = _deal_condition(
            {"created_after": "2026-09-01", "created_before": "2026-09-05"}
        )
        by_operator = {c["operator"]: c for c in condition["conditions"]}
        self.assertEqual(by_operator["GREATER_THAN_EQUAL"]["value"], wire.to_epoch("2026-09-01"))
        self.assertEqual(by_operator["LESS_THAN_EQUAL"]["value"], wire.to_epoch("2026-09-05"))
        for clause in condition["conditions"]:
            self.assertIsInstance(clause["value"], int, "createdAt filters must be epoch seconds")

    def test_dnc_false_uses_is_false_not_a_value(self):
        condition = _deal_condition({"dnc": False})
        self.assertEqual(condition["operator"], "IS_FALSE")
        self.assertNotIn("value", condition)

    def test_bad_date_surfaces_as_a_tool_error_not_a_crash(self):
        fake = FakeSaasRequests()
        result = _run("deal_search", {"created_after": "yesterday"}, fake)
        self.assertFalse(result.success)
        self.assertIn("ISO-8601", result.error)
        self.assertEqual(fake.calls, [], "a bad filter must not reach the network")


# ── (b) read-modify-write ──────────────────────────────────────────────────
STORED_DEAL = {
    "id": 501,
    "code": DEAL_CODE,
    "name": "Ravi Kumar - Skyline",
    "email": "ravi@example.com",
    "phoneNumber": "9876543210",
    "dialCode": 91,
    "assignedUserId": 77,
    "subSource": "Instagram",
    "tag": "hot",
    "stage": 3,
    "status": 9,
    "productId": 12,
    "active": True,
    "version": 4,
}

STORED_LEAD = {
    "id": 301,
    "code": LEAD_CODE,
    "name": "Ravi Kumar",
    "email": "ravi@example.com",
    "phoneNumber": "9876543210",
    "dialCode": 91,
    "source": "Meta",
    "subSource": "Instagram",
    "active": True,
    "version": 2,
}


class ReadModifyWriteTests(unittest.TestCase):
    """The single most damaging bug this package could ship.

    `updatableEntity` assigns the whitelisted fields from the request body with
    no null check, so a PUT carrying only the changed field erases the rest and
    answers 200. The agent would then report a successful edit over a record it
    had just blanked.
    """

    def test_deal_update_fetches_before_writing(self):
        fake = FakeSaasRequests(
            {
                ("GET", f"/tickets/code/{DEAL_CODE}"): STORED_DEAL,
                ("PUT", f"/tickets/code/{DEAL_CODE}"): {**STORED_DEAL, "tag": "warm"},
            }
        )
        result = _run("deal_update", {"code": DEAL_CODE, "tag": "warm"}, fake)
        self.assertTrue(result.success, result.error)
        self.assertEqual(fake.calls[0]["method"], "GET", "must read the stored row first")

    def test_deal_update_preserves_the_fields_it_did_not_change(self):
        fake = FakeSaasRequests(
            {
                ("GET", f"/tickets/code/{DEAL_CODE}"): STORED_DEAL,
                ("PUT", f"/tickets/code/{DEAL_CODE}"): STORED_DEAL,
            }
        )
        _run("deal_update", {"code": DEAL_CODE, "tag": "warm"}, fake)
        body = fake.call("PUT", f"/tickets/code/{DEAL_CODE}")["json"]
        self.assertEqual(body["tag"], "warm")
        for field in ("email", "assignedUserId", "subSource", "phoneNumber", "active", "version"):
            self.assertEqual(
                body[field],
                STORED_DEAL[field],
                f"{field} was dropped from the PUT body and would be blanked server-side",
            )

    def test_lead_update_preserves_the_fields_it_did_not_change(self):
        fake = FakeSaasRequests(
            {
                ("GET", f"/owners/code/{LEAD_CODE}"): STORED_LEAD,
                ("PUT", f"/owners/code/{LEAD_CODE}"): STORED_LEAD,
            }
        )
        _run("lead_update", {"code": LEAD_CODE, "name": "Ravi K."}, fake)
        body = fake.call("PUT", f"/owners/code/{LEAD_CODE}")["json"]
        self.assertEqual(body["name"], "Ravi K.")
        for field in ("email", "phoneNumber", "dialCode", "active"):
            self.assertEqual(body[field], STORED_LEAD[field], f"{field} would be blanked")

    def test_deal_update_does_not_move_the_stage(self):
        """The stage rides along unchanged, so `applyStageStatus` short-circuits.

        Moving a deal fires messaging rules and conversion events. It belongs
        to `deal_move_stage`, behind its own confirmation prompt.
        """
        tool = _tool("deal_update")
        names = {p.name for p in tool.parameters}
        self.assertNotIn("stage_id", names)
        self.assertNotIn("status_id", names)

        fake = FakeSaasRequests(
            {
                ("GET", f"/tickets/code/{DEAL_CODE}"): STORED_DEAL,
                ("PUT", f"/tickets/code/{DEAL_CODE}"): STORED_DEAL,
            }
        )
        _run("deal_update", {"code": DEAL_CODE, "tag": "warm"}, fake)
        body = fake.call("PUT", f"/tickets/code/{DEAL_CODE}")["json"]
        self.assertEqual(body["stage"], STORED_DEAL["stage"])
        self.assertEqual(body["status"], STORED_DEAL["status"])

    def test_update_with_nothing_to_change_is_refused_before_any_call(self):
        fake = FakeSaasRequests()
        result = _run("deal_update", {"code": DEAL_CODE}, fake)
        self.assertFalse(result.success)
        self.assertEqual(fake.calls, [])

    def test_a_code_of_the_wrong_shape_never_reaches_the_network(self):
        fake = FakeSaasRequests()
        result = _run("deal_get", {"code": "not-a-code"}, fake)
        self.assertFalse(result.success)
        self.assertIn("22 characters", result.error)
        self.assertEqual(fake.calls, [])


class MoveStageTests(unittest.TestCase):
    """A stage move goes through the app's own server function, not raw REST.

    `PATCH /tickets/req/{id}/stage` does every platform effect but NOT the
    in-app notification, which `leadzump.updateStageStatusAndSN` raises after
    it. Taking the shorter path would make an agent-driven move invisible in
    the feed the RM actually watches.
    """

    FN = "/api/core/function/execute/leadzump/updateStageStatusAndSN"

    def _fake(self) -> FakeSaasRequests:
        return FakeSaasRequests(
            {
                ("GET", f"/tickets/code/{DEAL_CODE}/eager"): {
                    **STORED_DEAL,
                    "productId": {"id": 12, "name": "Skyline Towers"},
                    "assignedUserId": {"id": 77, "firstName": "Asha", "lastName": "N"},
                },
                ("POST", self.FN): [{"name": "output", "result": {"result": True}}],
            }
        )

    def test_move_goes_through_the_notifying_server_function(self):
        fake = self._fake()
        result = _run(
            "deal_move_stage",
            {"code": DEAL_CODE, "stage_id": 5, "status_id": 11, "comment": "Site visit done"},
            fake,
        )
        self.assertTrue(result.success, result.error)
        body = fake.call("POST", self.FN)["json"]
        self.assertEqual(body["ticketId"], DEAL_CODE)
        self.assertEqual(
            body["ticketStatusRequest"],
            {"stageId": 5, "statusId": 11, "comment": "Site visit done"},
        )

    def test_move_carries_the_notification_payload(self):
        """Without productName the notification renders a blank where a name goes."""
        fake = self._fake()
        _run("deal_move_stage", {"code": DEAL_CODE, "stage_id": 5}, fake)
        body = fake.call("POST", self.FN)["json"]
        self.assertEqual(body["notificationPayLoad"]["productName"], "Skyline Towers")
        self.assertTrue(body["notificationPayLoad"]["userName"])
        self.assertEqual(body["oldAssignedUser"], 77, "the outgoing assignee must be named")

    def test_a_server_function_error_event_is_surfaced_not_swallowed(self):
        """The executor answers 200 with an `error` event — that is still a failure."""
        fake = FakeSaasRequests(
            {
                ("GET", f"/tickets/code/{DEAL_CODE}/eager"): STORED_DEAL,
                ("POST", self.FN): [{"name": "error", "result": "Stage does not belong"}],
            }
        )
        result = _run("deal_move_stage", {"code": DEAL_CODE, "stage_id": 999}, fake)
        self.assertFalse(result.success)
        self.assertIn("Stage does not belong", result.error)

    def test_move_without_a_stage_is_refused(self):
        fake = FakeSaasRequests()
        result = _run("deal_move_stage", {"code": DEAL_CODE}, fake)
        self.assertFalse(result.success)
        self.assertIn("pipeline_describe", result.error)
        self.assertEqual(fake.calls, [])


class DealCreateTests(unittest.TestCase):
    FN = "/api/core/function/execute/leadzump/createTicketAndSN"

    def test_create_goes_through_the_app_creation_path(self):
        fake = FakeSaasRequests({("POST", self.FN): [{"name": "output", "result": {"id": 9}}]})
        result = _run(
            "deal_create",
            {"name": "Ravi Kumar", "product_id": 12, "phone_number": "9876543210",
             "source": "Meta"},
            fake,
        )
        self.assertTrue(result.success, result.error)
        body = fake.call("POST", self.FN)["json"]
        ticket = body["ticketRequest"]
        self.assertEqual(ticket["name"], "Ravi Kumar")
        self.assertEqual(ticket["productId"], 12)
        self.assertEqual(ticket["phoneNumber"], {"countryCode": 91, "number": "9876543210"})
        self.assertEqual(ticket["source"], "Meta")

    def test_create_without_a_contact_is_refused_before_any_call(self):
        """A deal with neither number nor email cannot identify anyone."""
        fake = FakeSaasRequests()
        result = _run("deal_create", {"name": "Ravi", "product_id": 12}, fake)
        self.assertFalse(result.success)
        self.assertEqual(fake.calls, [])

    def test_duplicate_refusal_is_surfaced_verbatim(self):
        fake = FakeSaasRequests(
            {("POST", self.FN): [{"name": "error", "result": "A Deal already exists"}]}
        )
        result = _run(
            "deal_create", {"name": "Ravi", "product_id": 12, "phone_number": "9876543210"}, fake
        )
        self.assertFalse(result.success)
        self.assertIn("A Deal already exists", result.error)


class TaskListTests(unittest.TestCase):
    def test_mine_filters_on_created_by_not_an_assignee(self):
        """A Task has no assignee column; the shell's own overdue toaster
        filters on `createdBy`, and answering for the whole tenant instead
        would look like a working tool."""
        fake = FakeSaasRequests({("POST", "/tasks/eager/query"): _page([])})
        with mock.patch.object(SaasClient, "_request", fake):
            ctx = _context()
            ctx["auth"] = _auth()
            asyncio.run(_tool("task_list").execute({"mine": True}, ctx))
        condition = fake.call("POST", "/tasks/eager/query")["json"]["condition"]
        clauses = condition.get("conditions", [condition])
        self.assertIn(
            {"field": "createdBy", "operator": "EQUALS", "value": 42},
            clauses,
        )

    def test_open_is_the_default_status(self):
        fake = FakeSaasRequests({("POST", "/tasks/eager/query"): _page([])})
        _run("task_list", {}, fake)
        condition = fake.call("POST", "/tasks/eager/query")["json"]["condition"]
        self.assertEqual(condition, {"field": "isCompleted", "operator": "IS_FALSE"})

    def test_complete_sends_the_flag_as_a_query_param(self):
        """`completed` binds as a @RequestParam, not a body field."""
        fake = FakeSaasRequests(
            {("PUT", f"/tasks/req/{DEAL_CODE}/completed"): {"code": DEAL_CODE, "isCompleted": True}}
        )
        result = _run("task_complete", {"code": DEAL_CODE}, fake)
        self.assertTrue(result.success, result.error)
        call = fake.call("PUT", f"/tasks/req/{DEAL_CODE}/completed")
        self.assertEqual(call["params"], {"completed": "true"})
        self.assertIsNone(call["json"])


class HistoryToolTests(unittest.TestCase):
    def test_activity_takes_a_code_in_the_path(self):
        fake = FakeSaasRequests(
            {("GET", f"/activities/tickets/{DEAL_CODE}/eager"):
             _page([{"activityAction": "STAGE_CHANGE", "comment": "moved", "createdAt": 1788566400}])}
        )
        result = _run("deal_activity", {"code": DEAL_CODE}, fake)
        self.assertTrue(result.success, result.error)
        self.assertIn("STAGE_CHANGE", result.to_tool_result_content())

    def test_note_list_needs_a_numeric_id_not_a_code(self):
        """Notes carry their parent as a plain id column, unlike the write tools."""
        fake = FakeSaasRequests()
        result = _run("note_list", {}, fake)
        self.assertFalse(result.success)
        self.assertIn("numeric id", result.error)
        self.assertEqual(fake.calls, [])

    def test_note_list_filters_by_ticket_id(self):
        fake = FakeSaasRequests({("POST", "/notes/eager/query"): _page([{"code": "n" * 22, "content": "Called"}])})
        result = _run("note_list", {"deal_id": 501}, fake)
        self.assertTrue(result.success, result.error)
        condition = fake.call("POST", "/notes/eager/query")["json"]["condition"]
        self.assertEqual(condition, {"field": "ticketId", "operator": "EQUALS", "value": 501})
        self.assertIn("Called", result.to_tool_result_content())


# ── task types ─────────────────────────────────────────────────────────────
TASK_TYPES = {"content": [{"id": 1, "name": "Call"}, {"id": 2, "name": "Site visit"}]}


class TaskCreateTests(unittest.TestCase):
    def test_task_type_resolved_by_name(self):
        fake = FakeSaasRequests(
            {("GET", "/tasks/types"): TASK_TYPES, ("POST", "/tasks/req"): {"code": "t" * 22}}
        )
        result = _run(
            "task_create",
            {"deal_code": DEAL_CODE, "task_type": "site visit", "due_date": "2030-01-01"},
            fake,
        )
        self.assertTrue(result.success, result.error)
        body = fake.call("POST", "/tasks/req")["json"]
        self.assertEqual(body["taskTypeId"], 2)
        self.assertEqual(body["ticketId"], DEAL_CODE)
        self.assertEqual(body["dueDate"], wire.to_epoch("2030-01-01"))

    def test_unknown_task_type_names_the_valid_ones(self):
        fake = FakeSaasRequests({("GET", "/tasks/types"): TASK_TYPES})
        result = _run("task_create", {"deal_code": DEAL_CODE, "task_type": "Coffee"}, fake)
        self.assertFalse(result.success)
        self.assertIn("Call", result.error)
        self.assertIn("Site visit", result.error)

    def test_deal_and_lead_together_is_refused(self):
        fake = FakeSaasRequests()
        result = _run(
            "note_add",
            {"deal_code": DEAL_CODE, "lead_code": LEAD_CODE, "content": "spoke to them"},
            fake,
        )
        self.assertFalse(result.success)
        self.assertEqual(fake.calls, [])


# ── shaping results ────────────────────────────────────────────────────────
class ResultShapingTests(unittest.TestCase):
    def test_relations_are_labelled_and_times_made_readable(self):
        row = wire.slim(
            {
                "code": DEAL_CODE,
                "name": "Ravi Kumar - Skyline",
                "productId": {"id": 12, "name": "Skyline Towers"},
                "assignedUserId": {"id": 77, "firstName": "Asha", "lastName": "N"},
                "createdAt": 1788566400,
                "email": None,
            },
            ["code", "name", "productId", "assignedUserId", "createdAt", "email"],
        )
        self.assertEqual(row["productId"], "Skyline Towers")
        self.assertEqual(row["assignedUserId"], "Asha N")
        self.assertEqual(row["createdAt"], "2026-09-05T00:00:00Z")
        self.assertNotIn("email", row, "empty values are dropped rather than shown as null")

    def test_rows_are_capped_and_the_omission_is_stated(self):
        page = {
            "content": [{"code": f"c{i:021d}", "name": f"Deal {i}"} for i in range(40)],
            "totalElements": 400,
            "number": 0,
            "size": 40,
        }
        shaped = wire.slim_rows(page, ["code", "name"], max_rows=15)
        self.assertEqual(len(shaped["rows"]), 15)
        self.assertEqual(shaped["total"], 400)
        self.assertIn("15 of 40", shaped["note"])

    def test_wide_rows_are_dropped_whole_rather_than_truncated_mid_record(self):
        """A row's width is not fixed, so a row count alone does not bound the size.

        Fifteen wide deals overflow the tool's cap, and the run loop truncates
        from the end — leaving the model reading half a record with no way to
        tell. Dropping whole rows and saying so is the trade this makes.
        """
        wide = {"code": "c" * 22, "name": "Ravi Kumar - Skyline Towers Phase 2",
                "productId": "Skyline Towers Phase 2", "assignedUserId": "Thejasree Kullagalla",
                "source": "Whatsapp Business API", "latestComment": "x" * 300}
        shaped = wire.slim_rows(
            {"content": [dict(wide) for _ in range(15)], "totalElements": 1210,
             "number": 0, "size": 15},
            list(wide),
        )
        rendered = len(json.dumps(shaped["rows"]))
        self.assertLess(rendered, wire.ROW_CHAR_BUDGET + 400, "row budget not enforced")
        self.assertLess(len(shaped["rows"]), 15)
        self.assertIn("result size limit", shaped["note"])

    def test_one_oversized_row_is_still_returned(self):
        """One whole record beats none — the budget must not empty the result."""
        huge = {"code": "c" * 22, "latestComment": "x" * 20000}
        shaped = wire.slim_rows({"content": [huge], "totalElements": 1}, list(huge))
        self.assertEqual(len(shaped["rows"]), 1)

    def test_unexpected_shape_does_not_explode(self):
        self.assertEqual(wire.slim_rows("nope", ["code"])["rows"], [])


class ModelVisibilityTests(unittest.TestCase):
    """The model must actually receive the rows, not just a headline.

    `ToolResult.to_tool_result_content` renders `summary` INSTEAD of `data` —
    `data` is only the fallback when no summary is set. A read tool that pairs a
    one-line summary with a page of rows hands the model the line and drops the
    rows. That shipped once: the first live run answered "the breakdown tool is
    only returning headline counts", three times, and refused to quote numbers
    it could not see. These tests are why it cannot happen again.
    """

    def test_ok_puts_the_payload_where_the_model_reads_it(self):
        result = wire.ok({"rows": [{"name": "Skyline Towers", "counts": {"Fresh": 91}}]}, "1 group")
        seen = result.to_tool_result_content()
        self.assertIn("1 group", seen)
        self.assertIn("Skyline Towers", seen)
        self.assertIn("91", seen)

    def test_every_read_tool_hands_the_model_its_rows(self):
        """Drive each read tool over a canned backend and assert a value survives."""
        probes = [
            ("deal_search", {}, ("/tickets/eager/query", "POST"), _page([STORED_DEAL]), DEAL_CODE),
            ("lead_search", {}, ("/owners/eager/query", "POST"), _page([STORED_LEAD]), LEAD_CODE),
            ("deal_get", {"code": DEAL_CODE}, (f"/tickets/code/{DEAL_CODE}/eager", "GET"),
             STORED_DEAL, "Ravi Kumar - Skyline"),
            ("lead_get", {"code": LEAD_CODE}, (f"/owners/code/{LEAD_CODE}/eager", "GET"),
             STORED_LEAD, "Ravi Kumar"),
            ("product_list", {}, ("/products", "GET"),
             _page([{"id": 12, "name": "Skyline Towers", "productTemplateId": 4}]), "Skyline Towers"),
            ("stage_counts", {}, ("/stage-counts/products", "POST"),
             _page([{"id": 12, "name": "Skyline Towers",
                     "perCount": [{"id": "Fresh", "value": {"count": 91}}]}]), "91"),
        ]
        for name, params, (suffix, method), payload, needle in probes:
            with self.subTest(tool=name):
                fake = FakeSaasRequests({(method, suffix): payload})
                result = _run(name, params, fake)
                self.assertTrue(result.success, f"{name}: {result.error}")
                seen = result.to_tool_result_content()
                self.assertIn(
                    needle,
                    seen,
                    f"{name} gives the model a headline without its data:\n{seen[:300]}",
                )

    def test_pipeline_describe_hands_the_model_its_stages(self):
        stages = [{"parent": {"id": 3154, "name": "Fresh", "order": 1},
                   "child": [{"id": 3155, "name": "Open", "order": 1}]}]
        fake = FakeSaasRequests(
            {("GET", "/products/12"): {"id": 12, "productTemplateId": 4},
             ("GET", "/stages/values/ordered"): stages}
        )
        result = _run("pipeline_describe", {"product_id": 12}, fake)
        self.assertTrue(result.success, result.error)
        seen = result.to_tool_result_content()
        self.assertIn("Fresh", seen)
        self.assertIn("3154", seen)

    def test_a_read_is_not_silently_truncated_at_the_default_cap(self):
        """A full page of deals must fit under the cap it declares."""
        fake = FakeSaasRequests(
            {("POST", "/tickets/eager/query"): _page([STORED_DEAL] * 15, total=1210)}
        )
        result = _run("deal_search", {"size": 15}, fake)
        seen = result.to_tool_result_content()
        self.assertNotIn("truncated", seen, f"deal_search overflows its cap ({len(seen)} chars)")


# ── confirmation prompts ───────────────────────────────────────────────────
class ConfirmationMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = LeadZumpAgent.get_instance()

    def _message(self, tool_name: str, args: dict[str, Any]) -> str:
        return self.agent._build_confirmation_message(tool_name, "Display", args)

    def test_stage_move_states_the_outward_effects(self):
        """Approving a consequence you were not shown is not consent."""
        message = self._message("deal_move_stage", {"code": DEAL_CODE, "stage_id": 5})
        self.assertIn(DEAL_CODE, message)
        self.assertIn("WhatsApp", message)
        self.assertIn("conversion", message)

    def test_update_names_the_record_and_the_fields(self):
        message = self._message("deal_update", {"code": DEAL_CODE, "tag": "warm", "email": "a@b.c"})
        self.assertIn(DEAL_CODE, message)
        self.assertIn("tag", message)
        self.assertIn("email", message)

    def test_note_shows_what_will_be_written(self):
        message = self._message("note_add", {"deal_code": DEAL_CODE, "content": "Called, no answer"})
        self.assertIn("Called, no answer", message)
        self.assertIn("whole team", message)

    def test_every_confirmable_tool_has_a_domain_message(self):
        for name in MUTATING_TOOLS:
            message = self._message(name, {"code": DEAL_CODE})
            self.assertFalse(
                message.startswith("Confirm: "),
                f"{name} falls back to the generic framework prompt",
            )


# ── reused org tools, and the config band ──────────────────────────────────
class OrgReuseTests(unittest.TestCase):
    """Tools borrowed from AppBuilder, adapted without disturbing AppBuilder."""

    def test_appbuilder_originals_are_not_mutated(self):
        """These are the same objects AppBuilder's registry holds.

        Setting `kind` or editing `parameters` in place would silently change
        AppBuilder's own behaviour — start confirming its user admin, or drop a
        parameter its developers rely on.
        """
        from app.agents.appbuilder.tools.modlix.security import (
            assign_role_tool,
            list_roles_tool,
            list_users_tool,
        )

        self.assertEqual(assign_role_tool.kind, "tool")
        self.assertIn("client_code", [p.name for p in list_users_tool.parameters])
        self.assertIn("app_code", [p.name for p in list_roles_tool.parameters])

    def test_org_writes_are_confirmable_copies(self):
        from app.agents.leadzump.tools.org import ORG_MUTATING, ORG_TOOLS

        by_name = {t.name: t for t in ORG_TOOLS}
        for name in ORG_MUTATING:
            self.assertIn(name, by_name)
            self.assertEqual(by_name[name].kind, "elicitation", name)
            self.assertEqual(by_name[name].elicit_mode, "blocking", name)

    def test_privilege_authoring_tools_are_not_imported(self):
        """Granting an existing profile is team admin; inventing one is not."""
        names = {t.name for t in ALL_TOOLS}
        for excluded in (
            "create_role", "create_profile", "build_authority", "list_clients",
            "grant_app_access", "apply_transport_by_code", "export_security_app",
            "configure_app_for_customer_signup", "set_app_property", "verify_token",
            "get_client_by_code",
        ):
            self.assertNotIn(excluded, names, f"{excluded} should not be in this agent")

    def test_no_app_authoring_tools_leaked_in(self):
        """A CRM assistant must not be able to rewrite the app it runs inside."""
        names = {t.name for t in ALL_TOOLS}
        for excluded in (
            "create_page", "add_component", "delete_page", "create_theme",
            "delete_app", "create_storage", "delete_storage", "replace_page_definition",
        ):
            self.assertNotIn(excluded, names)

    def test_list_profiles_is_pinned_to_the_callers_app(self):
        """`list_profiles` needs a numeric app id no CRM user could know.

        It differs per environment (270 on local), so it is resolved from the
        app code the caller is signed in to rather than asked of the model.
        """
        from app.agents.leadzump.tools.org import ORG_TOOLS

        tool = next(t for t in ORG_TOOLS if t.name == "list_profiles")
        self.assertNotIn("app_id", [p.name for p in tool.parameters])

        fake = FakeSaasRequests(
            {("GET", "/api/security/applications"): _page([{"id": 270, "appCode": "leadzump"}]),
             ("GET", "/api/security/app/270/profiles"): _page([{"id": 9, "name": "Sales Member"}])}
        )
        ctx = _context()
        ctx["auth"] = _auth()
        with mock.patch.object(SaasClient, "_request", fake):
            result = asyncio.run(tool.execute({}, ctx))
        self.assertTrue(result.success, result.error)
        self.assertEqual(
            fake.call("GET", "/api/security/applications")["params"].get("appCode"), "leadzump"
        )
        fake.call("GET", "/api/security/app/270/profiles")
        self.assertEqual(ctx["session_context"]["app_id"], 270, "app id must be cached")

    def test_user_profiles_answers_what_get_user_cannot(self):
        """The gap a live conversation found: no assignment on the user DTO.

        The read exists at `users/{id}/app/{appId}/assignedProfiles`, and
        without it `assign_profile` is a write nothing can verify.
        """
        fake = FakeSaasRequests(
            {("GET", "/api/security/applications"): _page([{"id": 270}]),
             ("GET", "/api/security/users/110/app/270/assignedProfiles"): [
                 {"id": 22, "name": "LeadZump Admin", "description": "d",
                  "arrangement": {"x": {"roleId": 1, "subArrangements": {"y": {"roleId": 64}}}}}
             ]}
        )
        ctx = _context()
        ctx["auth"] = _auth()
        with mock.patch.object(SaasClient, "_request", fake):
            result = asyncio.run(_tool("user_profiles").execute({"user_id": "110"}, ctx))
        self.assertTrue(result.success, result.error)
        seen = result.to_tool_result_content()
        self.assertIn("LeadZump Admin", seen)
        self.assertNotIn(
            "arrangement", seen, "the nested role tree must be slimmed away, not forwarded"
        )
        self.assertNotIn("roleId", seen)

    def test_no_profiles_is_reported_as_no_access(self):
        fake = FakeSaasRequests(
            {("GET", "/api/security/applications"): _page([{"id": 270}]),
             ("GET", "/api/security/users/8/app/270/assignedProfiles"): []}
        )
        ctx = _context()
        ctx["auth"] = _auth()
        with mock.patch.object(SaasClient, "_request", fake):
            result = asyncio.run(_tool("user_profiles").execute({"user_id": "8"}, ctx))
        self.assertTrue(result.success, result.error)
        self.assertIn("no access", result.summary)

    def test_remove_profile_is_a_mutating_get(self):
        """The platform exposes it as GET, so nothing may assume GETs are safe."""
        fake = FakeSaasRequests({("GET", "/api/security/users/110/removeProfile/22"): True})
        result = _run("remove_profile", {"user_id": "110", "profile_id": "22"}, fake)
        self.assertTrue(result.success, result.error)
        fake.call("GET", "/api/security/users/110/removeProfile/22")
        self.assertIn("remove_profile", MUTATING_TOOLS)

    def test_role_tools_are_excluded_because_the_api_refuses_them(self):
        """A LeadZump admin holds Profile_READ but NOT Role_READ.

        `/api/security/rolev2` answers 403 for the people who would use this
        panel, so three tools that always fail were removed rather than left to
        spend prompt budget. Profiles are what LeadZump assigns.
        """
        names = {t.name for t in ALL_TOOLS}
        for excluded in ("list_roles", "assign_role", "remove_role"):
            self.assertNotIn(excluded, names)
        self.assertIn("assign_profile", names)
        self.assertIn("list_profiles", names)


class ConfigBandTests(unittest.TestCase):
    def test_source_add_reads_the_tree_before_replacing_it(self):
        """`POST sources` deletes every source absent from the payload.

        `SourceService.upsertTree` ends in
        `deleteByAppAndClientExcludingIds(...)`. Posting only the new source
        would leave the tenant with one source and orphan every existing deal's
        source value, and answer 200 doing it.
        """
        existing = [
            {"id": 1, "name": "Meta", "active": True, "children": [{"id": 2, "name": "Instagram"}]},
            {"id": 3, "name": "Walk-in", "active": True, "children": []},
        ]
        fake = FakeSaasRequests(
            {("GET", "/sources"): existing, ("POST", "/sources"): existing}
        )
        result = _run("source_add", {"name": "Property Expo"}, fake)
        self.assertTrue(result.success, result.error)

        self.assertEqual(fake.calls[0]["method"], "GET", "must read the tree first")
        posted = fake.call("POST", "/sources")["json"]
        names = [s.get("name") for s in posted]
        self.assertIn("Property Expo", names)
        self.assertIn("Meta", names)
        self.assertIn("Walk-in", names)
        self.assertEqual(len(posted), 3)

    def test_source_add_nests_under_a_parent(self):
        existing = [{"id": 1, "name": "Meta", "active": True, "children": []}]
        fake = FakeSaasRequests({("GET", "/sources"): existing, ("POST", "/sources"): existing})
        _run("source_add", {"name": "WhatsApp", "parent_source": "meta"}, fake)
        posted = fake.call("POST", "/sources")["json"]
        self.assertEqual([c["name"] for c in posted[0]["children"]], ["WhatsApp"])

    def test_source_add_refuses_rather_than_wiping_when_the_read_fails(self):
        """If the tree cannot be read, posting would delete the lot."""
        fake = FakeSaasRequests(
            {("GET", "/sources"): ToolResult(success=False, error="HTTP 503")}
        )
        result = _run("source_add", {"name": "Property Expo"}, fake)
        self.assertFalse(result.success)
        self.assertIn("NOT", result.error)
        self.assertEqual(
            [c for c in fake.calls if c["method"] == "POST"], [], "must not post blind"
        )

    def test_source_add_rejects_a_duplicate(self):
        existing = [{"id": 1, "name": "Meta", "active": True, "children": []}]
        fake = FakeSaasRequests({("GET", "/sources"): existing})
        result = _run("source_add", {"name": "meta"}, fake)
        self.assertFalse(result.success)
        self.assertIn("already exists", result.error)

    def test_source_add_names_the_options_for_an_unknown_parent(self):
        existing = [{"id": 1, "name": "Meta", "active": True, "children": []}]
        fake = FakeSaasRequests({("GET", "/sources"): existing})
        result = _run("source_add", {"name": "X", "parent_source": "Facebook"}, fake)
        self.assertFalse(result.success)
        self.assertIn("Meta", result.error)

    def test_product_update_is_read_modify_write(self):
        """`ProductService.updatableEntity` re-assigns the whitelist unconditionally."""
        stored = {
            "id": 12, "code": "p" * 22, "name": "Skyline", "productTemplateId": 4,
            "forPartner": True, "whatsappSessionCode": "wa-1",
            "logoFileDetail": {"name": "logo.png"}, "active": True, "version": 3,
        }
        fake = FakeSaasRequests(
            {("GET", f"/products/code/{'p' * 22}"): stored,
             ("PUT", f"/products/code/{'p' * 22}"): stored}
        )
        result = _run("product_update", {"code": "p" * 22, "name": "Skyline Towers"}, fake)
        self.assertTrue(result.success, result.error)
        self.assertEqual(fake.calls[0]["method"], "GET")
        body = fake.call("PUT", f"/products/code/{'p' * 22}")["json"]
        self.assertEqual(body["name"], "Skyline Towers")
        for field in ("whatsappSessionCode", "logoFileDetail", "forPartner", "productTemplateId"):
            self.assertEqual(body[field], stored[field], f"{field} would be blanked")

    def test_product_create_demands_a_template(self):
        """Without one the product has no pipeline and its deals cannot move."""
        fake = FakeSaasRequests()
        result = _run("product_create", {"name": "Skyline"}, fake)
        self.assertFalse(result.success)
        self.assertIn("pipeline", result.error)
        self.assertEqual(fake.calls, [])

    def test_stage_create_sends_parent_as_a_status(self):
        fake = FakeSaasRequests({("POST", "/stages/req"): {"id": 99}})
        result = _run(
            "stage_create",
            {"name": "Visit Done", "product_template_id": 4, "parent_stage_id": 3168},
            fake,
        )
        self.assertTrue(result.success, result.error)
        body = fake.call("POST", "/stages/req")["json"]
        self.assertEqual(body["parentId"], 3168)
        self.assertEqual(body["platform"], "PRE_QUALIFICATION")
        self.assertIn("status", result.summary)

    def test_stage_create_needs_a_template(self):
        fake = FakeSaasRequests()
        result = _run("stage_create", {"name": "Visit"}, fake)
        self.assertFalse(result.success)
        self.assertEqual(fake.calls, [])

    def test_task_type_create_defaults_to_the_deal_series(self):
        fake = FakeSaasRequests({("POST", "/tasks/types"): {"id": 7, "name": "Call"}})
        result = _run("task_type_create", {"name": "Call"}, fake)
        self.assertTrue(result.success, result.error)
        self.assertEqual(
            fake.call("POST", "/tasks/types")["json"]["contentEntitySeries"], "TICKET"
        )


# ── the turn reminder ──────────────────────────────────────────────────────
class TurnReminderTests(unittest.TestCase):
    def _reminder(self, context: dict) -> str:
        session = types.SimpleNamespace(
            context=context, messages=[], session_id="ACME_test0001", auth=None
        )
        return asyncio.run(LeadZumpAgent.get_instance().build_turn_reminder(session, 1))

    def test_reminder_carries_the_date_in_utc(self):
        text = self._reminder({})
        self.assertIn("UTC", text)
        self.assertIn("Current time:", text)

    def test_recent_records_are_surfaced(self):
        text = self._reminder({"recent_records": [f"deal_get → {DEAL_CODE}"]})
        self.assertIn(DEAL_CODE, text)

    def test_note_tool_outcome_records_the_subject(self):
        agent = LeadZumpAgent.get_instance()
        session = types.SimpleNamespace(context={}, session_id="ACME_test0001", auth=None)
        agent.note_tool_outcome(
            "deal_get", {"code": DEAL_CODE}, ToolResult(success=True), session
        )
        self.assertEqual(session.context["recent_records"], [f"deal_get → {DEAL_CODE}"])

    def test_a_failed_call_is_not_remembered(self):
        agent = LeadZumpAgent.get_instance()
        session = types.SimpleNamespace(context={}, session_id="ACME_test0001", auth=None)
        agent.note_tool_outcome(
            "deal_get", {"code": DEAL_CODE}, ToolResult(success=False, error="nope"), session
        )
        self.assertNotIn("recent_records", session.context)


if __name__ == "__main__":
    unittest.main()
