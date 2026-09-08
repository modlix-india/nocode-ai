# The clientCode in a URL is the HOST, not the tenant

The `<clientCode>` segment in a Modlix URL
(`/<appCode>/<clientCode>/page/<pageName>`) names the client **hosting** the
application. It is not the client of the user making the request, and it is not
a place to put a tenant you want the server to act on.

The server already derives both the consuming client and the hosting client from
the security context. Putting a tenant in the URL produces a path that only
resolves for a tenant that happens to host its own copy, and it silently
multiplies one resource into one-per-tenant.

**How to apply:** call `api/core/connections` style paths with no client code
and let the server resolve it. Add a client code only to say *who hosts this*,
which is a different question.

## Corollary: provider callback URLs are per ENVIRONMENT, never per tenant

When an external provider needs a callback URL, build it from the environment:

```
https://<env>.modlix.com/api/message/webhooks/<provider>
```

derived from `security.appCodeSuffix` (`""` / `".dev"` / `".stage"`), the same
value `IndexHTMLService.deriveBeaconHost` uses.

Resolve the tenant from the payload instead. On WhatsApp,
`metadata.phone_number_id` has a unique key on that column alone
(`UK2_WHATSAPP_PHONE_NUMBER_PHONE_NUMBER_ID`, not scoped by app or client), so
the row it matches carries both `APP_CODE` and `CLIENT_CODE`.

Per-tenant callback URLs had a real cost: Meta stores one
`override_callback_uri` per business account, so two tenants sharing an account
overwrote each other's, last write winning, invisibly from both sides.

**Do not over-correct into removing the override.** That was tried and was
wrong. A Meta app holds one app-level callback URL, so the per-account override
is what lets dev, stage and prod share one app while owning different accounts.
Per-tenant is bad, per-environment is necessary. Two environments on the *same*
provider account can never both receive; give each its own.

Resolving the tenant from an unverified payload before the signature check is
fine and is the established pattern: it selects which key to verify against, it
grants nothing.

## The general shape

**If a fact is derivable from the data, do not encode it in the URL.** Ask what
the payload already identifies before adding a parameter.
