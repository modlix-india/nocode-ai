# server-bulk-update

Update many rows in one call (e.g. mark-all-read).

**Notes:**

Look at: `CoreServices.Storage.ReadPage` step with a `filter` bound to a `Context`-built filter object (e.g. `Context.multiFilter`, `Context.bookingFilter`); a `System.Loop.ForEachLoop` iterating over `Steps.readPage*.output.result.content`; per-iteration `CoreServices.Storage.Update` steps writing back the mutated `Context.tempObj` / `Context.schemaObj` by `_id`.

**Entity type:** `server_function`

## Samples

- **rim** / `rim.updateAllProjectDocuments` (v36, clientCode=SYSTEM)
  - [rim.rim.updateAllProjectDocuments.json](rim.rim.updateAllProjectDocuments.json)
