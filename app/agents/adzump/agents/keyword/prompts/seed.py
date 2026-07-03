"""Seed-phase prompts (brand + generic) — the foundation of the whole run.

Seeds amplify: good seeds -> good autosuggest expansion -> more/better Planner ideas ->
more relevant selections, so these are the richest prompts of the set. Anchored on the
CORE TERMS / SIBLING CATEGORIES / LOCATION that build_dynamic_context injects.
``$max_seeds`` is filled at injection time.
"""

SEED_GENERIC = """\
STEP — SEED + EXPAND (generic). Draft up to $max_seeds seed phrases, then call expand_keywords
with them, then keyword_metrics. Seeds are the foundation: autosuggest widens them and the
Planner scores them, so weak seeds weaken everything downstream. Make them excellent.

THE QUESTION behind every seed: when someone needs this offering, what do they type into Google?
Think as the buyer with a need — not as the business describing itself.

1) SEED THE OFFERING, NOT ITS DIFFERENTIATORS.
   - Build every seed from a CORE TERM above — the product/service/solution customers search for.
   - A DIFFERENTIATOR is what a buyer discovers AFTER searching, not the search itself: materials,
     amenities, certifications, methods, awards, finishes, and marketing words (premium, luxury,
     best, world-class). Do NOT seed these.
   - Test each: "would a customer search for this, or search the offering and find this as a feature
     later?" If later -> it's a differentiator, drop it.
   - SIBLING CATEGORIES are usually negatives (a different product) — but use judgment, not a blanket
     ban: when a sibling's buyer could realistically be UP-SOLD to this offering (same buyer profile,
     just an adjacent segment or budget), it is a valid target. Seed only a SMALL minority of such
     upsell siblings — they are held as controlled cross-business (phrase) terms at selection. Skip
     siblings that serve a cheaper or different buyer who won't convert; those stay negatives.

2) ANCHOR TO LOCATION — this keeps the campaign local instead of national noise. If a LOCATION is
   given above, weight your seeds roughly like this (use BOTH the city and its service areas):
   - ~50%  "[core term] [location]"
   - ~20%  "[core term] near [location]" / "[core term] near me"
   - ~20%  "[action] [core term] [location]"   (buy / book / hire / for sale)
   - ~10%  "[qualifier] [core term] [location]" (best / top / affordable)
   If the LOCATION says national / online: use "[core term] online", "[core term] [use-case]",
   "[action] [core term]" instead — and do NOT invent city names.

3) QUALITY BAR — every seed must be:
   - SPECIFIC: built on a core term (or an approved upsell sibling), not just a modifier.
   - NATURAL: how real people search, not business jargon.
   - CONCISE: 2-3 words (4 with a location); never over the 10-word limit.
   - lowercase; no brand name (this is the non-branded set); no duplicates.

PATTERN (adapt to THIS business's core terms — do NOT copy these words):
- core "duplex villa", city "bengaluru" -> duplex villa bengaluru / duplex villa near me / buy duplex villa
- core "no-code crm", national -> no-code crm / crm for small business / best no-code crm

Before adding a seed, check: would a buyer searching it want what this business sells? If not, drop it."""

SEED_BRAND = """\
STEP — SEED + EXPAND (brand). Draft up to $max_seeds brand seed phrases, then call expand_keywords
with them, then keyword_metrics. EVERY seed contains the brand — this set protects the brand name.

BRAND-NAME HANDLING (do this first):
- Seed 1 is the FULL brand name, exactly.
- SINGLE-word brand: use it in full in every seed (a partial isn't the brand).
- MULTI-word brand: use the full brand AND its DISTINCTIVE words (a word unique to this brand, not a
  generic/ambiguous one). For a LONG brand (4+ words), seeds after the first use only the 2-3 most
  significant words (drop "the"/"and"/"of" and generic suffixes) to keep them short.

GENERATE ACROSS THESE ANGLES (weight navigation + location heaviest — they carry the volume):
1. CORE NAVIGATION — brand alone, and brand partials.
2. BRAND + LOCATION — brand/partial + the served city and service areas above (served area ONLY).
3. BRAND + CORE TERM — brand + what they SELL (from CORE TERMS above). Strict product-vs-feature:
   seed the MAIN offering, never an amenity/feature/finish — a feature is not what customers buy.
4. BRAND + INTENT — reviews, contact, price, book, near me (the ones that fit this business).
5. BRAND + CONFIGURATION — a real variant the offering has: a size, a plan tier, a model, a unit type.
6. MISSPELLINGS — 3-5 ACTUAL typo'd variants of the brand: vowel swap, missing/doubled letter,
   spacing slip, phonetic. Real misspellings, not the word "misspelling".

AVOID: locations not in the served area above; amenities/features as the product; technical specs or
measurements; generic industry terms that aren't the brand's offering; marketing fluff.

EXAMPLE (adapt to THIS brand — do NOT copy these words):
- brand "Subha White Waters" -> subha white waters (full, first) / white waters / white waters bengaluru
  / white waters villa / white waters reviews / white watters (a misspelling)

SHAPE: every seed carries a significant brand word; keep seeds to 2-3 words — except the first
(full brand) and "near me" terms (up to 4); lowercase; no duplicates."""
