---
name: StoreContext is unsafe before React mounts
description: Reading from the path-reactive store via getDataFromPath / setData in modules that load before React mounts corrupts the store and triggers spurious re-fetches
type: feedback
originSessionId: 42ed6ffa-3d6e-49fd-8510-bb02074f8b90
---
The path-reactive store at `nocode-ui/ui-app/client/src/context/StoreContext.ts` is a side-effecting API. Calling `getDataFromPath` for a path that hasn't been initialised yet creates empty entries / registers listeners as a side effect.

When a module that's part of the bootstrap chain (i.e. loaded before React mounts) calls `getDataFromPath`, those empty entries become "the value" of `Store.application` etc. Then `App.tsx`'s `addListenerAndCallImmediately` fires the callback with that empty value, treats it as `undefined`, and calls `getAppDefinition()` a second time — duplicate `/api/ui/application` + `/api/ui/theme` requests, and `RenderEngineContainer` races to load `page/undefined` because `Store.application.properties.defaultPage` isn't populated yet.

**Why:** discovered while implementing SSO3. `ssoModule.ts` originally imported `getDataFromPath` to check `Store.application.properties.sso3` and `Store.auth.isAuthenticated`. Even though those checks were gated, the import path of ssoModule from `appDefinition.ts` ran the reads at bootstrap time, which broke every cold load with duplicate fetches and a `page/undefined` 404.

**How to apply:**
- Modules in the bootstrap chain (`appDefinition.ts`, anything imported by `index.tsx` before React mounts, `ssoModule.ts`, etc.) MUST NOT call `getDataFromPath` / `setData` at module load or in functions that run before React mounts.
- Use runtime globals (e.g. `globalThis.__SSO_BEACON_HOST__`, `globalThis.domainAppCode`, `globalThis.__APP_BOOTSTRAP__`) injected by `IndexHTMLService.java` / `htmlRenderer.ts`, plus direct `localStorage` access.
- KIRun step functions (`Login.ts`, `Logout.ts`, etc.) run after React is mounted and CAN safely use `getDataFromPath`. Pass the values down to lower-level helpers as explicit parameters rather than having helpers reach for the store themselves.
