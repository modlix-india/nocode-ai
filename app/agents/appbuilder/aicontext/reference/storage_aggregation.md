---
name: storage-aggregation
description: "Grouped reads, batch update, and version history on storages: CoreServices.Storage.Aggregate / UpdateMany / GetVersionDetails, plus deleteVersion on delete. Added 2026-10-02."
---

# Grouped reads, batch update and version history on storages

Four capabilities that did not exist before 2026-10-02. If you were trained on
older pages you will not have seen them, and you will reach for the workaround
(read every row and fold it in the page) instead.

## Aggregate: grouped reads

`CoreServices.Storage.Aggregate` does filter, group, measure, filter again, page
in ONE server call. Before this, "revenue by month" meant reading every row into
the page and folding it in Kirun.

Results come back **flat**: one object per group, group keys and measures side by
side at the top level, never a nested `_id`. That is deliberate, and it is what
lets a Chart bind the result directly with no reshaping step.

```
agg: CoreServices.Storage.Aggregate(
        storageName = "sales",
        filter = {"field": "status", "operator": "EQUALS", "value": "PAID"},
        groupBy = [{"field": "region"}],
        aggregations = [{"function": "SUM", "field": "amount", "alias": "revenue"},
                        {"function": "COUNT", "alias": "orders"}],
        having = {"field": "revenue", "operator": "GREATER_THAN", "value": 10000},
        sort = [{"property": "region", "direction": "ASC"}])
```

Returns `{content: [{region, revenue, orders}, ...], page: {...}, total: n}`, so
bind a Chart with `data = Steps.agg.output.result.content` and
`xAxisDataSetPath = ["Data.region"]`, `yAxisDataSetPath = ["Data.revenue"]`.

- `function` is one of MAX, MIN, SUM, AVG, COUNT.
- `alias` must match `[A-Za-z_][A-Za-z0-9_]*`; it becomes an output key.
- COUNT with no `field` counts rows; COUNT with a `field` counts rows where that
  field is present and non-null, matching SQL `COUNT(col)`.
- `having` filters the ALIASES after grouping, and takes an ordinary condition.
  Do not pass a HavingCondition shape; it is rejected.
- `sort` may only name a group-key or measure alias. Anything else is a 400.
- Empty `groupBy` collapses the whole collection to a single row.
- SUM and AVG silently skip non-numeric values, so a column of numeric STRINGS
  aggregates to zero with no error. Store numbers as numbers.

## Date bucketing: numbers only, and the timezone matters

Grouping on a raw timestamp gives one group per row. Bucket it instead:

```
groupBy = [{"field": "orderDate", "alias": "month", "bucket": "MONTH",
            "encoding": "EPOCH_SECONDS", "timezone": "Asia/Kolkata"}]
```

`bucket` is YEAR, QUARTER, MONTH, WEEK, DAY or HOUR.

**`encoding` is required with `bucket` and must be declared, not guessed.** App
data dates are NOT BSON dates; they are plain numbers, because the write path
maps JSON primitives straight through. The Calendar component's `storageFormat`
writes `'x'` as epoch MILLISECONDS and `'X'` as epoch SECONDS, and the storage
schema calls both LONG, so nothing downstream can tell them apart. Declaring the
wrong one is wrong by a factor of 1000 and buckets into the year 56000 without
erroring. A server-side guard samples one row and rejects an obvious mismatch,
but it only catches a systematic error, not a collection with mixed encodings.

**Only number-typed fields can be bucketed.** A STRING date is rejected. That is
deliberate: strings are written through local-time getters, so they are already
wall-clock in the writer's timezone and need no conversion, which inverts the
timezone semantics rather than merely changing the parse.

`timezone` is an IANA zone, defaulting to UTC, and it is not a detail. An order
at 2026-10-01 03:00 IST is 2026-09-30 21:30 UTC. Bucketed by MONTH in UTC it
files under September; in Asia/Kolkata it files under October. Nothing errors
either way, the chart renders, and the totals are simply wrong. For an Indian
tenant, every day's 00:00 to 05:30 lands in the previous day unless you pass the
zone.

