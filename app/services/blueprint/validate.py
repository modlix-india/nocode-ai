"""The single write gate, on the service side.

This is a deliberate second copy of
`nocode-ui/ui-app/client/src/components/BlueprintEditor/validate.ts`. Two
implementations of one rule set is normally a smell, and here it is the point:
three independent writers put JSON into this field — the editor, a generator and
an agent — and only the editor goes through the TypeScript one. A rule enforced
on one of three paths is not enforced.

Keep the two in step. The rules, in the order they matter:

── Rule 1 is the one that fails silently ────────────────────────────────

NO ARRAYS, at any depth. `DifferenceExtractor` on the Java side treats an array
as OPAQUE::

    if (existing.isJsonPrimitive() || existing.isJsonArray())
        return Mono.just(incoming);

So changing one item of a forty-item list makes a tenant's override carry all
forty, and the base client's later corrections to the other thirty-nine never
reach them again. Nothing anywhere reports this. Every list-shaped thing is a
uid-keyed map with an `order` integer.

── Rule 2 is about how a binding resolves ───────────────────────────────

`updateLocationForChild` builds a child's path as ``${location}.${key}`` with no
quoting, so a key holding a `.` nests instead of addressing and a key holding a
`-` is read as subtraction. Minted uids are letter-first alphanumeric.
Letter-first because `shortUUID` is base62 with the digits leading its alphabet,
so roughly one key in six would otherwise start with a digit.

── Rule 3: refuse, never truncate ───────────────────────────────────────

A silently shortened plan is worse than no plan: it reads as complete and is
wrong about what the app is meant to be.
"""

from __future__ import annotations

import json
import random
import re
import string
from dataclasses import dataclass
from typing import Any

UID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

MAX_BYTES = 256 * 1024

# Gaps of 1000 so inserting between two siblings is one write. Renumbering every
# sibling would, under the differ, be a diff of every sibling.
ORDER_GAP = 1000

#: Keys that are legitimately not uids: the fixed vocabulary of the schema.
#: Mirrors RESERVED_KEYS in validate.ts.
RESERVED_KEYS: frozenset[str] = frozenset({
    "schemaVersion", "intent", "describes", "purpose", "decisions", "plan",
    "origin", "reconciled", "declined", "title", "name", "kind", "order",
    "feature", "componentKey", "layout", "route", "contentSource", "role",
    "appType", "audience", "glossary", "brand", "features", "objects",
    "security", "delivery", "sections", "fields", "relations", "lifecycle",
    "entity", "grain", "content", "media", "variables", "palette", "tone",
    "typeScale", "motion", "profiles", "gates", "access", "status", "why",
    "who", "requires", "shape", "steps", "trigger", "contract", "failureMode",
    "event", "channels", "audienceNote", "prompt", "generatedBy", "generatedAt",
    "fromTemplate", "choice", "because", "madeBy", "at", "supersedes", "area",
    "prose", "purposeNote", "in", "out", "does", "to", "cardinality",
    "required", "path", "param", "domains", "environments", "term", "means",
})

#: Maps whose keys are minted uids and must therefore satisfy the charset.
UID_KEYED: frozenset[str] = frozenset({
    "objects", "features", "sections", "fields", "relations", "decisions",
    "glossary", "profiles", "gates", "steps", "media", "ctas",
})

_ARRAY_MESSAGE = (
    "Arrays are not allowed anywhere in a blueprint. DifferenceExtractor treats "
    "an array as opaque, so one changed item carries the whole list into a "
    "tenant override and silently detaches them from later corrections. Use a "
    "uid-keyed map with an order integer."
)


@dataclass(frozen=True)
class ValidationIssue:
    """One reason a blueprint was refused."""

    path: str       # dotted, e.g. "plan.objects.abc.order". "" is the root.
    message: str

    def render(self) -> str:
        return f"{self.path or '(root)'}: {self.message}"


