"""Page helpers — fetch, save, component-tree manipulation.

Ported from modlix-mcp/modlix_mcp/page_ops.py. Differences from the source:
  - Imports SaasClient from app.core.tools.http_client (the nocode-ai client,
    not modlix-mcp's). Same interface — accepts headers per call.
  - Imports ToolResult from app.core.tools.base.
  - The 404 hint no longer mentions MODLIX_USERNAME/MODLIX_PASSWORD — in
    nocode-ai the JWT comes from the request, no fresh-login fallback exists.

Builds and mutates the Modlix page JSON shape:

    {
      "id": ..., "name": ..., "appCode": ..., "clientCode": ...,
      "rootComponent": "<uuid>",
      "componentDefinition": {
        "<uuid>": {
          "key": ..., "type": ..., "name": ...,
          "displayOrder": 0,
          "children": {"<child-uuid>": true, ...},
          "properties": {...},          # {propName: {"value": ...}}
          "styleProperties": {...},     # {styleKey: {"resolutions": {"ALL": {cssProp: {"value": ...}}}}}
          "bindingPath": {...},
        },
        ...
      },
      "eventFunctions": {...},
      "properties": {...},
      "translations": {...}
    }

The nocode-ai codebase ALSO has fetch_page_by_name / save_page in
`tools/_executor.py` for the legacy CRUD tools. The two coexist during the
port; once the old CRUD tools retire (after Phase 3 verification),
`_executor.py` can be deleted or repointed at this module.
"""

from __future__ import annotations

from typing import Any

from app.core.tools.base import ToolResult
from app.core.tools.http_client import SaasClient
from . import _draft_surface as drafts

API_PREFIX = "/api/ui/pages"


# ── Fetch & save ──────────────────────────────────────────────────────────────


