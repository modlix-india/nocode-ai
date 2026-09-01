# Implementation Notes: adzump competitor entries - verified URLs + typed CompetitorProfile

Spec: http://localhost:8765/plans/adzump-competitor-profile-plan.html
Progress: CP-1 done (`b212f59`) · CP-2 done · CP-3 waits on D-4

## Decisions Not in the Spec

- **Separate notes file**: `implementation-notes.md` already belongs to the next_action rework build; this plan gets its own file rather than mixing two specs' decision logs.
- **D-1..D-3 taken as the doc's recommendations** (dicts in session / typed write-back for the creatives attach / no post-parse URL stamp): the /visual-implement invocation on the reviewed doc is the go; D-4 (library name-key fallback) stays open until CP-3 per the doc's own gate.

### CP-1

- **`_clean_urls` / `_filter_self_references` stay dict-level**: the plan said "operate on models", but they are container cleaners inside the ingest module running right after `_normalize_entries` has already stamped the model shape - retyping them adds churn without a second consumer. The model boundary is the normalize step, not the cleaners.
- **`from_stored` never raises**: a malformed entry degrades to a name-only profile with a warning instead of crashing hydration of old stored records. LLM nulls on non-nullable fields (e.g. `key_usps: null`) fall back to defaults; only `url`/`pricing`/`weakness` keep an explicit null (they are nullable in the analyst schema).
- **`to_stored` may add `url: null` where the key was absent**: the dump always carries the 8 schema keys. Consumers only ever `.get()` these; the analyst schema declares url as present-or-null, so this is shape-tightening, not drift. The creatives triad is the exception - omitted entirely when never fetched, because craft's badge logic keys on key-presence (fetched-empty vs unfetched).
- **Single-lookup heals legacy lists**: `_lookup_single_competitor` normalizes the whole stored list through the model on entry, so pre-model sessions carrying `product_name` entries are repaired the first time they are touched; the `product_name` fallback reads at `competitor.py:420/526` are deleted.
- **creatives write-back is index-aligned**: `profiles` mirrors `competitors` by index (None for non-dict junk) so `competitors[i] = profile.to_stored()` can never land on the wrong entry when a list contains a malformed element.
- **`creatives_for_all` now takes `list[CompetitorProfile]`**: the library's `competitor_identity` reads `.url`/`.name` typed; the phantom `comp.get("domain")` fallback (never written by anything) died with it. Both retirements are grep-locked in `test_competitor_profile.py`.

### CP-2

- **Places API (New) over legacy Text Search**: the legacy `/place/textsearch` endpoint needs a second Place Details call to get the website; `places:searchText` with a field mask (`places.displayName,places.websiteUri`) returns it in one call. Heads up: the Google key must have "Places API (New)" enabled - if lookups log `places_search non-200 ... 403`, that's the missing enablement, not a code bug.
- **Adapter is a dumb lookup, guards live in discovery**: the plan sketched guards inside `find_business_website`; they landed in `_resolve_missing_urls` instead because both guards are discovery-owned concepts (`_normalize_name` fuzzy matching, `_is_aggregator_host` with the module's extra hosts). The adapter returns `{name, website}` or None, nothing else.
- **Shared-host reject = the aggregator check**: `AGGREGATOR_HOSTS` in `_shared.py` already contains facebook/instagram/youtube etc., so a GBP whose website is a social page is rejected by the same predicate that rejects 99acres - no second host list to maintain.
- **`pageSize: 1`, top listing only**: with the locality bias and the name-similarity guard, taking one result keeps the call cheapest; scanning N listings for a better name match can be added later if rejects show up in logs (`places_url_rejected` lines make that measurable).
- **Bias radius 50km**: metro-wide, and the API's documented cap. No-coords sessions still search, just unbiased - the name guard is the backstop.
- **Evidence-URL fix has no unit test**: the `fetch_url or url` pick sits inline in `_shortlist_competitors`'s evidence loop; extracting a one-line helper to make it testable buys a tautology. Covered by the manual rerun (working expectation: linked cards) instead.
