# shared — gotchas

Cross-cutting platform sharp edges captured from agent runs.

## JWT TTL ~30 minutes
The platform-issued JWT expires roughly 30 minutes after issue. Any
long-running automation that holds one JWT for hours WILL fail mid-task
with `401 Authentication failed`. Patterns to handle it:

- Short-lived scripts: just refresh once before launch (`POST /api/security/authenticate`).
- Hours-long jobs: chain multiple short sessions, refreshing between
  each (see `scripts/cfa_v4_overnight_loop.py`).
- Tool authors: catch 401 inside the tool's HTTP call and re-auth before
  retrying — the credentials must be available to the tool (env var or
  secrets store).

## HTTP responses aren't always JSON dicts
Some platform endpoints return:
- `DELETE /api/ui/pages/{id}` → bare `true` (a JSON literal, not a dict)
- Empty list pages → `{"content": [], ...}` (still a dict)
- Error envelope → `{"exceptionId": "...", "message": "...", ...}`

Code that unconditionally does `resp.get("content")` or `resp["id"]` on
DELETE results will AttributeError. SDK wrappers should normalise: bool/
int/str → `{"_status": <code>, "value": <parsed>}` and only THEN expose
`.get()`.

## `session.context` is persisted to MySQL `CONTEXT_JSON`
The session context dict gets saved to the `ai_sessions` table after
each turn. The `CONTEXT_JSON` column is bounded (TEXT class, ~64KB
typical). Stuffing base64 PNGs or anything binary-heavy into
`session.context` will overflow the column with the cryptic error
`(1406, "Data too long for column 'CONTEXT_JSON'")` — and the session
silently stops persisting.

For session-scoped caches of large payloads (screenshots, generated
images): use a PROCESS-LEVEL dict keyed by `session_id`, NOT
`session.context`. See `app/agents/appbuilderv4/tools/_shot_cache.py`.

## Component catalog URL is config-server controlled
`COMPONENT_CATALOG_URL` is loaded from the config server's
`application-default.yml` (`ai.componentCatalogUrl`), overriding any
`.env` value. If the config-server value is wrong (404), the catalog
loader silently falls back to a hardcoded 17-type list — masking the
problem.

Symptom: agent says "Apps count: 1, First 5 appCodes: [None]" or
`list_types()` returns 17 types instead of 72. Fix in
`nocode-saas/configfiles/application-default.yml`.

## Eureka discovery has a 30-60s propagation delay
After nocode-ai starts up with `EUREKA_ENABLED=true`, the gateway needs
30-60 seconds before `/api/ai/**` requests routed via service discovery
work. During that window you'll get `503 SERVICE_UNAVAILABLE "Unable to
find instance for ai"`. Just wait it out.

## App pages render via `apps.local.modlix.com` host
For a page in app `<appCode>` owned by client `<clientCode>`:
```
https://apps.local.modlix.com/<appCode>/<clientCode>/page/<pageName>
```

Both `*.local.modlix.com` subdomains resolve to 127.0.0.1; the gateway
routes by Host header. `localhost:8080` works for direct API calls but
NOT for `getPageDefinition` in the browser — use the proper hostname.
