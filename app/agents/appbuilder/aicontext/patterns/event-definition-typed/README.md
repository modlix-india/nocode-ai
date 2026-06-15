# event-definition-typed

Named event with payload schema.

**Notes:**

Look at: top-level `schema` block declaring the payload type (`SchemaType.STRING` or `OBJECT`); `name` as the event identifier; `message` referencing the originating transport id.

**Entity type:** `event_definition`

## Samples

- **landingpages** / `NEW_EVENT_DEF` (v6, clientCode=SYSTEM)
  - [landingpages.NEW_EVENT_DEF.json](landingpages.NEW_EVENT_DEF.json)
- **cxapp** / `BULK_DEMAND_LETTER_EVENT` (v13, clientCode=SYSTEM)
  - [cxapp.BULK_DEMAND_LETTER_EVENT.json](cxapp.BULK_DEMAND_LETTER_EVENT.json)
