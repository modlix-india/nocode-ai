# App transport: moving an app between environments

Export and import of a whole Modlix app is a "transport".

## Export per service, not combined

`POST /api/{core,ui}/transports/makeTransport` with body
`{"appCode": X, "clientCode": Y}` returns that service's transport zip directly:
`.cmodl` for core (Storage, Function, Schema JSON), `.umodl` for ui (Page, Style,
Theme JSON). Bearer auth, headers `appCode: appbuilder` and `clientCode: SYSTEM`.

**Do NOT use the combined `GET /api/multi/application/transport`.** It wraps
security.json + core.cmodl + ui.umodl through feign hops and **504s at the
gateway** on large apps. Per-service export is lighter and does not time out.

## Import

`POST /api/{core,ui}/transports/createAndApply?isForBaseApp=true&applicationCode=X`
with multipart `file=<the .cmodl/.umodl zip>`. The base path is `transports`,
plural. A non-`.json` filename is treated as a zip.

Transport is an **upsert** (`readToTransport`, create if absent else update), so
re-running overwrites target definitions and is idempotent.

## The traps

**Security transport is a no-op.** `security/TransportService.makeTransport` and
`createAndApply` are stubbed to return empty. Roles and permissions do NOT move,
and `security.json` is `{}`. The app row itself is created via
`securityService.createApp`, which needs `Authorities.Application_CREATE`.
`multi.createApplication` is NOT idempotent for an existing appCode, so check
first with `GET /api/security/applications/appCode/{appCode}`.

**Apply 504s on a whole grown-up app.** Measured 2026-08-26 promoting leadzump
dev to stage: 123 objects, 4.7MB zip, 41MB raw. It is the environment's own
nginx `proxy_read_timeout` at roughly 60s, not Cloudflare. On stage, 30 pages
took 56s and returned 200, 60 pages took 64s and returned 200 right on the edge,
100 pages returned 504. Per-chunk time is wildly variable (15-page chunks ranged
12s to 52s) and does not track payload size, so size is not a usable predictor.
**Chunk at about 15 objects.**

A 504 does NOT mean "slow but applied". WebFlux cancels the chain on client
disconnect, so a timed-out apply lands partially or not at all. Always re-verify.

Each chunk MUST get its own `uniqueTransportCode`.
`AbstractTransportService.create()` returns the *existing* transport when the
code matches, so reusing one code makes chunks 2..N silently re-apply chunk 1
and still answer 200.

The first apply into an environment is slow regardless of size (2 tiny pages
cold took 53s, 30 pages warm took 10s, all 122 objects warm took 14s), so spend
the cold request on a single object.

**Scoping ONE object by name exports several. Still open.** `readForTransport`
pushes names through `paramToConditionLRO`, which makes a **single** name a
`STRING_LOOSE_EQUAL` (an unanchored regex) while **two or more** become an exact
`IN`. Measured on leadzump: `--objects Page=deals` exports `deals`, `dealsBp`
and `dealsOptimized`, so promoting one page silently overwrites two.
`Page=deal` exports 8 pages. `Page=deal,xyzNoSuchPage` exports 0. All return
HTTP 200. A name with regex metacharacters 500s, so any URIPath route ending
`/**` cannot be scoped singly. **Until it is fixed, scope with 2+ names or not
at all.** See `platform_doc_read('filter_conditions')` for the underlying
operator rule.

**`encodedModl` in `addDefinition` is broken for zips** (double base64). Do not
use it.

## Verifying a promotion

Export both environments and compare each object with `.id`, `.createdAt`,
`.updatedAt`, `.createdBy`, `.updatedBy`, `.version`, `.message`,
`.componentVersions` and `.eventFunctionVersions` deleted. `componentVersions`
is per-component version bookkeeping and is legitimately environment-local, so
raw byte sizes always differ by a few hundred bytes to about 13KB per page even
when the content is identical.

URIPath ids differ per environment: apply matches on `name`, then creates a new
doc with a new id. Diffing by `.name` works regardless.

## Tool

`nocode-saas/app-transport.sh <from> <to> <appCode> [ui|core|both] [clientCode]`.
Parts default to both, clientCode to SYSTEM. It chunks the apply (`--chunk N`,
default 30, plus `--keep-going` and `--verify`) by splitting the exported zip
locally rather than re-exporting per chunk, which dodges the name-scoping bug
entirely. It prompts before writing to prod.
