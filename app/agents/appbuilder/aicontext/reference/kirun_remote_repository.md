# Cross-runtime Kirun execution: how server-side functions resolve & run in the browser

A page event function (browser-side Kirun) can call ANOTHER function that lives
on the server (Java Kirun). The browser doesn't ship the server function's
bytecode/logic; it asks the gateway to run it remotely and returns the result.
This file traces the exact resolve+execute path so an agent authoring functions
that cross the UI/server boundary understands what actually happens at runtime.

## The four players

| Role | File / class | What it does |
|---|---|---|
| Resolver | [nocode-ui RemoteRepository.tsx](../../nocode-ui/ui-app/client/src/Engine/RemoteRepository.tsx) | Implements `Repository<Function>` from `@fincity/kirun-js`. Maps `find(namespace, name)` → REST GET; caches results for 30 s. |
| Remote stub | [RemoteFunction (same file)](../../nocode-ui/ui-app/client/src/Engine/RemoteRepository.tsx) | Extends kirun-js `AbstractFunction`. `internalExecute()` makes one HTTP POST to the server-side execute endpoint with the argument map. |
| Aggregator | [HybridRepository (kirun-js)](https://www.npmjs.com/package/@fincity/kirun-js) | Tries N child repositories in order until one returns a function. Order picked by [runEvent.ts:182](../../nocode-ui/ui-app/client/src/components/util/runEvent.ts#L182). |
| Browser runtime | [`KIRuntime`](https://www.npmjs.com/package/@fincity/kirun-js) constructed at [runEvent.ts:215](../../nocode-ui/ui-app/client/src/components/util/runEvent.ts#L215) | Walks the step graph; for each step's `namespace.name`, calls `functionRepository.find(...)` → either runs locally (if returned function is a `KIRuntime`) or remotely (if it's a `RemoteFunction`). |

## Lookup order (browser-side)

When a UI Kirun step references some `namespace.name`, `HybridRepository.find()`
walks these in order, first hit wins:

1. **UI_FUN_REPO** — kirun-js built-in primitives (`System.*`, `UIEngine.*`, `Math.*`, …) bundled in the browser.
2. **PageDefinitionFunctionsRepository(pageDefinition)** — the current page's inline `eventFunctions` map (other event functions on this same page).
3. **RemoteRepository(CORE)** — server-side functions resolved via `/api/core/functions/repositoryFind?namespace=…&name=…`. The repo's `jsonConversion` returns a `RemoteFunction` (NOT a `KIRuntime`) for any CORE hit — the function definition is never executed in-browser.
4. **RemoteRepository(UI)** — UI functions from `/api/ui/functions/repositoryFind`. The `jsonConversion` returns a fresh `KIRuntime(fd)` — the function definition IS executed in-browser, the same way the page's own inline event functions are.

Both remote repos are cached per `(appCode, clientCode, includeKIRunRepos, repoServer)` tuple in module-level Maps so repeat lookups are free.

The URL prefix `/{appCode}/{clientCode}/page/repos/api/{core|ui}/functions/…`
encodes the requesting page's context — server uses this to pick the right
override-chain when resolving the function.

## Execute path for a CORE (server-side) function

```
browser KIRuntime
  └─ Steps.callServerFn (namespace=monkbars, name=sendEmail)
      └─ functionRepository.find('monkbars', 'sendEmail')
          ├─ UI_FUN_REPO: miss
          ├─ PageDefinitionFunctionsRepository: miss
          └─ RemoteRepository(CORE):
              GET /{app}/{client}/page/repos/api/core/functions/repositoryFind
                  ?appCode=monkbars&clientCode=SYSTEM&namespace=monkbars&name=sendEmail
              ← FunctionDefinition JSON (with steps)
              return new RemoteFunction(appCode, clientCode, fd, CORE)

  RemoteFunction.internalExecute(context):
      POST /api/core/function/execute/monkbars/sendEmail
        Headers: appCode, clientCode, Authorization, x-debug
        Body:    { "to": "user@x", "subject": "...", ... }
      ← [{name: "output", result: {…}}, …]
      → FunctionOutput
```

The server-side endpoint is [FunctionExecutionController.java:43-52](../../nocode-saas/core/src/main/java/com/fincity/saas/core/controller/FunctionExecutionController.java#L43-L52)
(`POST api/core/function/execute/{namespace}/{name}`). It:

1. Pulls `appCode` / `clientCode` from headers.
2. Pulls `X-Forwarded-Host` / `X-Forwarded-Port` (set by gateway/nginx) to reconstruct the originating origin — used by server-side internal calls back into UI services.
3. Hands the JSON-arg map to `CoreFunctionService.execute()` which constructs a server-side `KIRuntime` with a full server-side `HybridRepository` (built-ins + DB-backed function repo + REST_API connections + storage primitives).
4. Drains the FunctionOutput stream up to the first `OUTPUT` event and returns it as JSON.

## Execute path for a remote UI function (different app)

If `RemoteRepository(UI)` resolves the function, the browser gets the
`FunctionDefinition` back and constructs a fresh `new KIRuntime(fd, …)`.
That runtime runs the step graph entirely in the browser — but its internal
calls to OTHER functions go through the same `HybridRepository`, so a UI
function can still reach back to CORE functions via the chain above.

## Why this matters for tool authoring

- **Decide ahead of time whether logic lives UI or CORE.** Moving it later is breaking — UI step graphs can use `Page.*` / `Theme.*` / browser-only primitives that don't exist on the server, and CORE functions can use storage/REST connection primitives that don't exist in the browser. The function's `definition.steps[*].namespace` values lock you in.
- **`includeKIRunRepos=false` is the prod default** — the kirun-js built-ins are NOT exposed via remote lookup; only app-defined functions are. So `System.Math.Add` resolves via UI_FUN_REPO, never via remote repos.
- **HTTP boundary cost.** Every CORE function call inside a UI step graph is a round-trip. If a UI function calls `Steps.fetchA`, `Steps.fetchB`, `Steps.fetchC` sequentially against three different CORE functions, that's three HTTP requests. Batch when possible (e.g. one CORE function that does all three fetches).
- **Authority** is checked on the server execute endpoint via `executeAuth` on the function entity. Server side enforces; browser doesn't gate the call. So a UI function calling a server function with `executeAuth='Authorities.ADMIN'` will fail at the HTTP call for non-admin users.

## CFA implications

- `_conventions.UIENGINE_PRIMITIVES` (in [`app/agents/appbuilder/tools/modlix/_conventions.py`](../../tools/modlix/_conventions.py)) lists browser-only namespaces (`UIEngine`, `Page`, `Theme`, `LocalStore`, …) — these MUST not appear in CORE function step graphs.
- `add_step` / `update_step` / `set_dependencies` / `remove_step` accept `is_server: bool` — set to `True` for steps in CORE functions. Server side doesn't expose the surgical `PATCH /{id}/steps` endpoint, so the tool falls back to a full-document PUT.
- `decompile_function(name, is_server=True)` is how to round-trip a CORE function's step graph through DSL text.
- Cross-runtime calls (UI step → CORE function or vice-versa) are valid; the agent doesn't need to mark them specially — just author the called function on the right side via `create_function` (UI) vs `create_server_function` (CORE).

## File pointers

- Browser repo wiring: [nocode-ui RemoteRepository.tsx](../../nocode-ui/ui-app/client/src/Engine/RemoteRepository.tsx)
- Browser execution dispatcher: [nocode-ui runEvent.ts](../../nocode-ui/ui-app/client/src/components/util/runEvent.ts) (search `HybridRepository`)
- Server execute endpoint: [nocode-saas FunctionExecutionController.java](../../nocode-saas/core/src/main/java/com/fincity/saas/core/controller/FunctionExecutionController.java)
- Server-side function repo (used inside the server KIRuntime): `commons-core/.../CoreFunctionService.java`
