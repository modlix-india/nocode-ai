"""Auto-layout for Kirun step graphs.

Ported verbatim from modlix-mcp/modlix_mcp/kirun_layout.py — no internal
deps to rewrite, so this is a direct copy.

Mirrors the algorithm in `nocode-kirun/kirun-ui/src/util/autoLayout.ts` —
the same routine the visual KIRunEditor uses when it lays out a DSL-edit's
output. Keeps positions identical to what the editor would produce on its
own text→visual swap, so saving via the agent lands the graph in the same
shape as if a human typed the DSL in the editor and hit "visual".

Algorithm summary (port of autoLayoutFunctionDefinition):
  1. Collect each step's dependencies from THREE sources:
     - `dependentStatements` keys (`Steps.<name>.<output>`)
     - any `Steps.<name>.` regex match inside parameterMap expressions
     - any `Steps.<name>.` regex match inside parameterMap raw values
     (only deps pointing at a real step in this function, excluding self)
  2. Topological layering by Kahn's: each iteration emits the layer of
     remaining in-degree-zero nodes (sorted alphabetically). Cycle break:
     if nothing has in-degree zero, force the lowest-in-degree node.
  3. Positions:
     - x = startX + layerIdx * (nodeWidth + gap)
     - layer 0 packs nodes top-down at startY + cumulative heights
     - layer N>0 places each node at the barycenter (mean top) of its deps,
       then sorts the layer by targetY and packs without overlap

Defaults match the editor's text→visual call:
    nodeWidth=280, nodeHeight=180, gap=100, startX=startY=50
which yields horizontal stride 380 and per-node vertical stride 280.
"""

from __future__ import annotations

import re
from typing import Any

_DEFAULT_NODE_WIDTH = 280.0
_DEFAULT_NODE_HEIGHT = 180.0
_DEFAULT_GAP = 100.0
_DEFAULT_START_X = 50.0
_DEFAULT_START_Y = 50.0

# Same shape as the JS regex: must end with a dot to require a following
# segment (output / value / etc.), so a bare reference to a non-step like
# "StepsCounter" doesn't match.
_STEPS_REF_RE = re.compile(r"Steps\.([a-zA-Z0-9_\-]+)\.")


def _extract_step_refs(value: Any, step_names: set[str], deps: set[str]) -> None:
    """Walk strings / lists / dicts collecting Steps.<name>. references."""
    if isinstance(value, str):
        for m in _STEPS_REF_RE.finditer(value):
            name = m.group(1)
            if name in step_names:
                deps.add(name)
    elif isinstance(value, list):
        for item in value:
            _extract_step_refs(item, step_names, deps)
    elif isinstance(value, dict):
        for v in value.values():
            _extract_step_refs(v, step_names, deps)


def _collect_deps(step_name: str, statement: dict[str, Any], step_names: set[str]) -> set[str]:
    """Union of deps from dependentStatements + parameterMap expressions + values."""
    deps: set[str] = set()

    dep_map = statement.get("dependentStatements") or {}
    if isinstance(dep_map, dict):
        for dep_path, v in dep_map.items():
            if v is False:
                continue
            parts = str(dep_path).split(".")
            if len(parts) >= 2 and parts[0] == "Steps":
                dep_name = parts[1]
                if dep_name in step_names and dep_name != step_name:
                    deps.add(dep_name)

    param_map = statement.get("parameterMap") or {}
    if isinstance(param_map, dict):
        for param_refs in param_map.values():
            if not isinstance(param_refs, dict):
                continue
            for param_ref in param_refs.values():
                if not isinstance(param_ref, dict):
                    continue
                expr = param_ref.get("expression")
                if expr:
                    _extract_step_refs(expr, step_names, deps)
                val = param_ref.get("value")
                if val is not None:
                    _extract_step_refs(val, step_names, deps)

    deps.discard(step_name)
    return deps


