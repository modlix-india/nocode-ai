---
name: Page Preview URL Pattern
description: How to preview generated pages - URL pattern with appCode, clientCode, pageName
type: reference
originSessionId: a68f67da-54bd-43b6-a29c-00a7710d2ce0
---
Preview URL pattern: `https://<host>/<appCode>/<clientCode>/page/<pageName>`

Example from the appbuilder UI referrer: `https://apps.local.modlix.com/appbuilder/SYSTEM/page/agent`

So for an app `bobabangalore` with page `Home`: `https://apps.local.modlix.com/bobabangalore/SYSTEM/page/Home`

The host comes from the `X-Forwarded-Host` header in the auth context.
