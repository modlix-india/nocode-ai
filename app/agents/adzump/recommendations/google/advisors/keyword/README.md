# Keyword Recommendations Advisor

Generates keyword optimisation recommendations for Google Ads campaigns using a hybrid pipeline that combines deterministic rule-based analysis with LLM-powered generation and scoring.

## Architecture Overview

The advisor lives at `recommendations/google/advisors/keyword/` and is consumed by `GoogleRecommendationEngine` (`google/engine.py`). It implements a two-track strategy:

- **Track A — Critical Keyword Detection:** Scans existing campaign keywords for critical performance failures and emits `PAUSE` recommendations. Deterministic, no LLM involvement.
- **Track B — Keyword Discovery:** Expands seed keywords, fetches candidates from Google Keyword Planner and Native Recommendations API, selects the best candidates via an LLM call, and ranks them through a weighted scoring formula. Emits `ADD` recommendations.

Both tracks run in sequence for each campaign. `PAUSE` recommendations from Track A and `ADD` recommendations from Track B are merged into a single `KeywordRecommendation` list returned to the engine.

## File-by-File Breakdown

### `keyword_service.py` — Orchestrator

Entry point for the keyword advisor pipeline. Called by `GoogleRecommendationEngine._build_campaign_recommendation()`.

**Class:** `KeywordAdvisorService`

**Public method:**

```python
async def suggest_keyword_recommendations(
    self,
    campaign_group: Dict[str, Any],
    account_id: str,
    parent_id: str,
    client_code: str,
    auth_headers: dict,
) -> List[KeywordRecommendation]
```

**Pipeline:**

1. **Track A — `_review_poor_keywords(campaign_group)`:**
   Iterates over `campaign_group["entries"]` (the evaluated keyword list). For each entry where `strength == "poor"` and `is_critical == True`, it builds a `KeywordRecommendation` with `recommendation="PAUSE"`, using the `criterion_id` from the entry. Returns a list of PAUSE recommendations. No data-fetching or LLM calls — purely rule-based.

2. **Builds a context dict** containing brand name, business type, primary location, service areas, URL, unique features, summary, and auth headers — all sourced from the campaign group's business info.

3. **Track B — `idea_service.suggest_keywords()`:** Delegates to `KeywordIdeaService` (see below).

4. **Merges results:** Concatenates Track A PAUSE recs with Track B ADD recs and returns the combined list.

**Singleton:** `keyword_advisor = KeywordAdvisorService()` (module-level instance).

---

### `idea_service.py` — LLM Keyword Discovery Engine

The core of the discovery pipeline. Fetches candidates from two Google APIs, selects via LLM, scores, and ranks.

**Class:** `KeywordIdeaService`

**Constants (class-level):**

| Constant | Value | Purpose |
|---|---|---|
| `DEFAULT_SEMANTIC_SCORE` | `50.0` | Fallback when embedding fails |
| `MAX_ANCHOR_KEYWORDS` | `20` | Max "top" keywords used as embedding anchors |
| `MAX_SUGGESTIONS_IN_PROMPT` | `50` | Max candidates sent to the LLM selection prompt |
| `MAX_ENTRIES_FOR_AD_GROUP_FORMAT` | `50` | Max existing ad-group entries shown in prompt context |
| `MIN_KEYWORDS_PER_CAMPAIGN` | `15` | Minimum number of keyword recommendations to return |
| `KEYWORDS_PER_AD_GROUP` | `5` | Number of keywords per ad group for dynamic cap calculation |
| `IMPACT_UPLIFT_CAP` | `50` | Soft-cap for normalising native conversion uplift to 0-100 |
| `IMPACT_SCORE_DECIMALS` | `1` | Decimal precision for impact_score |
| `FALLBACK_VOLUME` | `0` | Default volume when historical metrics are missing |
| `FALLBACK_COMPETITION` | `"UNKNOWN"` | Default competition level when missing |
| `FALLBACK_COMPETITION_INDEX` | `0.0` | Default competition index when missing |
| `FALLBACK_MATCH_TYPE` | `"BROAD"` | Default match type for native recs without match type |

**Public method:**

```python
async def suggest_keywords(
    self,
    campaign_details: Dict[str, Any],
    account_id: str,
    parent_id: str,
    client_code: str,
    context: Dict[str, Any],
) -> List[KeywordRecommendation]
```

**Step-by-step pipeline:**

1. **Filter anchor keywords:** Extracts entries with `strength == "good"` or `"top"`. If none exist, returns `[]` early — no point generating suggestions without a baseline.

