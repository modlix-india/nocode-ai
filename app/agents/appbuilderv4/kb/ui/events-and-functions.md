# Events and Functions

## Event Functions

Stored in page `eventFunctions`. Triggered by component events (`onClick`, `onChange`, etc.) or page lifecycle (`onLoadEvent`).

### Structure

```json
{
  "eventFunctions": {
    "myEvent": {
      "name": "myEvent",
      "steps": {
        "stepName": {
          "statementName": "stepName",
          "name": "FunctionName",
          "namespace": "UIEngine",
          "parameterMap": {
            "paramName": {
              "p1": {"key": "p1", "type": "VALUE", "value": "someValue", "order": 1}
            }
          },
          "dependentStatements": {"Steps.prevStep.output": true}
        }
      }
    }
  }
}
```

### Parameter Reference Format

Inside `parameterMap`, each parameter has keyed entries:

```json
{"key": "p1", "type": "VALUE", "value": "literal", "order": 1}
{"key": "p1", "type": "EXPRESSION", "expression": "Page.counter + 1", "order": 1}
```

### Step Dependencies

Control execution order via `dependentStatements`:
- `"Steps.fetch.output": true` — run after fetch succeeds
- `"Steps.check.true": true` — run after If condition is true
- `"Steps.check.false": true` — run after If condition is false
- `"Steps.loop.iteration": true` — run each loop iteration

### Page Lifecycle

- `onLoadEvent`: event key to run when page loads
- `loadStrategy`: `"default"` (first load) | `"always"` (every visit) | `"once"` (per session)

## UIEngine Functions

All in namespace `UIEngine`. Used in event function steps.

### Navigation

| Function | Key Parameters |
|----------|---------------|
| `Navigate` | `linkPath` (page/URL), `target` (_self/_blank), `force`, `removeThisPageFromHistory` |
| `NavigateBack` | (none) |
| `NavigateForward` | (none) |
| `Refresh` | (none) |

### Data Operations

| Function | Key Parameters | Events |
|----------|---------------|--------|
| `FetchData` | `url`, `queryParams`, `pathParams`, `headers` | output (data), error (data, status) |
| `SendData` | `url`, `method` (POST/PUT/PATCH/DELETE), `payload`, `queryParams`, `pathParams`, `headers`, `downloadAsAFile`, `downloadFileName` | output, error |
| `DeleteData` | `url`, `queryParams`, `pathParams`, `headers` | output, error |

FetchData/SendData auto-include Authorization and clientCode headers.

### Store

| Function | Key Parameters |
|----------|---------------|
| `SetStore` | `path`, `value`, `deleteKey` |
| `GetStoreData` | `path` → output.data |

### Authentication

| Function | Key Parameters |
|----------|---------------|
| `Login` | `userName`, `password`, `otp`, `pin`, `rememberMe`, `cookie` |
| `Logout` | `ssoLogout` |

Login sets `Store.auth`, `LocalStore.AuthToken`. Logout clears them.

### UI

| Function | Key Parameters |
|----------|---------------|
| `Message` | `msg`, `type` (ERROR/WARNING/INFO/SUCCESS), `isGlobalScope` |
| `ScrollTo` | `vertical` (top/bottom/px), `horizontal`, `behaviour` (Instant/Smooth) |
| `ScrollToGrid` | `gridkey` (component key), `behaviour` |
| `CopyTextToClipboard` | `text` |
| `ShortUniqueId` | → output.id |
| `EncodeURIComponent` | `uriComponent` → output.encodedValue |
| `DecodeURIComponent` | `uriComponent` → output.decodedValue |

### Chaining Example

```json
{
  "steps": {
    "submit": {
      "name": "SendData", "namespace": "UIEngine",
      "parameterMap": {
        "url": {"p1": {"type": "VALUE", "value": "/api/submit"}},
        "method": {"p2": {"type": "VALUE", "value": "POST"}},
        "payload": {"p3": {"type": "EXPRESSION", "expression": "Page.formData"}}
      }
    },
    "showSuccess": {
      "name": "Message", "namespace": "UIEngine",
      "parameterMap": {
        "msg": {"p1": {"type": "VALUE", "value": "Saved!"}},
        "type": {"p2": {"type": "VALUE", "value": "SUCCESS"}}
      },
      "dependentStatements": {"Steps.submit.output": true}
    },
    "showError": {
      "name": "Message", "namespace": "UIEngine",
      "parameterMap": {
        "msg": {"p1": {"type": "EXPRESSION", "expression": "Steps.submit.error.data.message"}},
        "type": {"p2": {"type": "VALUE", "value": "ERROR"}}
      },
      "dependentStatements": {"Steps.submit.error": true}
    }
  }
}
```

