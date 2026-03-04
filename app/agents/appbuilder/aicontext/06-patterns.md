# State Management and Patterns

## Store Prefixes

| Prefix | Scope | Example | Resolves To |
|--------|-------|---------|-------------|
| `Store.` | Global app data | `Store.auth`, `Store.user.name` | Direct store path |
| `Page.` | Page-scoped data | `Page.formData`, `Page.counter` | `Store.pageData.{pageName}.x` |
| `Url.` | URL parameters | `Url.pageName`, `Url.queryParameters.id` | `Store.urlData.{pageName}.x` |
| `Theme.` | Theme variables | `Theme.primaryColor` | Theme value for current breakpoint |
| `Filler.` | App global vars | `Filler.companyName` | `Store.application.properties.fillerValues.x` |
| `Parent.` | Parent context | `Parent.name`, `Parent.id` | Current item in ArrayRepeater |
| `LocalStore.` | localStorage | `LocalStore.AuthToken` | Browser localStorage |

## Filler Values

Application-wide variables defined in `application.properties.fillerValues`:

```json
{
  "fillerValues": {
    "companyName": "Acme Corp",
    "apiPrefix": "/api/v1",
    "features": {"darkMode": true}
  }
}
```

Used in expressions: `Filler.companyName`, `Filler.apiPrefix + '/users'`

## URIPath Definitions

Custom API endpoints routing to KIRun functions:

```json
{
  "pathString": "/products/{id}",
  "pathDefinitions": {
    "GET": {
      "uriType": "KIRUN_FUNCTION",
      "kiRunFxDefinition": {
        "name": "getProduct", "namespace": "ProductService",
        "pathParamMapping": {"id": "productId"}
      }
    },
    "POST": {
      "uriType": "KIRUN_FUNCTION",
      "kiRunFxDefinition": {
        "name": "createProduct", "namespace": "ProductService",
        "bodyMapping": "productData"
      }
    }
  }
}
```

Handler types: `KIRUN_FUNCTION`, `PAGE`, `REDIRECT`

## Common Patterns

### Pattern: Page with Button Click

```json
{
  "rootComponent": "root",
  "componentDefinition": {
    "root": {
      "key": "root", "type": "Grid",
      "properties": {"layout": {"value": "SINGLECOLUMNLAYOUT"}},
      "children": {"heading": true, "btn": true}
    },
    "heading": {
      "key": "heading", "type": "Text",
      "properties": {
        "text": {"value": "Welcome"},
        "textContainer": {"value": "H1"}
      }
    },
    "btn": {
      "key": "btn", "type": "Button",
      "properties": {
        "label": {"value": "Get Started"},
        "onClick": {"value": "goToDashboard"}
      }
    }
  },
  "eventFunctions": {
    "goToDashboard": {
      "name": "goToDashboard",
      "steps": {
        "nav": {
          "statementName": "nav",
          "name": "Navigate", "namespace": "UIEngine",
          "parameterMap": {
            "linkPath": {"p1": {"key": "p1", "type": "VALUE", "value": "/page/dashboard", "order": 1}}
          }
        }
      }
    }
  }
}
```

### Pattern: Form with Validation

Use `Form` container + `TextBox` fields with `bindingPath` + `validation`.

```json
{
  "form": {
    "key": "form", "type": "Form",
    "properties": {"onSubmit": {"value": "handleSubmit"}},
    "children": {"email": true, "password": true, "submitBtn": true}
  },
  "email": {
    "key": "email", "type": "TextBox",
    "properties": {
      "label": {"value": "Email"},
      "bindingPath": {"value": "Page.form.email"},
      "validation": [
        {"type": "MANDATORY", "message": "Email required"},
        {"type": "EMAIL", "message": "Invalid email"}
      ]
    }
  },
  "password": {
    "key": "password", "type": "TextBox",
    "properties": {
      "label": {"value": "Password"},
      "isPassword": {"value": true},
      "bindingPath": {"value": "Page.form.password"},
      "validation": [{"type": "MANDATORY", "message": "Password required"}]
    }
  },
  "submitBtn": {
    "key": "submitBtn", "type": "Button",
    "properties": {"label": {"value": "Login"}}
  }
}
```

