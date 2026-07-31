---
name: componentDefinition shape invariants
description: Platform-wide invariants for a page's componentDefinition object. Especially relevant when writing components directly (instead of via add_component).
type: reference
---

# componentDefinition invariants

A Modlix page's `componentDefinition` is a flat dict keyed by component identifier:

```json
"componentDefinition": {
  "<key>": {
    "key": "<key>",         // MUST equal the outer key
    "name": "<display>",
    "type": "Grid" | "Text" | "Link" | "Animator" | ...,
    "displayOrder": <int>,  // sort position within parent's children
    "children": {           // child references (optional, by KEY not UUID)
      "<childKey>": true,
      ...
    },
    "properties": {...},
    "styleProperties": {...},
    ...
  },
  ...
}
```

## Invariant 1: outer key === inner `key`

`componentDefinition[K].key === K` for every K. Always. Mismatch breaks:
- The page editor's component tree (DOM ref to missing key)
- Anything that walks `.key` instead of the outer key (event-function resolvers, binding lookups)
- Visual diffs / debugging — components show up as anonymous

The MCP `add_component` tool handles this automatically — it uses the
provided `component_key` (or a generated UUID) as BOTH the outer dict key AND
the inner `.key`. Bug surface is when scripts write directly:

```python
# WRONG
cd[uuid.uuid4().hex] = {"key": "trustAnim", ...}

# RIGHT
key = "trustAnim"
cd[key] = {"key": key, ...}
```

## Invariant 2: parent's `children` dict references child by KEY

The `children` field on each component is `{<childKey>: true, ...}` — not UUIDs, not numeric indices. The keys are the same component-key strings used in the componentDefinition dict.

So:
- `componentDefinition.trustAnim.children` contains `{"trust": true}`
- `componentDefinition.root.children` contains `{..., "trustAnim": true, ...}`

When you reparent a component, you must update BOTH the old parent (remove from its children) AND the new parent (add to its children). When you rename a component's key, you must update every parent that references it.

## Invariant 3: displayOrder lives on the child, not the parent

Each component has its own `displayOrder` integer; rendering sorts siblings by it (alphabetical-by-key on ties). The parent's `children` dict only stores membership (`{childKey: true}`), not order.

Don't try to pack order info into the children dict.

## Invariant 4: `version` is platform-managed

Every component has an implicit version tracked at the page level
(`componentVersions[key]`). The surgical PATCH endpoint enforces
optimistic locking against this. Don't manually set component versions
when writing directly — use `add_component` / `patch_component_props` /
`patch_component_styles` which handle version negotiation.

## Pitfalls when writing components directly via scripts

The MCP tools (`add_component`, `move_component`, `remove_component`,
`patch_component_*`) all maintain these invariants. Reach for raw page
edits only when:
- You need atomicity across many changes (single save vs. many tool calls)
- You're doing a bulk migration / rename
- The tool surface doesn't cover the case

When you do edit raw:

1. **Outer key = inner key.** Use the human-readable component key as the
   dict key.
2. **Update children dicts on rename.** If you change a component's outer
   key, walk every other component and rewrite any matching child
   reference.
3. **Don't pack data into the children dict.** Just `{childKey: true}`.
4. **Preserve `displayOrder`** when moving — set it explicitly on the
   moved child rather than relying on insertion order.

## Pre-flight check

Before saving, run this sanity-check:

```python
mismatches = [
    (k, c.get("key"))
    for k, c in (page.get("componentDefinition") or {}).items()
    if isinstance(c, dict) and c.get("key") and c.get("key") != k
]
if mismatches:
    raise ValueError(f"componentDefinition key mismatches: {mismatches}")
```

Single line, catches the most common direct-write bug.