async def _list_pages(
    client: SaasClient,
    app_code: str,
    headers: dict[str, str],
    *,
    name: str | None = None,
    size: int = 1000,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """List page stubs filtered by app + optional name. Returns (rows, error)."""
    params: dict[str, Any] = {"page": 0, "size": size, "appCode": app_code}
    if name:
        params["name"] = name
    r = await client.get(API_PREFIX, headers=headers, params=params)
    if not r.success:
        return None, f"Failed to list pages: {r.error}"
    return (r.data or {}).get("content", []) if isinstance(r.data, dict) else [], None


def _detail_404_hint(page_name: str, page_id: str, err: str) -> str:
    """Hint when listing returned the id but detail-by-id 404'd.

    Common causes when this happens in the CFA:
      (a) Caller's JWT lacks read access on the page's clientCode — listing
          (which is filtered by appCode) returned the id, but the per-entity
          access check on detail failed and the platform 404s instead of 403s
          (security-by-obscurity).
      (b) Two pages share this name in the override chain and the listing
          picked an id the caller can't read.
    """
    return (
        f"Failed to read page '{page_name}' (id={page_id}): {err}. "
        "Listing returned the id but detail 404'd. Likely the caller's JWT "
        "doesn't have read access on that page's clientCode "
        "(the platform returns 404 instead of 403 for cross-client reads). "
        "Confirm via inspect_token that the JWT's clientCode matches the "
        "page's, OR list with explicit clientCode filter to find the right id."
    )


async def fetch_page_by_name(
    client: SaasClient,
    page_name: str,
    app_code: str,
    headers: dict[str, str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Find a page by name within an app, return its full document.

    Strategy:
      1. List with `?name=<page_name>` so the SERVER resolves the right id
         per the caller's auth context (avoids picking an override id from
         the wrong client when multiple pages share the name).
      2. If that's empty, fall back to size=1000 client-side filter — old
         server builds ignore the `name` query param.
      3. GET detail by id. If detail 404s while listing succeeded, surface a
         clearer hint covering the common causes (cross-client access,
         ambiguous name).
    """
    rows, err = await _list_pages(client, app_code, headers, name=page_name, size=5)
    if err:
        return None, err
    match = next((p for p in (rows or []) if p.get("name") == page_name), None)
    if match is None:
        rows, err = await _list_pages(client, app_code, headers, size=1000)
        if err:
            return None, err
        match = next((p for p in (rows or []) if p.get("name") == page_name), None)
        if not match:
            return None, f"Page '{page_name}' not found in app '{app_code}'."

    # Read the draft when this turn is drafting, so the agent sees the work it
    # did a moment ago rather than the last published version of it. The flag is
    # read-through: no draft row means the live document comes back unchanged.
    on = await drafts.active(client, headers, app_code)
    detail = await client.get(
        f"{API_PREFIX}/{match['id']}",
        headers=headers,
        params=drafts.params_with_draft(None, on),
    )
    if detail.success:
        return detail.data, None
    err_text = detail.error or ""
    if "404" in err_text or "not found" in err_text.lower():
        return None, _detail_404_hint(page_name, match["id"], err_text)
    return None, f"Failed to read page '{page_name}': {err_text}"


async def save_page(
    client: SaasClient,
    page_data: dict[str, Any],
    headers: dict[str, str],
    user_client_code: str,
    message: str = "",
) -> ToolResult:
    """Save a page with override-awareness.

    If the page's clientCode matches the user's client, PUT updates in place.
    Otherwise the user is editing a shared/parent page — strip the id and POST
    so the backend creates an override for the user's client.
    """
    body = {**page_data, "message": message or page_data.get("message", "")}
    object_client = body.get("clientCode", "")

    on = await drafts.active(client, headers, body.get("appCode") or "")

    if object_client and object_client != user_client_code:
        # Creating an override. Creation is never drafted: the backend would
        # still write a real live document, and a Draft row keyed on a name that
        # has no live counterpart has nothing to publish over.
        override = {k: v for k, v in body.items() if k != "id"}
        return await client.post(API_PREFIX, headers=headers, json=override)

    page_id = body.get("id")
    if not page_id:
        return ToolResult(success=False, error="save_page: page_data has no 'id' field.")
    return await client.put(
        f"{API_PREFIX}/{page_id}",
        headers=headers,
        json=body,
        params=drafts.params_with_draft(None, on),
    )


# ── Page construction ────────────────────────────────────────────────────────


def new_page_skeleton(name: str, app_code: str, client_code: str, title: str | None = None) -> dict[str, Any]:
    """Return a minimal page JSON ready to POST to /api/ui/pages."""
    root_key = "root"
    return {
        "name": name,
        "appCode": app_code,
        "clientCode": client_code,
        "rootComponent": root_key,
        "componentDefinition": {
            root_key: {
                "key": root_key,
                "type": "Grid",
                "name": "rootGrid",
                "displayOrder": 0,
                "children": {},
                "properties": {},
                "styleProperties": {},
            }
        },
        "eventFunctions": {},
        "properties": {"title": {"name": {"value": title or name}}} if title else {},
        "translations": {},
    }


# ── Tree rendering ──────────────────────────────────────────────────────────


def build_component_tree(page_data: dict[str, Any]) -> str:
    """Render the component tree as an indented text outline."""
    comp_def = page_data.get("componentDefinition", {})
    root = page_data.get("rootComponent", "")
    if not root or root not in comp_def:
        return "(empty page)"

    lines: list[str] = []

    def walk(key: str, depth: int) -> None:
        comp = comp_def.get(key)
        if not comp:
            return
        name = comp.get("name") or comp.get("key", key)
        ctype = comp.get("type", "?")
        lines.append(f"{'  ' * depth}- {key} ({ctype}) {name}")
        children = comp.get("children") or {}
        ordered = sorted(
            (k for k, active in children.items() if active and k in comp_def),
            key=lambda k: comp_def[k].get("displayOrder", 0),
        )
        for child_key in ordered:
            walk(child_key, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


# ── Mutation primitives — used by composition tools ──────────────────────────


def add_component(
    page_data: dict[str, Any],
    *,
    parent_key: str,
    component_key: str,
    component_type: str,
    name: str | None = None,
    properties: dict[str, Any] | None = None,
    style_properties: dict[str, Any] | None = None,
    binding_paths: dict[str, Any] | None = None,
    display_order: int | None = None,
) -> str | None:
    """Insert a new component under parent_key. Returns error message or None.

    `display_order=None` appends after the parent's existing children (max
    sibling displayOrder + 1). The runtime breaks displayOrder ties by key
    name, so leaving every sibling at 0 renders them alphabetically rather
    than in authoring order; that is exactly what a batch add would do
    without this default.
    """
    comp_def = page_data.setdefault("componentDefinition", {})
    if parent_key not in comp_def:
        return f"Parent '{parent_key}' not found"
    if component_key in comp_def:
        return f"Component '{component_key}' already exists"

    if display_order is None:
        sibling_orders = [
            int(comp_def[k].get("displayOrder") or 0)
            for k in (comp_def[parent_key].get("children") or {})
            if k in comp_def
        ]
        display_order = (max(sibling_orders) + 1) if sibling_orders else 0

    comp: dict[str, Any] = {
        "key": component_key,
        "type": component_type,
        "name": name or component_key,
        "displayOrder": display_order,
        "children": {},
        "properties": _to_component_props(_resolve_events(page_data, properties)),
        "styleProperties": style_properties or {},
    }
    for k, v in (binding_paths or {}).items():
        comp[k] = v

    comp_def[component_key] = comp
    comp_def[parent_key].setdefault("children", {})[component_key] = True
    return None


def update_component(
    page_data: dict[str, Any],
    *,
    component_key: str,
    properties: dict[str, Any] | None = None,
    style_properties: dict[str, Any] | None = None,
    binding_paths: dict[str, Any] | None = None,
    display_order: int | None = None,
) -> str | None:
    comp_def = page_data.setdefault("componentDefinition", {})
    if component_key not in comp_def:
        return f"Component '{component_key}' not found"
    comp = comp_def[component_key]
    if properties:
        comp.setdefault("properties", {}).update(
            _to_component_props(_resolve_events(page_data, properties))
        )
    if style_properties:
        _deep_merge(comp.setdefault("styleProperties", {}), style_properties)
    if display_order is not None:
        comp["displayOrder"] = display_order
    for k, v in (binding_paths or {}).items():
        comp[k] = v
    return None


def remove_component(
    page_data: dict[str, Any],
    *,
    component_key: str,
    recursive: bool = True,
) -> str | None:
    comp_def = page_data.setdefault("componentDefinition", {})
    root = page_data.get("rootComponent", "")
    if component_key not in comp_def:
        return f"Component '{component_key}' not found"
    if component_key == root:
        return "Cannot remove the root component"

    keys: set[str] = set()
    if recursive:
        _collect_descendants(comp_def, component_key, keys)
    keys.add(component_key)
    for k in keys:
        comp_def.pop(k, None)
    for comp in comp_def.values():
        comp.get("children", {}).pop(component_key, None)
    return None


def move_component(
    page_data: dict[str, Any],
    *,
    component_key: str,
    new_parent_key: str,
    display_order: int | None = None,
) -> str | None:
    comp_def = page_data.setdefault("componentDefinition", {})
    if component_key not in comp_def:
        return f"Component '{component_key}' not found"
    if new_parent_key not in comp_def:
        return f"New parent '{new_parent_key}' not found"
    if component_key == page_data.get("rootComponent"):
        return "Cannot move the root component"

    for comp in comp_def.values():
        comp.get("children", {}).pop(component_key, None)
    comp_def[new_parent_key].setdefault("children", {})[component_key] = True
    if display_order is not None:
        comp_def[component_key]["displayOrder"] = display_order
    return None


# ── Component-property normalization ─────────────────────────────────────────


def _resolve_events(page_data: dict[str, Any], props: dict[str, Any] | None) -> dict[str, Any]:
    """Turn event props that name an event function into its key (see _conventions)."""
    from . import _conventions as _c

    resolved, _notes = _c.resolve_event_prop_refs(page_data, props or {})
    return resolved


def _to_component_props(props: dict[str, Any]) -> dict[str, Any]:
    """Wrap raw {name: value} pairs into Modlix's {name: {"value": value}} shape.

    Preserves already-wrapped values:
      - single-valued: {"value": ...} or {"location": ...}
      - multi-valued: dict-of-entries where each entry has `property`/`order`
        (ANIMATION / ANIMATIONOBSERVER / validation / etc.)

    For multi-valued shape passthrough we delegate to conventions.is_multi_valued_shape
    so the detection stays in one place.
    """
    from . import _conventions as c

    out: dict[str, Any] = {}
    for k, v in props.items():
        if isinstance(v, dict) and ("value" in v or "location" in v):
            out[k] = v
        elif c.is_multi_valued_shape(v):
            out[k] = v  # already in multi-valued stored shape
        else:
            out[k] = {"value": v}
    return out


def _collect_descendants(comp_def: dict[str, Any], key: str, result: set[str]) -> None:
    children = comp_def.get(key, {}).get("children", {})
    for child_key, active in children.items():
        if active and child_key in comp_def:
            result.add(child_key)
            _collect_descendants(comp_def, child_key, result)


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for k, v in source.items():
        if k in target and isinstance(target[k], dict) and isinstance(v, dict):
            _deep_merge(target[k], v)
        else:
            target[k] = v


# ── Page summarization (surgical reads for huge pages) ──────────────────────


def build_page_summary(page_data: dict[str, Any]) -> dict[str, Any]:
    """High-level page overview: counts, type histogram, root structure, top events.

    Always cheap to produce regardless of page size. First read on any page.
    """
    comp_def: dict[str, Any] = page_data.get("componentDefinition") or {}
    events: dict[str, Any] = page_data.get("eventFunctions") or {}

    type_hist: dict[str, int] = {}
    binding_count = 0
    onclick_count = 0
    for comp in comp_def.values():
        if not isinstance(comp, dict):
            continue
        type_hist[comp.get("type", "?")] = type_hist.get(comp.get("type", "?"), 0) + 1
        for k in comp:
            if k.startswith("bindingPath"):
                binding_count += 1
        props = comp.get("properties") or {}
        if isinstance(props, dict) and ("onClick" in props or "onSubmit" in props):
            onclick_count += 1

    top_types = sorted(type_hist.items(), key=lambda kv: -kv[1])[:15]

    root_key = page_data.get("rootComponent", "")
    root = comp_def.get(root_key, {}) if isinstance(root_key, str) else {}
    root_children: list[dict[str, Any]] = []
    for child_key, active in (root.get("children") or {}).items():
        if not active or child_key not in comp_def:
            continue
        descendants: set[str] = set()
        _collect_descendants(comp_def, child_key, descendants)
        child = comp_def[child_key]
        root_children.append({
            "key": child_key,
            "type": child.get("type", "?"),
            "name": child.get("name", child_key),
            "subtree_size": len(descendants) + 1,
        })

    event_rows: list[dict[str, Any]] = []
    for key, defn in events.items():
        if not isinstance(defn, dict):
            continue
        event_rows.append({
            "key": key,
            "name": defn.get("name", "(unnamed)"),
            "steps": len((defn.get("steps") or {})),
        })
    event_rows.sort(key=lambda r: -r["steps"])

    return {
        "id": page_data.get("id"),
        "name": page_data.get("name"),
        "version": page_data.get("version"),
        "clientCode": page_data.get("clientCode"),
        "baseClientCode": page_data.get("baseClientCode"),
        "rootComponent": root_key,
        "rootType": root.get("type", "?"),
        "componentCount": len(comp_def),
        "eventFunctionCount": len(events),
        "componentsWithBindings": binding_count,
        "componentsWithClickHandlers": onclick_count,
        "topComponentTypes": [{"type": t, "count": n} for t, n in top_types],
        "rootChildren": root_children[:20],
        "topEventsBySteps": event_rows[:10],
        "title": _extract_title(page_data),
    }


def _extract_title(page_data: dict[str, Any]) -> str | None:
    title_obj = ((page_data.get("properties") or {}).get("title") or {})
    name = title_obj.get("name")
    if isinstance(name, dict):
        if "value" in name:
            return name["value"]
        if "location" in name:
            return f"<expression: {(name['location'] or {}).get('value')}>"
    return None


def build_subtree(
    page_data: dict[str, Any],
    root_key: str,
    *,
    max_depth: int = 3,
    max_components: int = 50,
) -> str:
    """Render the tree rooted at root_key, bounded by depth + count."""
    comp_def: dict[str, Any] = page_data.get("componentDefinition") or {}
    if root_key not in comp_def:
        return f"(component '{root_key}' not found)"

    lines: list[str] = []
    truncated = {"value": False}
    count = {"value": 0}

    def walk(key: str, depth: int) -> None:
        if count["value"] >= max_components:
            truncated["value"] = True
            return
        comp = comp_def.get(key)
        if not isinstance(comp, dict):
            return
        count["value"] += 1
        ctype = comp.get("type", "?")
        name = comp.get("name", key)
        props = comp.get("properties") or {}
        prop_keys = list(props.keys())[:4]
        if not prop_keys:
            prop_summary = ""
        else:
            more_marker = "...]" if len(props) > 4 else "]"
            prop_summary = "  [" + ", ".join(prop_keys) + more_marker
        lines.append(f"{'  ' * depth}- {key} ({ctype}) {name}{prop_summary}")
        if depth >= max_depth:
            children = comp.get("children") or {}
            active = [k for k, v in children.items() if v and k in comp_def]
            if active:
                lines.append(f"{'  ' * (depth+1)}... [{len(active)} children below depth limit; recurse from one]")
            return
        children = comp.get("children") or {}
        ordered = sorted(
            (k for k, active in children.items() if active and k in comp_def),
            key=lambda k: comp_def[k].get("displayOrder", 0),
        )
        for ck in ordered:
            walk(ck, depth + 1)

    walk(root_key, 0)
    if truncated["value"]:
        lines.append(f"... [hit max_components={max_components}; increase to see more]")
    return "\n".join(lines)


def search_components(
    page_data: dict[str, Any],
    *,
    component_type: str | None = None,
    name_contains: str | None = None,
    text_contains: str | None = None,
    has_binding: bool = False,
    has_event_handler: bool = False,
) -> list[dict[str, Any]]:
    """Find components matching filters. Returns key, type, name, depth."""
    comp_def: dict[str, Any] = page_data.get("componentDefinition") or {}
    root_key = page_data.get("rootComponent", "")

    depth: dict[str, int] = {}
    if root_key in comp_def:
        stack: list[tuple[str, int]] = [(root_key, 0)]
        while stack:
            k, d = stack.pop()
            if k in depth:
                continue
            depth[k] = d
            for child, active in (comp_def.get(k, {}).get("children") or {}).items():
                if active:
                    stack.append((child, d + 1))

    results: list[dict[str, Any]] = []
    for key, comp in comp_def.items():
        if not isinstance(comp, dict):
            continue
        if component_type and comp.get("type") != component_type:
            continue
        if name_contains and name_contains.lower() not in (comp.get("name") or "").lower():
            continue
        if text_contains:
            raw = str(comp.get("properties") or {})
            if text_contains.lower() not in raw.lower():
                continue
        if has_binding and not any(k.startswith("bindingPath") for k in comp):
            continue
        if has_event_handler:
            props = comp.get("properties") or {}
            if not (isinstance(props, dict) and any(p in props for p in ("onClick", "onSubmit", "onChange", "onBlur", "onFocus"))):
                continue
        results.append({
            "key": key,
            "type": comp.get("type", "?"),
            "name": comp.get("name", key),
            "depth": depth.get(key, -1),
        })

    results.sort(key=lambda r: (r["depth"], r["type"], r["name"]))
    return results


def summarize_component(page_data: dict[str, Any], component_key: str) -> dict[str, Any] | None:
    """Detail one component: type, properties, children, bindings, style keys."""
    comp_def = page_data.get("componentDefinition") or {}
    comp = comp_def.get(component_key)
    if not isinstance(comp, dict):
        return None
    out: dict[str, Any] = {
        "key": component_key,
        "type": comp.get("type", "?"),
        "name": comp.get("name", component_key),
        "displayOrder": comp.get("displayOrder"),
        "properties": comp.get("properties") or {},
        "children": list((comp.get("children") or {}).keys()),
    }
    for bp_key in ("bindingPath", "bindingPath2", "bindingPath3", "bindingPath4", "bindingPath5", "bindingPath6"):
        if bp_key in comp:
            out[bp_key] = comp[bp_key]
    style_props = comp.get("styleProperties") or {}
    if style_props:
        out["stylePropertyKeys"] = list(style_props.keys())
    return out


# ── Validation ───────────────────────────────────────────────────────────────


def validate_page_structure(page_data: dict[str, Any]) -> list[str]:
    """Return a list of structural issues; empty list means valid."""
    issues: list[str] = []
    comp_def = page_data.get("componentDefinition") or {}
    root = page_data.get("rootComponent")

    if not root:
        issues.append("Missing rootComponent.")
    elif root not in comp_def:
        issues.append(f"rootComponent='{root}' has no entry in componentDefinition.")

    # Reachability — every component should be reachable from root via children.
    reachable: set[str] = set()
    if root and root in comp_def:
        stack = [root]
        while stack:
            k = stack.pop()
            if k in reachable:
                continue
            reachable.add(k)
            for child, active in (comp_def.get(k, {}).get("children") or {}).items():
                if active:
                    stack.append(child)

    orphans = [k for k in comp_def if k not in reachable]
    for o in orphans:
        issues.append(f"Orphan component '{o}' is not reachable from root.")

    # Dangling child references
    for key, comp in comp_def.items():
        for child, active in (comp.get("children") or {}).items():
            if active and child not in comp_def:
                issues.append(f"Component '{key}' references missing child '{child}'.")

    return issues