### Pattern: ArrayRepeater List

```json
{
  "repeater": {
    "key": "repeater", "type": "ArrayRepeater",
    "properties": {"bindingPath": {"value": "Page.items"}},
    "children": {"card": true}
  },
  "card": {
    "key": "card", "type": "Grid",
    "children": {"itemName": true, "itemPrice": true}
  },
  "itemName": {
    "key": "itemName", "type": "Text",
    "properties": {
      "text": {"location": {"type": "EXPRESSION", "expression": "Parent.name"}}
    }
  },
  "itemPrice": {
    "key": "itemPrice", "type": "Text",
    "properties": {
      "text": {"location": {"type": "EXPRESSION", "expression": "'$' + Parent.price"}}
    }
  }
}
```

### Pattern: Conditional Visibility

Show/hide components based on state:

```json
{
  "adminMenu": {
    "key": "adminMenu", "type": "Menu",
    "properties": {
      "visibility": {"location": {"type": "EXPRESSION", "expression": "Store.auth.isAdmin"}}
    }
  },
  "loginBtn": {
    "key": "loginBtn", "type": "Button",
    "properties": {
      "visibility": {"location": {"type": "EXPRESSION", "expression": "!Store.auth.isAuthenticated"}},
      "label": {"value": "Login"},
      "onClick": {"value": "goToLogin"}
    }
  }
}
```

### Pattern: Responsive Layout

```json
{
  "container": {
    "key": "container", "type": "Grid",
    "styleProperties": {
      "s1": {
        "resolutions": {
          "ALL": {
            "display": {"value": "flex"},
            "flexDirection": {"value": "column"}
          },
          "DESKTOP_SCREEN": {
            "flexDirection": {"value": "row"},
            "gap": {"value": "20px"}
          }
        }
      }
    },
    "children": {"sidebar": true, "content": true}
  },
  "sidebar": {
    "key": "sidebar", "type": "Grid",
    "styleProperties": {
      "s1": {
        "resolutions": {
          "ALL": {"width": {"value": "100%"}},
          "DESKTOP_SCREEN": {"width": {"value": "250px"}, "flexShrink": {"value": "0"}}
        }
      }
    }
  }
}
```

### Pattern: Data Fetch on Page Load

```json
{
  "properties": {
    "onLoadEvent": "loadData",
    "loadStrategy": "always",
    "storeInitialization": {"Page.users": [], "Page.loading": true}
  },
  "eventFunctions": {
    "loadData": {
      "name": "loadData",
      "steps": {
        "fetch": {
          "statementName": "fetch",
          "name": "FetchData", "namespace": "UIEngine",
          "parameterMap": {
            "url": {"p1": {"key": "p1", "type": "VALUE", "value": "/api/users", "order": 1}}
          }
        },
        "saveData": {
          "statementName": "saveData",
          "name": "SetStore", "namespace": "UIEngine",
          "parameterMap": {
            "path": {"p1": {"key": "p1", "type": "VALUE", "value": "Page.users", "order": 1}},
            "value": {"p2": {"key": "p2", "type": "EXPRESSION", "expression": "Steps.fetch.output.data", "order": 1}}
          },
          "dependentStatements": {"Steps.fetch.output": true}
        },
        "doneLoading": {
          "statementName": "doneLoading",
          "name": "SetStore", "namespace": "UIEngine",
          "parameterMap": {
            "path": {"p1": {"key": "p1", "type": "VALUE", "value": "Page.loading", "order": 1}},
            "value": {"p2": {"key": "p2", "type": "VALUE", "value": false, "order": 1}}
          },
          "dependentStatements": {"Steps.fetch.output": true}
        }
      }
    }
  }
}
```
