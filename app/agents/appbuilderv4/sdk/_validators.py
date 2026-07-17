"""Shape validators that run inside the SDK before persisting objects.

Wired into `modlix.pages.replace` (and any future `modlix.apps.put_ui` /
`modlix.styles.replace` / `modlix.functions.replace`). When a write fails
validation the call raises `ModlixShapeError` — the agent sees the
message inline in its `code_run` stderr and learns the actual shape rule
without the platform ever seeing a malformed PUT.

Why this lives in the SDK and not in the platform: the agent's typical
mistake (appending styleProperty UUIDs instead of replacing them) produces
HTTP 200 from the platform — it stores everything, including bloat. The
visual breakage shows up later in `compare_to_source`. Catching it
pre-PUT here gives the agent a sharp, contextual error the same turn it
makes the mistake.

Add new validators by:
  1. Writing a `validate_<thing>(definition: dict) -> list[str]` that
     returns a list of human-readable issue strings (empty = valid).
  2. Calling it from the relevant write method in `_core.py`, raising
     `ModlixShapeError` when the list is non-empty.
"""

from __future__ import annotations


class ModlixShapeError(Exception):
    """Raised by SDK write methods when an object fails shape validation
    before being PUT to the platform. The message is the full validator
    report — surface it verbatim to the agent."""


# ── helpers ──────────────────────────────────────────────────────────────


def _is_uuid_like(s: object) -> bool:
    if not isinstance(s, str) or len(s) < 32:
        return False
    return all(c in "0123456789abcdef-" for c in s)


def _component_label(key: str, comp: dict) -> str:
    name = comp.get("name") if isinstance(comp, dict) else None
    return f"{name!r}" if name else f"key={key[:8]}"


# ── page validator ───────────────────────────────────────────────────────


_MAX_STYLE_PROPS_PER_COMPONENT = 1


def _validate_style_properties(label: str, sp: object, issues: list[str]) -> None:
    if not isinstance(sp, dict):
        issues.append(f"{label}: styleProperties must be a dict (got {type(sp).__name__})")
        return
    if len(sp) > _MAX_STYLE_PROPS_PER_COMPONENT:
        issues.append(
            f"{label}: styleProperties has {len(sp)} UUID entries — "
            f"max is {_MAX_STYLE_PROPS_PER_COMPONENT}. This is bloat from "
            "appending instead of replacing. Use "
            "modlix.components.set_style(component, {{...css...}}) to write "
            "style, or modlix.components.merge_style(component, {{...}}) "
            "to update a few keys without erasing the rest."
        )
    for uid, rule in sp.items():
        if not _is_uuid_like(uid):
            issues.append(f"  styleProperty key {uid!r} must be a uuid (use modlix.uuid())")
        if not isinstance(rule, dict):
            issues.append(f"  styleProperty {uid[:8]}: must be a dict")
            continue
        res = rule.get("resolutions")
        if not isinstance(res, dict):
            issues.append(f"  styleProperty {uid[:8]}: must contain 'resolutions': {{<RES>: {{...}}}}")
            continue
        for r_name, r_rules in res.items():
            if not isinstance(r_rules, dict):
                issues.append(f"  styleProperty {uid[:8]}.resolutions.{r_name}: must be dict")
                continue
            for css_key, val in r_rules.items():
                if not isinstance(val, dict) or "value" not in val:
                    short = repr(val)[:60]
                    issues.append(
                        f"  styleProperty {uid[:8]}.{r_name}.{css_key}: "
                        f"must be {{value: X}}, got {short}"
                    )


def _validate_properties(label: str, props: object, issues: list[str]) -> None:
    if props is None:
        return
    if not isinstance(props, dict):
        issues.append(f"{label}: properties must be a dict (got {type(props).__name__})")
        return
    for prop_name, prop_val in props.items():
        if not isinstance(prop_val, dict):
            issues.append(
                f"  property {prop_name!r}: must be {{value: X}} or "
                f"{{location: {{type: 'EXPRESSION', value: X}}}}, "
                f"got {type(prop_val).__name__}"
            )
            continue
        if "value" not in prop_val and "location" not in prop_val:
            issues.append(
                f"  property {prop_name!r}: must have either 'value' or 'location'"
            )


