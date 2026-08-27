# List-endpoint filters: Mongo is substring, JOOQ is exact

Every `GET` list endpoint in nocode-saas turns its query parameters into a
`FilterCondition` AND'd over the named field. There is no `?filter=`, `?q=` or
`?search=` convention: **the field NAME is the query-param key**, and the
operator is inferred from the value shape.

| Value shape | Operator | Behaviour |
|---|---|---|
| Single string value | `STRING_LOOSE_EQUAL` | `LIKE '%value%'`, substring |
| Same key repeated | `IN` | `field IN (v1, v2, ...)` |
| Empty / missing | `EQUALS ''` | matches only exactly-empty, rarely useful |

## The asymmetry that bites

There are two implementations of that convention and they differ on the single
-value case.

**Mongo stack** (`AbstractOverridableDataService.paramToConditionLRO`), used by
every list endpoint on ui-service and core-service: pages, functions, schemas,
storages, themes, templates, connections, event-actions, event-defs, uri-paths,
transports.

- Single string value is `STRING_LOOSE_EQUAL`, **substring**.
- `appCode` is added explicitly as `EQUALS`, not loose. If it is missing the
  condition becomes `appCode = null`, which matches NOTHING. **Every ui/core
  list call needs `?appCode=<X>`** or you get an empty result rather than the
  full set.
- `clientCode` resolves through the inheritance service to client + parents.
- Ignored before the generic loop: `clientCode, appCode, size, page, sort, eager`.

**JOOQ stack** (`ConditionUtil.parameterMapToMap`, near-identical copies in
`commons` and `commons2`), used by `/api/security/*`.

- Single value is `EQUALS`, **exact**. Different from Mongo.
- No default-ignored fields; `commons2` does not even accept an ignore list.
- No special handling for `appCode` / `clientCode`; they are ordinary `EQUALS`.

So `?name=foo` against a Mongo list returns every row containing "foo", while
`?name=foo` against the security application list returns only rows named
exactly `"foo"`.

A 2026-05-21 experiment aligned JOOQ to the Mongo convention and was reverted:
the JOOQ controllers serve more than search endpoints, and changing the default
operator for all of them was too broad.

## Substring search against a JOOQ endpoint: use POST /query

The `/query` variant takes explicit per-condition operators, so build an OR of
`STRING_LOOSE_EQUAL` conditions rather than trying to coax the GET params.

**Always pass `?? ''` for the search term.** `STRING_LOOSE_EQUAL` has no null
guard on the JOOQ path (`field.like("%" + value + "%")` in `AbstractDAO`), so a
null value becomes the literal `%null%` and matches rows containing "null",
not everything. An empty string gives `LIKE '%%'`, which matches all, and that
is the correct "nothing typed yet" behaviour.

Verified 2026-07-16 on `/api/security/users/query` (userName + emailId) and
`/api/security/clients/query` (name + code).

## FilterConditionOperator

`EQUALS`, `NOT_EQUALS`, `STRING_LOOSE_EQUAL`, `IN`, `NOT_IN`, `LESS_THAN`,
`LESS_THAN_EQUAL`, `GREATER_THAN`, `GREATER_THAN_EQUAL`, `IS_NULL`,
`IS_NOT_NULL`, `BETWEEN`, `NOT_BETWEEN`, `LIKE`, `NOT_LIKE`.

`LIKE` is raw SQL LIKE, caller controls the wildcards.

## Finding an app

App lookups accept `appName` and `appCode` as separate fields. Filtering an app
list by a display name will not match the code, and vice versa.
