---
name: project-personalization
description: "Personalization entity = runtime per-user UI preferences (column visibility, layout choices), written by the runtime, not by app builders."
metadata: 
  node_type: memory
  type: project
  originSessionId: 0a3b792f-b0ea-4757-9c52-ac7f531b7154
---

**Fact:** `ui.personalization` documents store per-USER runtime UI preferences — column visibility on tables, sort order, filter selections, layout choices that should persist across the user's sessions.

**Real example (from cxapp, doc `viewCustomerTable248`):**
```json
{
  "name": "viewCustomerTable248",
  "appCode": "cxapp",
  "clientCode": "BUILD",
  "createdBy": "248",
  "personalization": {
    "amountRecieved": true, "areaBooked": true, "customer": true,
    "dueAmount": true, "status": true,
    "dateOfBooking": false, "totalPrice": false, "unitNumber": false
  }
}
```
Name convention: `<componentName><userId>`. Created/owned by the user (`createdBy: "248"`). cxapp has 613 of these in dev.

**Why:** The runtime writes these as users interact (toggle columns, drag panels). Builders don't author them.

**How to apply (in the CFA):**
- Read tools (`list_personalizations`, `get_personalization`, `count_personalizations`) are exposed in the `runtime` module — useful for debugging "why does user X see this view?" or auditing personalization patterns.
- Write tools are deliberately ABSENT from the CFA — the runtime owns the write path, and direct writes would corrupt the runtime's idea of what a user has customized.
- Personalization isn't part of "building an app" — the CFA tagged these as a separate `runtime` group so the agent doesn't waste cycles authoring them during build flows.
