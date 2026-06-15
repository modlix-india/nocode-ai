# schemaform-driven

Auto-rendered form from a Storage's schema + fieldDefinitionMap.

**Notes:**

⚠️ Known issue: the current sample (`cxapp.testPage`) shows a SchemaForm with hardcoded inline data rather than a true schema-driven form. Until a better sample is captured, use this recipe's *intent* as a guide and consult the platform's storage docs + the SchemaForm component schema (`get_component_schema(type='SchemaForm')`) for the actual binding shape. The canonical pattern wires SchemaForm's `bindingPath` to a storage document and resolves field rendering through that storage's `fieldDefinitionMap`.

**Entity type:** `page`

## Samples

- **cxapp** / `testPage` (v103, clientCode=SYSTEM)
  - [cxapp.testPage.json](cxapp.testPage.json)
  - [cxapp.testPage.tree.txt](cxapp.testPage.tree.txt)
  - [cxapp.testPage.event.onload.dsl](cxapp.testPage.event.onload.dsl)
