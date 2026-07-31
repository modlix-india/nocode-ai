# Application and Page Structure

## Application Definition

Fetched via `GET /api/ui/application`. Key fields:

```typescript
{
  _id: string,           // MongoDB ID (used by UI tools)
  name: string,          // App name
  appCode: string,       // Unique code
  clientCode: string,    // Client code
  properties: {
    title?: string,
    defaultPage?: string,    // Home page name
    shellPage?: string,      // Shell wrapper page
    loginPage?: string,
    forbiddenPage?: string,
    notFoundPage?: string,
    signUp?: string,
    forgotPasswordPage?: string,
    termsConditionPage?: string,
    privacyPolicyPage?: string,
    defaultLanguage?: string,

    // Head injection
    links?: { [key: string]: { rel, href, type? } },
    scripts?: { [key: string]: { src, async?, defer? } },
    metas?: { [key: string]: { name?, content } },

    // Font and icon packs
    fontPacks?: { [key: string]: { name, code, order? } },
    iconPacks?: { [key: string]: { name } },  // e.g. "FREE_FONT_AWESOME_ALL"

    // Theme and style references
    themes?: { [key: string]: { name: string } },
    styles?: { [key: string]: { name: string } },

    // Global variables
    fillerValues?: { [key: string]: any },
  }
}
```

Shell page wraps all content pages (nav, header, footer). Pages with `wrapShell: true` (default) load inside the shell.

## Page Definition

Fetched via `GET /api/ui/page/{pageName}`. Key fields:

```typescript
{
  _id: string,
  name: string,          // Page name (used in URLs)
  appCode: string,
  clientCode: string,
  rootComponent: string, // Key into componentDefinition (STRING, not object)

  componentDefinition: {
    [key: string]: {
      key: string,
      name?: string,
      type: string,        // Component type (Grid, Button, etc.)
      properties?: { [propName: string]: ComponentProperty },
      styleProperties?: ComponentStyle,
      children?: { [childKey: string]: boolean },
      displayOrder?: number,
      bindingPath?: DataLocation,
      validations?: Array<Validation>,
    }
  },

  eventFunctions: {
    [key: string]: FunctionDefinition  // KIRun functions
  },

  properties: {
    title?: { name?: ComponentProperty, append?: ComponentProperty },
    onLoadEvent?: string,         // Event key to run on load
    loadStrategy?: "default" | "always" | "once",
    wrapShell?: boolean,
    storeInitialization?: { [path: string]: any },
    seo?: { description?, keywords?, robots?, author? },
    classes?: { [key: string]: StyleClassDefinition },
  },

  translations?: { [lang: string]: { [key: string]: string } },
}
```

## ComponentProperty

```typescript
interface ComponentProperty<T> {
  value?: T,                // Static value
  location?: DataLocation,  // Dynamic value
}

interface DataLocation {
  type: "VALUE" | "EXPRESSION",
  value?: string,       // Store path for VALUE type
  expression?: string,  // Expression for EXPRESSION type
}
```

Resolution priority: `location` (if resolves) → `value` (fallback)

Expressions can reference: `Store.x`, `Page.x`, `Url.x`, `Theme.x`, `Filler.x`, `Parent.x`, `LocalStore.x`

## Data Binding

Components can have `bindingPath` through `bindingPath10` (DataLocation objects) providing data context.

**ArrayRepeater** uses `bindingPath` to iterate arrays. Child components access current item via `Parent.` prefix:

```json
{
  "type": "ArrayRepeater",
  "bindingPath": {"type": "VALUE", "value": "Store.items"},
  "children": {"itemCard": true}
}
```

Inside `itemCard`, use `Parent.name`, `Parent.price` etc. to access item fields.

## Children Ordering

Children are sorted by: 1) `displayOrder` (ascending), 2) `key` (alphabetical).

## Schema Definitions

Used for function parameters, data validation, and form generation:

```json
{
  "definition": {
    "name": "Product", "namespace": "Store",
    "type": ["OBJECT"],
    "properties": {
      "name": {"type": "STRING", "minLength": 1},
      "price": {"type": "DOUBLE", "minimum": 0}
    },
    "required": ["name", "price"]
  }
}
```

Types: `INTEGER`, `LONG`, `FLOAT`, `DOUBLE`, `STRING`, `OBJECT`, `ARRAY`, `BOOLEAN`, `NULL`
String formats: `DATETIME`, `TIME`, `DATE`, `EMAIL`, `REGEX`
