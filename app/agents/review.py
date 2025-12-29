"""Review Agent - Validates and improves the merged output"""
from typing import List, Dict, Any
from app.agents.base import BaseAgent, AgentInput, AgentOutput
from app.config import settings
import json
import logging

logger = logging.getLogger(__name__)


class ReviewAgent(BaseAgent):
    """
    Reviews and validates the merged page output.
    
    Uses Sonnet model for complex validation and structure fixing.
    Review needs strong reasoning to validate and fix page definitions.
    """
    
    def __init__(self):
        # Use Sonnet for complex validation and fixing
        super().__init__("Review", model=settings.CLAUDE_SONNET)
    
    def get_system_prompt(self) -> str:
        return """You are a Review Agent for the Nocode UI system.

Your responsibility is to review and validate the merged page definition.

## ⚠️ CRITICAL RULES TO ENFORCE

### 1. FLAT componentDefinition Structure
- `rootComponent` MUST be a STRING (component key), NOT an object
- `componentDefinition` MUST be a FLAT map of all components
- `children` MUST be `{ "childKey": true }` references, NOT nested objects

**WRONG ❌:**
```json
{
  "rootComponent": {
    "key": "root",
    "children": { "child": { "key": "child" } }
  }
}
```

**CORRECT ✅:**
```json
{
  "rootComponent": "root",
  "componentDefinition": {
    "root": { "key": "root", "type": "Grid", "children": { "child": true } },
    "child": { "key": "child", "type": "Button" }
  }
}
```

### 2. Event Functions CANNOT Have Arguments
- Component onClick is a simple string: `{ "value": "eventFunctionKey" }`
- NO arguments, NO functionName objects
- For multiple similar buttons, create SEPARATE event functions

### 3. Event parameterMap Structure
Every parameter MUST have this structure:
```json
{
  "parameterName": {
    "one": {
      "key": "one",
      "type": "VALUE" or "EXPRESSION",
      "value": "..." or "expression": "...",
      "order": 1
    }
  }
}
```

## VALIDATION CHECKLIST

1. ✅ `rootComponent` is a STRING (not an object)
2. ✅ All components are in the FLAT `componentDefinition` map
3. ✅ Children are `{ "key": true }` references
4. ✅ Component onClick values are simple strings like `{ "value": "eventKey" }`
5. ✅ NO arguments passed to event functions
6. ✅ Event function parameterMap has proper nested structure
7. ✅ All referenced event functions exist in eventFunctions
8. ✅ All component types are valid (Grid, Button, Text, TextBox, Icon, Image, etc.)

## Output Format

Output the COMPLETE, VALID page definition:

```json
{
  "reasoning": "Summary of validation and fixes applied",
  "issues": ["List of issues found and fixed"],
  "improvements": ["List of improvements made"],
  "name": "pageName",
  "rootComponent": "rootComponentKey",
  "componentDefinition": {
    "rootComponentKey": {
      "key": "rootComponentKey",
      "type": "Grid",
      "children": { "child1": true }
    },
    "child1": {
      "key": "child1",
      "type": "Button",
      "properties": {
        "label": { "value": "Click Me" },
        "onClick": { "value": "onButtonClick" }
      }
    }
  },
  "eventFunctions": {
    "onButtonClick": { ... }
  },
  "properties": {
    "storeInitialization": { ... }
  }
}
```

## Rules
1. FIX any structural issues (nested children → flat componentDefinition)
2. FIX any event argument issues (create separate functions)
3. FIX any parameterMap structure issues
4. Output a COMPLETE, VALID page definition
5. Ensure the page will render correctly
"""
    
    def get_relevant_docs(self) -> List[str]:
        return [
            "00-critical-rules",
            "02-application-and-page-definitions",
            "03-component-system",
            "07-event-system",
            "15-examples-and-patterns",
            "22-component-reference"
        ]
    
    def _build_messages(self, input: AgentInput, rag_context: str) -> List[Dict]:
        """Override to include merged page in review context"""
        
        merged_page = input.context.get("merged_page", {})
        mode = input.context.get("mode", "create")
        
        # Get agent outputs for reference
        agent_outputs = []
        for name, output in input.previous_outputs.items():
            if output:
                agent_outputs.append(f"### {name.title()} Agent Output:\n```json\n{json.dumps(output, indent=2)}\n```")
        
        user_content = f"""
## User Request
{input.user_request}

## Mode
{mode}

## Merged Page Definition (to review and fix)
```json
{json.dumps(merged_page, indent=2)}
```

## Individual Agent Outputs (for reference)
{chr(10).join(agent_outputs)}

## Relevant Documentation
{rag_context if rag_context else "No additional documentation available."}

## Your Task
1. VALIDATE the merged page against critical rules
2. FIX any structure issues (nested children → flat componentDefinition)
3. FIX any event issues (arguments → separate event functions)
4. FIX any parameterMap issues (proper nested structure)
5. Output the COMPLETE, VALID page definition
"""
        
        return [{"role": "user", "content": user_content}]
