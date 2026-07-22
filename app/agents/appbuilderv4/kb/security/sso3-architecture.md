---
name: SSO3 architecture
description: Multi-app SSO via authzump.ai beacon, gated by app-dependency table, redirect-chain in hidden iframes
type: project
originSessionId: 42ed6ffa-3d6e-49fd-8510-bb02074f8b90
---
Cross-app SSO across the Modlix platform's hundreds of customer apps on independent eTLDs. Lives on the `feature/sso3` branch in both `nocode-saas` and `nocode-ui`.

**Why:** customer apps (sitezump.ai, adzump.ai, leadzump.ai, etc.) are on separate eTLDs, so cookies/localStorage can't span them. Need a single sign-on across the platform.

**Architecture:**
- **authzump.ai** is the central beacon (`local.authzump.ai`, `dev.authzump.ai`, `stage.authzump.ai`, `authzump.ai` per env, derived from `security.appCodeSuffix`). It's a Modlix app with no UI; holds cookie + localStorage on `.authzump.ai`. Google/Meta social-login OAuth integrations are already registered against the authzump.ai hostnames.
- **Two endpoints in `UniversalController.java`** (UI service):
  - `/sso/{token}` (already existed) — exchanges a one-time token for an auth session on whichever origin it's hit. Writes localStorage + optional cookie. Now also accepts an optional `designMode` query param that overrides the legacy `window.self !== window.top` self-detection (necessary because hidden-iframe callers always have self !== top).
  - `/hassso` (new) — runs in a hidden iframe on authzump's origin. Reads authzump's own localStorage (design-mode-aware key) for an `AuthToken`. If absent or expired, postMessages `{type:'sso:none'}` to parent. Otherwise calls `makeOneTimeToken` with the token as Bearer + `targetAppCode`/`targetClientCode` from query params, then postMessages `{type:'sso:token', token}` on success or `sso:none` on failure.
- **Parent flow is postMessage-based** (NOT redirect chain). The iframe stays on authzump throughout — never loads the parent's index, so no spurious bootstrap inside the iframe. On `sso:token`, parent does a top-level `window.location.replace('/sso/{token}?cookie=true&redirectUrl=currentURL&designMode=...')` to materialise the session locally; on `sso:none` it falls through to local login.
- **`OneTimeToken` extended** with `authMode` (COOKIE/BEARER), `originAppCode`, `targetAppCode`. `makeOneTimeToken` validates `security_app_dependency` (either direction) when `targetAppCode` is set, **except when source or target is `"authzump"`** — authzump is the platform broker and is universally trusted. Real customer-app-to-customer-app SSO still requires an explicit dependency row.
- **`IndexHTMLService.java` / `htmlRenderer.ts`** inject `window.__SSO_BEACON_HOST__` only when the rendered app has `properties.sso3 === true`. `ssoModule.ts` keys off this global as the "is SSO configured" signal.

**How to apply:**
- New SSO endpoints belong in `UniversalController.java` (UI service), not in security's `AuthenticationController.java`. Existing `/sso/{token}` is the canonical token exchange.
- Apps opt in by setting `application.properties.sso3: true` and adding rows to `security_app_dependency` between cooperating apps.
- `Login.ts` (KIRun step) reads `Store.application` and passes it explicitly to `isSsoEnabled(application)` — see `feedback_store_pre_mount.md` for why ssoModule must not reach for the store itself.
- The Flyway migration `V76__Add Auth Mode and App Codes to One Time Token.sql` was applied to local dev DB during build verification on 2026-05-05; production rollout still pending.
