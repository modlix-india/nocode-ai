"""Modlix platform conventions — the load-bearing grammar that every tool relies on.

Ported from modlix-mcp/modlix_mcp/conventions.py. Only change vs the source:
the lazy import in `is_multi_valued_property` points at nocode-ai's catalog
(`app.agents.appbuilder.catalog.get_catalog`) and is wrapped in try/except so
it degrades gracefully when the catalog singleton hasn't been registered yet.

Encodes:
  - Authority string grammar (Authorities.[APPCODE.]ROLE_X)
  - Kirun expression language prefixes (Steps., Arguments., Page., ...)
  - ComponentProperty wrap/unwrap (used INSIDE pages, not in app-level config)
  - ParameterReference + parameterMap construction for Kirun function steps
  - dependentStatements semantics (boolean = enabled-flag, not presence)
  - Kirun primitive catalog (System.*, System.Loop.*, etc.) and UIEngine.* (JS-only)
  - Responsive breakpoint keys for theme variables and styleProperties.resolutions
  - Component-type whitelist (rejected synonyms like 'Box', 'Container', 'Input')
  - Override/versioning predicates

Every tool that writes to the platform imports from here. When a convention here
is wrong, fix it in one place and every tool benefits.
"""

from __future__ import annotations

import re
import secrets
import uuid as _uuid
from typing import Any

# ── Authority grammar ────────────────────────────────────────────────────────
#
# Three forms (from AuthoritiesNameUtil.java in nocode-saas/security):
#   Role:        Authorities.[APPCODE.]ROLE_<RoleName>
#   Profile:     Authorities.[APPCODE.]PROFILE_<ProfileName>
#   Permission:  Authorities.[APPCODE.]<PermissionName>
# Plus the system literal Authorities.Logged_IN.
# APPCODE is uppercased; spaces in names become underscores.

_AUTH_PREFIX = "Authorities."
_AUTH_ROLE = "ROLE_"
_AUTH_PROFILE = "PROFILE_"
_AUTH_PATTERN = re.compile(
    r"^Authorities\.(?:(?P<appCode>[A-Z][A-Z0-9_]*)\.)?(?P<rest>.+)$"
)


def make_role_authority(role_name: str, app_code: str | None = None) -> str:
    """Authorities.[APPCODE.]ROLE_<RoleName> — spaces replaced with underscores."""
    parts = [_AUTH_PREFIX]
    if app_code:
        parts.append(f"{app_code.upper()}.")
    parts.append(f"{_AUTH_ROLE}{role_name.replace(' ', '_')}")
    return "".join(parts)


def make_profile_authority(profile_name: str, app_code: str | None = None) -> str:
    parts = [_AUTH_PREFIX]
    if app_code:
        parts.append(f"{app_code.upper()}.")
    parts.append(f"{_AUTH_PROFILE}{profile_name.replace(' ', '_')}")
    return "".join(parts)


def make_permission_authority(permission_name: str, app_code: str | None = None) -> str:
    parts = [_AUTH_PREFIX]
    if app_code:
        parts.append(f"{app_code.upper()}.")
    parts.append(permission_name.replace(' ', '_'))
    return "".join(parts)


def parse_authority(authority: str) -> dict[str, str | None] | None:
    """Split an authority string into {kind, app_code, name}.

    Returns None for unparseable strings. kind ∈ {'role', 'profile', 'permission', 'system'}.
    """
    if authority == "Authorities.Logged_IN":
        return {"kind": "system", "app_code": None, "name": "Logged_IN"}
    m = _AUTH_PATTERN.match(authority)
    if not m:
        return None
    rest = m.group("rest")
    app_code = m.group("appCode")
    if rest.startswith(_AUTH_ROLE):
        return {"kind": "role", "app_code": app_code, "name": rest[len(_AUTH_ROLE):]}
    if rest.startswith(_AUTH_PROFILE):
        return {"kind": "profile", "app_code": app_code, "name": rest[len(_AUTH_PROFILE):]}
    return {"kind": "permission", "app_code": app_code, "name": rest}


def validate_authority(authority: str) -> str | None:
    """Return an error message if the authority string is malformed; else None."""
    if parse_authority(authority) is None:
        return (
            f"Invalid authority '{authority}'. Expected one of: "
            "Authorities.[APPCODE.]ROLE_<Name>, "
            "Authorities.[APPCODE.]PROFILE_<Name>, "
            "Authorities.[APPCODE.]<Permission>, "
            "or Authorities.Logged_IN."
        )
    return None


# Common platform authorities, for documentation and autocomplete.
# Real list is open-ended (per-app permissions add new entries); these are the
# system-wide ones present in seed data and tests.
COMMON_AUTHORITIES: tuple[str, ...] = (
    "Authorities.Logged_IN",
    "Authorities.User_CREATE",
    "Authorities.User_READ",
    "Authorities.User_UPDATE",
    "Authorities.User_DELETE",
    "Authorities.Client_CREATE",
    "Authorities.Client_READ",
    "Authorities.Client_UPDATE",
    "Authorities.Client_DELETE",
    "Authorities.Application_CREATE",
    "Authorities.Application_READ",
    "Authorities.Application_UPDATE",
    "Authorities.Application_DELETE",
    "Authorities.Role_CREATE",
    "Authorities.Role_READ",
    "Authorities.Role_UPDATE",
    "Authorities.Role_DELETE",
    "Authorities.Profile_CREATE",
    "Authorities.Profile_READ",
    "Authorities.Profile_UPDATE",
    "Authorities.Profile_DELETE",
    "Authorities.Integration_CREATE",
    "Authorities.Integration_READ",
    "Authorities.Integration_UPDATE",
    "Authorities.Integration_DELETE",
    "Authorities.ROLE_Owner",
)


# ── Expression language ──────────────────────────────────────────────────────
#
# Kirun has a small expression language with token prefixes that map to runtime
# value extractors. See nocode-kirun/kirun-js/src/engine/runtime/expression/.
#
# IMPORTANT: not JavaScript. Operators differ:
#   =, !=, <, <=, >, >=         (single equals; no ==/===)
#   and, or, not                (NOT &&, ||, !)
#   ? :                          (ternary)
#   ??                           (null-coalescing)
#   +, -, *, /, %, //            (arithmetic; + also concatenates strings)
#   [...]                        (array/property access; dynamic keys OK)
#   ..                           (range)

