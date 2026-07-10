"""Negative-phase prompts (brand + generic). ``$max_negatives`` filled at injection time.

Negatives are reasoned, not scavenged: the agent reads the business model + the positives it
just selected and decides what would waste spend — it does NOT mine the dropped candidate pool
(that only drags in noise). Type-aware (brand vs generic) and anchored on the OFFERING /
CORE TERMS / SIBLING CATEGORIES / LOCATION context. As important as the positives.
"""

NEGATIVES_GENERIC = """\
STEP — NEGATIVE KEYWORDS (generic). Negatives matter as much as positives — they stop the budget
bleeding on searches that will never convert. REASON them from the BUSINESS and the POSITIVES you
just selected — do NOT scavenge leftover scored candidates. Call submit_negative_keywords, each:
{ keyword, reason, match_type (PHRASE|BROAD) }.

FIRST, read the business model — it decides what is a negative vs a real lead:
- Does it sell new / premium, or budget / used? Local or national / online? B2C or B2B? Does it
  offer free trials / DIY / courses? A word is a negative ONLY if THIS business doesn't serve it.

THEN produce exclusions, in priority order:
1. SIBLING CATEGORIES that are NOT upsell targets — the cheaper or different-buyer products from the
   SIBLING list above (the ones you did NOT keep as cross-business positives). Exclude their terms.
2. NON-BUYER INTENT — searches that signal no purchase for THIS business (e.g. recruitment, or
   free / DIY / course-seeking when it sells a paid product, "how to" / "what is" when it isn't served).
3. WRONG SEGMENT — condition or price signals that clash with what it sells (e.g. "used" / "cheap"
   for a premium seller), but ONLY where such a search is real in this category.
4. OUT-OF-AREA — other cities / regions not in the served area above (when location-bound).
5. WRONG AUDIENCE / USE CASE — B2B-vs-B2C mismatch, or use cases the business does not serve.

GUARDRAILS:
- Every negative must be a search a REAL person would type in THIS business's category — reason each
  from the business model, never from a generic bargain/DIY/used template (e.g. "used" / "second-hand"
  mean nothing for a bank or a software product; don't add them there).
- NEVER exclude a positive, a CORE TERM, or an upsell sibling you kept as a positive.
- Make each negative DISTINCT from your positives — a negative that's mostly the same words as a
  positive is auto-rejected by the system (it would block real traffic), so don't waste a slot on it.
- Prefer specific, multi-word exclusions — a broad single word over-blocks real traffic.
- MATCH TYPE — exclude the CONCEPT, not one string. PHRASE (default) blocks the term and anything
  containing it in order ("free glasses" also stops "free glasses uk"). BROAD blocks a search with
  ALL the words in any order — for multi-word concepts whose order varies ("cheap eyewear" ≈
  "eyewear cheap"). Never EXACT: it blocks only the literal query, letting every variation leak
  through. No duplicates or near-duplicates (keep the representative one).
- Each reason names the category (e.g. "sibling category — sells villaments, not budget apartments").
- Be thorough; a strong set is fine and welcome (20+ is OK)."""

NEGATIVES_BRAND = """\
STEP — NEGATIVE KEYWORDS (brand). Protect the brand campaign from spend that won't convert.
REASON from the BUSINESS and your brand POSITIVES — do NOT scavenge leftover scored candidates.
Call submit_negative_keywords, each: { keyword, reason, match_type (PHRASE|BROAD) }.

Brand negatives are DIFFERENT from generic — focus on:
1. COMPETITOR brand names — you don't want to pay on competitors inside a brand campaign.
2. DISTRUST / complaint — scam, fraud, "is [brand] legit", complaints, lawsuit (NOT plain "reviews",
   which is a real pre-purchase search).
3. SUPPORT-ONLY / NON-BUYER — login, sign in, customer care, helpline, careers, jobs (not buyers).
4. UNRELATED products the brand does NOT offer (the SIBLING CATEGORIES above, where a brand-prefixed
   search would mislead).

GUARDRAILS:
- NEVER exclude a positive brand keyword.
- Make each negative DISTINCT from your positives — one that's mostly the same words as a positive
  is auto-rejected by the system, so don't waste a slot on it.
- Prefer specific, multi-word exclusions; no duplicates.
- MATCH TYPE — PHRASE (default) blocks the term + anything containing it in order; BROAD blocks all
  the words in any order (multi-word concepts). Never EXACT — it blocks only the literal query and
  lets every variation leak through.
- Each reason names the category (e.g. "competitor — different builder", "support-only — not a buyer").
- Be thorough; a strong negative set protects the brand budget."""
