"""Page routing — A/B tests and rule-based landing pages, on one mechanism.

`properties.pageRouting` on the ui Application document maps the page name in a
URL to the page that actually renders. Because a variant of a page is just
another page, one mechanism covers both features the product sells separately:

    PERSONALIZATION   same address, a different page when the request matches
                      (campaign, referrer, country, device, signed-in)
    SPLIT             same address, one of N pages, chosen once per visitor

Two facts the tools below exist to keep straight, because getting either wrong
produces a rule that never fires and never complains:

**Routing outranks the page name.** If a route is keyed `pricing`, it decides,
whether or not a page called `pricing` exists. That is what lets a route be a
pure campaign address with no page behind it — so `route` is deliberately NOT
validated against the page list.

**Resolution is a single hop.** The page a rule selects is never itself
re-routed, so no chain can loop.

The resolver is `nocode-ui/ui-app/client/src/util/pageRouting.ts`, copied into
the SSR build so the browser and the server cannot disagree. `simulate` here is
a third reading of it (`_page_route_eval.py`) and decides nothing.

There is no patch endpoint for an app property, so every write reads the whole
Application document, edits the one subtree and PUTs it back. The same document
is edited by the appbuilder workspace's Page routing pane and by sitezump's
Page rules screen; nothing here is a private format.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from app.agents.appbuilder.tools.modlix._page_route_eval import (
    MATCH_MODES,
    OPERATORS,
    RULE_TYPES,
    SOURCES,
    consent_trap_reasons,
    describe_condition,
    explain,
    numeric,
    validate_condition,
)

_APPS_API = "/api/ui/applications"
_PAGES_API = "/api/ui/pages"
_DESC_APP_CODE = "appCode; defaults to the app this session is working in"
_DESC_ROUTE = (
    "The page name as it appears in the URL — `pricing` for /pricing. It does "
    "NOT have to be a page that exists: routing outranks the page name, so a "
    "route can be a campaign address of its own."
)


def _client_and_headers(context: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context.get("headers") or {}


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> str:
    from app.agents.appbuilder.tools._shared import resolve_app_code
    return resolve_app_code(params, context)


def _key() -> str:
    """A stable id for a rule, condition or arm.

    Alphanumeric only. These keys are addressed from the editors through
    expression brackets, and a key carrying a dot or a dash is a binding that
    resolves to nothing.
    """
    return uuid.uuid4().hex[:22]


async def _load_app(client: Any, headers: dict, app_code: str) -> tuple[dict | None, str | None]:
    """The whole ui Application document for an appCode.

    Two calls on purpose. The listing route strips `properties`, so the id it
    hands back is fetched again — reading routing off the listing row would
    report every app as having none.
    """
    listing = await client.get(_APPS_API, headers=headers, params={"appCode": app_code, "size": 1})
    if not listing.success:
        return None, listing.error
    content = (listing.data or {}).get("content", []) if isinstance(listing.data, dict) else []
    if not content:
        return None, f"no ui Application document for appCode '{app_code}'."

    detail = await client.get(f"{_APPS_API}/{content[0].get('id')}", headers=headers)
    if not detail.success:
        return None, detail.error
    return (detail.data if isinstance(detail.data, dict) else {}), None


async def _save_app(client: Any, headers: dict, doc: dict, routing: dict, message: str) -> ToolResult:
    doc.setdefault("properties", {})["pageRouting"] = routing
    doc["message"] = message
    saved = await client.put(f"{_APPS_API}/{doc.get('id')}", headers=headers, json=doc)
    if not saved.success:
        return ToolResult(success=False, error=saved.error)
    return ToolResult(success=True)


def _consent_settings(doc: dict) -> tuple[str | None, str | None]:
    """The app's consent page and the cookie it writes.

    Consent does not gate a split any more -- every visitor is drawn -- so these
    are read for one purpose only: refusing a rule that tries to reimplement the
    consent overlay as a routing destination.
    """
    props = doc.get("properties") or {}
    return props.get("consentPage"), (props.get("analytics") or {}).get("consentCookieName")


async def _page_names(client: Any, headers: dict, app_code: str) -> set[str]:
    listing = await client.get(_PAGES_API, headers=headers, params={"appCode": app_code, "size": 500})
    if not listing.success:
        return set()
    content = (listing.data or {}).get("content", []) if isinstance(listing.data, dict) else []
    return {p.get("name") for p in content if p.get("name")}


# ── reading and auditing ─────────────────────────────────────────────────────

def _audit(
    routing: dict,
    page_names: set[str],
    consent_page: str | None = None,
    consent_cookie: str | None = None,
) -> list[str]:
    """Everything in this table that will silently do nothing.

    None of these is an error the platform reports. A rule with a bad source,
    a split with one arm, a target page that was never built — each resolves to
    "no match" or "page not found" at request time, which is indistinguishable
    from nobody having set the rule up.
    """
    problems: list[str] = []

    for route_key, route in (routing or {}).items():
        where = f"/{route_key}"
        if route.get("enabled") is False:
            problems.append(f"{where}: switched off, so none of its rules run")
        rules = (route or {}).get("rules") or {}
        if not rules:
            problems.append(f"{where}: no rules, so the route does nothing")

        for rule_key, rule in rules.items():
            label = f"{where} → {rule.get('name') or rule_key}"
            rtype = rule.get("type")
            if rtype not in RULE_TYPES:
                problems.append(f"{label}: type {rtype!r} is not one the resolver knows; the rule never runs")
                continue

            for cond in ((rule.get("conditions") or {}).values()):
                why = validate_condition(cond)
                if why:
                    problems.append(f"{label}: condition ignored — {why}")

            match_mode = rule.get("conditionMatch")
            if match_mode is not None and match_mode not in MATCH_MODES:
                problems.append(f"{label}: conditionMatch {match_mode!r} is not ALL or ANY; ALL is used")

            # A consent page is never a destination. `set_page_route_rule`
            # refuses to write this shape now, so anything the audit finds was
            # written by hand in one of the editors, or by an older tool.
            for reason in consent_trap_reasons(rule, consent_page, consent_cookie)[0]:
                problems.append(f"{label}: {reason}")

            if rtype == "PERSONALIZATION":
                if not rule.get("conditions"):
                    problems.append(
                        f"{label}: a targeting rule with no conditions NEVER matches "
                        f"(deliberate — it would otherwise hijack the page)"
                    )
                if not rule.get("page"):
                    problems.append(f"{label}: names no page to show, so it is skipped even when it matches")
                elif page_names and rule["page"] not in page_names:
                    problems.append(f"{label}: points at page {rule['page']!r}, which does not exist yet")
            else:
                arms = (rule.get("variants") or {})
                usable = [v for v in arms.values() if v.get("page") and numeric(v.get("weight"), 1) > 0]
                if len(usable) < 2:
                    problems.append(
                        f"{label}: {len(usable)} arm(s) can actually be served, so this is not a test"
                    )
                for arm in arms.values():
                    if arm.get("page") and page_names and arm["page"] not in page_names:
                        problems.append(f"{label}: arm points at page {arm['page']!r}, which does not exist yet")
    return problems


def _outline(routing: dict) -> str:
    if not routing:
        return "(no page routing on this app)"
    lines: list[str] = []
    for route_key, route in routing.items():
        off = "" if route.get("enabled") is not False else "  [OFF]"
        lines.append(f"/{route_key}{off}")
        rules = sorted(
            ((route.get("rules") or {}).items()),
            key=lambda kv: numeric((kv[1] or {}).get("order"), 0),
        )
        for rule_key, rule in rules:
            roff = "" if rule.get("enabled") is not False else " [OFF]"
            kind = "A/B test" if rule.get("type") == "SPLIT" else "targeting"
            lines.append(f"  {rule.get('name') or '(unnamed)'}  ({kind}, key {rule_key}){roff}")
            conds = (rule.get("conditions") or {}).values()
            if conds:
                joiner = "any of" if rule.get("conditionMatch") == "ANY" else "all of"
                lines.append(f"    when {joiner}:")
                for cond in conds:
                    lines.append(f"      - {describe_condition(cond)}")
            if rule.get("type") == "SPLIT":
                for arm_key, arm in (rule.get("variants") or {}).items():
                    lines.append(
                        f"    arm {arm_key}: {arm.get('page') or '(no page)'} "
                        f"share {numeric(arm.get('weight'), 1):g}"
                    )
            else:
                lines.append(f"    show: {rule.get('page') or '(no page)'}")
    return "\n".join(lines)


async def _execute_get_page_routing(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_code = _resolve_app_code(params, context)
    if not app_code:
        return ToolResult(success=False, error="`app_code` is required (set in context or pass explicitly).")

    client, headers = _client_and_headers(context)
    doc, err = await _load_app(client, headers, app_code)
    if err:
        return ToolResult(success=False, error=err)

    routing = (doc.get("properties") or {}).get("pageRouting") or {}
    problems = _audit(
        routing, await _page_names(client, headers, app_code), *_consent_settings(doc)
    )

    summary = f"Page routing for '{app_code}':\n\n{_outline(routing)}"
    if problems:
        summary += "\n\nThese will silently do nothing:\n" + "\n".join(f"  - {p}" for p in problems)
    if params.get("raw"):
        summary += "\n\nRaw:\n" + json.dumps(routing, indent=2, default=str)
    return ToolResult(success=True, data={"pageRouting": routing, "problems": problems}, summary=summary)


get_page_routing_tool = ToolDefinition(
    name="get_page_routing",
    description=(
        "Read an app's page-routing table: which URLs serve a different page to "
        "some visitors, and which are running A/B tests. Also audits it and "
        "reports every rule that will silently never fire — a bad source, a "
        "targeting rule with no conditions, a one-armed test, a page that was "
        "never built. Start here before changing anything."
    ),
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="raw", type="boolean", required=False, default=False,
                      description="Also dump the raw JSON, for when you need the exact keys."),
    ],
    execute=_execute_get_page_routing,
)


# ── writing ──────────────────────────────────────────────────────────────────

def _build_conditions(raw: list[dict] | None) -> tuple[dict, list[str]]:
    conditions: dict[str, dict] = {}
    errors: list[str] = []
    for order, entry in enumerate(raw or []):
        cond = {
            "source": entry.get("source"),
            "operator": entry.get("operator"),
            "order": order,
        }
        for key, src in (("field", "field"), ("value", "value"), ("values", "values")):
            if entry.get(src) is not None:
                cond[key] = entry[src]
        if entry.get("case_sensitive") is True:
            cond["caseSensitive"] = True

        why = validate_condition(cond)
        if why:
            errors.append(f"condition {order + 1}: {why}")
            continue
        conditions[entry.get("key") or _key()] = cond
    return conditions, errors


def _build_variants(raw: list[dict] | None) -> tuple[dict, list[str]]:
    variants: dict[str, dict] = {}
    errors: list[str] = []
    for order, entry in enumerate(raw or []):
        page = entry.get("page")
        if not page:
            errors.append(f"arm {order + 1}: `page` is required — an arm with no page is never served")
            continue
        arm: dict[str, Any] = {"page": page, "order": order}
        # A real number, never a string. The resolver sums the weights, and in
        # JavaScript `0 + '2'` is `'02'` — two arms at 2 and 1 drew from a
        # total of twenty-one. The resolver coerces, but nothing should be
        # relying on that.
        #
        # Written back as an int when it is one, so the document does not gain
        # `3.0` where every other writer puts `3` and the editors' whole-number
        # fields have something to render.
        weight = numeric(entry.get("weight"), 1)
        arm["weight"] = int(weight) if weight == int(weight) else weight
        if entry.get("name"):
            arm["name"] = entry["name"]
        variants[entry.get("key") or _key()] = arm

    return variants, errors


async def _execute_set_page_route_rule(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_code = _resolve_app_code(params, context)
    if not app_code:
        return ToolResult(success=False, error="`app_code` is required (set in context or pass explicitly).")

    route = (params.get("route") or "").strip().lstrip("/")
    if not route:
        return ToolResult(success=False, error="`route` is required — the page name in the URL this applies to.")

    rule_type = params.get("rule_type")
    if rule_type not in RULE_TYPES:
        return ToolResult(success=False, error=f"`rule_type` must be one of {', '.join(RULE_TYPES)}")

    conditions, cond_errors = _build_conditions(params.get("conditions"))
    hard_errors = [e for e in cond_errors if not e.startswith("NOTE ")]

    rule: dict[str, Any] = {"type": rule_type}
    if params.get("name"):
        rule["name"] = params["name"]
    if params.get("order") is not None:
        rule["order"] = int(numeric(params["order"], 0))
    rule["enabled"] = params.get("enabled") is not False
    if conditions:
        rule["conditions"] = conditions
        rule["conditionMatch"] = params.get("condition_match") or "ALL"
        if rule["conditionMatch"] not in MATCH_MODES:
            hard_errors.append(f"`condition_match` must be ALL or ANY (got {rule['conditionMatch']!r})")

    notes: list[str] = [e[5:] for e in cond_errors if e.startswith("NOTE ")]

    if rule_type == "PERSONALIZATION":
        # Named `target_page`, not `page`: across this whole tool surface a bare
        # `page` is a pagination number, and one tool spelling it otherwise makes
        # the rule unlearnable for the model.
        if not params.get("target_page"):
            hard_errors.append("a targeting rule needs `target_page` — the page to show when it matches")
        else:
            rule["page"] = params["target_page"]
        if not conditions:
            hard_errors.append(
                "a targeting rule needs at least one condition. The resolver treats one with "
                "none as never matching, on purpose, so that a half-written rule cannot hijack "
                "the page. For a rule that applies to everyone, use a SPLIT."
            )
    else:
        variants, var_errors = _build_variants(params.get("variants"))
        hard_errors.extend(e for e in var_errors if not e.startswith("NOTE "))
        notes.extend(e[5:] for e in var_errors if e.startswith("NOTE "))
        if len(variants) < 2:
            hard_errors.append("a split needs at least two arms, each naming a page")
        rule["variants"] = variants

    if hard_errors:
        return ToolResult(success=False, error="Refusing to write a rule that could not work:\n  - "
                                               + "\n  - ".join(hard_errors))

    client, headers = _client_and_headers(context)
    doc, err = await _load_app(client, headers, app_code)
    if err:
        return ToolResult(success=False, error=err)

    # Checked after the load, not with the others, because deciding it needs the
    # app's own consent settings: which page is in the slot, and which cookie it
    # writes. A consent page is not a destination, so this is a refusal rather
    # than a warning -- the shape cannot be made to work by adjusting it.
    consent_page, consent_cookie = _consent_settings(doc)
    trap, trap_notes = consent_trap_reasons(rule, consent_page, consent_cookie)
    if trap:
        return ToolResult(
            success=False,
            error="Refusing to route to a consent page:\n  - " + "\n  - ".join(trap)
                  + "\n\nRead platform_doc_read('consent_page') before building one.",
        )
    notes.extend(trap_notes)

    routing = dict((doc.get("properties") or {}).get("pageRouting") or {})
    entry = dict(routing.get(route) or {})
    entry.setdefault("enabled", True)
    if params.get("route_enabled") is not None:
        entry["enabled"] = bool(params["route_enabled"])
    rules = dict(entry.get("rules") or {})

    rule_key = params.get("rule_key") or _key()
    replaced = rule_key in rules
    if rule.get("order") is None:
        rule["order"] = rules[rule_key].get("order", len(rules)) if replaced else len(rules)
    rules[rule_key] = rule
    entry["rules"] = rules
    routing[route] = entry

    message = params.get("message") or f"Page routing: {'updated' if replaced else 'added'} a rule on /{route}"
    saved = await _save_app(client, headers, doc, routing, message)
    if not saved.success:
        return saved

    page_names = await _page_names(client, headers, app_code)
    problems = _audit({route: entry}, page_names, *_consent_settings(doc))

    summary = (
        f"{'Replaced' if replaced else 'Added'} rule `{rule_key}` on /{route} in '{app_code}'.\n\n"
        f"{_outline({route: entry})}"
    )
    if notes:
        summary += "\n\nNotes:\n" + "\n".join(f"  - {n}" for n in notes)
    if problems:
        summary += "\n\nStill worth fixing:\n" + "\n".join(f"  - {p}" for p in problems)
    summary += (
        "\n\nThis is live for visitors now. Use simulate_page_route to check it "
        "sends the requests you expect where you expect."
    )
    return ToolResult(success=True, data={"ruleKey": rule_key, "route": route}, summary=summary)


set_page_route_rule_tool = ToolDefinition(
    name="set_page_route_rule",
    description=(
        "Add or replace ONE routing rule on ONE URL. Two kinds: PERSONALIZATION "
        "shows a different page when the request matches its conditions; SPLIT "
        "divides traffic across pages and keeps each visitor where they landed. "
        "Pass `rule_key` to replace an existing rule (from get_page_routing), "
        "omit it to add. Everything else on the app is left alone. Refuses a "
        "rule the resolver would silently ignore rather than writing it, and "
        "refuses to route to a cookie consent page at all — that belongs in "
        "`properties.consentPage`, which the platform draws OVER the real page; "
        "as a routing rule it replaces the page and traps every visitor on it."
    ),
    parameters=[
        ToolParameter(name="route", type="string", description=_DESC_ROUTE),
        ToolParameter(name="rule_type", type="string", enum=list(RULE_TYPES),
                      description="PERSONALIZATION (show a different page) or SPLIT (A/B test)"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="rule_key", type="string", required=False,
                      description="Replace this rule. Omit to add a new one."),
        ToolParameter(name="name", type="string", required=False,
                      description="What this rule is for, in words. Shown in both editors."),
        ToolParameter(name="target_page", type="string", required=False,
                      description="PERSONALIZATION only: the page to show when the rule matches."),
        ToolParameter(
            name="conditions", type="array", required=False,
            description=(
                "What must be true of the request. Each entry: {source, field, operator, "
                "value | values, case_sensitive?}. source is one of "
                f"{', '.join(SOURCES)}; QUERY/HEADER/COOKIE also need `field` (the parameter, "
                "header or cookie name — the referrer is HEADER field 'referer'). operator is one of "
                f"{', '.join(OPERATORS)}. DEVICE compares MOBILE/TABLET/DESKTOP; AUTH compares "
                "'true'/'false'; GEO compares a country code and only works when the CDN in front "
                "of the site reports one. REQUIRED for PERSONALIZATION — a rule with none never "
                "matches. Optional for SPLIT, where they decide who is in the test. Do NOT test "
                "the consent cookie: 'has not answered the cookie question' is not a routing "
                "condition, it is `properties.consentPage`, and this tool refuses it."
            ),
            items={"type": "object"},
        ),
        ToolParameter(name="condition_match", type="string", required=False, enum=list(MATCH_MODES),
                      description="ALL (default) or ANY"),
        ToolParameter(
            name="variants", type="array", required=False,
            description=(
                "SPLIT only, at least two. Each entry: {page, weight?, name?, key?}. "
                "`weight` is a relative share, not a percentage — 1 and 1 is an even split, 3 and 1 "
                "gives the first three quarters. Every visitor is drawn, whatever they answered "
                "about cookies."
            ),
            items={"type": "object"},
        ),
        ToolParameter(name="order", type="integer", required=False,
                      description="Rules are tried lowest first and the first match decides. Defaults to last."),
        ToolParameter(name="enabled", type="boolean", required=False, default=True,
                      description="False switches the rule off without deleting it."),
        ToolParameter(name="route_enabled", type="boolean", required=False,
                      description="False switches the whole URL's rules off."),
        ToolParameter(name="message", type="string", required=False, description="Commit message on the app document"),
    ],
    execute=_execute_set_page_route_rule,
)


async def _execute_delete_page_route(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_code = _resolve_app_code(params, context)
    if not app_code:
        return ToolResult(success=False, error="`app_code` is required (set in context or pass explicitly).")

    route = (params.get("route") or "").strip().lstrip("/")
    if not route:
        return ToolResult(success=False, error="`route` is required")
    rule_key = params.get("rule_key")

    client, headers = _client_and_headers(context)
    doc, err = await _load_app(client, headers, app_code)
    if err:
        return ToolResult(success=False, error=err)

    routing = dict((doc.get("properties") or {}).get("pageRouting") or {})
    if route not in routing:
        return ToolResult(success=False, error=f"'{app_code}' has no routing on /{route}")

    if rule_key:
        rules = dict((routing[route] or {}).get("rules") or {})
        if rule_key not in rules:
            return ToolResult(success=False, error=f"/{route} has no rule `{rule_key}` (get_page_routing lists them)")
        gone = rules.pop(rule_key)
        entry = dict(routing[route])
        entry["rules"] = rules
        routing[route] = entry
        what = f"rule `{rule_key}` ({gone.get('name') or gone.get('type')}) on /{route}"
    else:
        removed = routing.pop(route)
        count = len((removed or {}).get("rules") or {})
        what = f"/{route} and its {count} rule(s)"

    saved = await _save_app(client, headers, doc, routing,
                            params.get("message") or f"Page routing: removed {what}")
    if not saved.success:
        return saved
    return ToolResult(
        success=True,
        summary=f"Removed {what} from '{app_code}'. Visitors on that address now get the "
                f"page of that name, if one exists.",
    )


delete_page_route_tool = ToolDefinition(
    name="delete_page_route",
    description=(
        "Remove one routing rule, or a whole URL's rules when `rule_key` is omitted. "
        "Destructive and immediate — confirm with the user first, and read "
        "get_page_routing so you delete the rule you meant."
    ),
    parameters=[
        ToolParameter(name="route", type="string", description=_DESC_ROUTE),
        ToolParameter(name="rule_key", type="string", required=False,
                      description="Delete just this rule. Omit to delete the whole route."),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description="Commit message on the app document"),
    ],
    execute=_execute_delete_page_route,
)


# ── simulating ───────────────────────────────────────────────────────────────

async def _execute_simulate_page_route(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_code = _resolve_app_code(params, context)
    if not app_code:
        return ToolResult(success=False, error="`app_code` is required (set in context or pass explicitly).")

    client, headers = _client_and_headers(context)
    doc, err = await _load_app(client, headers, app_code)
    if err:
        return ToolResult(success=False, error=err)

    properties = doc.get("properties") or {}
    request = {
        "pageName": params.get("page_name") or "",
        "query": params.get("query") or {},
        # Header names are lower-cased here so a caller writing `Referer` gets
        # the answer the running service would give.
        "headers": {str(k).lower(): v for k, v in (params.get("headers") or {}).items()},
        "cookies": params.get("cookies") or {},
        "device": params.get("device"),
        "country": params.get("country"),
        "authenticated": params.get("authenticated"),
        "assignments": params.get("assignments") or {},
    }

    point = numeric(params.get("draw"), 0.5)
    point = min(max(point, 0.0), 0.999999)
    decision = explain(
        properties.get("pageRouting"), properties.get("defaultPage"), request, lambda: point,
    )

    lines = [
        f"Requested /{request['pageName'] or ''} on '{app_code}' → renders **{decision.page_name}**",
        "",
        "How it got there:",
        *(f"  {line}" for line in decision.trace),
    ]
    if decision.new_assignment:
        lines += [
            "",
            f"This visitor would be put into arm `{decision.new_assignment[1]}` and a "
            f"`modlix_page_variant` cookie written, so every later visit gets the same page.",
        ]
    lines += [
        "",
        f"(Simulated from the rules as stored, with the draw pinned at {point:g}. The live "
        f"decision is made by nocode-ui/ui-app/client/src/util/pageRouting.ts, which the SSR "
        f"service also runs.)",
    ]
    return ToolResult(
        success=True,
        data={
            "pageName": decision.page_name,
            "routeKey": decision.route_key,
            "ruleKey": decision.rule_key,
            "variantKey": decision.variant_key,
        },
        summary="\n".join(lines),
    )


simulate_page_route_tool = ToolDefinition(
    name="simulate_page_route",
    description=(
        "Answer 'what would this visitor see, and why' against the rules as stored. "
        "Use it after writing a rule, and whenever someone says a rule is not "
        "working — the trace names the rule that decided, or the reason each one "
        "was skipped, which is the part the platform never reports. Reads only."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", required=False,
                      description="The page name in the URL. Empty or 'index' means the app's default page."),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="query", type="object", required=False,
                      description="Query parameters, e.g. {\"utm_campaign\": \"dentists\"}"),
        ToolParameter(name="headers", type="object", required=False,
                      description="Request headers, e.g. {\"referer\": \"https://google.com/\"}"),
        ToolParameter(name="cookies", type="object", required=False, description="Cookies the visitor sends"),
        ToolParameter(name="device", type="string", required=False, enum=["MOBILE", "TABLET", "DESKTOP"],
                      description="What the user-agent would be classified as"),
        ToolParameter(name="country", type="string", required=False,
                      description="Country code the CDN would report, e.g. IN"),
        ToolParameter(name="authenticated", type="boolean", required=False,
                      description="Whether the visitor is signed in. Omit for 'not determined'."),
        ToolParameter(name="assignments", type="object", required=False,
                      description="Split arms this visitor already holds: {ruleKey: variantKey}"),
        ToolParameter(name="draw", type="number", required=False, default=0.5,
                      description="Pins the random draw in [0,1) so a split gives a repeatable answer."),
    ],
    execute=_execute_simulate_page_route,
)


TOOLS: list[ToolDefinition] = [
    get_page_routing_tool,
    set_page_route_rule_tool,
    delete_page_route_tool,
    simulate_page_route_tool,
]