2. **Expand seeds via `KeywordSeedExpander`:**
   Passes the good/top keywords along with business metadata. The expander uses an LLM call (fast tier) to brainstorm 10 related keywords, then hits the Google Autocomplete API to expand both the original seeds and the LLM-generated seeds. Returns a deduplicated list.

3. **Fetch Google Keyword Planner suggestions:**
   Calls `keyword_planner_adapter.generate_keyword_ideas()` with the expanded seeds plus the campaign URL. Returns keyword ideas with volume, competition level, and competition index.

4. **Fetch and hydrate Native Recommendations:**
   Calls `google_recommendations_adapter.fetch_recommendations()` filtered to `RecommendationType.KEYWORD` for the current campaign. For each returned recommendation:
   - Fetches historical metrics (volume, competition) via `keyword_planner_adapter.generate_historical_metrics()`
   - Computes an `impact_score` from the predicted conversion uplift: `min(uplift / 50 * 100, 100)`, capped at 100, with `None` if no uplift
   - Falls back to `FALLBACK_VOLUME` (0), `FALLBACK_COMPETITION` ("UNKNOWN"), `FALLBACK_COMPETITION_INDEX` (0.0), and `FALLBACK_MATCH_TYPE` ("BROAD") where data is missing
   - Each native rec is tagged with `is_native_recommendation: True`

5. **Combine and deduplicate:** Merges Google Planner suggestions with hydrated native recs; deduplicates by lowercase keyword text, keeping the first occurrence.

6. **Compute semantic scores:**
   Calls `scorer.calculate_semantic_scores()` with the deduplicated suggestion texts and the top/top anchor keywords. Computes max cosine similarity (via OpenAI embeddings) against each anchor keyword, normalised to a 0-100 scale. Unmatched suggestions get `DEFAULT_SEMANTIC_SCORE` (50.0).

7. **LLM selection (`_llm_select_keywords`):**
    - Loads the `recommendations/keyword_suggestion_prompt.txt` prompt template
   - Formats the prompt with campaign context, existing ad-group structure, and the candidate suggestions (capped at `MAX_SUGGESTIONS_IN_PROMPT`)
   - Each suggestion line includes: keyword text, volume, competition, competition index, ROI (impact_score for native, "—" for planner), and semantic score. Native recs are tagged with `[G]`.
   - Calls the LLM (balanced tier, 4000 max tokens) with the prompt
   - Parses the JSON response, expecting `{"keywords": [...]}` or `{"selected_keywords": [...]}`
   - Returns the list of selected keyword dicts, or `[]` on parse failure

8. **Assign ad groups:**
   Calls `scorer.assign_ad_groups()` to map each selected keyword to the nearest ad group based on embedding similarity of existing keywords in each ad group.

9. **Build final recommendations:**
   Enriches each LLM-selected keyword with the full Google Planner / Native Rec data, filters out any suggestions that already exist in the campaign, then calls `scorer.score_and_rank_keywords()` to compute weighted scores.

   The number of keywords returned is capped at `max(MIN_KEYWORDS_PER_CAMPAIGN, ad_group_count * KEYWORDS_PER_AD_GROUP)` — a dynamic floor of 15 keywords per campaign, scaled by the number of ad groups at 5 per group.

   Returns `List[KeywordRecommendation]` with `recommendation="ADD"`, `origin="KEYWORD"`, and all relevant metrics.

**Singleton:** `idea_service = KeywordIdeaService()` (module-level instance, held as `self.idea_service` by `KeywordAdvisorService`).

---

### `seed_expander.py` — Seed Keyword Expansion

Expands a small set of high-performing keywords into a broader list of seed keywords for the Keyword Planner API.

**Class:** `KeywordSeedExpander`

**Constants:**

| Constant | Value | Purpose |
|---|---|---|
| `LLM_SEED_COUNT` | `10` | Number of seed keywords the LLM should generate |
| `AUTOCOMPLETE_MAX_SUGGESTIONS` | `5` | Max autocomplete suggestions per seed keyword |

**Public method:**

```python
async def expand_seeds(
    self,
    good_keywords: list[str],
    business_type: str,
    primary_location: str,
    features_context: str,
    brand_name: str = "",
) -> list[str]
```

**Pipeline:**

1. **LLM seed generation (`_generate_llm_seeds`):**
    Loads the `recommendations/seed_expansion_prompt.txt` template and formats it with the performing keywords, business type, location, features, and brand name. Sends to the LLM (fast tier, 500 max tokens). Expects a JSON response with a `"seed_keywords"` array. Returns up to 10 seed keywords.

