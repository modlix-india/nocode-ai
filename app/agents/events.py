"""Events Agent - Two-Phase: Analyze (Haiku) + Generate JS (Sonnet) + Convert to KIRun"""
from typing import List, Dict, Any, Optional
from app.agents.base import BaseAgent, AgentInput, AgentOutput
from app.config import settings
from app.streaming.events import ProgressCallback
import json
import logging

logger = logging.getLogger(__name__)


class EventsAnalyzerAgent(BaseAgent):
    """
    Phase 1: Analyzes what events are needed.
    
    Uses Haiku for fast, cheap analysis.
    Outputs a list of events with their purposes.
    """
    
    def __init__(self):
        super().__init__("EventsAnalyzer", model_tier="fast")
    
    def get_system_prompt(self) -> str:
        return """You are an Events Analyzer for the Nocode UI system.

Your job is to ANALYZE what event functions are needed, NOT generate them.

You will receive:
1. The user's request
2. The component definitions from the Layout and Component agents

Look at the components to identify all interactive elements that need events.

## Output Format

List ALL events needed with their purpose:

```json
{
  "reasoning": "Brief explanation of event requirements",
  "events_needed": [
    {
      "name": "appendDigit0",
      "purpose": "Append digit 0 to the calculator display",
      "trigger": "onClick",
      "component": "btn0"
    },
    {
      "name": "appendDigit1",
      "purpose": "Append digit 1 to the calculator display",
      "trigger": "onClick",
      "component": "btn1"
    },
    {
      "name": "onClear",
      "purpose": "Clear the calculator display",
      "trigger": "onClick",
      "component": "btnClear"
    },
    {
      "name": "onSubmitForm",
      "purpose": "Submit the login form",
      "trigger": "onClick",
      "component": "submitButton"
    }
  ],
  "store_paths": ["Page.display", "Page.calculator.value"]
}
```

## CRITICAL RULES

1. **SEPARATE event functions for each action** - NO arguments
   - For 10 calculator buttons: appendDigit0, appendDigit1, ..., appendDigit9
   - For 4 operators: operatorAdd, operatorSubtract, operatorMultiply, operatorDivide

2. **NO parameterized events** - Create individual functions
   - WRONG: onNumberClick with argument "5"
   - CORRECT: appendDigit5

3. List ALL events exhaustively - don't group them

4. **Look at the components from previous agents** to identify buttons, forms, inputs

## Rules
1. Analyze the user request AND the component structure from previous outputs
2. Identify ALL interactive components (buttons, inputs, forms)
3. List each event function needed with the correct component key
4. Include store paths that events will modify
"""
    
    def get_relevant_docs(self) -> List[str]:
        return [
            "00-critical-rules",
            "07-event-system",
            "08-functions-and-actions",
            "23-javascript-event-guide",
            "22-component-reference"
        ]
    
    def _build_messages(self, input: AgentInput, rag_context: str) -> List[Dict]:
        """Override to explicitly include component information"""
        
        # Get layout and component outputs from previous agents
        layout_output = input.previous_outputs.get("layout", {})
        component_output = input.previous_outputs.get("component", {})
        
        # Combine components from both outputs
        all_components = {}
        if "componentDefinition" in layout_output:
            all_components.update(layout_output["componentDefinition"])
        if "components" in component_output:
            all_components.update(component_output["components"])
        
        # Extract interactive components for easier analysis
        interactive_components = []
        for key, comp in all_components.items():
            comp_type = comp.get("type", "")
            if comp_type in ["Button", "TextBox", "Dropdown", "Checkbox", "RadioButton", "Link", "Icon"]:
                interactive_components.append({
                    "key": key,
                    "type": comp_type,
                    "label": comp.get("properties", {}).get("label", {}).get("value", ""),
                    "text": comp.get("properties", {}).get("text", {}).get("value", "")
                })
        
        user_content = f"""
## User Request
{input.user_request}

## All Components (from Layout + Component agents)
```json
{json.dumps(all_components, indent=2)}
```

## Interactive Components (need events)
```json
{json.dumps(interactive_components, indent=2)}
```

## Existing Page Context
```json
{json.dumps(input.context.get("existingPage", {}), indent=2) if input.context.get("existingPage") else "No existing page"}
```

## Relevant Documentation
{rag_context if rag_context else "No additional documentation available."}

## Your Task
Analyze all the components above and list ALL events needed.
For EACH button, input, or interactive component, specify what event function it needs.
Output valid JSON only, wrapped in ```json code blocks.
"""
        
        return [{"role": "user", "content": user_content}]


