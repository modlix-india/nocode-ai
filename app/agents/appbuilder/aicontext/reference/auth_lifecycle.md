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
- **~1 year** (`iat → iat + 31_557_600`): a longer-TTL token Kiran's local
  stack issues for tooling/automation (via `rememberMe: true` on the login).
  Practically equivalent in privileges but doesn't auto-refresh — and is
  still subject to the server-side invalidation above, so it's not a
  paste-once-forget solution.

For modlix-mcp automation, the ~1-year token is more practical, but you
still need to handle 401-mid-session. Currently `auth.py` doesn't auto-
recover from a mid-session 401 — it just throws. ROADMAP item.

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
case-insensitive** (confirmed by Kiran 2026-05-18): `Kiran@modlix.com` and
`kiran@modlix.com` resolve to the same registration.

If the array is empty → user has no account anywhere with that password.
If length 1 → use directly. If > 1 → present a picker.

### Step 2 — POST `/api/security/authenticate`

URL: `https://<gateway>/<appCode>/<clientCode>/page/api/security/authenticate`
Body:
```json
{
  "userName": "Kiran@modlix.com",
  "userId": 142,                    ← from Step 1
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

## auth.py status (TODO)

`modlix_mcp.auth.py._login` currently:
- Sends `appCode` + `clientCode` headers from settings (added 2026-05-18)
- Does NOT pre-discover the user's clients

For ambiguous accounts (same email across clients), the login will fail
with 403. Workaround: use `MODLIX_TOKEN` (a pre-acquired JWT) instead of
USERNAME/PASSWORD until auth.py learns the multi-client flow.

ROADMAP item: extend `auth.py._login` to:
1. First call a discovery endpoint (TBD which exact one — needs probing).
2. If multiple registrations found, require a new setting
   `MODLIX_LOGIN_CLIENT_CODE` (or fall back to `DEFAULT_CLIENT_CODE`) to
   disambiguate.
3. On 401 during normal calls, attempt one silent re-login before
   surfacing the error to the caller.

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
