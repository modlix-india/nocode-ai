# Implementation Notes: adzump competitor entries - verified URLs + typed CompetitorProfile

Spec: http://localhost:8765/plans/adzump-competitor-profile-plan.html
Progress: CP-1 done (`b212f59`) · CP-2 done (`08a62ae`) · CP-3 dropped (D-4 superseded by the source swap) · scrapecreators source done

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

### ScrapeCreators source (post-plan, Kailash's call 2026-09-01)

- **Why**: adlibrary.com results were inconsistent (regional crawl lag; 1-of-5 fetch). scrapecreators.com scrapes Meta's real Ad Library: `is_active`/`start_date`/`end_date` are Meta's values, and search supports a country filter. Decisions taken via chips: default source NOW (adlibrary stays as `ADS_INTEL_SOURCE=adlibrary` fallback), status=ALL (full history; real `is_active` marks live ads).
- **Keyword search needs advertiser selection**: unlike a company query, `/search/ads` mixes many pages. `_ads_of_the_advertiser` keeps exactly ONE page's ads: the page whose ads link to the competitor's domain (catches a parent-brand page advertising the project microsite), else the page whose name fuzzy-matches, else an honest empty fetch - a wrong advertiser's creatives must never enter the shared library. This replaces D-4 entirely: the fetch is name-driven, so CP-3's name-key fallback is moot.
- **Exact phrase first, one unordered retry**: multi-word project names match junk with `keyword_unordered`; zero exact-phrase hits fall back once.
- **Protocol grew `country: str = ""`**: threaded from `product_data.place.country_code` in `_fetch_stage`; adlibrary ignores it (no regional filter on that API).
- **PAGE_LIMIT = 3 cursor pages** per search (each page is a metered credit); cap still `MAX_CREATIVES_PER_COMPETITOR = 60`.
- **Video field names are best-effort**: the docs example had `videos: []`; the adapter reads `video_hd_url`/`video_sd_url`/`video_preview_image_url` (ScrapeCreators' documented Meta scrape shape). First live video fetch should be eyeballed; an unknown shape degrades to an empty asset URL, which the renderer already skips.
- **Metrics are thin by design**: Meta's library exposes spend/impressions only for EU/political ads; only `estSpend` is mapped when present. The craft metric tiles omit zeros already.
- **config.py commit dance**: the file is skip-worktree'd (local GATEWAY_URL override); the commit stages a crafted HEAD+settings blob so only the new settings land, then skip-worktree is restored.
- **Not live-tested**: SCRAPECREATORS_API_KEY is not in variables.sh yet; Kailash adds it, then the real-estate session rerun validates end to end.
