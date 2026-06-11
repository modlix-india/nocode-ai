# Budget & Bidding Recommendations Advisor

Analyses campaign budgets and bidding strategies using a hybrid pipeline that combines Google Ads native recommendations with deterministic heuristic diagnosis. Runs once per account (before per-campaign keyword processing), producing one `BudgetBiddingRecommendation` per campaign.

## Architecture Overview

The advisor lives at `recommendations/google/advisors/budget/` and is consumed by `GoogleRecommendationEngine` (`google/engine.py`). It implements a three-stage pipeline:

1. **Data collection (`CampaignMetricsAdapter`):** Fetches campaign context (budget, bidding strategy, impression share, conversion volume, portfolio linkage) in a single GAQL query.
2. **Bidding advisor (`_score_bidding_recommendation`):** Evaluates bidding strategy maturity using conversion-volume-based decision trees, preferring native Google recommendations when available.
3. **Budget diagnosis (`diagnose_budget`):** Classifies the campaign's constraint type from impression share signals and computes a recommended budget amount.

Both bidding and budget outputs are merged into a unified `BudgetBiddingRecommendation` model per campaign.

```
BudgetBiddingAdvisorService.analyse()
  │
  ├─ CONCURRENT:
  │   ├─ CampaignMetricsAdapter.fetch_campaign_contexts()    ← single GAQL query
  │   ├─ GoogleRecommendationsAdapter.fetch_multiple_types()  ← bidding rec types
  │   └─ GoogleRecommendationsAdapter.fetch_multiple_types()  ← budget rec types
  │
  └─ Per campaign (sequential):
       ├─ _score_bidding_recommendation()                    ← conversion-based decision tree
       │    ├─ Priority 1: Native Google rec present        → surface with confidence
       │    └─ Priority 2: No native rec                    → apply maturity decision tree
       │
       ├─ diagnose_budget()                                  ← impression share + native recs
       │    ├─ Parse native recs (CAMPAIGN_BUDGET, MOVE_UNUSED_BUDGET, etc.)
       │    ├─ Classify constraint (BUDGET_CONSTRAINED / BID_CONSTRAINED / MIXED / NONE)
       │    ├─ Apply Maximize Conversions IS guard
       │    ├─ Apply freshness downgrade
       │    ├─ Reconcile with native rec amounts
       │    └─ Add advisory notes (low-budget, learning phase, pacing)
       │
       └─ Merge → BudgetBiddingRecommendation
```

## File-by-File Breakdown

### `budget_service.py` — Orchestrator

Entry point for the budget & bidding pipeline. Called by `GoogleRecommendationEngine._process_account()` once per account (all campaigns together).

**Class:** `BudgetBiddingAdvisorService`

**Public method:**

```python
async def analyse(
    self,
    account_id: str,
    parent_id: str,
    client_code: str,
    context: Dict[str, Any],
    campaign_ids: Optional[List[str]] = None,
) -> List[BudgetBiddingRecommendation]
```

**Pipeline:**

1. **Concurrent fetch:** Three `asyncio.gather` tasks run in parallel:
   - `CampaignMetricsAdapter.fetch_campaign_contexts()` — fetches budget, bidding, performance, and impression share data for all (or filtered) campaigns. When `campaign_ids` is provided, a GAQL `WHERE campaign.id IN (...)` clause filters server-side. Returns one normalised context dict per campaign (see Diagnostics sections below).
   - `GoogleRecommendationsAdapter.fetch_multiple_types()` with `_BIDDING_REC_TYPES` — fetches native bidding recommendations (9 types, see below).
   - `GoogleRecommendationsAdapter.fetch_multiple_types()` with `_BUDGET_REC_TYPES` — fetches native budget recommendations (4 types, see below).

2. **Index native recs by campaign:** `_index_by_campaign()` flattens the `{rec_type: [recs]}` map into `{campaign_resource_name: [recs]}` for O(1) lookup per campaign.

3. **Per-campaign loop:** For each campaign context:
   - `_score_bidding_recommendation(ctx, native_bidding_recs)` — bidding advisor
   - `diagnose_budget(ctx, bidding_result, native_budget_recs)` — budget diagnosis
   - Determine `apply_order` (bidding first if both recommended)
   - Determine `scope` (PORTFOLIO or CAMPAIGN)
   - Merge into `BudgetBiddingRecommendation` model