2. **Autocomplete expansion (`_expand_with_autocomplete`):**
   Takes the **original** good keywords and calls `batch_fetch_autocomplete_suggestions()` on each. Collects up to 5 suggestions per seed. Then does the same for the **LLM-generated** seeds. All results are aggregated.

3. **Deduplication:** Merges original seeds + autocomplete suggestions + LLM seeds + LLM autocomplete suggestions into a single list, removing duplicates. Returns the combined seed list.

---

### `scorer.py` — Scoring and Ranking

Computes embedding-based semantic similarity, assigns keywords to ad groups, and applies a weighted scoring formula to rank keyword candidates.

**Module-level constants:**

| Constant | Value | Purpose |
|---|---|---|
| `SCORE_WEIGHTS` | `{ volume: 0.22, competition: 0.18, business_relevance: 0.23, intent: 0.14, semantic: 0.13, native_boost: 0.10 }` | Dimension weights for the scoring formula |
| `VOLUME_SCORE_TIERS` | `[(0, 100, 20), (100, 500, 40), (500, 1000, 60), (1000, 5000, 80), (5000, inf, 100)]` | Maps search volume ranges to scores |
| `BUSINESS_RELEVANCE_SCORES` | `{ high: 100, medium: 60, low: 20 }` | Maps relevance labels to scores |
| `INTENT_TYPE_SCORES` | `{ transactional: 100, commercial: 80, navigational: 60, informational: 40, unknown: 20 }` | Maps intent labels to scores |
| `CROSS_BUSINESS_PENALTY` | `0.5` | Multiplier for cross-business keywords |
| `MINIMUM_SCORE` | `40` | Floor — keywords below this are dropped |
| `NATIVE_UPLIFT_CAP` | `50` | Uplift at which native_boost reaches 100 |
| `NATIVE_FLAT_BONUS` | `60` | Flat bonus for native recs with no uplift data |
| `FALLBACK_COMPETITION_INDEX` | `0.5` | Default competition index when missing |
| `FALLBACK_BUSINESS_RELEVANCE` | `"medium"` | Default relevance label when missing |
| `FALLBACK_BUSINESS_SCORE` | `60` | Default business score when label undefined |
| `FALLBACK_INTENT` | `"unknown"` | Default intent label when missing |
| `FALLBACK_INTENT_SCORE` | `20` | Default intent score when label undefined |
| `DEFAULT_SEMANTIC_SCORE` | `50.0` | Default semantic score when embedding fails |
| `FALLBACK_VOLUME_SCORE` | `20.0` | Default volume score when volume falls outside all tiers |
| `SCORE_DECIMALS` | `2` | Rounding precision for all scores |
| `PERCENTAGE_MULTIPLIER` | `100` | Multiplier to normalise 0-1 values to 0-100 |
| `NATIVE_BOOST_MAX_SCORE` | `100` | Cap for native_boost |
| `FALLBACK_VOLUME` | `0` | Default volume when missing from keyword data |
| `FALLBACK_CONVERSIONS` | `0` | Default conversions when missing from impact data |

**Public functions:**

```python
async def generate_embeddings(texts: List[str]) -> List[List[float]]
```
Generates embeddings via the LLM provider (resolves to OpenAI `text-embedding-3-small`). Used by both `assign_ad_groups` and `calculate_semantic_scores`.

```python
async def assign_ad_groups(
    new_keywords: List[str], existing_keywords: List[Dict[str, Any]]
) -> Dict[str, Dict[str, str]]
```
Groups existing keywords by `ad_group_id`, generates embeddings for all keywords (new + existing), then assigns each new keyword to the ad group containing the existing keyword with the highest cosine similarity. Returns `{ keyword: { ad_group_id, ad_group_name } }`.

Optimisation: if only one ad group exists, skips embeddings entirely and assigns all keywords to it directly.

```python
async def calculate_semantic_scores(
    suggestion_texts: List[str], anchor_texts: List[str]
) -> Dict[str, float]
```
Generates embeddings for both suggestion and anchor texts, then for each suggestion computes its max cosine similarity against any anchor keyword, normalised to a 0-100 scale. Returns `{ keyword: score }`.

```python
def score_and_rank_keywords(keywords: List[Dict[str, Any]]) -> List[Dict[str, Any]]
```
Scores each keyword via `_score_single_keyword()`, filters out any scoring below `MINIMUM_SCORE` (returns `None`), then sorts descending by `final_score`.

