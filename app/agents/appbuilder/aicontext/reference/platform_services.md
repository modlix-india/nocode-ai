---
name: platform-services
description: Map of nocode-saas microservice service classes → responsibilities. The "why" behind each modlix-mcp tool.
metadata:
  type: reference
---

# nocode-saas service map

Each modlix-mcp tool ultimately hits a Spring controller, which delegates to
a *service class* that holds the business logic — override merging, version
locking, cascade rules, authority checks. When a tool returns surprising
results, the answer usually lives in the service class.

This map points at the class file + the key methods. Read the source when
debugging tool behaviour; don't try to infer from tool docstrings alone.

## security service

Path: [nocode-saas/security/src/main/java/com/fincity/security/service/](../../nocode-saas/security/src/main/java/com/fincity/security/service)

| Service | Responsibility | Key methods |
|---|---|---|
| **AuthenticationService** | Login / token issue / refresh / verify. Password, OTP, social, one-time-token flows. Sets `verifiedAppCode` on the JWT. | `authenticate()`, `verifyToken()`, `refreshToken()`, `revoke()` |
| **UserService** | User CRUD, status transitions (ACTIVE/INACTIVE/LOCKED/DELETED), `getUserAuthorities()` which joins roles + profiles + appends `Authorities.Logged_IN`. Lockout counters. | `findNonDeletedUserNActiveClient()`, `getUserAuthorities()`, `increaseFailedAttempt()`, `unblockUser()` |
| **ClientService** | Client tenant CRUD. `isUserClientManageClient()` is the hierarchy gate every cross-tenant write checks. | `read()`, `update()`, `isUserClientManageClient()` |
| **ClientHierarchyService** | The 4-level managed-by chain (manageClientLevel0..3). `isClientBeingManagedBy()` is the predicate for "can I act on this tenant?". | `create()`, `isClientBeingManagedBy()`, `getClientHierarchy()` |
| **RoleV2Service** | Role + authority CRUD. Builds the per-app authorities map cached against userId. | `getRoleAuthoritiesPerApp()`, role CRUD |
| **ProfileService** | Profiles = named bundles of roles. `getProfileAuthorities()` resolves per (app, profileIds, hierarchy). | `getProfileAuthorities()`, profile CRUD |
| **AppService** | Security-side App records. `hasReadAccess()` / `hasWriteAccess()` enforce the appAccessType (OWN/ANY/EXPLICIT) + per-client grants. `deleteEverything()` is the hard-delete cascade. | `hasReadAccess()`, `addClientAccess()`, `deleteEverything()` |
| **OtpService** | OTP generate/verify per ClientOtpPolicy (EMAIL/SMS channels, expiry, resend caps). | `generateOtp()`, `verifyOtp()` |
| **AppRegistrationIntegrationTokenService** | Social-login + SSO3 exchange. Validates upstream OAuth tokens, mints platform OneTimeToken for app jump. | (used by `/authenticate/social`) |
| **SoxLogService** | Audit log (CREATE/UPDATE/DELETE/ASSIGN/UNASSIGN/LOGIN per object type). Fire-and-forget async writes. | `createLog()` |

## ui service

Path: [nocode-saas/ui/src/main/java/com/fincity/saas/ui/service/](../../nocode-saas/ui/src/main/java/com/fincity/saas/ui/service)

| Service | Responsibility | Key methods |
|---|---|---|
| **PageService** | Page CRUD + the surgical PATCH endpoints (`patchComponent`, `patchEventFunction`) with per-component / per-event version locking. Increments `componentVersions[key]` and `eventFunctionVersions[key]`. | `patchComponent()`, `patchEventFunction()`, `updatableEntity()` |
| **ApplicationService** | App definition CRUD (`/api/ui/applications`). Manages `defaultPage`, `loginPage`, `shellPage` references; CSP/fontPacks/iconPacks etc. | `read()`, `update()`, `index()` |
| **FunctionService** | UI Kirun function CRUD + surgical PATCH `/steps` (with `expectedVersion`). | `patchSteps()`, function CRUD |
| **ThemeService**, **StyleService** | Theme variable maps; raw global CSS dumps. Plain CRUD; no surgical endpoints. | (inherited from AbstractOverridableDataService) |
| **SchemaService** (ui) | UI-scoped schemas (rarely used; most apps have 0). | repositoryFind / repositoryFilter |
| **UriPathService** | Defines REST routes that invoke Kirun functions. Path params map to function arguments via `pathParamMapping`. | `applyOverride()` (handles per-method bindings) |
| **MobileAppService** | Native mobile build artifacts (Android keystore, splash, icon, status). Status reflects async build outcome. | (build pipeline lifecycle) |
| **PersonalizationService** | Per-user runtime preferences. Owned by the platform runtime; agents read only. | (read-mostly) |
| **EngineService** | Compiled-style + theme read paths for the runtime. Caches per app. | `readStyle()`, `readTheme()`, `readPage()` |
| **InheritanceService** | The order() helper that returns the client-code chain (own → parent → SYSTEM) used for override resolution. | `order(appCode, urlClientCode, clientCode)` |