### Using FetchData Result

```json
{
  "fetch": {
    "name": "FetchData", "namespace": "UIEngine",
    "parameterMap": {"url": {"p1": {"type": "VALUE", "value": "/api/data"}}}
  },
  "save": {
    "name": "SetStore", "namespace": "UIEngine",
    "parameterMap": {
      "path": {"p1": {"type": "VALUE", "value": "Page.data"}},
      "value": {"p2": {"type": "EXPRESSION", "expression": "Steps.fetch.output.data"}}
    },
    "dependentStatements": {"Steps.fetch.output": true}
  }
}
```

## KIRun System Functions

Used in event functions and custom KIRun function definitions.

### System

| Function | Namespace | Description |
|----------|-----------|-------------|
| `If` | System | Conditional: events `true`, `false`, `output`. Param: `condition` |
| `GenerateEvent` | System | Emit event with results. Params: `eventName`, `results` |
| `Print` | System | Debug output. Param: `values` (variadic) |
| `Wait` | System | Delay. Param: `millis` |

### System.Loop

| Function | Description | Key Params |
|----------|-------------|------------|
| `RangeLoop` | Loop from/to/step | `from`, `to`, `step` → iteration.index |
| `ForEachLoop` | Iterate array | `source` → iteration.each, iteration.index |
| `CountLoop` | Loop N times | `count` → iteration.index |
| `Break` | Exit loop | (none) |

### System.Context

| Function | Description | Key Params |
|----------|-------------|------------|
| `Create` | Create variable | `name`, `schema` |
| `Set` | Set variable | `name` (Context.x), `value` |
| `Get` | Get variable | `name` → output.value |

### System.Array (key functions)

`InsertLast`(source, element), `AddFirst`, `Delete`(source, index), `Sort`(source, ascending), `IndexOf`(source, element), `Concatenate`(source1, source2), `SubArray`(source, from, length), `Reverse`, `RemoveDuplicates`, `Join`(source, delimiter)

### System.String (key functions)

`Trim`, `LowerCase`, `UpperCase`, `Contains`(source, search), `StartsWith`, `EndsWith`, `Replace`(source, search, replace), `Split`(source, delimiter), `Concatenate`(values variadic), `SubString`(source, from, to), `Length`, `IndexOf`

### System.Math (key functions)

`Add`(values variadic), `Absolute`, `Ceiling`, `Floor`, `Round`, `Power`(value, power), `Maximum`(values), `Minimum`(values), `Random`, `RandomInt`(min, max)

### System.Date (key functions)

`GetCurrentTimestamp`, `EpochMillisecondsToTimestamp`, `TimestampToEpochMilliseconds`, `GetDay/Month/Year/Hours/Minutes`, `AddTime`, `Difference`, `IsBefore/IsAfter`, `ToDateString`, `FromDateString`

### System.Object

`ObjectKeys`(source), `ObjectValues`, `ObjectEntries`, `ObjectDeleteKey`(source, key), `ObjectPutValue`(source, key, value)

### System.JSON

`JSONParse`(source) → output.value, `JSONStringify`(source) → output.value

## Custom KIRun Function Definitions

Reusable functions stored in MongoDB. Can be called from event functions or URIPath endpoints.

```json
{
  "definition": {
    "name": "myFunction",
    "namespace": "MyApp",
    "parameters": {
      "input": {"parameterName": "input", "schema": {"type": "STRING"}}
    },
    "events": {
      "output": {"name": "output", "parameters": {"result": {"type": "STRING"}}}
    },
    "steps": { ... }
  }
}
```

Expression context in functions: `Arguments.x` (input params), `Steps.x` (step outputs), `Context.x` (local variables)
