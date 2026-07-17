# Workflow: create-app-full

**Goal:** Create a new Modlix app that's reachable by users in a browser.

**Touches services:** security, ui

## Preconditions

- Caller has `Authorities.Application_CREATE` (or is a SYSTEM-client sysadmin).
- Intended `appCode` is letters-only — no digits, dashes, underscores.

## Steps

### 1. Security registration (creates the app row in security)

```python
sec_resp = modlix.post('/api/security/applications', {
    'appCode': 'myapp',
    'appName': 'myapp',          # MUST equal appCode (platform validation)
    'appType': 'SITE',            # APP | SITE | POSTER
    'appAccessType': 'OWN',       # OWN | ANY | EXPLICIT
})
# 200 → returns {id, ...}
# 500 with "duplicate" → app already exists; this step is idempotent, continue to step 2
```

### 2. UI override doc (makes the app readable via /api/ui/*)

```python
ui_resp = modlix.post('/api/ui/applications', {
    'appCode': 'myapp',
    'name': 'myapp',              # MUST equal appCode
    'clientCode': 'SYSTEM',       # owning tenant
    'message': 'Created via workflow create-app-full',
    'properties': {
        'defaultPage': 'home',
        'fontPacks': {},          # platform shell crashes if undefined
        'iconPacks': {},
    },
    'languages': {'en': {}},      # MAP, not a list
    'translations': {},
})
# 200 → returns {id, version, ...}
# 409 → UI doc already exists; the app is now fully usable
```

Without step 2, every subsequent `/api/ui/*` read returns 403 even
though the security row exists. The Modlix UI button only calls step
1 — the IDE creates step 2 on first save.

## Failure modes

- `400 "App Code should not contain any special characters"` → step 1's `appCode` has digits/dashes/underscores. Use letters only.
- `400 "Application name should match with appCode"` → `appName` (step 1) or `name` (step 2) must literally equal `appCode`.
- `500 duplicate key` on step 1 → app already exists; proceed to step 2 with same appCode (idempotent).
- `403` on subsequent `/api/ui/*` reads after step 1 → step 2 was skipped.

## Anonymous browser access

If you want users without a login to view pages in this app, set
`appAccessType: 'ANY'` in step 1 (or PUT the security row later to flip
it). With `OWN` the browser gets a 403 when there's no JWT.

## Public marketing-style pages

Pages should omit `permission` (default public). For clone-style apps
where you want NO chrome around the pages, also omit `shellPage` from
`properties` so pages render standalone.

## Related workflows

- `delete-app-full` — cleanup (security + ui + mongo + caches)
- `grant-app-access` — required when `appAccessType: 'EXPLICIT'`
- `update-app-defaultPage` — change the landing page
- `create-page` — first page inside the new app