## core service

Path: [nocode-saas/core/src/main/java/com/fincity/saas/core/service/](../../nocode-saas/core/src/main/java/com/fincity/saas/core/service)

| Service | Responsibility | Key methods |
|---|---|---|
| **CoreFunctionService** | Server-side Kirun function CRUD + execution. Runs the function graph through the Java Kirun runtime; resolves primitives via the hybrid repository (built-ins + app + remote). | `read()`, `update()`, `execute()` |
| **StorageService** | Storage definition CRUD + validation. Validates relation targets exist; fires per-op auth checks; runs BEFORE/AFTER triggers (functions referenced as `Namespace.FunctionName`). | `validate()`, trigger dispatch in `create()`/`update()`/`delete()` |
| **SchemaService** (core) | Kirun schema CRUD (the data-model layer). repositoryFind/Filter walk override chain. | `repositoryFind()`, `repositoryFilter()` |
| **ConnectionService** | External integration CRUD (REST_API/SMTP/EXOTEL/WHATSAPP). connectionDetails carries credentials — never log or surface raw. | (per-subType validation) |
| **TemplateService** | i18n message templates (email/sms). `templateParts` keyed by locale; recipient + locale via Kirun expressions. | (CRUD + send-time resolution) |
| **NotificationService** | Notification config (mostly placeholder shape in dev). | – |
| **EventDefinitionService** | Named platform events with payload schemas. | (CRUD) |
| **EventActionService** | Event handlers — task pipelines (CALL_CORE_FUNCTION tasks) that fire when matching events emit. | (task dispatch) |
| **AbstractFunctionService** | Generic function-doc save logic shared by ui + core. Increments `version` on every update. | `update()` (version bump) |

## commons-mongo (cross-cutting)

Path: [nocode-saas/commons-mongo/src/main/java/com/fincity/saas/commons/mongo/](../../nocode-saas/commons-mongo/src/main/java/com/fincity/saas/commons/mongo)

| Component | Responsibility |
|---|---|
| **AbstractOverridableDataService** | The override engine. `getMergedSources()` walks the baseClientCode chain; `extractOverride()` calls each entity's `extractDifference()` to produce a diff doc; reads auto-merge via `applyOverride()`. **This is where the "save in a child tenant creates an override doc" magic happens.** |
| **AbstractMongoDataController** | Generic CRUD HTTP layer. POST creates (override-aware via service); PUT updates with version check; DELETE soft-deletes per status flags. |
| **DifferenceApplicator / DifferenceExtractor** | Recursive deep-merge / deep-diff for the Map<String,Object> definition fields (componentDefinition, eventFunctions, etc.). Null in a diff means "delete this inherited key". |
| **AbstractTransportController** | Bundle export (POST /makeTransport) + apply (GET /applyTransport/{id}). Each domain (ui/core/security) extends. |
| **VersionService** | Per-entity version history. Each save creates a Version doc with the prior state. |

## When to consult this map

- **A tool returns a doc that looks "merged"** → it's the override engine at work. Read `AbstractOverridableDataService.readPageFilterLRO` to see the merge.
- **A PUT comes back 412 PRECONDITION_FAILED** → version-lock conflict; check `PageService.updatableEntity` or `patchComponent` for the version logic.
- **An authority check fails unexpectedly** → trace through `UserService.getUserAuthorities` to see what authorities were computed at login.
- **A Storage create returns "relation target not found"** → `StorageService.validate` is the check.
- **A function fails at runtime** → execution happens in `CoreFunctionService.execute` (or the JS Kirun runtime client-side); inspect the step's `parameterMap` and `dependentStatements`.

Most modlix-mcp tools are thin wrappers around the controllers; the real
behaviour lives in the services above. When tool output surprises an agent,
the answer is almost always one level down — at the service, not the tool.
