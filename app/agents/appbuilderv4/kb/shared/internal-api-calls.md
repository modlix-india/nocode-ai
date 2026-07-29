---
name: internal-api-calls
description: How Modlix functions call platform REST endpoints — UIEngine.SendData / FetchData / DeleteData.
metadata:
  type: reference
---

# Internal REST calls from inside a function

When a Kirun function (page event function or server function) needs to call
an internal platform endpoint (e.g. `POST /api/security/users/invite`,
`GET /api/ui/pages`, `DELETE /api/core/storages/{id}`), it uses three
Kirun primitives:

- **`UIEngine.SendData`** — for create/update (POST / PUT / PATCH)
- **`UIEngine.FetchData`** — for reads (GET)
- **`UIEngine.DeleteData`** — for deletes (DELETE)

All three wrap the gateway and inject the caller's auth context, so
functions don't have to construct a Connection of type REST_API just to hit
internal services. The auth header (`Authorization: Bearer ...`) comes from
the caller's session — don't set it manually in the step's parameterMap.

**Honest note:** these are documented as `UIEngine.*` primitives, which by
naming convention are JS-runtime (browser-side). For **server-side functions
running in Java Kirun**, the equivalent primitive likely has a different
namespace. Verify by sampling a real `core.function` doc whose
`definition.steps[*].name` is `SendData` / `FetchData` / `DeleteData` and
checking what namespace appears. The corpus task
`server-call-external-rest` should land examples; if it returns zero, the
server-side primitive name is something else and we need to look at
kirun-java source.

# Parameter shape

Typical input to these step primitives:

```
url:         "/api/security/users/invite"   (relative; gateway prepends host)
payload:     <request body>                  (SendData; not for FetchData/DeleteData)
headers:     optional extra headers
queryParams: optional URL params
```

# When to use a Connection instead

Connections (REST_API_BASIC / REST_API_OAUTH2 / EXOTEL / SMTP / WHATSAPP)
are for **external** APIs that need bespoke auth — Stripe, Twilio, Exotel,
SMTP servers, social login providers. For internal modlix endpoints, just
use SendData / FetchData / DeleteData with the relative path.

# Discovering endpoint paths

Read the source. The agent should consult [platform_services.md](platform_services.md)
to map a need ("create a user", "list workflows") to the right service
class, then use the `code_workspace` tools (`code_read`, `code_grep`) to
read the matching controller's `@RequestMapping` annotations and find
the exact path. The CFA does not ship a pre-built API catalog — the
source IS the catalog. See [branch_awareness.md](branch_awareness.md)
for confirming the workspace's checkout matches the target env.
