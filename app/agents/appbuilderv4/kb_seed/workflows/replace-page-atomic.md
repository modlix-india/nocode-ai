# Workflow: replace-page-atomic

**Goal:** Atomically replace a page's `componentDefinition` + `properties`
with a fully composed new version, in one PUT.

**Touches services:** ui

## Why "atomic"

The alternative pattern — dozens of `add_component` / `patch_component_*`
calls — burns turns and produces inconsistent intermediate states.
Atomic replace lets you compose the entire page in Python, then send
the full document in one shot.

## Preconditions

- The page must already exist (create it first via `create-page` workflow).
- You have a complete page definition dict containing `rootComponent` +
  `componentDefinition`.

## Steps

### 1. Resolve page name → Mongo id

`/api/ui/pages/{X}` takes the Mongo `id`, NOT the page name. So:

```python
pages = modlix.get('/api/ui/pages',
                   params={'appCode': 'myapp', 'size': 200})
target = next(p for p in pages['content'] if p['name'] == 'home')
page_id = target['id']
```

### 2. Fetch current full detail

The PUT requires the entire document. Skipping this step makes the
PUT return 200 but silently DROP your changes (id/version/clientCode
are missing).

```python
current = modlix.get(f'/api/ui/pages/{page_id}')
```

### 3. Merge your edits into the fetched dict

```python
merged = dict(current)
merged['rootComponent'] = '<new-root-uuid>'
merged['componentDefinition'] = {
    '<new-root-uuid>': {
        'key': '<new-root-uuid>',
        'name': 'rootGrid',
        'type': 'Grid',
        'children': {'<child-uuid>': True},
        'properties': {},
        'styleProperties': {},
    },
    # ... rest of the tree
}
merged['properties'] = {...}
merged['message'] = 'Hero region built'
```

### 4. PUT the merged document

```python
resp = modlix.put(f'/api/ui/pages/{page_id}', body=merged)
# 200 → returns the new version
```

The v4 SDK helper `modlix.pages.replace(name, definition, app_code='myapp', message='...')`
does steps 1-4 for you — pass `definition` containing only the fields you
want to change.

## Wrap conventions in the definition

- Component `children`: `{childKey: True}` map (NOT a list).
- Property literals: `{value: 'x'}`.
- Property expressions: `{location: {type: 'EXPRESSION', value: 'Page.x'}}`.
- `styleProperties` keys: UUIDs via `modlix.uuid()`.
- `styleProperties` values: `{resolutions: {ALL: {<cssProp>: {value: '...'}}}}`.
- `bindingPath`: `{type: 'VALUE', value: 'Page.fieldName'}`.

## Failure modes

- Partial PUT returns 200 but no-op'd → you forgot to merge with current.
- `404 "Page with id <name> not found"` → you hit the endpoint with a name; need the id.
- `403 "Forbidden access to the application..."` → the app's UI override doc is missing (run create-app-full step 2) OR your JWT lacks app access (run grant-app-access).

## Related workflows

- `create-page` — create the page before replacing
- `create-app-full` — must run before any page can be created
