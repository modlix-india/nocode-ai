# FetchData turns 204 into an empty STRING

`UIEngine.FetchData` sets `output.data = axios response.data` (`FetchData.ts:102`).
For an HTTP **204 No Content** the body is empty, so `response.data` is `""`,
an empty **string**, not null and not undefined.

## Why that breaks a form

If a component then binds to `Page.x.someField` where `Page.x` was SetStore'd to
that `""`, the client TokenValueExtractor treats the string as an array/string
and tries to use `someField` as an index. `parseInt("someField")` is NaN, so it
throws `ExpressionEvaluationException` **"<field> is not a number"**
(`TokenValueExtractor.ts` ~L300, `handleArrayAccess`).

You get **one error per bound field**. A seven-field billing form reports
"Error 1 of 7".

## `?? {}` does not rescue it

An empty string is not nullish, so the fallback never fires. A ternary or
object-literal guard in the UI is also fragile: the expression engine evaluates
both ternary branches and chokes on object literals
(`platform_doc_read('expression_gotchas')`).

## The fix belongs on the server

**An endpoint that feeds an object binding must return 200 with an object (a
blank DTO or `{}`), never 204.**

```java
// wrong
.defaultIfEmpty(ResponseEntity.noContent().build())

// right
.defaultIfEmpty(new Dto()).map(ResponseEntity::ok)
```

Seen 2026-07-23 on security `GET /api/security/billing-profile` for a buyer
whose client had no saved profile. It crashed the sitezump buyTokens checkout
popup with "legalName is not a number" seven times over. Root-caused with
`drive_page`, because the crash fired on popup open rather than page load.

## Related

- `platform_doc_read('expression_gotchas')` for why `??` cannot patch over this.