class EventsGeneratorAgent(BaseAgent):
    """
    Phase 2: Generates event functions as JavaScript code.
    
    Uses Sonnet to generate JavaScript (which LLMs are good at).
    The JavaScript is then converted to KIRun by the JS2KIRun converter.
    """
    
    def __init__(self):
        super().__init__("EventsGenerator", model_tier="balanced")
    
    def get_system_prompt(self) -> str:
        return """You are an Events Generator for the Nocode UI system.

Generate JavaScript code for each event function. The code will be automatically converted to KIRun format.

## Output Format

```json
{
  "reasoning": "Brief explanation of implementation",
  "eventFunctions": {
    "appendDigit5": "Page.display = Page.display + '5';",
    "onClear": "Page.display = '0';",
    "calculateResult": "Page.result = Page.operand1 + Page.operand2;",
    "onLogin": "fetch('/api/login', { method: 'POST', body: Page.formData }); Page.loggedIn = Steps.sendData1.output.success;"
  },
  "componentEvents": {
    "btn5": { "onClick": ["appendDigit5"] },
    "clearBtn": { "onClick": ["onClear"] }
  }
}
```

## JavaScript Rules (CRITICAL - Follow These Exactly)

### 1. NO Local Variables
All state must use Page.* or Store.* paths. Never use const, let, or var.

```javascript
// WRONG
const total = 0;
total = total + 1;

// CORRECT  
Page.total = 0;
Page.total = Page.total + 1;
```

### 2. Store Paths
Use these prefixes for data access:
- `Page.*` - Page-scoped state
- `Store.*` - Global application state
- `Steps.*` - Results from previous steps (API calls)

### 3. Simple Assignments
```javascript
Page.counter = 0;
Page.display = Page.display + "5";
Page.isActive = !Page.isActive;
Page.total = Page.price * Page.quantity;
```

### 4. String Concatenation
```javascript
Page.display = Page.display + "5";
Page.message = Page.firstName + " " + Page.lastName;
```

### 5. API Calls
```javascript
// GET request
fetch("/api/users");
Page.users = Steps.fetchData1.output.data;

// POST request
fetch("/api/login", { method: "POST", body: Page.credentials });
Page.result = Steps.sendData1.output.data;
```

### 6. Navigation
```javascript
navigate("/dashboard");
navigate("/users/" + Page.userId);
```

### 7. Conditionals
```javascript
if (Page.isLoggedIn) {
  navigate("/dashboard");
} else {
  navigate("/login");
}
```

### 8. Show Messages
```javascript
alert("Success!");
showMessage("Error occurred", "ERROR");
```

## Examples for Common Patterns

### Calculator Digit Button
```javascript
Page.display = Page.display + "5";
```

### Calculator Clear
```javascript
Page.display = "0";
Page.operand1 = 0;
Page.operator = "";
```

### Toggle Boolean
```javascript
Page.isVisible = !Page.isVisible;
```

### Form Submit
```javascript
fetch("/api/submit", { method: "POST", body: Page.formData });
if (Steps.sendData1.output.success) {
  Page.success = true;
  navigate("/success");
} else {
  Page.error = Steps.sendData1.output.message;
}
```

### Increment Counter
```javascript
Page.counter = Page.counter + 1;
```

## What NOT to Do

- NO local variables (const, let, var)
- NO arrow functions
- NO destructuring
- NO Promise.all
- NO complex async patterns
- NO closures

Keep each event function simple and focused on a single action.
"""
    
    def get_relevant_docs(self) -> List[str]:
        return [
            "00-critical-rules",
            "07-event-system",
            "23-javascript-event-guide",
            "15-examples-and-patterns",
            "22-component-reference"
        ]
    
    def _build_messages(self, input: AgentInput, rag_context: str) -> List[Dict]:
        """Override to include events analysis in generator context"""
        
        # Get the events analysis from previous outputs
        events_analysis = input.previous_outputs.get("events_analysis", {})
        
        user_content = f"""
## User Request
{input.user_request}

## Events to Generate (from analysis)
```json
{json.dumps(events_analysis, indent=2)}
```

## Existing Page Context
```json
{json.dumps(input.context.get("existingPage", {}), indent=2) if input.context.get("existingPage") else "No existing page"}
```

## Relevant Documentation
{rag_context if rag_context else "No additional documentation available."}

## Your Task
Generate JavaScript code for ALL the event functions listed in the analysis.
Each event function should be a JavaScript string that follows the rules above.
Output valid JSON only, wrapped in ```json code blocks.
"""
        
        return [{"role": "user", "content": user_content}]
    
    def _post_process_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Convert JavaScript event functions to KIRun format"""
        try:
            from app.services.js2kirun.converter import EventFunctionConverter
            
            event_functions = result.get("eventFunctions", {})
            converted_functions = {}
            conversion_errors = []
            
            converter = EventFunctionConverter()
            
            for event_name, js_code in event_functions.items():
                if isinstance(js_code, str):
                    # It's JavaScript - convert to KIRun
                    try:
                        kirun_def = converter.convert(js_code, event_name)
                        converted_functions[event_name] = kirun_def
                        logger.debug(f"Converted event '{event_name}' from JS to KIRun")
                    except Exception as e:
                        logger.warning(f"Failed to convert event '{event_name}': {e}")
                        conversion_errors.append(f"{event_name}: {str(e)}")
                        # Keep original if conversion fails
                        converted_functions[event_name] = {
                            "name": event_name,
                            "steps": {},
                            "_conversion_error": str(e),
                            "_original_js": js_code
                        }
                elif isinstance(js_code, dict):
                    # Already in KIRun format (unlikely but handle it)
                    converted_functions[event_name] = js_code
                else:
                    logger.warning(f"Unexpected type for event '{event_name}': {type(js_code)}")
            
            result["eventFunctions"] = converted_functions
            
            if conversion_errors:
                result["_conversion_warnings"] = conversion_errors
            
            return result
            
        except ImportError as e:
            logger.error(f"JS2KIRun converter not available: {e}")
            return result
        except Exception as e:
            logger.error(f"Error in post-processing: {e}")
            return result


class EventsAgent(BaseAgent):
    """
    Two-Phase Events Agent: Analyze then Generate.
    
    Phase 1 (Haiku): Analyze what events are needed
    Phase 2 (Sonnet): Generate JavaScript event functions
    Phase 3: Convert JavaScript to KIRun format
    
    This is a facade that orchestrates the phases.
    """
    
    BATCH_SIZE = 10  # Max events per generation call
    
    def __init__(self):
        super().__init__("Events", model_tier="balanced")
        self.analyzer = EventsAnalyzerAgent()
        self.generator = EventsGeneratorAgent()
    
    def get_system_prompt(self) -> str:
        # Not used directly - delegates to analyzer/generator
        return self.generator.get_system_prompt()
    
    def get_relevant_docs(self) -> List[str]:
        return self.generator.get_relevant_docs()
    
    async def execute(
        self, 
        input: AgentInput,
        progress: Optional[ProgressCallback] = None
    ) -> AgentOutput:
        """Execute two-phase event generation with JS-to-KIRun conversion"""
        try:
            # Phase 1: Analyze with Haiku
            if progress:
                await progress.agent_thinking(
                    "Events", 
                    "Analyzing required events (Phase 1)..."
                )
            
            analysis_result = await self.analyzer.execute(input, progress)
            
            if not analysis_result.success:
                return analysis_result
            
            events_analysis = analysis_result.result
            events_needed = events_analysis.get("events_needed", [])
            
            logger.info(f"Events analysis found {len(events_needed)} events needed")
            
            if not events_needed:
                # No events needed
                return AgentOutput(
                    agent_name="Events",
                    success=True,
                    result={"eventFunctions": {}, "componentEvents": {}},
                    reasoning="No events required for this request"
                )
            
            # Phase 2: Generate JavaScript with Sonnet (in batches if needed)
            if progress:
                await progress.agent_thinking(
                    "Events", 
                    f"Generating {len(events_needed)} event functions as JavaScript..."
                )
            
            if len(events_needed) <= self.BATCH_SIZE:
                # Single batch - generate all at once
                gen_input = AgentInput(
                    user_request=input.user_request,
                    context=input.context,
                    previous_outputs={"events_analysis": events_analysis}
                )
                
                gen_result = await self.generator.execute(gen_input, progress)
                
                if gen_result.success:
                    # Phase 3: Convert JavaScript to KIRun
                    if progress:
                        await progress.agent_thinking(
                            "Events",
                            "Converting JavaScript to KIRun format..."
                        )
                    
                    converted_result = self.generator._post_process_result(gen_result.result)
                    
                    return AgentOutput(
                        agent_name="Events",
                        success=True,
                        result=converted_result,
                        reasoning=f"Generated {len(events_needed)} events as JS, converted to KIRun"
                    )
                else:
                    return gen_result
            else:
                # Multiple batches
                return await self._generate_in_batches(
                    events_needed, events_analysis, input, progress
                )
            
        except Exception as e:
            logger.error(f"Events agent error: {e}")
            return AgentOutput(
                agent_name="Events",
                success=False,
                result={},
                errors=[str(e)]
            )
    
    async def _generate_in_batches(
        self,
        events_needed: List[Dict],
        full_analysis: Dict,
        input: AgentInput,
        progress: Optional[ProgressCallback]
    ) -> AgentOutput:
        """Generate events in batches to avoid token limits"""
        
        batches = [
            events_needed[i:i+self.BATCH_SIZE] 
            for i in range(0, len(events_needed), self.BATCH_SIZE)
        ]
        
        logger.info(f"Generating {len(events_needed)} events in {len(batches)} batches")
        
        all_event_functions = {}
        all_component_events = {}
        
        for i, batch in enumerate(batches):
            if progress:
                await progress.agent_thinking(
                    "Events",
                    f"Generating batch {i+1}/{len(batches)} ({len(batch)} events)..."
                )
            
            # Create analysis for just this batch
            batch_analysis = {
                "reasoning": full_analysis.get("reasoning", ""),
                "events_needed": batch,
                "store_paths": full_analysis.get("store_paths", []),
                "_batch_info": f"Batch {i+1}/{len(batches)}"
            }
            
            gen_input = AgentInput(
                user_request=input.user_request,
                context=input.context,
                previous_outputs={"events_analysis": batch_analysis}
            )
            
            gen_result = await self.generator.execute(gen_input, progress)
            
            if gen_result.success and gen_result.result:
                # Convert JavaScript to KIRun
                if progress:
                    await progress.agent_thinking(
                        "Events",
                        f"Converting batch {i+1} to KIRun..."
                    )
                
                converted_result = self.generator._post_process_result(gen_result.result)
                
                # Merge batch results
                batch_functions = converted_result.get("eventFunctions", {})
                batch_events = converted_result.get("componentEvents", {})
                
                all_event_functions.update(batch_functions)
                
                # Merge component events (append to arrays)
                for comp_key, events in batch_events.items():
                    if comp_key not in all_component_events:
                        all_component_events[comp_key] = {}
                    for event_type, handlers in events.items():
                        if event_type not in all_component_events[comp_key]:
                            all_component_events[comp_key][event_type] = []
                        if isinstance(handlers, list):
                            all_component_events[comp_key][event_type].extend(handlers)
                        else:
                            all_component_events[comp_key][event_type].append(handlers)
            else:
                logger.warning(f"Batch {i+1} generation failed: {gen_result.errors}")
        
        return AgentOutput(
            agent_name="Events",
            success=True,
            result={
                "eventFunctions": all_event_functions,
                "componentEvents": all_component_events
            },
            reasoning=f"Generated {len(all_event_functions)} events in {len(batches)} batches, converted to KIRun"
        )
