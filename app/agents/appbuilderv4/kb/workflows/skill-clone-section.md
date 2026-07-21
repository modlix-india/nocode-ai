---
name: clone-section
description: Copy a section (subtree of components) from one Modlix page to another using the modlix-mcp MCP tools.
---

# clone-section

Use this skill to lift a chunk of UI from one page and drop it onto another — e.g. "copy the header from `landing` to `pricing`", "reuse the form on `signup` for `inviteUser`".

## When to use

- "Take the hero section from page X and put it on page Y."
- "Duplicate this form into a new page."
- "I want the same header across all pages."

## When NOT to use

- Whole-page duplication — easier to read the page JSON and POST it back with a new name.
- The user wants a single shared component, not a clone — Modlix has theme/style and override mechanisms for that; suggest those instead.

## Required tools

- `get_page(include='full')` — get the source page's full JSON, including `componentDefinition`.
- `add_component` — recreate each component on the destination page.
- `set_bindings`, `set_styles`, `update_component_props` — only needed if you want to tweak as you copy.
- `validate_page` — confirm the destination is clean.

## Workflow

1. **Identify source and destination.** Confirm both `page_name` values and the source's subtree root (the component key at the top of the section to clone).

2. **Fetch the source.** `get_page(name=source, include='full')` → JSON. Parse `componentDefinition` and find the source root and all descendants (walk the `children` maps).

3. **Walk the subtree breadth-first.** For each component in dependency order (parents before children):
   - On the first node, call `add_component(page_name=dest, parent_key=<dest parent>, component_type=src_node.type, properties=<unwrapped>, style_properties=src_node.styleProperties, binding_paths=<bindingPath* keys from src>)`.
   - For every other node, use the *new* key of its parent (returned by `add_component`) — keep a `src_key -> dest_key` map.

4. **Unwrap properties before passing.** `add_component` re-wraps `{value: ...}`, so strip that layer first when reading from source:

   ```python
   raw = {k: (v.get("value") if isinstance(v, dict) and "value" in v else v) for k, v in src_props.items()}
   ```

   If the source uses `{location: ...}` (an expression), pass it through verbatim.

5. **Don't clone binding paths blindly.** Bindings often reference page-specific state (`Page.formData.email`). Either:
   - Mirror the same state shape on the destination page, or
   - Rewrite the binding paths during the walk (`Page.X` is global to the page, so it's only "shared" if both pages have the same state shape).

6. **Validate.** `validate_page(dest)` to catch any missing-child references.

7. **Preview.** `get_preview_url(dest)`.

## Pitfalls

- **Key collisions.** UUIDs are fresh per `add_component`, so collisions don't happen — but don't try to reuse source keys.
- **Style inheritance.** If the source relies on a parent's style (e.g. `Flex direction=COLUMN`), recreate that parent too — copying only the leaf won't carry the layout.
- **Event functions.** `eventFunctions` live on the page, not the component. If the source button calls `onClickSubmit`, that function must also exist (or be created) on the destination page.