EXPRESSION_PREFIXES: frozenset[str] = frozenset({
    "Steps",       # outputs of prior steps in the same function/event
    "Arguments",   # function input parameters
    "Context",     # System.Context values inside a function
    "Parent",      # iteration context inside ArrayRepeater children
    "Page",        # page-scoped state (Page.userForm.email)
    "Store",       # app-wide store
    "LocalStore",  # browser localStorage
    "Theme",       # theme variables
    "Url",         # URL params + query strings
    "Filler",      # filler state (form-like overlays)
})

_EXPRESSION_REF_RE = re.compile(
    r"\b(" + "|".join(sorted(EXPRESSION_PREFIXES, key=len, reverse=True)) + r")\.[A-Za-z_$][\w$\.]*"
)

_STEPS_REF_RE = re.compile(r"\bSteps\.([A-Za-z_$][\w$]*)")

# Component properties whose value must be an eventFunctions KEY. The browser
# runtime resolves these with a direct map lookup
# (`pageDefinition.eventFunctions[onClick]`), so a human function name silently
# resolves to undefined and the handler never fires.
EVENT_PROP_NAMES: tuple[str, ...] = (
    "onClick", "onChange", "onBlur", "onFocus", "onLoad", "onSubmit",
    "onEnter", "onSelect", "onSuccess", "onError", "onClear", "onSearch",
    "onDoubleClick", "onHover", "onScrollReachedEnd",
)


def resolve_event_prop_refs(
    page_data: dict[str, Any], properties: dict[str, Any] | None
) -> tuple[dict[str, Any], list[str]]:
    """Rewrite event props that carry a function NAME into its eventFunctions key.

    Returns (properties, notes). Writing `onClick: "handleLogin"` is the natural
    thing to do and reads correctly, but the runtime looks events up by key, so
    it produces a dead handler that no validation used to catch. Resolve it here
    so the natural form works, and report what was rewritten.
    """
    if not properties:
        return properties or {}, []
    event_fns = (page_data or {}).get("eventFunctions") or {}
    name_to_key = {
        v["name"]: k
        for k, v in event_fns.items()
        if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"]
    }
    if not name_to_key:
        return properties, []
    out = dict(properties)
    notes: list[str] = []
    for prop_name in EVENT_PROP_NAMES:
        if prop_name not in out:
            continue
        raw = out[prop_name]
        ref = raw.get("value") if isinstance(raw, dict) else raw
        if not isinstance(ref, str) or ref in event_fns or ref not in name_to_key:
            continue
        key = name_to_key[ref]
        out[prop_name] = {"value": key} if isinstance(raw, dict) else key
        notes.append(f"{prop_name}: '{ref}' -> key '{key}'")
    return out, notes


def extract_expression_refs(expr: str) -> list[str]:
    """Return every <Prefix>.<path> reference found in an expression."""
    return _EXPRESSION_REF_RE.findall(expr)


# ── Binding-path coercion ────────────────────────────────────────────────
#
# Agent-friendly write helpers accept bare strings like "Page.email" and
# emit the canonical Modlix shape {"type": "VALUE", "value": "Page.email"}.
# Anything that doesn't look like a Modlix expression-prefix path is
# rejected with a clear error spelling out the valid prefixes.

_BINDING_PATH_HEAD_RE = re.compile(r"^([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$\.]*)$")


def coerce_binding_path(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Normalize one bindingPath value to the canonical {type:VALUE, value:'...'}.

    Returns (wrapped, error). Exactly one is non-None.

    Inputs accepted:
      - "Page.email"                            → {"type":"VALUE","value":"Page.email"}
      - {"type":"VALUE","value":"Page.email"}   → passthrough
      - {"value":"Page.email"}                  → {"type":"VALUE","value":"Page.email"} (adds type)
      - {"type":"EXPRESSION","expression":"..."} → passthrough (rare)
    Rejected:
      - Empty / whitespace strings
      - Strings whose head isn't a Modlix expression prefix (Page / Store /
        LocalStore / Parent / Theme / Url / Filler / Arguments / Steps / Context)
      - Dicts missing both `value` and `expression`
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, "Empty binding path"

    if isinstance(raw, dict):
        if "type" in raw and ("value" in raw or "expression" in raw):
            return raw, None  # already canonical
        if "value" in raw:
            inner = raw["value"]
            if isinstance(inner, str):
                w, e = coerce_binding_path(inner)
                if w:
                    return w, None
                return None, e
            return {"type": "VALUE", "value": inner}, None
        if "expression" in raw:
            return {"type": "EXPRESSION", "expression": raw["expression"]}, None
        return None, (
            "bindingPath dict must include `value` or `expression`. "
            "Got keys: " + ", ".join(sorted(raw.keys())) + ". Pass a bare "
            "string like 'Page.email' and the tool will wrap it."
        )

    if not isinstance(raw, str):
        return None, f"bindingPath must be a string or dict, got {type(raw).__name__}"

    path = raw.strip()
    m = _BINDING_PATH_HEAD_RE.match(path)
    if not m:
        return None, (
            f"bindingPath '{path}' is not a Modlix expression path. "
            f"Expected format: <Prefix>.<dotted.path>, where Prefix ∈ "
            f"{{Page, Store, LocalStore, Parent, Theme, Url, Filler}}."
        )
    head = m.group(1)
    if head not in EXPRESSION_PREFIXES:
        return None, (
            f"bindingPath prefix '{head}' is not valid. Use one of: "
            f"{sorted(EXPRESSION_PREFIXES)}."
        )
    return {"type": "VALUE", "value": path}, None


def coerce_binding_paths_map(
    raw: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]] | None, list[str]]:
    """Normalize a binding_paths map (`{bindingPath: '...', bindingPath2: '...'}`).

    Returns (wrapped_map, errors). `errors` is a list — one entry per slot
    that failed coercion. On any error, `wrapped_map` is None.
    """
    if not raw:
        return {}, []
    if not isinstance(raw, dict):
        return None, [f"binding_paths must be a dict, got {type(raw).__name__}"]
    out: dict[str, dict[str, Any]] = {}
    errs: list[str] = []
    for slot, value in raw.items():
        if not isinstance(slot, str) or not slot.startswith("bindingPath"):
            errs.append(f"binding_paths key '{slot}' must start with 'bindingPath' (e.g. bindingPath, bindingPath2)")
            continue
        wrapped, err = coerce_binding_path(value)
        if err:
            errs.append(f"{slot}: {err}")
            continue
        out[slot] = wrapped  # type: ignore[assignment]
    if errs:
        return None, errs
    return out, []