4. Returns `List[BudgetBiddingRecommendation]` — one per campaign.

#### Recommendation Type Groups (module-level constants)

**`_BIDDING_REC_TYPES`** (9 types queried from the API):

| RecommendationType | Value | What It Recommends |
|---|---|---|
| `TARGET_CPA_OPT_IN` | `"TARGET_CPA_OPT_IN"` | Switch from Manual CPC to Target CPA |
| `TARGET_ROAS_OPT_IN` | `"TARGET_ROAS_OPT_IN"` | Switch from Target CPA to Target ROAS |
| `MAXIMIZE_CONVERSIONS_OPT_IN` | `"MAXIMIZE_CONVERSIONS_OPT_IN"` | Switch to Maximize Conversions |
| `MAXIMIZE_CONVERSION_VALUE_OPT_IN` | `"MAXIMIZE_CONVERSION_VALUE_OPT_IN"` | Switch to Maximize Conversion Value |
| `MAXIMIZE_CLICKS_OPT_IN` | `"MAXIMIZE_CLICKS_OPT_IN"` | Switch to Maximize Clicks |
| `RAISE_TARGET_CPA` | `"RAISE_TARGET_CPA"` | Raise target CPA for more conversions |
| `LOWER_TARGET_ROAS` | `"LOWER_TARGET_ROAS"` | Lower target ROAS for more volume |
| `SET_TARGET_CPA` | `"SET_TARGET_CPA"` | Set a target CPA where none exists |
| `SET_TARGET_ROAS` | `"SET_TARGET_ROAS"` | Set a target ROAS where none exists |

**`_BUDGET_REC_TYPES`** (4 types queried from the API):

| RecommendationType | Value | What It Recommends |
|---|---|---|
| `CAMPAIGN_BUDGET` | `"CAMPAIGN_BUDGET"` | Increase budget for budget-limited campaigns |
| `FORECASTING_CAMPAIGN_BUDGET` | `"FORECASTING_CAMPAIGN_BUDGET"` | Increase budget ahead of forecasted traffic |
| `MARGINAL_ROI_CAMPAIGN_BUDGET` | `"MARGINAL_ROI_CAMPAIGN_BUDGET"` | Adjust budget to maximise marginal ROI |
| `MOVE_UNUSED_BUDGET` | `"MOVE_UNUSED_BUDGET"` | Shift unused budget from unconstrained to constrained campaigns |

#### Conversion Confidence Scoring (`_conversion_confidence`)

A helper function that maps conversion volume to a `ConfidenceLevel` for a given recommendation type. Never blocks a recommendation — only annotates it with confidence.

The thresholds are practitioner conventions (not official Google limits — see module docstring for source references). Each recommendation type has `"high"` and `"medium"` thresholds defined in the `_CONV_THRESHOLDS` table:

| Threshold Constant | Value | Used By |
|---|---|---|
| `CONV_THRESHOLD_TARGET_CPA` | `30` | All tCPA-related rec types ("high") |
| `CONV_THRESHOLD_MAX_CONVERSIONS` | `15` | All auto-bidding recs ("high"), all recs ("medium") |
| `CONV_THRESHOLD_TARGET_ROAS` | `50` | All tROAS-related rec types ("high") |
| `CONV_THRESHOLD_ROAS_LOW_VOLUME` | `20` | Lower tROAS threshold for low-volume campaigns |

Rule: `conversions >= high → HIGH`, `conversions >= medium → MEDIUM`, else `LOW`.

#### Bidding Strategy Maturity Decision Tree (`_score_bidding_recommendation`)

Evaluates which bidding strategy change (if any) to recommend for a single campaign.

**Priority 1 — Native Google recommendation present:** Iterates in priority order (`TARGET_ROAS_OPT_IN`, `TARGET_CPA_OPT_IN`, `MAXIMIZE_CONVERSION_VALUE_OPT_IN`, `MAXIMIZE_CONVERSIONS_OPT_IN`, `RAISE_TARGET_CPA`, `LOWER_TARGET_ROAS`, `SET_TARGET_CPA`, `SET_TARGET_ROAS`, `MAXIMIZE_CLICKS_OPT_IN`). If a native rec exists for that type, surfaces it with conversion-based confidence and `google_rec_confirmed=True`. If confidence is LOW, appends a note about sub-threshold conversion volume.