**`_score_single_keyword` internals:**

```
volume_score      = tiered_lookup(volume)                    → 20-100
competition_score = (1 - competitionIndex) × 100             → 0-100
business_score    = lookup(business_relevance)                → 20/60/100
intent_score      = lookup(intent)                            → 20-100
semantic_score    = from embedding or 50.0                    → -100-100

if is_native_recommendation:
    uplift = potential_conversions - base_conversions
    native_boost = min(uplift / 50 × 100, 100) if uplift > 0
                   else 60                                    → 0-100

final = volume × 0.22
      + competition × 0.18
      + business × 0.23
      + intent × 0.14
      + semantic × 0.13
      + native_boost × 0.10

if is_cross_business:
    final ×= 0.5

if final < 40: drop()
```

Each scored keyword also gets a `score_breakdown` dict with the individual dimension scores for transparency.

---

### `evaluator.py` — Metric Performance Evaluator

Rule-based classifier that assigns a `strength` label (`"poor"`, `"good"`, `"top"`) to each existing keyword based on its performance metrics.

**Configuration (`MetricEvaluatorConfig` dataclass):**

| Field | Default | Purpose |
|---|---|---|
| `ctr_threshold` | `2.0` | CTR below this is flagged |
| `quality_score_threshold` | `4` | QS below this is flagged |
| `cpl_multiplier` | `1.5` | CPL > 1.5× median is flagged |
| `min_clicks_for_conversions` | `15` | Min clicks before conversion rate is meaningful |
| `conversion_rate_threshold` | `1.0` | Conv rate below this is flagged |
| `critical_click_threshold` | `50` | Clicks above this with no conversions = critical |
| `critical_cost_threshold` | `2000.0` | Cost above this with no conversions = critical |
| `default_max_cpl` | `2000.0` | Upper bound for CPL normalisation |
| `default_min_cpl` | `50.0` | Lower bound for CPL normalisation |
| `top_performer_percentage` | `0.2` | Top 20% of good keywords upgraded to "top" |
| `performance_weights` | `{ efficiency: 0.40, impressions: 0.30, conversions: 0.30 }` | Weights for top-performer ranking |

**Class:** `MetricPerformanceEvaluator`

```python
def evaluate(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]
```
Adds `strength` and `reason` to each entry.

A keyword is classified as `"poor"` if:
- Zero impressions (plus a note if budget is likely the cause)
- Two or more performance issues are detected (high CPL, low CTR, low QS, low conversion rate)
- Any critical issue exists (high spend with no conversions, many clicks with no conversions)

Otherwise it is classified as `"good"`.

```python
def mark_top_performers(self, entries: List[Dict[str, Any]]) -> None
```
Ranks "good" entries by a composite score (`efficiency × 0.40 + impressions × 0.30 + conversions × 0.30`) and upgrades the top 20% to `"top"`. Mutates the list in place.

**Performance issue detection (`_identify_performance_issues`):**

Checks for these flags:
- `"high_cpl"` — CPL > 1.5× median CPL
- `"low_ctr"` — CTR < 2%
- `"low_quality_score"` — Quality Score < 4
- `"low_conversion_rate"` — Conversion rate < 1% (only if clicks ≥ 15)

Also detects critical issues:
- `"high_spend_no_conversions"` — cost > 2000 with zero conversions
- `"high_clicks_no_conversions"` — clicks ≥ 50 with zero conversions

---

## Data Flow Diagram

```
GoogleRecommendationEngine.generate_recommendations()
  │
  ├─ PARALLEL: accounts API + fetch_campaign_mappings()      ← mapping_service.py
  │
  └─ PARALLEL per account:
       └─ _process_account()
            │
            ├─ PARALLEL:
            │   ├─ budget_bidding_advisor.analyse(campaign_ids=None)  ← all campaigns
            │   └─ fetch_keyword_metrics(campaign_ids=mapping.keys()) ← filtered at query level
            │
            ├─ Evaluator: evaluate() → mark_top_performers()
            ├─ _group_by_campaign() → enrich with product data
            │
            └─ PARALLEL per campaign:
                 └─ _build_campaign_recommendation()
                      │
                      ├── keyword_advisor.suggest_keyword_recommendations()
                      │    │
                      │    ├── Track A: _review_poor_keywords()
                      │    │     → PAUSE KeywordRecommendation[]
                      │    │
                      │    └── Track B: idea_service.suggest_keywords()
                      │         │
                      │         ├── seed_expander.expand_seeds()
                      │         │    ├── LLM → 10 seed keywords
                      │         │    └── Autocomplete API
                      │         │
                      │         ├── keyword_planner.generate_keyword_ideas()
                      │         ├── _fetch_and_hydrate_native_recommendations()
                      │         │
                      │         ├── Deduplicate & semantic score
                      │         ├── _llm_select_keywords()
                      │         │
                      │         ├── assign_ad_groups()
                      │         └── score_and_rank_keywords()
                      │              → ADD KeywordRecommendation[]
                      │
                      └── CampaignRecommendation(
                            keywords=[...],
                            budget_bidding=pre_fetched_budget_rec
                          )

  └─ PARALLEL storage store() for all CampaignRecommendations
```

