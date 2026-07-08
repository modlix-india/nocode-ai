"""Select-phase prompts (brand + generic). ``$target_count`` filled at injection time.

The agent picks positives from the real Planner-scored candidates (never invents). These port
the old positive-selection guardrails — pre-filter, in-area-volume priority, match-type strategy,
and the Option-B cross-business (upsell-sibling) rule carried over from seeding — onto our
context (CORE TERMS / SIBLING CATEGORIES / LOCATION). Real demand sets the count; never pad.
"""

SELECT_GENERIC = """\
STEP — SELECT POSITIVES (generic). You now have real Google data (keyword | volume | competition
| CPC). Select up to $target_count — let real demand set the count, never pad with weak terms to
hit a number. Call submit_positive_keywords EXACTLY ONCE, each item:
{ keyword (exact text from the data), match_type (EXACT|PHRASE),
  intent (commercial|transactional|informational|navigational), is_cross_business, rationale }.

PRE-FILTER every candidate before selecting (drop the ones that fail):
- RELEVANCE: it's built on a CORE TERM above, or it qualifies as an upsell sibling (see below).
  Exclude anything containing the brand name.
- LOCATION: if it names a place, that place must be the served city / area above — drop out-of-area
  locations (a different city = wasted spend). Place-less keywords are fine.
- DEMAND: drop 0-volume terms — a generic phrase nobody searches is a dead seed, not a keyword;
  skip long technical variants (>4 words with specs/measurements).
- REDUNDANCY: don't select one concept phrased several ways — word-order, singular/plural, connector
  words (luxury apartments bangalore / luxury apartments IN bangalore), or a longer keyword that just
  restates a shorter pick. Keep the single highest-volume version.

PRIORITISE in this order until you reach the target:
1. High-volume CORE terms IN the served area (volume + location both strong).
2. Core term + the served city / service areas above.
3. Buyer intent — buy / book / hire / for sale / price + core term.
4. Qualified — best / top / near me + core term.
5. Strong commercial / transactional core terms without a location, to reach 15.

SIBLINGS / CROSS-BUSINESS (the upsell judgment from seeding):
- A SIBLING-CATEGORY candidate is a positive ONLY if its buyer could be UP-SOLD to this offering
  (same buyer, adjacent segment / budget). Then set is_cross_business=true and match_type=PHRASE —
  never EXACT, never broad.
- A sibling serving a cheaper or different buyer is NOT a positive — leave it for negatives.

MATCH TYPE (aim ~80-90% PHRASE, ~10-20% EXACT):
- PHRASE is the default workhorse — reach with the context preserved.
- EXACT only for tight, high-intent, conversion-ready terms (complete buyer intent, 4-5 words).

PAGING: fetch_more_candidates shows the next (lower-volume) page. If the shown set lacks enough
in-area or core terms, page through more FIRST, gather all picks across pages, then submit ONCE.

Use the keyword text EXACTLY; never invent. Rationale: one line — why selected + why that match type."""

SELECT_BRAND = """\
STEP — SELECT POSITIVES (brand). You now have real Google data (keyword | volume | competition |
CPC). Select up to $target_count brand keywords — let real demand set the count, never pad to a
number. Call submit_positive_keywords EXACTLY ONCE (same item shape as generic).

ELIGIBILITY (brand protection): keep ONLY keywords containing at least one SIGNIFICANT word of the
brand name (for a multi-word brand, ANY significant word qualifies — ignore "the"/"and"/"of").
A keyword with no brand word is NOT eligible — reject it.

MANDATORY FIRST: the first positive is the FULL brand name, match_type PHRASE (brand protection).

PRIORITISE in this order:
1. The full brand name and its close variants / misspellings — own these even at low or zero
   volume (brand protection; a new brand's terms are quiet now but still yours to hold).
2. High-volume brand terms — brand + core term / location / buying intent.
3. Brand + the served city / service areas above (drop locations not served).
4. Brand + BUYING intent — reviews, price, plans, buy, book.

VOLUME DISCIPLINE: past the brand-name assets in (1), keep a brand+location or brand+offering term
only if it shows real search demand. When the brand name itself has volume, do NOT manufacture
zero-volume brand+X combinations to reach a count — they can never serve. Keep brand terms at zero
ONLY when the brand has no search history anywhere (a genuinely new brand).

MATCH TYPE:
- Position 1: PHRASE (mandatory).
- Rest: ~80-90% PHRASE (the workhorse for brand discovery + protection), ~10-20% EXACT for
  high-intent conversion terms (brand + price / buy / a specific configuration).

FILTER OUT (a keyword hitting any of these is rejected — even at high volume):
- existing-user / support searches — login, sign in, account, contact, customer care, download.
  These are people you already have or who won't convert; they go in negatives, never here.
- keywords over 5 words; technical specs / measurements; any term with no brand word.
- one concept phrased several ways — word-order, spacing (canva login / canvalogin), or connector
  words (with/without in·for·the). Keep the single highest-volume version.
Note: a feature / amenity is FINE when the brand is present ("[brand] pool villas" is a brand search) —
the brand makes it brand-focused; reject only if no brand word or too long.

PAGING: brand runs are compact, but if key brand variations are missing, page through more FIRST,
then submit ONCE. Use the keyword text EXACTLY; never invent."""

SELECT_COMPETITOR_BRAND = """\
STEP — SELECT POSITIVES (competitor brand). You now have real Google data (keyword | volume |
competition | CPC) covering all competitors. You MUST select keywords for EVERY single competitor listed above, 
aiming for up to $target_count PER COMPETITOR. Let real demand set the exact count, but you CANNOT skip a competitor. Call submit_competitor_keywords EXACTLY ONCE, each item:
{ keyword (exact text from the data), competitor_name (must match a competitor listed above),
  match_type (EXACT|PHRASE), intent (commercial|transactional|informational|navigational),
  is_cross_business, rationale }.

ELIGIBILITY (per competitor, same brand-protection logic as a brand campaign): keep ONLY
keywords containing at least one significant word of THAT competitor's own brand name. A
keyword with no competitor brand word is NOT eligible — reject it. Never attribute a keyword to
the wrong competitor.

There is NO negatives phase for this type — the exclusions below are permanent rejections, not
a later cleanup step:
- SUPPORT / ACCOUNT-MANAGEMENT — login, sign in, account, customer care, download: reject, these
  are the competitor's own existing users, not conquest targets.
- DISTRUST / COMPLAINT — scam, fraud, "is [competitor] legit", lawsuit, complaints: reject, these
  searchers are vetting trust, not shopping. Plain "reviews" and "vs" / "alternatives" / "pricing"
  ARE kept — real pre-purchase comparison searches, the prime conquest surface.
- CAREERS / JOBS — reject, not buyers.
- keywords over 5 words; technical specs / measurements; any term with no competitor brand word.

PRIORITISE in this order, per competitor:
1. The competitor's brand name and close variants / misspellings.
2. High-volume brand + core-term / location / buying-intent combinations.
3. Brand + comparison intent — reviews, vs, alternatives, pricing.

MATCH TYPE (aim ~80-90% PHRASE, ~10-20% EXACT): PHRASE is the default; EXACT only for tight,
high-intent, conversion-ready terms.

PAGING: fetch_more_candidates shows the next (lower-volume) page. 
If any competitor listed above has NO keywords visible on this first page, you MUST call fetch_more_candidates to page through the data before submitting. 
Smaller competitors' terms may be on page 2 or 3 because they have lower search volumes than the large competitors. Gather all picks across pages, then submit ONCE.

Use the keyword text EXACTLY; never invent. Rationale: one line — why selected + why that match
type."""