def validate_blueprint(blueprint: Any) -> list[ValidationIssue]:
    """Check a blueprint before it is written.

    Returns EVERY issue rather than the first, so a malformed document produced
    by a generator can be reported whole instead of one round trip per problem.
    A generator that has to be told about forty bad keys one at a time will
    never converge.
    """
    issues: list[ValidationIssue] = []
    if blueprint is None:
        return issues

    if not isinstance(blueprint, dict):
        issues.append(ValidationIssue("", "A blueprint must be an object."))
        return issues

    _walk(blueprint, "", None, issues)

    try:
        size = len(json.dumps(blueprint))
    except (TypeError, ValueError):
        issues.append(ValidationIssue("", "A blueprint must be serialisable to JSON."))
        return issues

    if size > MAX_BYTES:
        issues.append(ValidationIssue("", (
            f"A blueprint is {round(size / 1024)}KB, over the "
            f"{MAX_BYTES // 1024}KB budget. Refused rather than truncated, "
            "because a silently shortened plan is worse than none."
        )))

    return issues


def _walk(node: Any, path: str, parent_key: str | None, issues: list[ValidationIssue]) -> None:
    if isinstance(node, (list, tuple, set)):
        issues.append(ValidationIssue(path, _ARRAY_MESSAGE))
        return
    if not isinstance(node, dict):
        return

    keys_are_uids = parent_key is not None and parent_key in UID_KEYED

    for key, value in node.items():
        child_path = f"{path}.{key}" if path else str(key)

        if not isinstance(key, str):
            issues.append(ValidationIssue(
                child_path, "Blueprint keys must be strings; JSON has no other kind.",
            ))
            continue

        if keys_are_uids and key not in RESERVED_KEYS and not UID_PATTERN.match(key):
            issues.append(ValidationIssue(child_path, (
                f'"{key}" is not a usable key. A binding path is built by '
                "concatenation without quoting, so a dot nests and a hyphen is "
                "read as subtraction. Keys must be letter-first and alphanumeric."
            )))

        # bool is an int in Python, and `"order": true` is not an order.
        if key == "order" and value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            issues.append(ValidationIssue(child_path, (
                "order must be an integer. Inserts use gaps of 1000 so one "
                "insert is one write rather than a diff of every sibling."
            )))

        _walk(value, child_path, key, issues)


def render_issues(issues: list[ValidationIssue], limit: int = 20) -> str:
    """Issues as one readable block, for a tool result or an HTTP detail."""
    shown = [i.render() for i in issues[:limit]]
    if len(issues) > limit:
        shown.append(f"…and {len(issues) - limit} more.")
    return "\n".join(shown)


def mint_uid() -> str:
    """A key that is safe to address: letter-first, then alphanumeric."""
    return random.choice(string.ascii_lowercase) + "".join(
        random.choice(string.ascii_lowercase + string.digits) for _ in range(7)
    )


def next_order(keyed_map: dict[str, Any] | None) -> int:
    """The next order value for a keyed map."""
    values = list((keyed_map or {}).values())
    if not values:
        return ORDER_GAP
    highest = 0
    for v in values:
        order = v.get("order") if isinstance(v, dict) else None
        if isinstance(order, int) and not isinstance(order, bool):
            highest = max(highest, order)
    return highest + ORDER_GAP


def coerce_lists(node: Any) -> Any:
    """Turn any list into a uid-keyed map with `order`, recursively.

    A repair, not a validation. A model told twenty times not to emit arrays
    still emits them, and refusing a whole plan over a shape that converts
    mechanically wastes the generation. So generated output is coerced and then
    validated; a blueprint arriving from an agent's `blueprint_set` is only
    validated, because there the array is a bug in the caller that should be
    reported rather than papered over.
    """
    if isinstance(node, (list, tuple)):
        out: dict[str, Any] = {}
        for index, item in enumerate(node):
            entry = coerce_lists(item)
            if not isinstance(entry, dict):
                entry = {"value": entry}
            entry.setdefault("order", (index + 1) * ORDER_GAP)
            out[mint_uid()] = entry
        return out
    if isinstance(node, dict):
        return {k: coerce_lists(v) for k, v in node.items()}
    return node
