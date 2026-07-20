---
name: Versioning Model - Component-Level for Pages
description: Pages use per-component + per-event-function versioning, not section-level. Functions and Applications use whole-document versioning.
type: project
originSessionId: a68f67da-54bd-43b6-a29c-00a7710d2ce0
---
**Final versioning model (decided 2026-04-11):**

- **Pages**: Per-component-key (`componentVersions`) + per-event-function (`eventFunctionVersions`) versioning. Two users editing different components succeed concurrently. Same component → 412.
- **Functions**: Whole-document versioning (existing `version` field). Steps are tightly coupled — per-step versioning adds complexity without benefit.
- **Applications**: Whole-document versioning. Small objects.
- **Full page PUT**: Increments ALL component + event versions (invalidates concurrent component PATCHes).
- **Component PATCH**: Increments that component's version + whole-doc version. Does NOT affect other components.

**Why:** Section-level versioning (componentDefinition as a section) was too coarse — componentDefinition is the largest part of a page. Component-level versioning enables collaborative editing and matches how the AI agent works (reads one component, modifies it, saves it).

**How to apply:** Backend endpoints are `PATCH /{id}/components/{key}` and `PUT /{id}/events/{name}`. The generic `patchSection` on AbstractOverridableDataService was removed — versioning logic lives directly in PageService.
