---
name: storage-db-readonly
description: "Per-tenant storage databases (e.g. ABDUL1_cxapp, CITYV_cxapp) hold runtime data and must NEVER be written to directly. Read-only access for debugging only."
---

The Modlix platform separates definition databases from data databases:
- `ui` and `core` MongoDB databases hold SYSTEM-level definitions + client overrides (pages, functions, storages, themes, etc.). The CFA can read AND write these via its modlix tools.
- The per-tenant databases named `<CLIENT_CODE>_<app_code>` (e.g. `ABDUL1_cxapp`, `CITYV_cxapp`, `ABD_kyc`) hold runtime row data inserted via the application's own server-side Kirun functions running on Storage definitions.

**Rule:** the CFA must NEVER write to `<CLIENT>_<app>` storage databases. Inserts/updates/deletes go through the platform's storage operations (server-side Kirun calling the data layer), not through direct Mongo writes.

**Why:** Writing directly bypasses storage triggers (BEFORE_CREATE/AFTER_UPDATE), per-op auth gates (createAuth/readAuth/updateAuth/deleteAuth), audit logging, indexes, and relation cascade constraints. Direct writes produce data that the runtime doesn't know was inserted, breaking event flows and audits.

**How to apply:**
- Storage CRUD tools (`create_storage`, `update_storage`, etc.) build/edit Storage *definitions* in `core.storage` (the schema, relations, triggers, indexes, auth) — fine.
- Tools that touch *actual data rows* are READ-ONLY by design and clearly labeled: `query_storage_rows`, `count_storage_rows`, `get_storage_row`. No write tools exist on the data side, and none should be added.
- If the user asks to insert/update/delete data rows, the CFA must instead author or invoke a server-side Kirun function that performs the write through the platform's storage operations.
- The same caution applies even more strongly to prod-mongo: read for diagnostics, never write.