def _validate_children(label: str, children: object, issues: list[str]) -> None:
    if children is None:
        return
    if not isinstance(children, dict):
        issues.append(
            f"{label}: children must be a dict {{childKey: True}} (got "
            f"{type(children).__name__})"
        )
        return
    for child_key, val in children.items():
        if val is not True:
            issues.append(
                f"  children[{child_key[:8]}]: value must be True (got {val!r})"
            )


def _validate_component(key: str, comp: object, issues: list[str]) -> None:
    if not isinstance(comp, dict):
        issues.append(f"component {key!r}: not a dict (got {type(comp).__name__})")
        return
    label = "component " + _component_label(key, comp)
    if not comp.get("type"):
        issues.append(f"{label}: missing 'type'")
    if not comp.get("name"):
        issues.append(f"{label}: missing 'name'")
    _validate_style_properties(label, comp.get("styleProperties") or {}, issues)
    _validate_properties(label, comp.get("properties"), issues)
    _validate_children(label, comp.get("children"), issues)


def validate_page(definition: dict) -> list[str]:
    """Inspect a page document's `componentDefinition` for shape issues.

    Returns a list of human-readable problems. Empty list = valid.

    Checks:
      - componentDefinition is a dict of {key: component}
      - each component has type, name, valid styleProperties / properties / children
      - styleProperties has at most 1 UUID entry (no bloat)
      - styleProperty value shape: {value: X}
      - properties shape: {value: X} or {location: {...}}
      - children shape: {childKey: True}
    """
    issues: list[str] = []
    if not isinstance(definition, dict):
        issues.append(f"page definition must be a dict (got {type(definition).__name__})")
        return issues
    cd = definition.get("componentDefinition")
    if cd is None:
        return issues  # page may not include cd in this write
    if not isinstance(cd, dict):
        issues.append(f"componentDefinition must be a dict (got {type(cd).__name__})")
        return issues
    for key, comp in cd.items():
        _validate_component(key, comp, issues)
    return issues


# ── app validator ────────────────────────────────────────────────────────


def validate_app_ui(definition: dict) -> list[str]:
    """Inspect a UI-side application document (the override doc PUT to
    `/api/ui/applications/<id>`)."""
    issues: list[str] = []
    if not isinstance(definition, dict):
        issues.append(f"app definition must be a dict (got {type(definition).__name__})")
        return issues
    props = definition.get("properties")
    if props is None:
        return issues
    if not isinstance(props, dict):
        issues.append(f"properties must be a dict (got {type(props).__name__})")
        return issues
    fp = props.get("fontPacks")
    if fp is not None:
        if not isinstance(fp, dict):
            issues.append("fontPacks must be a dict {uuid: {name, code}}")
        else:
            for k, v in fp.items():
                if not _is_uuid_like(k):
                    issues.append(f"fontPacks key {k!r} must be a uuid")
                if not isinstance(v, dict):
                    issues.append(f"fontPacks[{k[:8]}]: must be a dict {{name, code}}")
                    continue
                if not isinstance(v.get("name"), str):
                    issues.append(f"fontPacks[{k[:8]}]: 'name' must be a string")
                if not isinstance(v.get("code"), str):
                    issues.append(
                        f"fontPacks[{k[:8]}]: 'code' must be a string of HTML "
                        "(e.g. '<style>@font-face{{...}}</style>')"
                    )
    return issues


# ── formatter ────────────────────────────────────────────────────────────


def format_issues(label: str, issues: list[str], hint: str = "") -> str:
    """Build a single multi-line error message from a list of shape issues."""
    head = f"{label} has {len(issues)} shape issue(s); save aborted:"
    body = "\n".join(f"  - {i}" for i in issues[:25])
    trail = "" if len(issues) <= 25 else f"\n  ...and {len(issues) - 25} more"
    if hint:
        return f"{head}\n{body}{trail}\n\n{hint}"
    return f"{head}\n{body}{trail}"
