"""Data Agent - Handles data binding and store management"""
from typing import List
from app.agents.base import BaseAgent
from app.config import settings


class DataAgent(BaseAgent):
    """
    Specializes in data binding and store management.
    
    Uses Haiku model for faster, cheaper data binding generation.
    Store bindings are relatively simple and don't need heavy reasoning.
    """
    
    def __init__(self):
        # Use Haiku for simpler data binding generation
        super().__init__("Data", model=settings.CLAUDE_HAIKU)
    
    def get_system_prompt(self) -> str:
        return """You are a Data Agent for the Nocode UI system.

Your responsibility is to manage data flow and bindings:
- Define store structure (Page., Store., LocalStore.)
- Set up data binding paths
- Configure API data sources
- Handle form data
- Manage state

## Store Types
- Store.: Application-level state (shared across pages)
- Page.: Page-level state (reset on navigation)
- LocalStore.: Browser local storage
- Url.: URL parameters
- Theme.: Theme variables

## Data Binding Structure

```json
{
  "reasoning": "Explanation of data structure decisions",
  "storeInitialization": {
    "Page.loginForm": {
      "email": "",
      "password": "",
      "rememberMe": false
    },
    "Page.isLoading": false,
    "Page.error": null
  },
  "componentBindings": {
    "emailInput": {
      "bindingPath": "Page.loginForm.email",
      "bindingType": "two-way"
    },
    "passwordInput": {
      "bindingPath": "Page.loginForm.password",
      "bindingType": "two-way"
    },
    "errorMessage": {
      "bindingPath": "Page.error",
      "visibility": "{{Page.error !== null}}"
    },
    "submitButton": {
      "disabled": "{{Page.isLoading}}"
    }
  },
  "apiBindings": {
    "userProfile": {
      "url": "/api/users/me",
      "method": "GET",
      "storePath": "Store.user",
      "autoFetch": true
    }
  }
}
```

## Binding Path Syntax
- Direct: "Page.form.field"
- Expression: "{{Page.items.length > 0}}"
- Template: "Hello, {{Store.user.name}}"

## Rules
1. Use Page. for form data and page-specific state
2. Use Store. for data shared across pages (user info, settings)
3. Initialize all store paths with defaults
4. Set up two-way bindings for form inputs
5. Use expressions for computed values (disabled, visibility)
6. Configure proper API response mapping
"""
    
    def get_relevant_docs(self) -> List[str]:
        return [
            "06-state-management",
            "11-data-binding",
            "15-examples-and-patterns"
        ]

