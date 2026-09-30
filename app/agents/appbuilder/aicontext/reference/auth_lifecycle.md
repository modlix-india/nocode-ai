# Modlix auth lifecycle — JWTs, hostnames, multi-client users

Captured during the appbuilder rebuild session 2026-05-18, where every
shortcut around auth bit me. This file is the surface I wish I'd read before
trying to script logins.

## Tokens are bound to a hostname

The JWT's `hostName` claim is set at issue time to the gateway host that
authenticated the user. A token issued by `appbuilder.local.modlix.com`
returns **401** when sent to `apps.local.modlix.com` even though the
signature is valid, the exp is in the future, and the user is real.

Practical impact: if a stack runs MULTIPLE local hostnames (e.g. one nginx
serving `apps.local.modlix.com` and one serving `appbuilder.local.modlix.com`),
make sure `MODLIX_GATEWAY_URL` matches the host that issued the JWT.
Decode the `hostName` claim from the JWT payload to confirm before guessing.

## Tokens have server-side state, not just claims

Even within their `exp` window, JWTs can be invalidated by:

- The user logging out (revokes the token at the gateway)
- A second login from the same user (some servers behave as single-session)
- The security service restarting and losing in-memory session state
- The gateway's session cache evicting entries under memory pressure

When this happens you see **401 Unauthorized** with a fresh `exceptionId` —
NOT a JWT signature error. Treat 401 on a token you JUST issued as a
session-side rejection, not a token-format issue.

## JWT lifetimes seen in this stack

Two distinct lifetimes observed locally:
- **~1 hour** (`iat → iat + 3600`): the normal browser-session token
  produced by logging into the app UI. These get refreshed by the SPA's
  background expiry watcher (`nocode-ui/.../index.tsx:95-114`).
- **~1 year** (`iat → iat + 31_557_600`): a longer-TTL token issued for
  tooling/automation (via `rememberMe: true` on the login).
  Practically equivalent in privileges but doesn't auto-refresh — and is
  still subject to the server-side invalidation above, so it's not a
  paste-once-forget solution.

The CFA doesn't refresh tokens on the caller's behalf — the developer JWT
arrives via the chat request's Authorization header (extracted by
`require_auth_context`) and is used as-is for every authoring tool. A 401
from any platform endpoint is surfaced through the tool result and the
caller is expected to re-authenticate. We deliberately do NOT try to
silently re-login: the CFA never has the caller's password, and pretending
to recover would hide an auth-context regression from the user.

App-user tokens (used only by `screenshot_page` / `drive_page` /
`call_as_app_user`) are different — see "App-user resolution" below.

## Multi-client user model — the login pre-step

A single email/username can be registered under multiple clients in a
client HIERARCHY. The same person might have:

- A SYSTEM-client account (platform admin)
- A tenant-specific account under client X for app A
- Another tenant-specific account under client Y for app B

`POST /api/security/authenticate` with just `{userName, password}` returns
**403 "No registration available for the selected client on this application"**
because the gateway can't disambiguate which (client, app) combination the
user is trying to log into. The error reads like "user not found" but it's
actually "user not unambiguously findable in the given app context."

The flow the SPA uses (confirmed by browser-curl 2026-05-18):

### Step 1 — POST `/api/security/users/findUserClients`

URL: `https://<gateway>/<appCode>/<clientCode>/page/api/security/users/findUserClients`
Body: `{"userName": "...", "password": "...", "rememberMe": true}`

Returns an array of `{userId, client: {id, code, name, ...}}` entries — one
per (client) where this user is registered. **Username matching is
case-insensitive** (confirmed 2026-05-18): `User@example.com` and
`user@example.com` resolve to the same registration.

If the array is empty → user has no account anywhere with that password.
If length 1 → use directly. If > 1 → present a picker.

### Step 2 — POST `/api/security/authenticate`

URL: `https://<gateway>/<appCode>/<clientCode>/page/api/security/authenticate`
Body:
```json
{
  "userName": "user@example.com",
  "userId": 1,                      ← from Step 1
  "identifierType": "EMAIL_ID",     ← REQUIRED, not optional
  "password": "Pass@1234",
  "rememberMe": true,
  "cookie": true
}
```

Returns:
```json
{
  "accessToken": "eyJhbGciOiJ...",
  "accessTokenExpiryAt": 1810620407,   ← Unix seconds
  "user": { "id": 142, ... },
  ...
}
```