The bucket key comes back in the SAME encoding the field was stored in, so the
date formatter that renders the raw field renders the bucket too.

## UpdateMany

`CoreServices.Storage.UpdateMany(storageName, dataArray, override)` updates many
rows in one call. Each entry must carry its own `_id`. Gated on the storage's
`updateAuth`, the same bar a single update passes.

## Version history from a page

`CoreServices.Storage.GetVersionDetails(storageName, objectId, includeObject)`
returns one row's version/audit trail. Before this, history was reachable over
REST but not from a page, so an app could not render its own audit trail.

- Scoped to ONE `objectId`. There is still no cross-row version query, so
  "edits per user last month" remains unanswerable.
- `includeObject = false` drops the row snapshot and leaves
  `operation`, `message`, `createdBy`, `createdAt`, `objectId`. An audit view
  rarely needs the payload and the snapshot is most of the bytes.
- A version row exists only when the storage has `isAudited` or `isVersioned`.
  With `isAudited` alone there is no `object` snapshot at all, only who/when/what.

## deleteVersion on delete

`CoreServices.Storage.Delete` and `DeleteByFilter` take `deleteVersion`:

- **false (the default)** writes a DELETE version row capturing the final state
  before the row goes. Previously a delete left no audit trace whatsoever and
  orphaned the row's existing history.
- **true** purges that row's version history instead.

Only affects storages that already opted into `isAudited` or `isVersioned`.

## Per-operation auth: the formats, and what unset means

`createAuth` / `readAuth` / `updateAuth` / `deleteAuth` take an **expression**,
not a single token. Every shape below is in live use in dev:

| Shape | Example |
|---|---|
| Bare permission | `Authorities.User_READ`, `Authorities.Booking_read` |
| Global role | `Authorities.ROLE_Owner`, `Authorities.ROLE_Application_CREATE` |
| App-scoped role | `Authorities.CXAPP.ROLE_Super_Admin`, `Authorities.SHOPKEEP.ROLE_Shopkeep_Admin` |
| App-scoped profile | `Authorities.LEADZUMP.PROFILE_LeadZump_Admin` |
| Any signed-in user | `Authorities.Logged_IN` |

They combine into a **boolean expression** using the `and` / `or` keywords, with
brackets for grouping:

- `Authorities.TASKMATE.ROLE_Admin or Authorities.TASKMATE.ROLE_Manager`
- `Authorities.SHOPKEEP.ROLE_Shopkeep_Admin or Authorities.SHOPKEEP.ROLE_Shopkeep_Manager or Authorities.SHOPKEEP.ROLE_Shopkeep_Cashier`
- row-level, mixing a role with a comparison against the row being read —
  `Authorities.TASKMATE.ROLE_Admin or (Authorities.TASKMATE.ROLE_Manager and Context.user.id = Row.ownerId)`

**A comma-separated list is NOT valid.** `A,B,C` is not an expression the
evaluator understands, so it cannot produce the boolean `true` that
`hasAuthority` requires: it denies everyone rather than granting any of the
three. Four storage definitions in dev are written this way and are effectively
locked. Write `A or B or C` instead.

The app-scoped prefix is the APPCODE uppercased. Prefer an app-scoped role over a
bare permission when the storage belongs to one app, so the grant cannot leak
across apps.

**Unset means open.** The runtime returns true for a null or blank authority, and
168 of the 219 storage definitions in dev leave them unset, so unset is the
majority configuration rather than an edge case. That is a legitimate choice for
reference data, but it is still a choice: **if you leave a storage's auth unset,
say so to the user in plain words, that the storage is open to all.** Do not set
one by reflex either. `Authorities.Logged_IN` on a storage a page reads makes the
page 403 for a signed-out visitor, and through the function-execute route that
surfaces as 403 rather than the 401 the direct REST route gives, which reads like
a bug and is not one.
