"""A Python reading of the page-routing resolver, for explaining rules.

## What this is, and what it is not

The real resolver is
`nocode-ui/ui-app/client/src/util/pageRouting.ts`. That file is the contract:
it is copied verbatim into the SSR build, so the browser and the SSR service
run the same code and cannot disagree about which page a request gets. This
module is a third reading of the same rules, and it decides nothing — it exists
so a tool can ANSWER a question ("why does my campaign rule never fire?")
without a browser, a deploy, or a guess.

**If you change `pageRouting.ts`, change this.** The two can drift, and a
simulation that is confidently wrong is worse than no simulation, so
`explain()` always names the source of truth in its output and
`tests/test_page_routing_eval.py` walks the same cases the jest suite does.

## Why it traces

The failures this feature produces are all silent. A rule whose condition names
a header the edge does not send does not error, it simply never matches, which
looks exactly like a rule nobody wrote. A personalization rule with no
conditions never matches either — deliberately, so a half-written rule cannot
hijack a page — and that is not something anyone guesses. So every rule
considered leaves a line saying what happened to it, and the lines are the
answer rather than a debugging aid.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

# Mirrors `pageRouting.ts`. A source or operator outside these sets is not a
# validation nicety: the resolver's switch falls through to "no match", so a
# rule carrying one never fires and never complains.
SOURCES = ("QUERY", "HEADER", "COOKIE", "DEVICE", "AUTH", "GEO")
OPERATORS = (
    "EQUALS", "NOT_EQUALS", "CONTAINS", "NOT_CONTAINS", "STARTS_WITH",
    "ENDS_WITH", "MATCHES", "IN", "NOT_IN", "EXISTS", "NOT_EXISTS",
)
NEEDS_FIELD = ("QUERY", "HEADER", "COOKIE")
RULE_TYPES = ("PERSONALIZATION", "SPLIT")
MATCH_MODES = ("ALL", "ANY")

MAX_PATTERN_LENGTH = 512

ASSIGNMENT_COOKIE = "modlix_page_variant"
QUERY_COOKIE = "modlix_route_query"


DEFAULT_CONSENT_COOKIE_NAME = "modlix_analytics_consent"


def consent_trap_reasons(
    rule: dict,
    consent_page: str | None = None,
    consent_cookie: str | None = None,
) -> tuple[list[str], list[str]]:
    """Why a rule must not be written, and what is merely worth saying.

    A consent page is NEVER a routing destination. The platform renders
    `properties.consentPage` as an OVERLAY on top of whatever page the visitor
    asked for; a routing rule REPLACES that page instead, and the replacement is
    a trap, because the only way back out is for the consent page to set the
    very cookie the rule is testing. Get one button's `onClick` wrong -- it
    takes the event KEY, not its name -- and every visitor is stuck on the
    consent page for ever, with nothing logged anywhere.

    Seen live on `crumbco` in 2026-09: every request for /home was answered with
    the consent card, Accept all was inert, and the A/B test underneath it had
    never once run.

    Returns (refusals, notes).
    """
    targets = [rule["page"]] if rule.get("page") else []
    targets += [arm["page"] for arm in (rule.get("variants") or {}).values() if arm.get("page")]

    refusals = [
        reason
        for reason in (
            _points_at_the_consent_slot(targets, consent_page),
            _tests_the_consent_cookie(rule, targets, consent_cookie),
        )
        if reason
    ]
    if refusals:
        return refusals, []

    looks_like = next((t for t in targets if "consent" in str(t).lower()), None)
    if looks_like is None:
        return [], []
    return [], [
        f"{looks_like!r} reads like a consent page. If it is one, wire it with "
        f"`properties.consentPage` rather than routing — that renders it over the real "
        f"page instead of in place of it."
    ]


def _points_at_the_consent_slot(targets: list, consent_page: str | None) -> str | None:
    slot = (consent_page or "").strip()
    if not slot:
        return None
    target = next((t for t in targets if str(t).strip().lower() == slot.lower()), None)
    if target is None:
        return None
    return (
        f"{target!r} is this app's consent page (`properties.consentPage`). The platform "
        f"already shows it over every page until the visitor answers. Routing to it "
        f"replaces the page they asked for, and they cannot get back: the rule only stops "
        f"matching once the consent page writes the cookie. Leave the slot to do its job "
        f"and route somewhere else."
    )


def _tests_the_consent_cookie(rule: dict, targets: list, consent_cookie: str | None) -> str | None:
    cookie = (consent_cookie or DEFAULT_CONSENT_COOKIE_NAME).strip().lower()
    for condition in (rule.get("conditions") or {}).values():
        if condition.get("source") != "COOKIE" or condition.get("operator") != "NOT_EXISTS":
            continue
        field = str(condition.get("field") or "").strip().lower()
        if field != cookie and "consent" not in field:
            continue
        shown = targets[0] if targets else "another page"
        return (
            f"this rule sends visitors who have NOT answered the cookie question to "
            f"{shown!r}, replacing the page they asked for. That is the platform's job, "
            f"not routing's: set `properties.consentPage` and the page is drawn as an "
            f"overlay, with the real page working underneath it. As a rule it is a trap "
            f"— the only escape is the consent page setting {condition.get('field')!r}, "
            f"so a single mis-wired button locks every visitor out of the site."
        )
    return None


@dataclass
class Decision:
    """What the resolver would do, and the reasoning that got there."""

    page_name: str
    route_key: str | None = None
    rule_key: str | None = None
    variant_key: str | None = None
    new_assignment: tuple[str, str] | None = None
    trace: list[str] = field(default_factory=list)


def numeric(value: Any, fallback: float) -> float:
    """A weight or an order as a number, whatever the document holds.

    Several writers reach this document and a text input writes a string. The
    resolver coerces for the same reason: `0 + '2'` is `'02'` in JavaScript, so
    uncoerced weights of 2 and 1 summed to twenty-one.

    Always a float, including on the fallback path. Callers do float arithmetic
    on the result, and handing back whatever the caller happened to pass as
    `fallback` made the return type depend on the argument.
    """
    if isinstance(value, bool):
        return float(fallback)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return float(fallback)
    return float(fallback)


def _ordered(mapping: dict[str, Any] | None) -> list[tuple[str, Any]]:
    """Entries lowest `order` first, ties keeping insertion order."""
    if not mapping:
        return []
    items = [(k, v) for k, v in mapping.items() if v]
    return sorted(items, key=lambda kv: numeric((kv[1] or {}).get("order"), 0))


def _fold(value: str, case_sensitive: Any) -> str:
    return value if case_sensitive is True else value.lower()


def _condition_value(condition: dict, request: dict) -> str | None:
    source = condition.get("source")
    field_name = condition.get("field")

    if source == "QUERY":
        return (request.get("query") or {}).get(field_name) if field_name else None
    if source == "HEADER":
        # Header names are case-insensitive on the wire and the caller hands us
        # a lower-cased map, so the authored field is lower-cased to match.
        return (request.get("headers") or {}).get(field_name.lower()) if field_name else None
    if source == "COOKIE":
        return (request.get("cookies") or {}).get(field_name) if field_name else None
    if source == "DEVICE":
        return request.get("device")
    if source == "GEO":
        return request.get("country")
    if source == "AUTH":
        auth = request.get("authenticated")
        # None rather than 'false' when the caller did not say, so EXISTS can
        # tell "anonymous" from "not determined".
        return None if auth is None else ("true" if auth else "false")
    return None


def _candidates(condition: dict) -> list[str]:
    values = condition.get("values")
    if values:
        return [str(v) for v in values]
    raw = condition.get("value")
    if raw is None:
        return []
    return [part.strip() for part in str(raw).split(",")]


def _matches_pattern(actual: str, condition: dict) -> bool:
    pattern = condition.get("value")
    if not pattern or len(str(pattern)) > MAX_PATTERN_LENGTH:
        return False
    try:
        flags = 0 if condition.get("caseSensitive") is True else re.IGNORECASE
        return re.search(str(pattern), actual, flags) is not None
    except re.error:
        # An unparseable pattern is an authoring mistake. It must not take the
        # page down: this runs on the request path for every visitor.
        return False


def match_condition(condition: dict, request: dict) -> bool:
    actual = _condition_value(condition, request)
    present = actual is not None and actual != ""
    operator = condition.get("operator")

    if operator == "EXISTS":
        return present
    if operator == "NOT_EXISTS":
        return not present

    if not present:
        # A missing value satisfies the negative operators and nothing else.
        return operator in ("NOT_EQUALS", "NOT_CONTAINS", "NOT_IN")

    case_sensitive = condition.get("caseSensitive")
    value = _fold(str(actual), case_sensitive)
    raw = condition.get("value")
    expected = None if raw is None else _fold(str(raw), case_sensitive)
    listed = {_fold(c, case_sensitive) for c in _candidates(condition)}

    if operator == "EQUALS":
        return expected is not None and value == expected
    if operator == "NOT_EQUALS":
        return expected is None or value != expected
    if operator == "CONTAINS":
        return expected is not None and expected in value
    if operator == "NOT_CONTAINS":
        return expected is None or expected not in value
    if operator == "STARTS_WITH":
        return expected is not None and value.startswith(expected)
    if operator == "ENDS_WITH":
        return expected is not None and value.endswith(expected)
    if operator == "MATCHES":
        return _matches_pattern(str(actual), condition)
    if operator == "IN":
        return value in listed
    if operator == "NOT_IN":
        return value not in listed
    return False


def describe_condition(condition: dict) -> str:
    """One condition as a sentence, for the trace and for listings."""
    source = condition.get("source") or "?"
    name = condition.get("field")
    what = f"{source}" + (f"[{name}]" if name else "")
    operator = (condition.get("operator") or "?").lower().replace("_", " ")
    value = condition.get("values") or condition.get("value")
    return f"{what} {operator}" + (f" {value!r}" if value is not None else "")


def _conditions_match(rule: dict, request: dict, trace: list[str], label: str) -> bool:
    conditions = _ordered(rule.get("conditions"))

    if not conditions:
        if rule.get("type") == "SPLIT":
            trace.append(f"{label}: no conditions, so everyone takes part")
            return True
        # Vacuously true under ALL semantics, which would let a half-written
        # rule hijack the page the moment it was saved.
        trace.append(
            f"{label}: SKIPPED — a targeting rule with no conditions never "
            f"matches, by design"
        )
        return False

    mode = rule.get("conditionMatch") or "ALL"
    results = [(key, cond, match_condition(cond, request)) for key, cond in conditions]
    for key, cond, ok in results:
        trace.append(
            f"{label}:   {'yes' if ok else 'no '}  {describe_condition(cond)}"
            + ("" if ok else f"  (request had {_condition_value(cond, request)!r})")
        )

    matched = any(r[2] for r in results) if mode == "ANY" else all(r[2] for r in results)
    if not matched:
        trace.append(f"{label}: SKIPPED — {mode} of its conditions did not hold")
    return matched


def _draw_variant(
    rule_key: str,
    rule: dict,
    request: dict,
    draw: Callable[[], float],
    trace: list[str],
    label: str,
) -> tuple[str, str, tuple[str, str] | None] | None:
    variants = _ordered(rule.get("variants"))
    if not variants:
        trace.append(f"{label}: SKIPPED — a test with no pages in it")
        return None

    existing = (request.get("assignments") or {}).get(rule_key)
    if existing:
        held = next((v for k, v in variants if k == existing), None)
        if held and held.get("page"):
            # Reusing it writes nothing.
            trace.append(f"{label}: visitor already in arm {existing!r}")
            return existing, held["page"], None
        trace.append(f"{label}: stored arm {existing!r} no longer exists, redrawing")


    eligible = [kv for kv in variants if kv[1].get("page") and numeric(kv[1].get("weight"), 1) > 0]
    if not eligible:
        trace.append(f"{label}: SKIPPED — no arm has both a page and a share above zero")
        return None

    total = sum(numeric(v.get("weight"), 1) for _, v in eligible)
    point = draw() * total
    for key, variant in eligible:
        point -= numeric(variant.get("weight"), 1)
        if point < 0:
            trace.append(
                f"{label}: drew arm {key!r} → {variant['page']} "
                f"(share {numeric(variant.get('weight'), 1):g} of {total:g})"
            )
            return key, variant["page"], (rule_key, key)

    key, variant = eligible[-1]
    trace.append(f"{label}: drew the last arm {key!r} → {variant['page']}")
    return key, variant["page"], (rule_key, key)


def _apply_route(
    routing: dict, route_key: str, request: dict, draw: Callable[[], float], trace: list[str]
) -> Decision | None:
    if not route_key:
        return None

    route = (routing or {}).get(route_key)
    if not route:
        trace.append(f"no rules are attached to {route_key!r}")
        return None
    if route.get("enabled") is False:
        trace.append(f"{route_key!r} is switched off")
        return None

    rules = _ordered(route.get("rules"))
    if not rules:
        trace.append(f"{route_key!r} has no rules")
        return None

    for rule_key, rule in rules:
        label = f"  rule {rule.get('name') or rule_key}"
        if rule.get("enabled") is False:
            trace.append(f"{label}: SKIPPED — switched off")
            continue
        if not _conditions_match(rule, request, trace, label):
            continue

        if rule.get("type") == "SPLIT":
            choice = _draw_variant(rule_key, rule, request, draw, trace, label)
            # A split that cannot produce an arm falls through rather than
            # ending resolution, so it does not shadow a rule beneath it.
            if not choice:
                continue
            variant_key, page, assignment = choice
            return Decision(
                page_name=page, route_key=route_key, rule_key=rule_key,
                variant_key=variant_key, new_assignment=assignment, trace=trace,
            )

        if rule.get("page"):
            trace.append(f"{label}: APPLIES → {rule['page']}")
            return Decision(page_name=rule["page"], route_key=route_key, rule_key=rule_key, trace=trace)

        trace.append(f"{label}: SKIPPED — it matched, but names no page to show")

    return None


def explain(
    routing: dict | None,
    default_page: str | None,
    request: dict,
    draw: Callable[[], float] | None = None,
) -> Decision:
    """Resolve a request and say why.

    `request` takes the same keys as `PageRouteRequest` in `pageRouting.ts`:
    `pageName`, `query`, `headers` (lower-cased names), `cookies`, `device`,
    `country`, `authenticated`, `assignments`.
    """
    draw = draw or (lambda: 0.5)
    routing = routing or {}
    trace: list[str] = []
    requested = (request.get("pageName") or "").strip()

    trace.append(f"requested page: {requested or '(none)'}")

    direct = _apply_route(routing, requested, request, draw, trace)
    if direct:
        return direct

    if (not requested or requested == "index") and default_page:
        trace.append(f"empty or 'index', so the default page {default_page!r} decides")
        via_default = _apply_route(routing, default_page, request, draw, trace)
        if via_default:
            return via_default
        trace.append(f"nothing applied → {default_page}")
        return Decision(page_name=default_page, trace=trace)

    trace.append(f"nothing applied → {requested}")
    return Decision(page_name=requested, trace=trace)


def validate_condition(condition: dict) -> str | None:
    """Why the resolver would ignore this condition, if it would."""
    source = condition.get("source")
    if source not in SOURCES:
        return f"source must be one of {', '.join(SOURCES)} (got {source!r})"

    operator = condition.get("operator")
    if operator not in OPERATORS:
        return f"operator must be one of {', '.join(OPERATORS)} (got {operator!r})"

    if source in NEEDS_FIELD and not condition.get("field"):
        return f"a {source} condition needs `field` — which parameter, header or cookie to read"

    if operator not in ("EXISTS", "NOT_EXISTS") and condition.get("value") is None and not condition.get("values"):
        return f"operator {operator} needs a `value`"

    if operator == "MATCHES":
        pattern = str(condition.get("value") or "")
        if len(pattern) > MAX_PATTERN_LENGTH:
            return f"the pattern is longer than {MAX_PATTERN_LENGTH} characters and is ignored at runtime"
        try:
            re.compile(pattern)
        except re.error as err:
            return f"the pattern does not compile ({err}); at runtime it simply never matches"
    return None