**Priority 2 — No native rec, apply decision tree:**

| Current Strategy | Conversions/month | Recommended Action |
|---|---|---|
| `MANUAL_CPC` | ≥ 30 | `TARGET_CPA_OPT_IN` |
| `MANUAL_CPC` | ≥ 15 | `MAXIMIZE_CONVERSIONS_OPT_IN` |
| `MANUAL_CPC` | < 15 | `MAXIMIZE_CLICKS_OPT_IN` |
| `MAXIMIZE_CONVERSIONS` | ≥ 30 | `TARGET_CPA_OPT_IN` (add CPA constraint) |
| `TARGET_CPA` | ≥ 50, no tROAS | `TARGET_ROAS_OPT_IN` |
| `TARGET_ROAS` | < 20, has tROAS | `LOWER_TARGET_ROAS` |

**Portfolio scope detection:** If the campaign uses a portfolio bidding strategy (`is_portfolio = True`) AND the recommended action involves changing a target, the rationale is appended with a note that the change must be applied at the portfolio level. Affected types: `RAISE_TARGET_CPA`, `LOWER_TARGET_ROAS`, `SET_TARGET_CPA`, `SET_TARGET_ROAS`, `TARGET_CPA_OPT_IN`, `TARGET_ROAS_OPT_IN`.

**Return dict keys:**

| Key | Type | Description |
|---|---|---|
| `bidding_rec_type` | `str \| None` | RecommendationType value, or None |
| `bidding_rec_rationale` | `str` | Human-readable explanation |
| `bidding_confidence` | `ConfidenceLevel` | `"high"` / `"medium"` / `"low"` |
| `bidding_blocked_reason` | `None` | Always None (never blocks — only scores) |
| `google_rec_confirmed` | `bool` | True when a native Google rec was found |

**Key design note:** This service does NOT hard-block any recommendation. Even with zero conversions and `MANUAL_CPC`, it will still recommend `MAXIMIZE_CLICKS_OPT_IN` (which has a `{"high": 0, "medium": 0}` threshold, so confidence will always be HIGH). The rationale includes the actual conversion volume so the user has context.

---

### `diagnosis_service.py` — Budget Diagnosis

Pure diagnosis logic — no API calls. Takes a normalised campaign context dict and a bidding recommendation dict, and produces a budget constraint diagnosis with confidence scoring.

**Public function:**

```python
def diagnose_budget(
    campaign_context: Dict[str, Any],
    bidding_recommendation: Optional[Dict[str, Any]] = None,
    native_budget_recs: Optional[list] = None,
) -> Dict[str, Any]
```

#### Configurable Thresholds (module-level constants)

| Constant | Value | Purpose |
|---|---|---|
| `BUDGET_LOST_IS_THRESHOLD` | `0.10` | Budget constraint if > 10% impressions lost to budget |
| `RANK_LOST_IS_THRESHOLD` | `0.20` | Bid constraint if > 20% impressions lost to rank |
| `PACING_WARNING_THRESHOLD` | `0.20` | Pacing warning when recommended change > 20% |
| `LOW_BUDGET_ADVISORY_RATIO` | `2.0` | Advisory note when budget < 2× tCPA |

All thresholds are practitioner conventions with source notes in the module docstring — not official Google limits.

#### Step-by-Step Diagnosis Pipeline

