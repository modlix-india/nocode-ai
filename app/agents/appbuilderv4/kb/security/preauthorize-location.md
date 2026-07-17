---
name: PreAuthorize on Service in entity-processor
description: In nocode-saas/entity-processor, role checks via @PreAuthorize go on the service method, not the controller — even though most existing controllers have it
type: feedback
originSessionId: 1a27fbb2-2155-4922-9aa8-e060acd706f6
---
In `nocode-saas/entity-processor`, put `@PreAuthorize("hasAuthority('Authorities.ROLE_X')")` on the **service** method (the one with business logic), not on the controller endpoint.

**Why:** Kiran corrected this directly — services are the business-logic gatekeepers, and other call paths (kirun reactive functions, internal service-to-service calls) bypass controllers entirely. Putting auth at the controller leaves those paths unprotected.

**How to apply:**
- For any new write/mutation method in entity-processor that needs role-based gating, annotate the **service method** with `@PreAuthorize`.
- Don't double up — if it's on the service, leave the controller endpoint plain.
- Reads typically don't need it; only writes/mutations.
- Note: most existing entity-processor controllers (e.g., `PartnerController`) do put `@PreAuthorize` on the controller. That's legacy / non-canonical — follow the service-method placement going forward.
- Authority constants live in `entity-processor/.../constant/BusinessPartnerConstant.java` (e.g., `OWNER_ROLE = "Authorities.ROLE_Owner"`).
