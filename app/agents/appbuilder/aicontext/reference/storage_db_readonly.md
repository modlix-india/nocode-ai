---
name: feedback-storage-db-readonly
description: "Per-tenant storage databases (e.g. ABDUL1_cxapp, CITYV_cxapp) must NEVER be written to by modlix-mcp — read-only access only, for debugging."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 0a3b792f-b0ea-4757-9c52-ac7f531b7154
---

The Modlix platform separates definition databases from data databases:
- `ui` and `core` MongoDB databases hold SYSTEM-level definitions + client overrides (pages, functions, storages, themes, etc.). modlix-mcp can read AND write these.
- The per-tenant databases named `<CLIENT_CODE>_<app_code>` (e.g. `ABDUL1_cxapp`, `CITYV_cxapp`, `ABD_kyc`) hold runtime row data inserted via the application's own server-side Kirun functions running on Storage definitions.

**Rule:** modlix-mcp must NEVER write to `<CLIENT>_<app>` storage databases. Inserts/updates/deletes go through the platform's storage operations (server-side Kirun calling the data layer), not through direct Mongo writes.

**Why:** Writing directly bypasses storage triggers (BEFORE_CREATE/AFTER_UPDATE), per-op auth gates (createAuth/readAuth/updateAuth/deleteAuth), audit logging, indexes, and relation cascade constraints. Direct writes produce data that the runtime doesn't know was inserted, breaking event flows and audits.

**How to apply:**
- Storage CRUD tools build/edit Storage *definitions* in `core.storage` (the schema, relations, triggers, indexes, auth) — fine.
- Tools that touch *actual data rows* must be READ-ONLY and clearly labeled (e.g. `query_storage_rows`, `count_storage_rows`, `inspect_storage_row`) for debugging.
- Never expose `insert_row`, `update_row`, `delete_row` style tools on the data side. If an agent needs to insert data, it should call the platform's storage-write functions through Kirun, not Mongo directly.
- The same caution applies even more strongly to prod-mongo: read for diagnostics, never write.