**Step 1 — Parse native budget recommendations:** Iterates through `native_budget_recs` (if any) and extracts:
- `CAMPAIGN_BUDGET` → `native_rec_amount` (Google's recommended budget in currency units)
- `MOVE_UNUSED_BUDGET` → `move_unused_signal = True`

**Step 2 — Classify constraint type** via `_classify_constraint()`:

Impression share identity: `Search IS + Lost IS (Budget) + Lost IS (Rank) ≈ 100%`

| Condition | ConstraintType |
|---|---|
| budget_lost_IS > 10%, rank_lost_IS < 20% | `BUDGET_CONSTRAINED` |
| rank_lost_IS > 20%, budget_lost_IS < 5% | `BID_CONSTRAINED` |
| Both elevated | `MIXED_CONSTRAINT` |
| Neither elevated | `NONE` |

**IMPORTANT — Maximize Conversions IS guard:** Google explicitly states that `search_budget_lost_impression_share` is unreliable for Maximize Conversions and Maximize Conversion Value campaigns because these strategies are designed to spend the full daily budget (always "limited by budget" by design). When the campaign uses one of these strategies:
- `constraint_type` is forced to `NONE` (regardless of IS signals)
- `base_confidence` is downgraded to LOW
- An explanatory note is appended to `blocking_issues`

Source: https://support.google.com/google-ads/answer/7381968

**Step 3 — Apply freshness downgrade:** Checks `metric_freshness` metadata attached by `CampaignMetricsAdapter`. If impression share data is > 2 days old (`is_fresh = False`):
- `base_confidence` is downgraded to LOW
- The freshness warning is appended to `blocking_issues`

Google Ads impression share metrics have a 24–48 hour computation lag (source: https://support.google.com/google-ads/answer/7103314).

**Step 4 — Reconcile diagnosis with native recs and produce output:**

| Constraint Type | Native Rec Present | Behaviour |
|---|---|---|
| `BUDGET_CONSTRAINED` | Yes | Use native_rec_amount as recommended_budget. Confidence = HIGH (unless stale). `google_rec_confirmed = True` |
| `BUDGET_CONSTRAINED` | No | No recommended_budget. Confidence = MEDIUM (unless stale). Generic rationale. |
| `BID_CONSTRAINED` | Yes | No budget rec. Native amount added to blocking_issues with note to fix bids first. Confidence = MEDIUM (unless stale). |
| `BID_CONSTRAINED` | No | No budget rec. Confidence = MEDIUM (unless stale). |
| `MIXED_CONSTRAINT` | Any | No budget rec. Native amount added to blocking_issues. Resolve bidding first, then reassess. Confidence = MEDIUM (unless stale). |
| `NONE` | Any | No budget rec. Rationale: "No significant budget or rank constraint detected." |

**Step 5 — Low-budget advisory note (tCPA campaigns only):** `_low_budget_advisory()` checks if `current_budget < target_cpa × LOW_BUDGET_ADVISORY_RATIO (2.0)`. If so, appends an advisory note to `budget_rec_rationale`. This is a UI hint only — it does NOT raise `recommended_budget` and does NOT block any recommendation.

**Critical design note about the 2× tCPA rule:** Google's documentation does NOT state a "daily budget ≥ 2× tCPA" as a Smart Bidding requirement. The 2× figure appears in Google's Help Center only in the context of overdelivery: Google may spend up to 2× the daily budget on high-traffic days. See the module docstring for the full analysis with source references. We surface an advisory note only — no auto-raising of the recommended budget based on this unverified multiplier.

Source: https://support.google.com/google-ads/answer/1704424

**Step 6 — Learning phase guard:** When both a bidding strategy change AND a budget increase are recommended, a `learning_phase_warning` is generated:
"Apply the bidding change first and allow 1–2 weeks for the learning phase to complete before increasing the budget, to avoid resetting the Smart Bidding learning cycle."

**Step 7 — Pacing warning:** When `recommended_budget` exceeds `current_budget` by more than `PACING_WARNING_THRESHOLD (20%)`, `_budget_change_warning()` generates a pacing warning string: "Large single-step budget changes can temporarily disrupt pacing. Consider applying this in stages."

**Return dict keys:**

| Key | Type | Description |
|---|---|---|
| `constraint_type` | `ConstraintType` | Classification: BUDGET_CONSTRAINED, BID_CONSTRAINED, MIXED_CONSTRAINT, or NONE |
| `budget_confidence` | `ConfidenceLevel` | `"high"` / `"medium"` / `"low"` |
| `recommended_budget` | `float \| None` | Suggested daily budget in currency units, or None |
| `budget_rec_rationale` | `str \| None` | Human-readable explanation |
| `pacing_warning` | `str \| None` | Warning when change > 20%, or None |
| `learning_phase_warning` | `str \| None` | Warning when both bidding + budget change recommended, or None |
| `google_rec_confirmed` | `bool` | True when a native CAMPAIGN_BUDGET rec agrees with diagnosis |
| `blocking_issues` | `list[str]` | Issues surfaced to the user (stale data, Maximize Conversions IS guard, bid-constrained native rec conflict, mixed constraint hold) |
| `native_rec_amount` | `float \| None` | Google's suggested budget from native rec, or None |
| `move_unused_signal` | `bool` | True when MOVE_UNUSED_BUDGET native rec exists |

**What this service does NOT do:**
- It does not fetch any data from the Google Ads API.
- It does not hard-block any recommendation. All outputs are confidence-scored so callers can present them with appropriate context rather than suppressing them.
- It does NOT enforce a "daily budget ≥ 2× tCPA" rule as a hard gate (see the 2× tCPA note above).

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
            ├─ CONCURRENT for ENTIRE account:
            │   ├─ budget_bidding_advisor.analyse()           ← ALL campaigns, no campaign filter
            │   │    │
            │   │    ├─ CONCURRENT (asyncio.gather):
            │   │    │   ├─ CampaignMetricsAdapter.fetch_campaign_contexts()
            │   │    │   │    └─ GAQL: campaign + budget + bidding + IS metrics DURING LAST_30_DAYS
            │   │    │   │
            │   │    │   ├─ GoogleRecommendationsAdapter.fetch_multiple_types(_BIDDING_REC_TYPES)
            │   │    │   │    └─ 9 native bidding rec types via GAQL
            │   │    │   │
            │   │    │   └─ GoogleRecommendationsAdapter.fetch_multiple_types(_BUDGET_REC_TYPES)
            │   │    │        └─ 4 native budget rec types via GAQL
            │   │    │
            │   │    └─ Per campaign (iterative, synchronous):
            │   │         ├─ _score_bidding_recommendation()
            │   │         ├─ diagnose_budget()
            │   │         └─ Merge → BudgetBiddingRecommendation
            │   │
            │   └─ fetch_keyword_metrics(campaign_ids=mapping.keys())
            │
            ├─ Evaluator → group_by_campaign
            │
            └─ PARALLEL per campaign:
                 └─ _build_campaign_recommendation()
                      └─ CampaignRecommendation(
                            keywords=[...],
                            budget_bidding=[one BudgetBiddingRecommendation]
                          )

  └─ PARALLEL storage store() for all CampaignRecommendations
```

## Output Model

`BudgetBiddingRecommendation` (defined in `recommendations/models.py`):

| Field | Type | Description |
|---|---|---|
| `campaign_id` | `str` | Google Ads campaign ID |
| `campaign_name` | `str` | Campaign name |
| `scope` | `ScopeType` | `"CAMPAIGN"` or `"PORTFOLIO"` |
| `portfolio_strategy_id` | `Optional[str]` | Portfolio strategy ID when applicable |
| `portfolio_strategy_name` | `Optional[str]` | Portfolio strategy name when applicable |
| `current_strategy` | `str` | Current bidding strategy type |
| `bidding_rec_type` | `Optional[str]` | Recommended bidding change, or None |
| `bidding_rec_rationale` | `Optional[str]` | Bidding recommendation explanation |
| `bidding_confidence` | `ConfidenceLevel` | `"high"` / `"medium"` / `"low"` |
| `bidding_blocked_reason` | `Optional[str]` | Always None (never blocks) |
| `current_budget` | `float` | Current daily budget in currency units |
| `recommended_budget` | `Optional[float]` | Recommended daily budget, or None |
| `budget_rec_type` | `Optional[str]` | Always `"CAMPAIGN_BUDGET"` when budget recommended, else None |
| `budget_rec_rationale` | `Optional[str]` | Budget recommendation explanation |
| `budget_confidence` | `ConfidenceLevel` | `"high"` / `"medium"` / `"low"` |
| `apply_order` | `List[str]` | Order to apply changes (e.g. `["bidding", "budget"]`) |
| `learning_phase_warning` | `Optional[str]` | Warning when both changes recommended |
| `constraint_type` | `ConstraintType` | `BUDGET_CONSTRAINED`, `BID_CONSTRAINED`, `MIXED_CONSTRAINT`, or `NONE` |
| `google_rec_confirmed` | `bool` | True when a native rec aligns with our diagnosis |
| `blocking_issues` | `List[str]` | Issues surfaced to the user |
| `pacing_warning` | `Optional[str]` | Pacing warning when > 20% change |
| `move_unused_budget_signal` | `bool` | True when MOVE_UNUSED_BUDGET rec exists |
| `metric_freshness_warning` | `Optional[str]` | Staleness warning from CampaignMetricsAdapter |

## Campaign Context Schema (CampaignMetricsAdapter output)

Each dict returned by `fetch_campaign_contexts()` contains these normalised fields:

| Key | Type | Source |
|---|---|---|
| `campaign_id` | `str` | `campaign.id` |
| `campaign_name` | `str` | `campaign.name` |
| `campaign_status` | `str` | `campaign.status` |
| `bidding_strategy_type` | `str` | `campaign.bidding_strategy_type` |
| `target_cpa` | `float \| None` | Effective tCPA (campaign or portfolio) in currency units |
| `target_roas` | `float \| None` | Effective tROAS (campaign or portfolio) |
| `is_portfolio` | `bool` | True when `campaign.bidding_strategy` is set |
| `portfolio_strategy_resource` | `str` | Resource name when portfolio |
| `portfolio_strategy_id` | `str \| None` | `bidding_strategy.id` |
| `portfolio_strategy_name` | `str \| None` | `bidding_strategy.name` |
| `portfolio_strategy_type` | `str \| None` | `bidding_strategy.type` |
| `portfolio_strategy_status` | `str \| None` | `bidding_strategy.status` |
| `budget_amount` | `float` | `campaign_budget.amount_micros` / 1,000,000 |
| `budget_explicitly_shared` | `bool` | `campaign_budget.explicitly_shared` |
| `cost` | `float` | Sum of `metrics.cost_micros` / 1,000,000 over 30 days |
| `conversions` | `float` | Sum of `metrics.conversions` over 30 days |
| `conversions_value` | `float` | Sum of `metrics.conversions_value` over 30 days |
| `clicks` | `int` | Sum of `metrics.clicks` over 30 days |
| `search_impression_share` | `float \| None` | Latest `metrics.search_impression_share` |
| `budget_lost_impression_share` | `float \| None` | Latest `metrics.search_budget_lost_impression_share` |
| `rank_lost_impression_share` | `float \| None` | Latest `metrics.search_rank_lost_impression_share` |
| `metric_freshness` | `dict` | `{ is_fresh, age_days, warning }` from `_assess_metric_freshness()` |

### Impression Share Freshness Assessment

Google Ads impression share metrics have a 24–48 hour computation lag. The adapter assesses freshness by finding the most recent `segments.date` across all returned rows:

| Condition | `is_fresh` | `age_days` | `warning` |
|---|---|---|---|
| No segment date | `False` | `None` | "Impression share data age is unknown..." |
| Parse error | `False` | `None` | "Could not parse segment date." |
| ≤ 2 days | `True` | 0-2 | `None` |
| > 2 days | `False` | delta | "Impression share data is N day(s) old..." |

### Portfolio Campaign Detection

A campaign is classified as portfolio (`is_portfolio = True`) when `campaign.bidding_strategy` (the resource name field) is populated. Standard strategies embed the bidding scheme directly on the campaign and leave this field absent/empty.

When the campaign is on a portfolio strategy, the effective tCPA and tROAS are read from `bidding_strategy.*` sub-objects rather than from the campaign-level fields.

### GAQL Query Details

The adapter uses a single GAQL query with `segments.date DURING LAST_30_DAYS`, which returns one row per (campaign, date) pair. The adapter:
1. Groups rows by campaign ID
2. Sums `cost_micros`, `conversions`, `conversions_value`, and `clicks` across all dates (30-day aggregates)
3. Keeps the latest row's impression share values (non-aggregable — campaign-level figures regardless of date)
4. Attaches the freshness metadata to all rows

When `campaign_ids` filter is provided, a `WHERE campaign.id IN (...)` clause is appended to the base query.

## Calling Contexts

The budget advisor is called from four distinct paths (see `tools/optimization.py` and `engine.py`):

### Path 1: Batch Account Processing (`generate_recommendations`)

In `_process_account()` (batch path):
- `budget_bidding_advisor.analyse(campaign_ids=None)` — fetches ALL campaigns in the account
- Runs CONCURRENTLY with `fetch_keyword_metrics()`
- The budget recs are stored in a dict `{campaign_id: BudgetBiddingRecommendation}` and passed to per-campaign `_build_campaign_recommendation()`

### Path 2: Single-Campaign Force Refresh (`run_optimization_for_campaign`)

In `run_optimization_for_campaign()`:
- Iterates through accessible accounts, looking for the one containing the target campaign
- Calls `budget_bidding_advisor.analyse(campaign_ids=[campaign_id])` — fetches ONE campaign
- Also runs CONCURRENTLY with `fetch_keyword_metrics(campaign_ids=[campaign_id])`
- Returns the first matching account's recommendation

### Path 3: Stored Recommendations — No Refresh

The `optimization` tool checks `recommendation_storage_service` first. If a fresh `CampaignRecommendation` with `completed=False` exists, it is returned directly — no budget advisor call is made.

### Path 4: Stored Not Found → Trigger Refresh

If no stored recommendation exists but a product mapping IS found (checked via `fetch_campaign_mappings`), the tool calls `engine.run_optimization_for_campaign()`, which in turn calls the budget advisor.

## Error Handling

- **Data fetch failures:** `CampaignMetricsAdapter.fetch_campaign_contexts()` returns `[]` on any exception. The advisor logs the failure and skips the account — returns `[]` with no campaigns to process.
- **Native rec fetch failures:** Native recommendation calls use the adapter's built-in error handling. If a specific rec type fails, the `fetch_multiple_types` loop continues with remaining types. Missing native recs simply result in no native confirmation — the heuristic decision tree still runs.
- **Empty campaign contexts:** If `campaign_contexts` is empty or `None`, `analyse()` logs a warning and returns `[]`.
- **Missing native recs:** `_index_by_campaign()` produces an empty dict when no recs are returned. Both `_score_bidding_recommendation` and `diagnose_budget` handle empty lists gracefully — the heuristic logic still runs.
- **Per-campaign isolation in engine:** `_build_campaign_recommendation()` wraps its body in try/except. If one campaign's budget rec processing throws, it is logged and returns `None` — the error never propagates to kill other campaigns in the same account batch.
- **Freshness:** Stale impression share data only downgrades confidence — it never blocks recommendations from being generated.
- **Portfolio campaign edge case:** If a campaign uses a portfolio strategy but the bidding strategy details are unavailable (unlikely, but possible with API errors), `is_portfolio` is still True based on the resource name, but `portfolio_strategy_id` and related fields will be None. The recommendation still works — the portfolio note just won't include specific strategy details.

## Dependency Graph

```
budget_service.py (BudgetBiddingAdvisorService)
  ├── campaign_metrics.py (CampaignMetricsAdapter) — data collection layer
  │    └── google_ads_client — GAQL query execution
  ├── recommendations.py (GoogleRecommendationsAdapter) — native rec fetching
  │    └── google_ads_client — GAQL query execution
  ├── diagnosis_service.py (diagnose_budget) — pure logic, no API calls
  └── models.py (BudgetBiddingRecommendation, ConfidenceLevel, etc.) — data models

engine.py (GoogleRecommendationEngine)
  └── budget_service.py (budget_bidding_advisor) — orchestrator singleton
       └── Called once per account, concurrently with keyword metrics fetch

optimization.py
  └── engine.run_optimization_for_campaign() — single-campaign path
       └── Calls budget_bidding_advisor.analyse(campaign_ids=[...])
```

## LLM Calls

The Budget & Bidding advisor does NOT use any LLM calls. It is entirely deterministic — powered by conversion-volume-based decision trees and impression share analytics. This is a key architectural difference from the Keyword Recommendations advisor, which makes 2 LLM calls per campaign.
