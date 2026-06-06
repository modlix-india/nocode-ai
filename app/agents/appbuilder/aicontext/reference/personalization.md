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

**How to apply (for modlix-mcp tooling):**
- Read tools (`list_personalization`, `get_personalization`, `count_personalization`) are valuable — debugging, dashboards, agent inspection.
- Write tools (`create_personalization`, `update_personalization`) should be rare — only useful for seeding defaults for new users or migrating preferences. Don't make them prominent.
- Personalization isn't part of "building an app" — keep tools clearly tagged so agents don't waste cycles authoring them during build flows.
