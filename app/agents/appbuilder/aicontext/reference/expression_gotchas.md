# Expression engine gotchas

The KIRun client expression engine is NOT JavaScript. These are the traps that
produce a blank page or a React error boundary rather than a clear error.
Read alongside `platform_doc_read('component_layout')` for where expressions go.

## Ternary evaluates BOTH branches

`cond ? a : b` does not short-circuit. The engine evaluates the operands of the
true branch AND the false branch, then picks. So:

```
(Store.wallet.threshold > 0) ? (Store.wallet.threshold * 10) : 0
```

throws `* cannot be applied to a null value` when `threshold` is null, even
though the condition is false. An uncaught expression error in a bound property
crashes the whole page through the React error boundary ("Something went wrong").

**Rule: guard EVERY arithmetic operand with `?? 0` and every string operand with
`?? ''`, in both branches, regardless of which branch can actually run.**

The same hazard bites ArrayRepeater card templates that serve two item shapes
(e.g. a FIXED bundle has a null `pricePerToken`, a CUSTOM one has a null
`price`). Guard both.

## `??` re-evaluates its result. Never `?? '-'`

`ExpressionEvaluator.applyBinaryOperation` re-evaluates the result of a `??`
when the result is a string that `looksLikeExpression`: it matches
`/[+\-*/%=<>!&|?:]/` (any of those operator characters) OR contains a scope
prefix (`Store.` / `Page.` / `Parent.` / `Steps.` / `Context.` / `Arguments.`).

Only `??` does this. `+`, `=`, ternary and the rest do not re-evaluate their
results, so `'Tokens: ' + x` is always safe.

Consequences:

- `Parent.paymentMethod ?? '-'` on a null row yields `'-'`, which looks like an
  expression, gets re-parsed as the operator `-`, and throws
  "Unexpected token ... found EOF".
- `Parent.reason ?? '-'` crashes even on NON-null rows, because a value like
  `"Token purchase INV/2026-27/1/3"` contains `/` and `-`.

The source `ExpressionEvaluator.ts` wraps the re-eval in try/catch, but the
`@fincity/kirun-js` build the UI actually bundles does not, so the parse error
escapes. In a table cell that takes down the entire Table.

Rules:

- Prefer a **single-path** expression (`Parent.field`). A bare path is resolved
  by token extraction, never goes through `applyBinaryOperation`, and is never
  re-evaluated. This is why stock `Parent.status` cells are safe even when the
  value contains `/` or `-`.
- Never use `?? '-'` or any fallback containing operator characters. A plain
  word is fine: `?? 'Pending'`, `?? 'PAID'`.
- `?? ''` is safe. The guard skips zero-length results.

## Operators

Supported: `=` `!=` `<` `<=` `>` `>=` `and` `or` `not` `? :` `??`
`+` `-` `*` `/` `%` `//` `[...]` `..`

- Equality is `=`, single equals. `==` throws "Extra operator undefined found"
  because the lexer reads two `=`. Verified with a kirun-js unit test.
- `not` **does work**. `Operation.UNARY_LOGICAL_NOT` is declared in
  `Operation.ts`, parsed in `ExpressionParser.ts`, evaluated by
  `LogicalNotOperator`. leadzump `dealProfile` gates its whole desktop tabs
  block on `not Store.devices.MOBILE_LANDSCAPE_SCREEN_SMALL` in production.
  Do not restructure data to avoid negation.
- `||` and `&&` do **not** work. `Page.x.length || 0` errors with
  "Extra operator undefined found". Use `??` for fallback, `or` / `and` for
  boolean logic.
- Array literal `[]` is rejected by the RUNTIME parser ("Unexpected token ...
  found LEFT_BRACKET"). **Trap:** `save_function_from_text` compiles
  `Page.x.content ?? []` without error because the save-time compiler is
  lenient, then it blows up at execution. To guard a null array source, gate the
  step behind a `System.If` instead of defaulting in the expression:
  `System.If(condition = not (Page.x.totalElements = 0))`.
- There is no `.map()` / `.filter()` / any method call. Compute in a function
  step and bind to the result.

## Binding shape: the key is `expression`, not `value`

```json
{ "text": { "location": { "type": "EXPRESSION", "expression": "Parent.name" } } }
```

`{"location": {"type": "EXPRESSION", "value": "..."}}` is stored verbatim and
the renderer ignores it, giving a blank cell. The write helpers normalize this
(`normalize_location` in `_conventions.py`), but if you compose raw page JSON
yourself, use `expression`.

## Scopes

Only four root scopes exist: `Store`, `LocalStore`, `Page`, `Parent`. There is
no `Local`. Inside an ArrayRepeater or a Table row, row data is `Parent.<field>`.

## `UIEngine.ExecuteJSFunction` needs `unsafe-eval` in the CSP

It uses `new Function(...)`, so the app's CSP `script-src` (or the `default-src`
fallback) must include `'unsafe-eval'`. Without it the enforced CSP blocks the
eval, ExecuteJSFunction catches the throw and raises its error event, the
function **silently no-ops**, and anything it was supposed to define (a
`window.*` global, say) never exists.

CSP lives in the UI app definition at `properties.csp`, a map of camelCase
directives, set via `update_app`. The CDN host is auto-appended.
`screenshot_page(capture_console=true)` surfaces both the eval violation and any
other CSP block.

## Debugging

Append `?debug=true` to the page URL to surface expression parse errors instead
of a bare error boundary.
