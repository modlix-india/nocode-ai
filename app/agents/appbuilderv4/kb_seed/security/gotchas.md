# security — gotchas

Captured from real agent runs that paid the cost of discovering these.

## `Authorities.ANYTIME` does NOT exist
There is NO generic "anyone can view" authority string. Values like
`Authorities.ANYTIME`, `Authorities.ANY`, `Authorities.PUBLIC` are not in
the platform's authority enum — every agent that has used them has either
silently no-op'd or thrown 400 on validation.

Real authority strings are namespaced like `Authorities.User_READ`,
`Authorities.Application_READ`, `Authorities.Page_UPDATE`, etc. They come
from the `security_v2_authority` table at runtime.

## App creation is a 2-step recipe
ONE POST to `/api/security/applications` (security row) does NOT make the
app usable. Without the second POST to `/api/ui/applications` (UI override
doc), every subsequent `/api/ui/*` read returns 403 even though the
security row exists.

The platform's UI button only calls step 1 — the IDE creates step 2 on
first save. The CFA agent owns both writes; see workflows/create-app-full.

```python
# Step 1
sec = modlix.post('/api/security/applications', {
    'appCode': 'myapp', 'appName': 'myapp',   # appName MUST equal appCode
    'appType': 'SITE', 'appAccessType': 'OWN',
})
# Step 2
ui = modlix.post('/api/ui/applications', {
    'appCode': 'myapp', 'name': 'myapp',       # name MUST equal appCode
    'clientCode': 'SYSTEM',
    'properties': {'defaultPage': 'home', 'fontPacks': {}, 'iconPacks': {}},
    'languages': {'en': {}},                   # MAP, not a list
    'translations': {},
})
```

## `appCode` is letters-only
The security validator at AppService.java:107 enforces
`onlyAlphabetAllowed` — no digits, no dashes, no underscores. `v4clone`
fails with "App Code should not contain any special characters". Use
`vclone` instead.

## `appName` must equal `appCode` in BOTH steps
Platform-side validation in both endpoints rejects mismatched values with
"Application name should match with appCode" — even though `appName` is
in step 1's body and `name` is in step 2's body. Always set them equal to
`appCode`.

## `appAccessType` values
- `OWN` — only the creating client can read (default). Anonymous browsers get 403.
- `ANY` — public; any client (or anonymous) can read. Use for marketing/landing pages.
- `EXPLICIT` — requires `POST /api/security/applications/{id}/access/{clientCode}` per allowed client.

If a page renders fine for your JWT but a browser opens it and gets 403, the app is `OWN` and the browser session has no JWT. Either flip to `ANY` or sign in first.

## JWT TTL is ~30 minutes
The platform-issued JWT has a short lifetime. A long-running script that
holds one JWT across hours of work WILL fail mid-task. Two patterns:
- Chain multiple chat sessions, refresh JWT between (see
  scripts/cfa_v4_overnight_loop.py).
- Or build an SDK that re-auths inside the subprocess on 401.

## App delete returns vague 400 on success
`DELETE /api/security/applications/{appCode}` often returns a generic 400
"Please try again. A server error - <id>" even when the row IS deleted.
Don't trust the response code — re-list to confirm.

## Cache eviction endpoints don't exist at the obvious paths
`DELETE /api/security/cache/keys/{X}` returns 404 — that surface isn't
mounted on the security service. The shared cache (cmn-) TTLs naturally;
just wait or restart. To force-evict from code, use the platform's own
admin tools, not these paths.
