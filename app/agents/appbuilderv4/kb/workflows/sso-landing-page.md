# sso-landing-page

Single-sign-on landing — handles inbound /sso/{token} redirects.

**Notes:**

⚠️ The current sample (`cxapp.newappprocessone`) is an onboarding-flow page, NOT a true SSO landing. The pattern's intent is to receive an inbound `/sso/{token}` redirect, decode the token, write the resolved auth into Store, and forward to the user's landing page. Look at: the page's `onLoadEvent` for token decode + `Page.params.token` access; a `System.Context` step setting `Store.AuthToken` before navigation; the SubPage/redirect strategy used to forward the user. For a real example, look at the SSO3 architecture notes in the reference docs.

**Entity type:** `page`

## Samples

- **cxapp** / `newappprocessone` (v21, clientCode=SYSTEM)
  - [cxapp.newappprocessone.json](cxapp.newappprocessone.json)
  - [cxapp.newappprocessone.tree.txt](cxapp.newappprocessone.tree.txt)
