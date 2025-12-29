"""Events Agent - Two-Phase: Analyze (Haiku) + Generate (Sonnet)"""
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
        super().__init__("EventsAnalyzer", model=settings.CLAUDE_HAIKU)
    
    def get_system_prompt(self) -> str:
        return """You are an Events Analyzer for the Nocode UI system.

Your job is to ANALYZE what event functions are needed, NOT generate them.

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

## Rules
1. Analyze the user request and component structure
2. Identify ALL interactive components (buttons, inputs, forms)
3. List each event function needed
4. Include store paths that events will modify
"""
    
    def get_relevant_docs(self) -> List[str]:
        return [
            "00-critical-rules",
            "07-event-system",
            "08-functions-and-actions",
            "22-component-reference"
        ]


class EventsGeneratorAgent(BaseAgent):
    """
    Phase 2: Generates event function definitions.
    
    Uses Sonnet for complex event generation.
    Takes the analysis and generates actual event functions.
    """
    
    def __init__(self):
        super().__init__("EventsGenerator", model=settings.CLAUDE_SONNET)
    
    def get_system_prompt(self) -> str:
        return """You are an Events Generator for the Nocode UI system.

You receive a list of events to generate. Create the actual event function definitions.

## Event Function Structure

Event functions use KIRun format with steps. Each step has `parameterMap` with nested parameters:

```json
{
  "eventFunctions": {
    "onButtonClick": {
      "name": "onButtonClick",
      "steps": {
        "updateStore": {
          "statementName": "updateStore",
          "name": "SetStore",
          "namespace": "UIEngine",
          "parameterMap": {
            "path": {
              "one": {
                "key": "one",
                "type": "VALUE",
                "value": "Page.counter",
                "order": 1
              }
            },
            "value": {
              "one": {
                "key": "one",
                "type": "EXPRESSION",
                "expression": "Page.counter + 1",
                "order": 1
              }
            }
          },
          "position": { "left": 100, "top": 100 }
        }
      }
    }
  },
  "componentEvents": {
    "myButton": {
      "onClick": ["onButtonClick"]
    }
  }
}
```

## Parameter Types
- `"type": "VALUE"` - Static value
- `"type": "EXPRESSION"` - Dynamic expression using paths like `Page.counter`

## parameterMap Structure

EVERY parameter must have this nested structure:
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

## SetStore Function (Most Important)

Use SetStore to update state:
```json
{
  "statementName": "setDisplay",
  "name": "SetStore",
  "namespace": "UIEngine",
  "parameterMap": {
    "path": {
      "one": { "key": "one", "type": "VALUE", "value": "Page.display", "order": 1 }
    },
    "value": {
      "one": { "key": "one", "type": "VALUE", "value": "Hello", "order": 1 }
    }
  }
}
```

## Navigate Function

```json
{
  "statementName": "navigate",
  "name": "Navigate",
  "namespace": "UIEngine",
  "parameterMap": {
    "linkPath": {
      "one": { "key": "one", "type": "VALUE", "value": "/home", "order": 1 }
    }
  }
}
```

## Dependent Statements (Chaining)

To run steps in sequence, use `dependentStatements`:
```json
{
  "step2": {
    "statementName": "step2",
    "name": "SetStore",
    "namespace": "UIEngine",
    "dependentStatements": ["step1"],
    "parameterMap": { ... }
  }
}
```

## Output Format

```json
{
  "reasoning": "Explanation of implementation",
  "eventFunctions": {
    "eventName": { ... }
  },
  "componentEvents": {
    "componentKey": {
      "onClick": ["eventName"]
    }
  }
}
```

## Rules
1. Generate ALL events from the provided list
2. Use proper parameterMap structure with nested keys
3. Use EXPRESSION type for dynamic values
4. Chain steps with dependentStatements for sequential execution
5. Map events to components using componentEvents
6. Component onClick property is `{ "value": "eventFunctionKey" }`
"""
    
    def get_relevant_docs(self) -> List[str]:
        return [
            "00-critical-rules",
            "07-event-system",
            "08-functions-and-actions",
            "21-kirun-system-functions",
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
Generate ALL the event functions listed in the analysis.
Output valid JSON only, wrapped in ```json code blocks.
"""
        
        return [{"role": "user", "content": user_content}]


class EventsAgent(BaseAgent):
    """
    Two-Phase Events Agent: Analyze then Generate.
    
    Phase 1 (Haiku): Analyze what events are needed
    Phase 2 (Sonnet): Generate the event functions in batches
    
    This is a facade that orchestrates the two phases.
    """
    
    BATCH_SIZE = 10  # Max events per generation call
    
    def __init__(self):
        super().__init__("Events", model=settings.CLAUDE_SONNET)
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
        """Execute two-phase event generation"""
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
            
            # Phase 2: Generate with Sonnet (in batches if needed)
            if progress:
                await progress.agent_thinking(
                    "Events", 
                    f"Generating {len(events_needed)} event functions (Phase 2)..."
                )
            
            if len(events_needed) <= self.BATCH_SIZE:
                # Single batch - generate all at once
                gen_input = AgentInput(
                    user_request=input.user_request,
                    context=input.context,
                    previous_outputs={"events_analysis": events_analysis}
                )
                
                gen_result = await self.generator.execute(gen_input, progress)
                
                return AgentOutput(
                    agent_name="Events",
                    success=gen_result.success,
                    result=gen_result.result,
                    reasoning=f"Analyzed {len(events_needed)} events, generated in single batch"
                )
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
                # Merge batch results
                batch_functions = gen_result.result.get("eventFunctions", {})
                batch_events = gen_result.result.get("componentEvents", {})
                
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
            reasoning=f"Generated {len(all_event_functions)} events in {len(batches)} batches"
        )
