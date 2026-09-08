---
name: Feedback - Precision on platform concepts
description: Imprecise claims about platform architecture get corrected here - always verify domain-specific distinctions
type: feedback
originSessionId: a68f67da-54bd-43b6-a29c-00a7710d2ce0
---
Never conflate these platform concepts:
- **Override system** (DifferenceExtractor/Applicator) is multi-tenant inheritance (Client A→B delta), NOT versioning
- **Page event functions** can call Core functions directly — they do NOT need UI functions as intermediaries
- **Sites (SITE)** don't use themes — colors go inline in components. Only APPs use themes/styles.
- **Workflows** are not implemented yet — don't include them in plans or tools

**Why:** Each of these was corrected during planning. Getting them wrong signals a misunderstanding of the platform.

**How to apply:** When discussing the platform, double-check these distinctions. If unsure, ask rather than assume.