**Critical**: the bare-payload version (`{userName, password, rememberMe}`)
ALSO returns 403 "No registration available" — `userId` and
`identifierType` are functionally required even when the username is
unambiguous. The SPA always sends them; tooling should too.

`identifierType` enum (per AuthenticationRequest):
- `EMAIL_ID` — when userName is an email
- `PHONE_NUMBER` — when userName is a phone
- `USER_NAME` — when userName is a username string
- See `nocode-saas/.../AuthenticationIdentifierType.java` for the full set.

## CFA auth model — what the agent should know

The CFA has TWO separate identity slots per session. They flow through
different code paths and fail differently.

### Slot 1 — Developer (the caller's JWT)

- **Source**: the `Authorization: Bearer …` header on the chat request,
  validated by `app.core.base_auth.require_auth_context`.
- **Used by**: every authoring tool (pages, components, kirun, schemas,
  themes, security CRUD, …).
- **Lifetime**: whatever the platform issued — we don't refresh.
- **On 401**: surface the tool error verbatim. Don't attempt re-login —
  we don't have the caller's password. Tell the user the auth context
  expired and they need to re-authenticate.

### Slot 2 — App-user (token OR username + password)

Lives only in the chat request body's `app_user` field. Used exclusively by
tools that need to act AS an end-user inside the customer's app:
`screenshot_page`, `drive_page`, `call_as_app_user`.

Resolution lives in `app.core.session.BaseSession.get_app_user_token()`:

1. If `app_user.token` was provided, cache and return it.
2. Else, if `app_user.{username, password}` were provided, run the
   multi-client login flow:
   1. `POST /api/security/users/findUserClients` with `{userName, password}`
      and the session's `appCode` + `clientCode` headers — returns the
      `userId` for the unambiguous (or first) registration.
   2. `POST /api/security/authenticate` with `{userName, userId, password,
      rememberMe: false}` and the same headers — returns the `accessToken`.
3. Cache the resulting token on the session; future tool calls reuse it.
4. Raise a clear `RuntimeError` if neither slot is populated, with a
   remediation hint pointing back to the chat-request shape.

This is the multi-client flow the old modlix-mcp `auth.py` did NOT
implement — the CFA learned it from `findUserClients` + `authenticate`
straight away. Implementation lives in
[`app/core/session.py:158-235`](../../core/session.py#L158).

### What the agent should communicate to the user

- 401 on an authoring tool → "your auth context expired; please re-open the
  chat after refreshing the page."
- "app-user credentials required for this tool" → "this tool needs to
  render the app as an end user; pass `app_user.{username, password}` (or
  an already-obtained `token`) in the next chat request."
- 403 "No registration available for the selected client on this
  application" during app-user login → the username doesn't exist under
  the target app/client combination. Confirm the app_code on the session.

### Known limits

- **No mid-session re-auth.** If the developer JWT expires mid-conversation,
  every subsequent tool call errors out. The session has no path to refresh.
  The user must re-authenticate at the chat surface.
- **App-user token TTL is whatever the platform issues** (often short).
  Long-running conversations that drive the browser repeatedly may hit a
  401 after enough idle time. The session caches the token forever once
  resolved — there is NO auto-invalidation on 401 today
  ([`session.py:158-176`](../../core/session.py#L158) sets
  `_app_user_token` only on init and after a successful login). A 401
  from the browser-drive tools therefore needs a fresh chat request — or
  a future improvement that clears the cached token on 401 and retries
  the two-step login once. Same shape as the developer-JWT limit above.

## Quick decode helper

When you receive a JWT, decode the payload before trusting it:

```python
import base64, json
payload = jwt_str.split('.')[1]; payload += '=' * (-len(payload) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload))
# Key fields: hostName, loggedInClientCode, appCode, userId, iat, exp
```

The `hostName` tells you which gateway URL to use. `exp` tells you the
nominal validity window (which the server may still reject). `appCode` +
`loggedInClientCode` tell you the auth context.

## Summary checklist before using a JWT

- [ ] Decode and inspect `hostName` — does `MODLIX_GATEWAY_URL` match?
- [ ] Is `exp` in the future?
- [ ] Do a sanity `GET /api/security/verifyToken` round-trip BEFORE using
  it for real work — a 401 here means re-login, no point retrying anything else.
- [ ] If you're operating across multiple hostnames in the same stack, keep
  separate JWTs per host; don't expect one to work across hostnames.