# ── styleProperties coercion ─────────────────────────────────────────────
#
# Agent-friendly inline styling: accept either a flat CSS dict
# (`{"backgroundColor": "#fff"}`) or the canonical nested shape
# (`{<uuid>: {resolutions: {ALL: {<prop>: {value: "..."}}}}}`).
# Always emits the canonical shape so the renderer accepts it.

_RESERVED_RULE_FIELDS = {"resolutions", "condition", "pseudoState"}


def coerce_style_properties(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Normalize an inline styleProperties payload.

    Inputs accepted:
      - Flat: `{"backgroundColor": "#fff", "padding": "16px"}` → one
        auto-generated rule under ALL breakpoint with `{value: ...}` wraps.
      - Nested-but-unwrapped: `{<uuid>: {resolutions: {ALL: {bg: "#fff"}}}}`
        → leaves get auto-wrapped to `{value: "#fff"}`.
      - Canonical (fully-wrapped): passthrough.
    Rejected:
      - Mixed top-level (some flat CSS, some uuid-rules).
      - Unknown nested shape (no `resolutions`, no `condition`, etc.).
    """
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, f"style_properties must be a dict, got {type(raw).__name__}"
    if not raw:
        return {}, None

    # Distinguish "flat CSS dict" vs "rule dict". A rule-shape entry
    # is a dict with at least one of `resolutions`/`condition`/`pseudoState`.
    flat_keys: list[str] = []
    rule_keys: list[str] = []
    for k, v in raw.items():
        if isinstance(v, dict) and (set(v.keys()) & _RESERVED_RULE_FIELDS):
            rule_keys.append(k)
        else:
            flat_keys.append(k)

    if flat_keys and rule_keys:
        return None, (
            f"style_properties mixes flat CSS keys ({flat_keys}) with rule "
            f"objects ({rule_keys}). Pass EITHER a flat {{cssProp: cssValue}} "
            f"map OR the canonical {{<uuid>: {{resolutions: ...}}}} shape, "
            f"not both."
        )

    if flat_keys:
        # Flat-shape: wrap into one auto-generated rule under ALL.
        rule_id = secrets.token_hex(16)
        wrapped_leaves = {prop: {"value": val} for prop, val in raw.items()}
        return {rule_id: {"resolutions": {"ALL": wrapped_leaves}}}, None

    # Nested-shape: walk rules + ensure every leaf is wrapped.
    out: dict[str, Any] = {}
    for rule_id, rule in raw.items():
        if not isinstance(rule, dict):
            return None, f"style_properties rule '{rule_id}' must be a dict"
        new_rule: dict[str, Any] = {}
        for field, body in rule.items():
            if field == "resolutions":
                if not isinstance(body, dict):
                    return None, f"styleProperties[{rule_id}].resolutions must be a dict"
                new_res: dict[str, Any] = {}
                for bp, leaves in body.items():
                    if not isinstance(leaves, dict):
                        return None, f"styleProperties[{rule_id}].resolutions.{bp} must be a dict"
                    new_leaves: dict[str, Any] = {}
                    for prop, val in leaves.items():
                        if isinstance(val, dict) and ("value" in val or "location" in val):
                            new_leaves[prop] = val  # already wrapped
                        else:
                            new_leaves[prop] = {"value": val}
                    new_res[bp] = new_leaves
                new_rule["resolutions"] = new_res
            else:
                new_rule[field] = body
        out[rule_id] = new_rule
    return out, None


# ── Properties coercion ──────────────────────────────────────────────────
#
# Auto-detect expression-path strings in raw properties so the agent can
# write {text: "Page.greeting"} and have it become the canonical
# {text: {location: {type: "EXPRESSION", value: "Page.greeting"}}}.

def normalize_location(loc: Any) -> Any:
    """Repair one `location` dict into the shape the browser runtime reads.

    StoreContext.getDataFromLocation (nocode-ui src/context/StoreContext.ts:136-141)
    dispatches on `type` and reads a DIFFERENT key for each:

        type VALUE       -> loc['value']        a bare path, e.g. "Page.email"
        type EXPRESSION  -> loc['expression']   a computed expression

    A location with `type: EXPRESSION` but only `value` set therefore resolves to
    undefined: makePropertiesObject drops the property, the component renders
    blank, and getPaths (src/components/util/getPaths.ts:245-251) also reads
    `expression`, so no listener is registered and it never updates either.

    That mis-shape is invisible at write time and fatal at runtime, so repair it
    here rather than rejecting: move `value` to `expression` under type
    EXPRESSION, and fill `value` from `expression` under type VALUE.
    """
    if not isinstance(loc, dict):
        return loc
    ltype = loc.get("type")
    if ltype == "EXPRESSION" and "expression" not in loc and "value" in loc:
        fixed = {k: v for k, v in loc.items() if k != "value"}
        fixed["expression"] = loc["value"]
        return fixed
    if ltype == "VALUE" and "value" not in loc and "expression" in loc:
        fixed = {k: v for k, v in loc.items() if k != "expression"}
        fixed["value"] = loc["expression"]
        return fixed
    return loc


def _normalize_prop_locations(prop: Any) -> Any:
    """Apply normalize_location to a property wrapper's `location`, if any."""
    if isinstance(prop, dict) and isinstance(prop.get("location"), dict):
        out = dict(prop)
        out["location"] = normalize_location(prop["location"])
        return out
    return prop


def coerce_property_value(raw: Any) -> Any:
    """Wrap a single property value into Modlix's stored shape.

    - Already-wrapped ({value: ...} or {location: ...}): passthrough, but with
      any `location` repaired by normalize_location first.
    - Bare Modlix path ("Page.email") or a computed expression containing one
      ("(Page.a ?? 0) - Page.b"): {location:{type:EXPRESSION, expression:...}}.
      The key is `expression`, not `value` (see normalize_location). Computed
      expressions previously fell through to a literal, so the raw expression
      text rendered on the page instead of its value.
    - Otherwise: literal {value: raw}
    """
    if isinstance(raw, dict) and ("value" in raw or "location" in raw):
        return _normalize_prop_locations(raw)
    if isinstance(raw, str):
        text = raw.strip()
        m = _BINDING_PATH_HEAD_RE.match(text)
        if (m and m.group(1) in EXPRESSION_PREFIXES) or _EXPRESSION_REF_RE.search(text):
            return {"location": {"type": "EXPRESSION", "expression": text}}
    return {"value": raw}


def steps_referenced(expr: str) -> set[str]:
    """Return the set of step names referenced in an expression."""
    return set(_STEPS_REF_RE.findall(expr or ""))


def validate_expression(expr: str) -> str | None:
    """Best-effort lint: warn about JavaScript-isms that won't work in Kirun.

    Returns an error message if a known-bad pattern is found; else None.
    Not a full parser — catches the most common mistakes.
    """
    if not isinstance(expr, str):
        return f"Expression must be a string, got {type(expr).__name__}."
    bad = [
        ("===", "use a single '=' (not '===')"),
        ("==", "use a single '=' (not '==')"),
        ("!==", "use '!=' (not '!==')"),
        ("&&", "use 'and' (not '&&')"),
        ("||", "use 'or' (not '||')"),
        ("=>", "Kirun has no arrow functions"),
        ("`", "no template literals; concatenate with '+'"),
    ]
    for token, msg in bad:
        if token in expr:
            return f"Expression contains JS syntax '{token}': {msg}."
    # Standalone '!' (negation) — only flag when it's NOT part of != or !==.
    if re.search(r"(?<![!=])!(?!=)", expr):
        return "Expression contains '!' — Kirun uses 'not' for negation."
    return None


# ── ComponentProperty wrapping (page components only, NOT app-level config) ──
#
# Inside a page's componentDefinition[<key>].properties, every value must be
# wrapped: either {"value": X} for literals, or
# {"location": {"type": "EXPRESSION", "value": "Page.x"}} for binds.
# At the application properties level, values are NOT wrapped (just strings).

def wrap_component_props(props: dict[str, Any]) -> dict[str, Any]:
    """Wrap raw {name: value} props into Modlix's component-property shape.

    Preserves already-wrapped values (those with 'value' or 'location' keys
    or in the multi-valued dict-of-entries shape).

    NOTE: This wrapper is single-valued only; it does NOT convert a list /
    sugared multi-valued input into the platform's multi-valued shape. Use
    `wrap_props_catalog_aware` (below) for that — it consults the catalog
    and the existing stored shape to pick the right wrap per property.
    """
    out: dict[str, Any] = {}
    for k, v in props.items():
        if isinstance(v, dict) and ("value" in v or "location" in v):
            out[k] = v
        elif is_multi_valued_shape(v):
            out[k] = v  # passthrough — already wrapped multi-valued
        else:
            out[k] = {"value": v}
    return out


def unwrap_component_props(props: dict[str, Any]) -> dict[str, Any]:
    """Inverse of wrap_component_props: pull raw values out for re-editing."""
    out: dict[str, Any] = {}
    for k, v in props.items():
        if isinstance(v, dict) and "value" in v and "location" not in v:
            out[k] = v["value"]
        else:
            out[k] = v  # expression refs and other forms pass through
    return out


def make_expression_prop(expression: str) -> dict[str, Any]:
    """Build the {location: {type: 'EXPRESSION', expression: '...'}} form.

    The key is `expression`, not `value`: under type EXPRESSION the runtime
    reads location.expression (see normalize_location).
    """
    return {"location": {"type": "EXPRESSION", "expression": expression}}


# ── Multi-valued property handling ────────────────────────────────────────────
#
# Some properties (Animator.animation, *.validation, Calendar.disableDates,
# Chart.dataSetColors, Tabs.tabs, etc.) accept an ORDERED LIST of entries
# rather than a single value. The platform's stored shape for these is:
#
#   "<propName>": {
#     "<entryKey>": {                       # any unique string
#       "order": 0,                         # sort index within this prop
#       "property": {                       # the rule
#         "value": {                        # one nested {value: ...} wrap
#           "<subField1>": {"value": ...},  # each sub-field individually
#           "<subField2>": {"value": ...},  #   wrapped
#           ...
#         }
#       }
#     },
#     ...
#   }
#
# This is the ANIMATIONOBSERVER editor's shape — verified against prod pages.
# Simpler multi-valued editors (e.g. Iframe.sandbox: list of strings) use
# `{"property": {"value": <primitive>}}` with no sub-fields. We mirror the
# existing shape when present; otherwise we use the ANIMATIONOBSERVER default
# for dicts and the primitive shape for scalars.

# Properties that are multi-valued across many components but that the CDN
# catalog doesn't always enumerate per-component (because they come from
# COMMON_COMPONENT_PROPERTIES at the source level). When the catalog says
# nothing about a property, fall back to this list.
KNOWN_MULTI_VALUED_PROPS: frozenset[str] = frozenset({
    "animation",          # ANIMATIONOBSERVER on Animator + any component
    "animationObserver",  # alias used in some component declarations
    "validation",         # validators on form inputs
})


def is_multi_valued_shape(value: Any) -> bool:
    """True if the value already conforms to the multi-valued dict-of-entries shape.

    Pattern: a non-empty dict where every entry is itself a dict with at
    least `property` or `order` — the signature of an ANIMATION /
    ANIMATIONOBSERVER entry, or the simpler `{property: {value: x}}` entry.
    """
    if not isinstance(value, dict) or not value:
        return False
    for entry in value.values():
        if not isinstance(entry, dict):
            return False
        if "property" not in entry and "order" not in entry:
            return False
    return True


def is_multi_valued_property(
    component_type: str,
    prop_name: str,
    existing_value: Any = None,
) -> bool:
    """Decide whether a property is multi-valued.

    Resolution order:
      1. If `existing_value` is already in multi-valued shape, treat as multi-valued.
         (Honors what the platform actually stored; catalog can be stale.)
      2. If the catalog marks the property `multiValued: True`, multi-valued.
      3. If the property name is in KNOWN_MULTI_VALUED_PROPS, multi-valued.
      4. Otherwise single-valued.
    """
    if is_multi_valued_shape(existing_value):
        return True
    # Catalog lookup — imported lazily to avoid a circular dependency. Wrapped
    # in try/except so we degrade gracefully when the catalog singleton hasn't
    # been registered yet (e.g. in unit tests, or before main.py's lifespan
    # has set it). In that case we fall through to KNOWN_MULTI_VALUED_PROPS.
    info: dict[str, Any] = {}
    try:
        from app.agents.appbuilder.catalog import get_catalog  # type: ignore[attr-defined]
        info = get_catalog().get_component_info(component_type) or {}
    except (ImportError, AttributeError, RuntimeError):
        info = {}

    matching = next(
        (
            p
            for p in (info.get("properties") or [])
            if isinstance(p, dict) and p.get("name") == prop_name
        ),
        None,
    )
    if matching and matching.get("multiValued") is True:
        return True
    return prop_name in KNOWN_MULTI_VALUED_PROPS


def _wrap_subfields(sub_dict: dict[str, Any]) -> dict[str, Any]:
    """Wrap each sub-field of a rule with `{value: ...}` unless already wrapped."""
    out: dict[str, Any] = {}
    for k, v in sub_dict.items():
        if isinstance(v, dict) and ("value" in v or "location" in v):
            out[k] = v
        else:
            out[k] = {"value": v}
    return out


def _make_multi_valued_entry(rule: Any, order: int) -> dict[str, Any]:
    """Build one entry of the multi-valued dict, given the raw rule value.

    If `rule` is already in entry shape (has `property` or `order`), passthrough.
    If `rule` is a dict of sub-fields, wrap each with `{value: ...}` under
    `property.value`.
    If `rule` is a primitive, wrap as `{property: {value: <primitive>}}`.
    """
    if isinstance(rule, dict) and ("property" in rule or "order" in rule):
        # Already an entry — but ensure order is set.
        out = dict(rule)
        out.setdefault("order", order)
        return out
    if isinstance(rule, dict):
        return {
            "order": order,
            "property": {"value": _wrap_subfields(rule)},
        }
    return {
        "order": order,
        "property": {"value": rule},
    }


def wrap_multi_valued(value: Any) -> dict[str, Any]:
    """Convert a sugared multi-valued input into the stored dict-of-entries shape.

    Accepts:
      - dict already in entries shape: passthrough (each entry's `order` defaulted)
      - list of rule dicts (or primitives): one entry per item, order = index
      - single rule dict (not already an entry): wrapped as the sole entry

    The entry keys are freshly minted hex strings.
    """
    import secrets

    if is_multi_valued_shape(value):
        # Already entries — passthrough with order defaults filled in.
        out: dict[str, Any] = {}
        for i, (k, entry) in enumerate(value.items()):
            e = dict(entry)
            e.setdefault("order", i)
            out[k] = e
        return out

    if isinstance(value, list):
        return {
            secrets.token_hex(8): _make_multi_valued_entry(rule, order=i)
            for i, rule in enumerate(value)
        }

    if isinstance(value, dict):
        return {secrets.token_hex(8): _make_multi_valued_entry(value, order=0)}

    # Scalar — also a valid (though unusual) multi-valued input.
    return {secrets.token_hex(8): _make_multi_valued_entry(value, order=0)}


def wrap_props_catalog_aware(
    component_type: str,
    properties: dict[str, Any],
    existing_props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap raw props into Modlix's stored shape, multi-valued-aware.

    For each (name, value):
      - If `value` is already in `{value: ...}` / `{location: ...}` shape:
        passthrough (caller knew what they were doing).
      - If the property is multi-valued (catalog says so, or existing stored
        value is multi-valued shape, or name is in KNOWN_MULTI_VALUED_PROPS):
        convert sugared list / single-rule input to the dict-of-entries shape.
      - Otherwise single-valued: wrap as `{value: <raw>}`.

    `existing_props` is the component's current `properties` dict (used for
    shape detection so we can match prior writes).
    """
    out: dict[str, Any] = {}
    existing = existing_props or {}
    for name, value in properties.items():
        # Caller-pre-wrapped scalar form — always preserve.
        if isinstance(value, dict) and ("value" in value or "location" in value):
            out[name] = value
            continue

        existing_value = existing.get(name)
        if is_multi_valued_property(component_type, name, existing_value):
            out[name] = wrap_multi_valued(value)
        else:
            # Single-valued; preserve already-multi-valued passthrough too in
            # case the caller really did pass a sub-dict that just happens to
            # look multi-valued (shouldn't normally happen for single-valued).
            if is_multi_valued_shape(value):
                out[name] = value
            else:
                out[name] = {"value": value}
    return out


# ── ParameterReference + parameterMap construction ───────────────────────────
#
# A function step's parameterMap is doubly nested:
#   parameterMap: {
#     <paramName>: {
#       <uuid>: { key, type: VALUE|EXPRESSION, value?, expression?, order, ... }
#     }
#   }
# The inner map allows variadic params (one paramName mapped to many refs,
# e.g. System.Print's 'values' parameter takes any number of inputs).
# `key` mirrors the inner-map key; `order` controls evaluation/iteration order.

def _new_param_key() -> str:
    """Generate a short identifier-like key matching the platform's keying style."""
    # The platform uses 22-char base-62-ish keys, but standard UUIDs are
    # accepted too. We use UUIDs for safety; the editor will tolerate both.
    return _uuid.uuid4().hex


def make_value_ref(value: Any, order: int = 1, key: str | None = None) -> dict[str, Any]:
    """A literal-value parameter reference: type=VALUE."""
    k = key or _new_param_key()
    return {
        "key": k,
        "type": "VALUE",
        "value": value,
        "expression": "",
        "order": order,
    }


def make_expression_ref(expression: str, order: int = 1, key: str | None = None) -> dict[str, Any]:
    """An expression parameter reference: type=EXPRESSION, runtime evaluates."""
    err = validate_expression(expression)
    if err:
        raise ValueError(err)
    k = key or _new_param_key()
    return {
        "key": k,
        "type": "EXPRESSION",
        "expression": expression,
        "order": order,
    }


def make_parameter_map(values: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Build a step's parameterMap from a flat {paramName: value-or-list} dict.

    Conventions for the input values:
      - A plain literal       → make_value_ref(value)
      - A string starting with one of EXPRESSION_PREFIXES followed by '.' → expression
      - A dict with key 'expression' or 'value' or 'type' → passed through as-is
      - A list                → multiple refs under the same param, ordered by index

    For ambiguous cases (e.g. a literal string that happens to start with "Page."),
    wrap explicitly with {"value": ...} to force literal treatment.
    """
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for name, raw in values.items():
        result[name] = {}
        items = raw if isinstance(raw, list) else [raw]
        for i, item in enumerate(items, start=1):
            ref = _coerce_to_param_ref(item, order=i)
            result[name][ref["key"]] = ref
    return result


def _coerce_to_param_ref(item: Any, order: int) -> dict[str, Any]:
    if isinstance(item, dict) and "type" in item and "key" in item:
        return item  # already a full ParameterReference
    if isinstance(item, dict) and ("value" in item or "expression" in item):
        # Partial form: {value: ...} or {expression: ...}
        kind = "EXPRESSION" if "expression" in item and item["expression"] else "VALUE"
        if kind == "EXPRESSION":
            return make_expression_ref(item["expression"], order=order)
        return make_value_ref(item.get("value"), order=order)
    if isinstance(item, str) and _looks_like_expression(item):
        return make_expression_ref(item, order=order)
    return make_value_ref(item, order=order)


def _looks_like_expression(s: str) -> bool:
    head = s.split(".", 1)[0]
    return head in EXPRESSION_PREFIXES


# ── dependentStatements semantics ────────────────────────────────────────────
#
# Key format:  "Steps.<stepName>.<eventName>"  OR  "<stepName>"
# Value:       boolean — TRUE means an active dependency; FALSE means present
#              but disabled (a visual-editor artifact for paused edges).
# See nocode-kirun/kirun-js/src/engine/dsl/transformer/JSONToText.ts:206:
#   `if (step.dependentStatements[depKey] !== true) continue;`
# So only the TRUE entries actually gate execution.
#
# In addition to explicit deps, the runtime infers IMPLICIT deps from any
# Steps.<name>.* references inside the step's parameterMap expressions.

def make_dependency_key(step_name: str, event: str | None = None) -> str:
    """Build a dependentStatements key. event=None → bare step name."""
    return f"Steps.{step_name}.{event}" if event else step_name


def make_dependent_statements(*deps: tuple[str, str | None] | str) -> dict[str, bool]:
    """Build a dependentStatements dict with all entries enabled.

    Each dep can be a bare step name (str) or a tuple (step_name, event).
    Examples:
      make_dependent_statements("create")                       → {"Steps.create.output": True} (via convention below)
      make_dependent_statements(("if", "true"), ("if", "false"))→ {"Steps.if.true": True, "Steps.if.false": True}
    Bare step names are treated as Steps.<name>.output by convention; pass a tuple to be explicit.
    """
    out: dict[str, bool] = {}
    for d in deps:
        if isinstance(d, str):
            out[make_dependency_key(d, "output")] = True
        else:
            step, event = d
            out[make_dependency_key(step, event)] = True
    return out


def active_dependencies(dep_map: dict[str, bool] | None) -> set[str]:
    """Filter a dependentStatements map to the entries that are actually live."""
    if not dep_map:
        return set()
    return {k for k, v in dep_map.items() if v is True}


# ── Kirun primitive catalog (subset; full discovery is at runtime) ───────────
#
# The platform has no public "list primitives" API. These are the catalogs
# enumerated in nocode-kirun source for both runtimes. UIENGINE_PRIMITIVES is
# JS-only (cannot run in the Java/core runtime).

KIRUN_NAMESPACES: dict[str, tuple[str, ...]] = {
    "System": ("If", "Wait", "Make", "GenerateEvent", "ValidateSchema", "Print"),
    "System.Context": ("Create", "Get", "Set"),
    "System.Loop": ("CountLoop", "RangeLoop", "ForEachLoop", "Break"),
    "System.Array": (
        "Add", "AddFirst", "AddLast", "Insert", "InsertLast",
        "Delete", "DeleteFirst", "DeleteLast", "DeleteFrom",
        "Join", "Concatenate", "Copy", "SubArray", "Reverse", "Sort", "Rotate", "Shuffle",
        "RemoveDuplicates", "IndexOf", "IndexOfArray", "LastIndexOf", "LastIndexOfArray",
        "BinarySearch", "Frequency", "Contains", "Compare", "Equals", "MisMatch", "Disjoint",
        "Min", "Max", "Fill", "ArrayToObjects", "ArrayToArrayOfObjects",
    ),
    "System.String": (
        "Concatenate", "ToString", "Reverse", "Split",
        "PrePad", "PostPad", "TrimTo", "InsertAtGivenPosition",
        "DeleteForGivenLength", "ReplaceAtGivenPosition", "Matches", "RegionMatches", "Frequency",
    ),
    "System.Math": (
        "Add", "Subtract", "Multiply", "Divide",
        "Absolute", "Minimum", "Maximum", "Hypotenuse", "Random", "RandomInt",
    ),
    "System.Date": (
        "GetCurrentTimestamp", "FromDateString", "ToDateString",
        "EpochToTimestamp", "TimestampToEpoch", "AddSubtractTime",
        "Difference", "TimeAs", "StartEndOf", "LastFirstOf",
        "IsBetween", "FromNow", "IsValidISODate", "GetNames", "SetTimeZone",
    ),
    "System.Object": ("Keys", "Values", "Entries", "PutValue", "DeleteKey", "Convert"),
    "System.JSON": ("Parse", "Stringify"),
}

# JS-only primitives executed by the browser-side Kirun runtime. The Java
# runtime (core functions) does NOT have these. GENERATED from
# nocode-ui/ui-app/client/src/functions/all.ts by scripts/gen_uiengine_catalog.py
# (see _uiengine_catalog.py). The hand-written list this replaced carried 11
# names that never existed (Read/Create/Update/Delete/GetStore/OpenModal/...)
# and hid FetchData, which is what pushed the agent into SetStore mock data.
from ._uiengine_catalog import UIENGINE_SIGNATURES  # noqa: E402

UIENGINE_PRIMITIVES: frozenset[str] = frozenset(UIENGINE_SIGNATURES)


def is_core_runtime_compatible(namespace: str) -> bool:
    """True if a step targeting this namespace will run in the Java/core runtime.

    UIEngine.* and any namespace not in KIRUN_NAMESPACES is treated as
    UI-only (or app-specific) and not guaranteed to run server-side.
    """
    if namespace == "UIEngine":
        return False
    return namespace in KIRUN_NAMESPACES


def validate_step_call(namespace: str, name: str) -> str | None:
    """Light-touch validation: warn if the (namespace, name) isn't recognized.

    Does not block — apps can define their own functions in any namespace.
    Returns a warning string for unknown built-ins; None otherwise.
    """
    if namespace in KIRUN_NAMESPACES:
        if name not in KIRUN_NAMESPACES[namespace]:
            return (
                f"'{name}' is not a known built-in in namespace '{namespace}'. "
                f"Known: {', '.join(KIRUN_NAMESPACES[namespace])}"
            )
    if namespace == "UIEngine" and name not in UIENGINE_PRIMITIVES:
        return (
            f"'{name}' is not a recognized UIEngine primitive. "
            f"Known: {', '.join(sorted(UIENGINE_PRIMITIVES))}"
        )
    return None


# ── Responsive breakpoints ───────────────────────────────────────────────────
#
# Used as keys in Theme.variables and in component styleProperties.resolutions.
# Sourced from real prod usage scan (53,380 ALL + 21,000+ specific entries):
#
#   - <NAME>_SCREEN          → applies at this breakpoint AND wider
#   - <NAME>_SCREEN_ONLY     → applies only within this exact breakpoint
#   - <NAME>_SCREEN_SMALL    → applies at the small end of this breakpoint
#
# `LARGE_DESKTOP_SCREEN` from earlier conventions had 0 prod usage and is gone.
# `WIDE_SCREEN` is the actual wide-screen breakpoint.

BREAKPOINTS: tuple[str, ...] = (
    "ALL",
    "WIDE_SCREEN",
    "DESKTOP_SCREEN", "DESKTOP_SCREEN_ONLY", "DESKTOP_SCREEN_SMALL",
    "TABLET_LANDSCAPE_SCREEN", "TABLET_LANDSCAPE_SCREEN_ONLY", "TABLET_LANDSCAPE_SCREEN_SMALL",
    "TABLET_POTRAIT_SCREEN", "TABLET_POTRAIT_SCREEN_ONLY", "TABLET_POTRAIT_SCREEN_SMALL",
    "MOBILE_LANDSCAPE_SCREEN", "MOBILE_LANDSCAPE_SCREEN_ONLY", "MOBILE_LANDSCAPE_SCREEN_SMALL",
    "MOBILE_POTRAIT_SCREEN", "MOBILE_POTRAIT_SCREEN_ONLY",
)
_BREAKPOINT_SET = frozenset(BREAKPOINTS)


def validate_breakpoint(name: str) -> str | None:
    if name not in _BREAKPOINT_SET:
        return f"Unknown breakpoint '{name}'. Valid: {', '.join(BREAKPOINTS)}"
    return None


# ── styleProperties shape ────────────────────────────────────────────────────
#
# Real structure (confirmed against prod, 54,285 rules across 500 pages):
#
#   styleProperties: {
#     "<rule-uuid>": {
#       "resolutions": {
#         "ALL":                            { "<leafCssProp>": { "value": "..." } },
#         "MOBILE_LANDSCAPE_SCREEN_SMALL":  { "<leafCssProp>": { "value": "..." } },
#         ...
#       }
#     },
#     "<another-rule-uuid>": { ... }
#   }
#
# Each rule is keyed by a generated identifier (UUID-like). The cssProp at the
# leaf encodes <subComponent>-<cssProp>:<pseudoState>:
#   - plain:           "fontSize"            (89.6% of prod usage)
#   - sub-component:   "text-fontSize"       (9.3%)
#   - pseudo-state:    "transform:hover"     (1.0%)
#   - both:            "step-animationName:hover" (0.08%)
#
# Animation properties live HERE on any component (animationName/Duration/
# TimingFunction/Delay/Direction/FillMode/IterationCount/PlayState; transition*;
# transform*). The Animator component is rare (3 uses platform-wide).


def make_css_prop_key(css_prop: str, sub_component: str = "", pseudo_state: str = "") -> str:
    """Encode the leaf cssProp name as `<subComp>-<cssProp>:<pseudo>`.

    css_prop must be camelCase (paddingTop, marginLeft, borderTopLeftRadius) —
    NEVER kebab-case shorthand (padding, margin). The platform uses long-form
    camelCase exclusively (24,691 width, 22,042 fontSize, etc.).
    """
    leaf = f"{sub_component}-{css_prop}" if sub_component else css_prop
    if pseudo_state:
        leaf = f"{leaf}:{pseudo_state}"
    return leaf


def parse_css_prop_key(leaf: str) -> dict[str, str]:
    """Inverse of make_css_prop_key. Returns {sub_component, css_prop, pseudo_state}."""
    pseudo = ""
    if ":" in leaf:
        leaf, pseudo = leaf.split(":", 1)
    sub_component = ""
    css_prop = leaf
    if "-" in leaf:
        first_dash = leaf.index("-")
        # Heuristic: subComponent has at least 2 chars before the dash AND the part
        # after the dash starts lowercase (a cssProp). Long-form CSS props with
        # dashes ("border-top") aren't a thing here — camelCase only.
        candidate_sub = leaf[:first_dash]
        candidate_prop = leaf[first_dash + 1:]
        if len(candidate_sub) >= 2 and candidate_prop and candidate_prop[0].islower():
            sub_component = candidate_sub
            css_prop = candidate_prop
    return {"sub_component": sub_component, "css_prop": css_prop, "pseudo_state": pseudo}


def make_style_rule(
    css_prop: str,
    css_value: Any,
    *,
    breakpoint: str = "ALL",
    sub_component: str = "",
    pseudo_state: str = "",
) -> dict[str, Any]:
    """Build ONE styleProperties rule (the value at `styleProperties[<rule-uuid>]`).

    Returns the inner shape:
        { "resolutions": { "<breakpoint>": { "<leafKey>": { "value": <css_value> } } } }

    Caller assigns a fresh UUID as the outer key when adding the rule to a
    component, or merges into an existing rule's resolutions if a rule for
    this leafKey already exists.
    """
    if validate_breakpoint(breakpoint):
        raise ValueError(validate_breakpoint(breakpoint))
    leaf = make_css_prop_key(css_prop, sub_component, pseudo_state)
    return {
        "resolutions": {
            breakpoint: {leaf: {"value": css_value}}
        }
    }


def find_style_rule_for_leaf(
    style_properties: dict[str, Any],
    leaf: str,
) -> str | None:
    """Find the rule-uuid in styleProperties whose resolutions touch leafKey.

    Returns the rule key, or None if no existing rule manages this cssProp.
    """
    for rule_key, rule in (style_properties or {}).items():
        if not isinstance(rule, dict):
            continue
        resolutions = rule.get("resolutions") or {}
        if not isinstance(resolutions, dict):
            continue
        for bp_block in resolutions.values():
            if isinstance(bp_block, dict) and leaf in bp_block:
                return rule_key
    return None


# ── Component type whitelist ─────────────────────────────────────────────────
#
# Pulled from the AppBuilder agent's persona + observed in production app defs.
# This list catches the most-common mistakes (Box, Container, Div, Input) where
# Claude reaches for web/React names that the platform doesn't define.

VALID_COMPONENT_TYPES: frozenset[str] = frozenset({
    "Grid", "Flex", "SectionGrid", "SubPage",
    "Text", "Label", "Link",
    "Button", "ButtonBar",
    "TextBox", "TextArea", "PhoneNumber", "Otp", "Calendar", "ColorPicker",
    "Image", "Icon", "Video", "Audio", "Iframe", "Gallery",
    "Dropdown", "CheckBox", "RadioButton", "ToggleButton",
    "Table", "Tabs", "Stepper", "Menu",
    "Popup", "Popover", "Form", "SchemaForm",
    "ArrayRepeater", "Carousel", "SmallCarousel", "ImageWithBrowser",
    "FileUpload", "RangeSlider", "ProgressBar", "Tags", "Timer",
    "Chart", "MarkdownTOC", "Animator", "TextList",
})

# Common synonyms that often appear in LLM output but are NOT valid Modlix types.
INVALID_COMPONENT_SYNONYMS: dict[str, str] = {
    "Box": "Grid",
    "Container": "Grid",
    "Div": "Grid",
    "Row": "Flex",          # use direction=ROW
    "Column": "Flex",       # use direction=COLUMN
    "Input": "TextBox",
    "Select": "Dropdown",
    "Checkbox": "CheckBox",
    "Radio": "RadioButton",
    "Switch": "ToggleButton",
    "DatePicker": "Calendar",
    "Modal": "Popup",
}


def validate_component_type(type_name: str) -> str | None:
    """Advisory check for the most common typing mistakes.

    Returns a warning for known-bad synonyms (Box → Grid, Input → TextBox) so
    the agent can correct. Returns None for everything else — even unknown
    types — because the platform has 75+ component types (TableColumn,
    FileSelector, KIRun Editor, ...) and a hard whitelist would block valid
    work. The backend is the authoritative validator.
    """
    suggestion = INVALID_COMPONENT_SYNONYMS.get(type_name)
    if suggestion:
        return f"'{type_name}' is not a Modlix component type — use '{suggestion}' instead."
    return None


# ── Override / versioning predicates ─────────────────────────────────────────
#
# Every overridable entity (Pages, Functions, Themes, Styles, Schemas, ...)
# carries: clientCode (owner), baseClientCode (parent in hierarchy, or null),
# version (whole-doc optimistic lock), message (commit message).
# Pages additionally carry componentVersions and eventFunctionVersions for
# per-element locks used by PATCH /api/ui/pages/{id}/components/{key}.

def is_override_doc(doc: dict[str, Any]) -> bool:
    """True if the entity is an override of a parent (baseClientCode is set)."""
    return bool(doc.get("baseClientCode"))


def is_owned_by(doc: dict[str, Any], client_code: str) -> bool:
    """True if this entity is directly owned by the given client (no override needed to edit)."""
    return doc.get("clientCode") == client_code


def expected_version_for(doc: dict[str, Any]) -> int:
    """Return the version to pass back on PUT/PATCH for optimistic locking."""
    return int(doc.get("version", 1))


def component_version_for(doc: dict[str, Any], component_key: str) -> int:
    """Per-component version for surgical PATCH on Pages. Defaults to 1."""
    cv = doc.get("componentVersions") or {}
    return int(cv.get(component_key, 1))


def event_function_version_for(doc: dict[str, Any], function_name: str) -> int:
    ev = doc.get("eventFunctionVersions") or {}
    return int(ev.get(function_name, 1))


# ── URI path parsing ─────────────────────────────────────────────────────────
#
# URIPath.pathString uses curly-brace params: "/hello/{a}/{b}/C/B".
# pathDefinitions[<method>].kiRunFxDefinition.pathParamMapping maps path
# variable names to the target function's parameter names.

_URI_PARAM_RE = re.compile(r"\{([A-Za-z_][\w]*)\}")


def parse_uri_path(path_string: str) -> tuple[str, list[str]]:
    """Return (path_regex, [param_names]) for a Modlix URI path template."""
    params: list[str] = []
    def repl(m: re.Match[str]) -> str:
        params.append(m.group(1))
        return r"([^/]+)"
    regex = _URI_PARAM_RE.sub(repl, path_string)
    return regex, params


# ── Identifier validation ────────────────────────────────────────────────────
#
# appCode / name conventions vary by entity. Apps and pages allow letters and
# digits (must start with a letter). Functions/schemas can include dots in their
# names for namespace.local-name addressing (e.g. "TestUI.fibonaccii").

_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*$")
_NAMESPACED_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*(?:\.[a-zA-Z][a-zA-Z0-9]*)*$")


def validate_simple_name(name: str) -> str | None:
    if not name:
        return "Name must not be empty."
    if not _NAME_RE.match(name):
        return f"Invalid name '{name}': must start with a letter and contain only letters and digits."
    return None


def validate_namespaced_name(name: str) -> str | None:
    """For Function/Schema names that may be dotted (Namespace.LocalName)."""
    if not name:
        return "Name must not be empty."
    if not _NAMESPACED_NAME_RE.match(name):
        return (
            f"Invalid namespaced name '{name}': segments must start with a letter, "
            "contain only letters and digits, and be dot-separated."
        )
    return None


# ── Public API ───────────────────────────────────────────────────────────────

__all__ = [
    # Authority
    "make_role_authority", "make_profile_authority", "make_permission_authority",
    "parse_authority", "validate_authority", "COMMON_AUTHORITIES",
    # Expressions
    "EXPRESSION_PREFIXES", "extract_expression_refs", "steps_referenced", "validate_expression",
    # Component properties
    "wrap_component_props", "unwrap_component_props", "make_expression_prop",
    "normalize_location", "EVENT_PROP_NAMES", "resolve_event_prop_refs",
    "wrap_props_catalog_aware", "wrap_multi_valued",
    "is_multi_valued_shape", "is_multi_valued_property",
    "KNOWN_MULTI_VALUED_PROPS",
    # ParameterReference + parameterMap
    "make_value_ref", "make_expression_ref", "make_parameter_map",
    # dependentStatements
    "make_dependency_key", "make_dependent_statements", "active_dependencies",
    # Primitives
    "KIRUN_NAMESPACES", "UIENGINE_PRIMITIVES", "UIENGINE_SIGNATURES",
    "is_core_runtime_compatible", "validate_step_call",
    # Styles
    "BREAKPOINTS", "validate_breakpoint",
    "make_css_prop_key", "parse_css_prop_key", "make_style_rule", "find_style_rule_for_leaf",
    # Components
    "VALID_COMPONENT_TYPES", "INVALID_COMPONENT_SYNONYMS", "validate_component_type",
    # Override / versioning
    "is_override_doc", "is_owned_by", "expected_version_for",
    "component_version_for", "event_function_version_for",
    # URI paths
    "parse_uri_path",
    # Identifiers
    "validate_simple_name", "validate_namespaced_name",
]