def auto_layout_steps(
    steps: dict[str, Any],
    *,
    node_width: float = _DEFAULT_NODE_WIDTH,
    node_height: float = _DEFAULT_NODE_HEIGHT,
    gap: float = _DEFAULT_GAP,
    start_x: float = _DEFAULT_START_X,
    start_y: float = _DEFAULT_START_Y,
) -> int:
    """Mutate `steps` in place: set `position: {left, top}` on each entry.

    Returns the number of steps repositioned. Safe on empty / non-dict input.
    """
    if not isinstance(steps, dict) or not steps:
        return 0

    step_names = set(steps.keys())
    depends_on: dict[str, set[str]] = {}
    depended_by: dict[str, set[str]] = {name: set() for name in step_names}

    for name, statement in steps.items():
        deps = _collect_deps(name, statement if isinstance(statement, dict) else {}, step_names)
        depends_on[name] = deps

    for name, deps in depends_on.items():
        for dep_name in deps:
            depended_by[dep_name].add(name)

    in_degree: dict[str, int] = {name: len(deps) for name, deps in depends_on.items()}
    assigned: set[str] = set()
    layers: list[list[str]] = []

    while len(assigned) < len(steps):
        # Preserve original step order before sorting; collect candidates that
        # have all deps satisfied so far.
        layer = [n for n in steps if n not in assigned and in_degree.get(n, 0) == 0]

        if not layer:
            # Cycle break: nothing has in-degree zero. Force the min-in-degree
            # remaining node so we make forward progress.
            min_deg = float("inf")
            min_node: str | None = None
            for n in steps:
                if n in assigned:
                    continue
                deg = in_degree.get(n, 0)
                if deg < min_deg:
                    min_deg = deg
                    min_node = n
            if min_node is None:
                break
            layer = [min_node]

        # Sort each layer alphabetically — matches the JS `a.localeCompare(b)`.
        layer.sort()
        layers.append(layer)

        for name in layer:
            assigned.add(name)
            for dependent in depended_by.get(name, ()):
                in_degree[dependent] = in_degree.get(dependent, 1) - 1

    positions: dict[str, tuple[float, float]] = {}

    for layer_idx, layer in enumerate(layers):
        x = start_x + layer_idx * (node_width + gap)

        if layer_idx == 0:
            current_y = start_y
            for name in layer:
                positions[name] = (x, current_y)
                current_y += node_height + gap
            continue

        # Each downstream node aims for the mean top-Y of its deps; ties
        # broken by stable secondary sort key (original layer order index)
        # to match JS Array.prototype.sort stability.
        targets: list[tuple[str, float, int]] = []
        for idx, name in enumerate(layer):
            deps = depends_on.get(name, set())
            target_y = start_y
            if deps:
                ys = [positions[d][1] for d in deps if d in positions]
                if ys:
                    target_y = sum(ys) / len(ys)
            targets.append((name, target_y, idx))
        targets.sort(key=lambda t: (t[1], t[2]))

        last_y = start_y
        last_assigned = False
        for name, target_y, _idx in targets:
            min_y = last_y + node_height + gap if last_assigned else last_y
            y = max(target_y, min_y)
            positions[name] = (x, y)
            last_y = y
            last_assigned = True

    # Write back into the step dicts.
    count = 0
    for name, (left, top) in positions.items():
        step = steps.get(name)
        if not isinstance(step, dict):
            continue
        step["position"] = {"left": left, "top": top}
        count += 1
    return count


def auto_layout_definition(defn: dict[str, Any]) -> int:
    """Apply auto-layout to a FunctionDefinition or page-event-function shape.

    Handles both:
      - top-level `steps` (functions, server functions, page event functions)
      - nested `eventFunctions[<key>].steps` (page-level event maps)
    """
    if not isinstance(defn, dict):
        return 0
    total = 0
    if isinstance(defn.get("steps"), dict):
        total += auto_layout_steps(defn["steps"])
    event_funcs = defn.get("eventFunctions")
    if isinstance(event_funcs, dict):
        for ev in event_funcs.values():
            if isinstance(ev, dict) and isinstance(ev.get("steps"), dict):
                total += auto_layout_steps(ev["steps"])
    return total
