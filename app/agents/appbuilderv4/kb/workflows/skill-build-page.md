---
name: build-page
description: Build a Modlix page from a natural-language description, using the modlix-mcp MCP tools.
---

# build-page

Use this skill when the user asks you to build a new page (or a fresh section of one) in their Modlix no-code platform. It orchestrates the `modlix-mcp` MCP tools to plan, compose, validate, and preview.

## When to use

- "Build me a login page with email/password and a sign-in button"
- "Create a dashboard page with a stats grid and a recent-orders table"
- "Make a page that has a header, hero section, and CTA"

## When NOT to use

- The user wants to edit an existing page in place — use the composition tools directly (`add_component`, `update_component_props`, `set_styles`, `set_bindings`, `move_component`, `remove_component`).
- The user wants to inspect a page — use `get_page` / `validate_page` directly.
- The user is asking about something that isn't a Modlix page (regular code, etc.).

## Required tools (provided by the modlix-mcp MCP server)

- `whoami` — sanity-check auth before doing anything destructive.
- `list_apps` / `get_app` — find / confirm the target `appCode`.
- `list_component_types` / `get_component_schema` / `get_component_examples` — discover what's available and how to configure it.
- `list_pages` / `get_page` — see what already exists.
- `create_page` — make the page (always starts with a root `Grid`).
- `add_component`, `update_component_props`, `set_styles`, `set_bindings`, `move_component`, `remove_component` — compose the tree.
- `validate_page` — sanity-check structure.
- `get_preview_url` — hand the user a clickable URL.

## Workflow

1. **Confirm context.** If the user hasn't named an app, call `list_apps` and ask them which one. Don't guess — `MODLIX_DEFAULT_APP_CODE` may or may not be set in the MCP server env.

2. **Plan the structure.** Sketch the component tree in your head (or in chat for complex pages) before calling any tool. Use a small number of containers (`Grid` / `Flex`) and put leaf components (`Text`, `Button`, `TextBox`, `Image`, `Icon`) inside them.

3. **Look up component schemas you're unsure about.** A 30-line `get_component_schema('Button')` call is cheaper than guessing wrong property names and looping with errors.

4. **Create the page.** `create_page(name, app_code, title)`. It comes with a root `Grid` keyed `"root"`.

5. **Compose top-down.** Add containers first, then their children:
   ```
   add_component(page_name="login", parent_key="root", component_type="Flex",
                 properties={"direction": "COLUMN", "gap": "16px", "alignItems": "center"})
   # the call returns the new key, e.g. <uuid-1>
   add_component(page_name="login", parent_key="<uuid-1>", component_type="Text",
                 properties={"text": "Sign in", "textType": "H1"})
   add_component(page_name="login", parent_key="<uuid-1>", component_type="TextBox",
                 properties={"label": "Email", "placeholder": "you@company.com"},
                 binding_paths={"bindingPath": {"type": "VALUE", "value": "Page.formData.email"}})
   ```

6. **Bind inputs.** Form inputs need a `bindingPath` to a page-state location. Use `set_bindings` after the fact or `binding_paths=` on `add_component`.

7. **Wire events.** For `Button.onClick`, set the `onClick` property to the name of an event function on the page. Creating event functions is not yet exposed as an MCP tool — point the user at the editor for now, or update the page-level `eventFunctions` map via the Modlix UI.

8. **Validate.** Call `validate_page` to catch orphans and dangling refs.

9. **Preview.** Call `get_preview_url` and give the user the link.

## Property shape — important

The MCP tools accept *raw* property values and wrap them into Modlix's `{value: ...}` shape for you:

```python
properties={"label": "Save"}        # tool wraps → {"label": {"value": "Save"}}
```

If you need an expression (computed/bound), pass the full `{location: {type: "EXPRESSION", value: "..."}}` form yourself:

```python
properties={"label": {"location": {"type": "EXPRESSION", "value": "Page.formData.userName"}}}
```

Style properties are *not* auto-wrapped — pass the full nested shape:

```python
style_properties={
  "(root)": {"resolutions": {"ALL": {"backgroundColor": {"value": "#fafafa"}, "padding": {"value": "24px"}}}}
}
```

## Common mistakes to avoid

- **Don't pre-generate component keys.** Let `add_component` generate UUIDs; capture each returned key for the next call.
- **Don't update the root component's type.** It's a `Grid` — re-parent if you need `Flex` behavior.
- **Don't skip validation.** A page can save with orphans; the preview just won't render them.
- **Don't fight the API on retries.** If a property errors with "unknown prop", call `get_component_schema` instead of guessing again.
