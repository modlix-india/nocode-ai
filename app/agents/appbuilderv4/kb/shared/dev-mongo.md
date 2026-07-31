---
name: reference-dev-mongo
description: "Dev + prod MongoDB connection details and the platform's database layout (definitions vs runtime data)."
metadata: 
  node_type: memory
  type: reference
  originSessionId: 0a3b792f-b0ea-4757-9c52-ac7f531b7154
---

**Dev MongoDB:**
```
mongosh "mongodb://admin:<REDACTED — ask the project owner>@dev-mongo:27017/?retryWrites=true&w=majority&authSource=admin"
```
Use the short hostname `dev-mongo` (in /etc/hosts → 172.16.1.76); the FQDN in the original URL did not resolve.

**Prod MongoDB:** Same admin user; password also `<REDACTED — ask the project owner>` is plausible but not confirmed — ask before connecting. Host `prod-mongo` (in /etc/hosts → 172.16.1.236).

**Database layout (both envs):**
- `ui` — all UI-layer definitions, SYSTEM root + per-client overrides. Collections: `application`, `page`, `theme`, `style`, `function`, `schema`, `filler`, `uri_path`, `mobileApp`, `personalization`, `version`, `transport`.
- `core` — backend-layer definitions, same override model. Collections: `function`, `schema`, `storage`, `connection`, `template`, `notification`, `eventDefinition`, `eventAction`, `filler`, `version`, `transport`.
- `<CLIENT>_<app_code>` (e.g. `ABDUL1_cxapp`, `CITYV_cxapp`, `BUILD_cxapp`) — per-tenant **runtime data** for each Storage. Hundreds of these. READ-ONLY per [[feedback-storage-db-readonly]].

**Override model in collections:** Documents within `ui.*` / `core.*` are discriminated by `{appCode, clientCode, name}`. SYSTEM is the root; child clients have `baseClientCode` pointing to parent. Reads auto-merge parent into child via `extractDifference`/`applyOverride`.

**MySQL (dev):**
```
mysql -h dev-mysql -u admin -p<REDACTED — ask the project owner>
```
Holds security entities (users/clients/apps/roles/profiles) plus other service tables (`core`, `files`, `multi`, `message`, `notification`, `entity_collector`, `entity_processor`, `worker`, `ai`).
