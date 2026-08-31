# ui — gotchas

Captured from real agent runs.

## Page `permission` is OPT-IN, not required
A page document with NO `permission` field is publicly accessible (subject
to the app's `appAccessType`). Adding `permission` RESTRICTS the page to
users with that authority.

There is NO generic "public" authority. Don't add
`Authorities.ANYTIME` — it isn't real. Just omit the field for public
pages.

## Page `wrapShell` is OPT-IN
Setting `wrapShell: true` opts the page into being wrapped by the app's
configured `shellPage` (the chrome around the page). Default is
unwrapped/standalone.

For clone scenarios and any marketing/landing/standalone page, set
`wrapShell: false` explicitly to be safe — some apps have shell
defaults that surprise you. The v3 clonelinear scenario broke because
the appbuilder app's shell ALWAYS wrapped pages regardless of the
page-level setting; the fix was to build in a different app (`vclone`)
with no shellPage configured.

## `/api/ui/pages/{X}` takes a MONGO ID, not a name
This endpoint:
```
GET /api/ui/pages/{X}
```
expects `{X}` to be the Mongo `id` of the page document, NOT the page
`name`. Hitting it with a name returns `404 "Page with id <name> not
found"`.

To fetch by name: list the pages with `appCode` filter, find the entry
whose `name` matches, then GET by its `id`:
```python
pages = modlix.get('/api/ui/pages', params={'appCode': 'X', 'size': 200})
home = next(p for p in pages['content'] if p['name'] == 'home')
detail = modlix.get(f'/api/ui/pages/{home["id"]}')
```

The v4 SDK's `modlix.pages.get('home', app_code='X')` does this lookup
internally.

## List endpoint strips nested `properties` — use the detail endpoint
`GET /api/ui/applications?appCode=<X>` returns a Spring page with a list
of app summaries. That summary view sometimes omits or nulls `properties`
even when the full doc has them — it's not a stripped view, it's a list
projection.

Always check `properties` via `GET /api/ui/applications/{mongo_id}` (by
id), not via the list filter. Same trap exists on page list-vs-detail.

## `/api/ui/applications/{X}` also takes a Mongo ID
Same trap. `GET /api/ui/applications/myapp` returns
`404 "Application with id myapp not found"`. To fetch one app's UI
override doc by `appCode`:
```python
docs = modlix.get('/api/ui/applications', params={'appCode': 'myapp'})
ui_doc = docs['content'][0]
```

## `pageType=PAGE` filter returns 0 rows — DON'T USE IT
Adding `pageType=PAGE` (or any value) to the `/api/ui/pages` query string
filters everything to zero. Just omit the param entirely.

## Page replace requires the FULL document
`PUT /api/ui/pages/{id}` with a partial body returns `200` but SILENTLY
DROPS your changes. The platform expects the full document including
`id`, `version`, `clientCode`, `createdAt`, etc.

Correct pattern: fetch current → merge your edits → PUT the merged dict.
The v4 SDK's `modlix.pages.replace(name, definition, ...)` does this
merge for you.

## UI app create body needs specific fields
The POST to `/api/ui/applications` rejects with `400 "Please try again"`
when these are missing:
- `clientCode` (set to the owning client, usually "SYSTEM")
- `properties.fontPacks: {}` (platform shell crashes if undefined)
- `properties.iconPacks: {}` (same)
- `languages` as a `{<code>: {}}` map (not a list)

Use the workflow in `workflows/create-app-full.md` for the canonical body.

## Component definition shape rules
- `componentDefinition` is a flat map `{<uuid>: <component>}`.
- The page-level `rootComponent` field points at one UUID key in that map.
- Each component has `key`, `name`, `type`, `properties`, `styleProperties`, optional `children`.
- `children` is a MAP `{childKey: True}` — NOT a list, NOT a bare set.
- Property literals wrap as `{value: 'x'}`. Expressions wrap as `{location: {type: 'EXPRESSION', value: 'Page.x'}}`.
- `bindingPath` shape: `{type: 'VALUE', value: 'Page.fieldName'}`.
- `styleProperties` keys are UUIDs (use `modlix.uuid()`); values are `{resolutions: {ALL: {<cssProp>: {value: '...'}}}}`.

## Component types: `Text` not `TextLabel`
The platform's text-display component is named `Text` in the catalog —
NOT `TextLabel`, `Label`, or `Span`. Calling
`modlix.catalog.get_schema('TextLabel')` raises KeyError.

When you need a button: `Button`. Container: `Grid` (most common) or
`Container`. Page root container: `Page`.
