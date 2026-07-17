# AppBuilder v4 — Build Notes

Code-first authoring agent. The LLM writes Python that imports the
`modlix` SDK and posts state to the platform. We add tools one at a time;
each tool addition is logged here with the bench scenario that justified
it.

## Why v4 exists

v3 had 221 tools, ~50KB of persona, and the LLM kept skipping the most
important ones (`compare_to_source` was called 0 times in the first
clonelinear build) because the surface was too large to navigate. v4 is
deliberately minimal so the LLM keeps everything in its head, and so we
can see which behaviours fail purely from agent skill — not from
forgetting where a tool lives.

## Architectural rules

1. **One write primitive.** `code_run` runs a Python script in a subprocess
   sandbox. The script can `import modlix` to reach auth-bound HTTP,
   the component catalog, and page/app CRUD. Mutating the platform
   ALWAYS goes through `code_run`. We never add a tool that wraps
   `modlix.pages.replace(...)` from the outside; that's just `code_run`
   calling the SDK.

2. **Read tools are okay when the LLM genuinely can't do it in Python.**
   Vision is the canonical case: the agent can't take a screenshot of an
   external URL from inside the sandbox (no Playwright). That's a
   dedicated tool. Same for things that need session-wide state the
   sandbox can't see (e.g. a cached source-screenshot handle).

3. **The persona stays under 3KB.** Discovery is the SDK's job, not the
   persona's. If the agent doesn't know what to call, give it a one-line
   "ask `modlix.catalog.get_schema('X')`" answer in the persona, not a
   thick spec.

4. **No mutating endpoints outside `code_run`.** When in doubt, the
   answer is "compose it in Python and POST it through `modlix.post(...)`".

## Current tool surface

| # | Tool | Added | Justification |
|---|---|---|---|
| 1 | `code_run` | v0 | The single write primitive — every mutation goes through here. |

## Pending tool additions (in order)

These will be added one at a time, AFTER a smoke / bench scenario fails
for lack of them. Each addition needs a recorded failure scenario.

| Tool | What it does | Trigger to add it |
|---|---|---|
| `screenshot_external_url` | Playwright captures of arbitrary URLs, attached as image content blocks. Caches each shot under a stable `source_handle` for `compare_to_source`. | First clone scenario where the agent needs to author from an external site. |
| `extract_site_assets` | Harvests `<img>` / `<svg>` / bg-image from a source URL; uploads to Modlix files; returns a manifest. | First clone scenario that needs real product imagery. |
| `compare_to_source` | Diff a rendered Modlix page against a cached source screenshot via Claude vision; returns structured JSON. | First clone scenario where the agent needs an iterative correction loop. |
| `screenshot_page` | Render a Modlix page and attach the PNG. Identity via session app-user. | First scenario where the agent needs to verify its own build before declaring done. |
| `drive_page` | Persistent Playwright sessions for interactive flows. | First scenario that needs hover/scroll/click interactions to verify (e.g. POS flow). |

We do NOT pre-add tools. If a scenario succeeds without one, it stays
unadded.

## The modlix SDK surface

(See `sdk/_core.py` for the canonical reference; this is a summary.)

- `modlix.config` — auto-populated from env vars: `gateway_url`,
  `auth_token`, `app_code`, `client_code`, `catalog_url`.
- `modlix.get / post / put / delete` — auth-bound HTTP helpers. Headers
  (`Authorization`, `clientCode`, `appCode`, `X-Forwarded-*`) are
  injected automatically. Returns the parsed JSON; non-JSON responses
  become `{_status, _text}`.
- `modlix.catalog.list_types()` — every component-type name.
- `modlix.catalog.get_schema(name)` — full schema for one type.
- `modlix.catalog.search(keyword)` — fuzzy name/summary search.
- `modlix.pages.list/get/create/replace/delete/validate` — page CRUD.
  `list` returns a plain list (Spring `content` array unwrapped).
- `modlix.apps.list/get/create/update/delete` — app CRUD. App-level
  properties are RAW values (not wrapped `{value:...}`).
- `modlix.uuid()` — fresh uuid4 string. Use as the styleProperties rule
  key.

## Sandbox semantics

- Process-level isolation (subprocess). The script cannot reach the
  agent's memory.
- Default timeout 60s, hard cap 180s. Killed cleanly on overrun;
  partial stdout/stderr returned to the agent.
- Stdout cap 6KB, stderr cap 4KB in the tool result. Don't blow them on
  one call.
- Auth is per-call. The agent's developer JWT goes into env vars right
  before spawn and is gone after the subprocess exits.
- Network is unrestricted (the subprocess can `requests.get(...)` any
  URL). The gateway's own auth still applies for platform writes.

## Open issues

1. **Component catalog URL is wrong in the config server.** Both v3 and
   v4 hit `https://apps.local.modlix.com/component-catalog.json` (404).
   The correct URL is `https://cdn-local.modlix.com/js/dist/component-catalog.json`.
   Fix in `nocode-saas/configfiles/application-default.yml`:
   `ai.componentCatalogUrl` or equivalent.

2. **`apps.list()` shape was originally Spring-paginated; unwrapped to a
   list in v0.1.** If we add per-page filters/cursors later, surface them
   via explicit kwargs.

3. **Per-app KB writes** — v3 has propose-then-commit through `kb_app_*`
   tools. v4 does not yet. When we add it, it goes through `code_run`
   calling `modlix.post('/api/ai/learning/kb/...', ...)`, not as a
   dedicated tool.
