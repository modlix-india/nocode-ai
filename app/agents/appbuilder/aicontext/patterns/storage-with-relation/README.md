# storage-with-relation

Storage that declares a TO_MANY relation to another storage.

**Notes:**

Look at: the `relations` object on the root with a named key (e.g. `category`) pointing to another storage via `storageName`; `relationType: "TO_MANY"` with `deleteConstraint`/`updateConstraint` typically `"NOTHING"`; `fieldName: "_id"` as the join field. Prod uses `relationType='TO_MANY'` only.

**Entity type:** `storage`

## Samples

- **marketingai** / `blogs` (v8, clientCode=SYSTEM)
  - [marketingai.blogs.json](marketingai.blogs.json)