## Scoring Formula Summary

Each keyword receives a `final_score` (0-100) representing its predicted value:

| Dimension | Weight | Range | Data Source |
|---|---|---|---|
| Volume | 0.22 | 20-100 | Google Keyword Planner historical metrics |
| Competition | 0.18 | 0-100 | `(1 - competitionIndex) × 100`, planner data |
| Business Relevance | 0.23 | 20-100 | LLM-classified label (high/medium/low) |
| Intent | 0.14 | 20-100 | LLM-classified label (transactional/commercial/etc.) |
| Semantic Similarity | 0.13 | 0-100 | Max cosine similarity vs anchor keywords |
| Native Boost | 0.10 | 0-100 | Google's predicted conversion uplift (or flat 60) |

Cross-business penalty: × 0.5. Minimum score to survive: 40.

## Output

`List[KeywordRecommendation]` (defined in `recommendations/models.py`):

| Field | Type | Description |
|---|---|---|
| `text` | `str` | Keyword text |
| `match_type` | `Literal` | `"EXACT"`, `"PHRASE"`, or `"BROAD"` |
| `recommendation` | `Literal` | `"ADD"` (Track B) or `"PAUSE"` (Track A) |
| `reason` | `str` | LLM rationale or rule-based explanation |
| `origin` | `Optional[Literal]` | Always `"KEYWORD"` |
| `ad_group_id` | `Optional[str]` | Assigned ad group |
| `ad_group_name` | `Optional[str]` | Assigned ad group name |
| `criterion_id` | `Optional[str]` | Present only for PAUSE recommendations |
| `resource_name` | `Optional[str]` | Google Ads resource name (native recs) |
| `metrics` | `Optional[dict]` | `{ volume, competition, competitionIndex, semantic_score }` |
| `score` | `Optional[float]` | Weighted final score (0-100) |
| `quality_score` | `Optional[int]` | Present only for PAUSE recommendations |
| `applied` | `bool` | UI tracking flag, always `False` at generation time |

## LLM Calls Per Campaign

| Step | Model Tier | Max Tokens | Purpose |
|---|---|---|---|
| Seed expansion | fast | 500 | Brainstorm 10 related keywords |
| LLM selection | balanced | 4000 | Select best keywords from candidates |

Two LLM calls per campaign. Embedding calls (for semantic scoring and ad-group assignment) use OpenAI `text-embedding-3-small` and do not count as LLM inference calls.

## Error Handling

- **Empty inputs:** If no good/top keywords exist, `suggest_keywords()` returns `[]` early.
- **API failures:** Google Planner and Native Recommendations API failures return `[]` or `None`; the pipeline continues with whatever data is available.
- **LLM failures:** If the LLM call fails or returns unparseable JSON, `_llm_select_keywords()` catches the exception, logs `"llm_selection_failed"`, and returns `[]`.
- **Embedding failures:** `assign_ad_groups()` and `calculate_semantic_scores()` catch exceptions, log warnings, and return `{}` (ad groups unassigned; semantic scores default to 50.0).
- **Empty results:** If no keywords make it past LLM selection or scoring, `suggest_keywords()` returns `[]`. The engine handles this gracefully — the campaign may still get budget/bidding recommendations.
- **Per-campaign isolation:** `_build_campaign_recommendation()` wraps its body in a try/except. If one campaign's LLM call or processing throws, it is logged and returns `None` — the error never propagates to kill other campaigns in the same account batch.
- **Missing product mapping:** `generate_recommendations` filters campaigns to only those present in AISuggestedData. `run_optimization_for_campaign` returns `None` immediately if the requested campaign_id has no mapping — no API calls are made.
