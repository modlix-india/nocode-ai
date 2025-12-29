"""Component Agent - Two-Phase: Analyze (Haiku) + Generate (Sonnet)"""
from typing import List, Dict, Any, Optional
from app.agents.base import BaseAgent, AgentInput, AgentOutput
from app.config import settings
from app.streaming.events import ProgressCallback
import json
import logging

logger = logging.getLogger(__name__)


class ComponentAnalyzerAgent(BaseAgent):
    """
    Phase 1: Analyzes what components are needed.
    
    Uses Haiku for fast, cheap analysis.
    Outputs a list of components with their types and purposes.
    """
    
    def __init__(self):
        super().__init__("ComponentAnalyzer", model=settings.CLAUDE_HAIKU)
    
    def get_system_prompt(self) -> str:
        return """You are a Component Analyzer for the Nocode UI system.

Your job is to ANALYZE what components are needed, NOT generate them in detail.

## Output Format

List ALL components needed with their types and purposes:

```json
{
  "reasoning": "Brief explanation of component requirements",
  "components_needed": [
    {
      "key": "loginForm",
      "type": "Grid",
      "purpose": "Container for login form elements",
      "parent": "pageRoot"
    },
    {
      "key": "emailInput",
      "type": "TextBox",
      "purpose": "Email input field",
      "parent": "loginForm",
      "properties_hint": "label, placeholder, bindingPath"
    },
    {
      "key": "passwordInput",
      "type": "TextBox",
      "purpose": "Password input field with type=password",
      "parent": "loginForm",
      "properties_hint": "label, type, bindingPath"
    },
    {
      "key": "submitButton",
      "type": "Button",
      "purpose": "Submit the login form",
      "parent": "loginForm",
      "properties_hint": "label, onClick event"
    }
  ]
}
```

## Available Component Types
- Grid: For layout containers
- Text: For displaying text content
- Button: For actions (has label property)
- TextBox: For text/password input
- Checkbox: For boolean inputs
- Dropdown: For selection lists
- RadioButton: For single selection
- Image: For displaying images
- Icon: For icons (Font Awesome, Material)
- Link: For navigation links

## Rules
1. Analyze the user request and layout structure
2. Identify ALL UI components needed
3. List each component with its type and purpose
4. Specify parent relationships
5. Add hints about key properties needed
6. Keep analysis concise but complete
"""
    
    def get_relevant_docs(self) -> List[str]:
        return [
            "00-critical-rules",
            "03-component-system",
            "04-property-system",
            "22-component-reference"
        ]


class ComponentGeneratorAgent(BaseAgent):
    """
    Phase 2: Generates component definitions.
    
    Uses Sonnet for detailed component generation.
    Takes the analysis and generates actual component definitions.
    """
    
    def __init__(self):
        super().__init__("ComponentGenerator", model=settings.CLAUDE_SONNET)
    
    def get_system_prompt(self) -> str:
        return """You are a Component Generator for the Nocode UI system.

You receive a list of components to generate. Create the actual component definitions.

## CRITICAL: Flat componentDefinition

Output components as a FLAT map, NOT nested.

**WRONG ❌:**
```json
{
  "components": {
    "form": {
      "children": {
        "input": { "key": "input", "type": "TextBox" }
      }
    }
  }
}
```

**CORRECT ✅:**
```json
{
  "components": {
    "form": {
      "key": "form",
      "type": "Grid",
      "children": { "emailInput": true, "passwordInput": true }
    },
    "emailInput": {
      "key": "emailInput",
      "type": "TextBox",
      "properties": {
        "textBox": {
          "label": { "value": "Email" }
        }
      }
    },
    "passwordInput": {
      "key": "passwordInput", 
      "type": "TextBox",
      "properties": {
        "textBox": {
          "label": { "value": "Password" },
          "type": { "value": "password" }
        }
      }
    }
  }
}
```

## Component Properties

### Button
```json
{
  "type": "Button",
  "properties": {
    "label": { "value": "Click Me" },
    "onClick": { "value": "eventFunctionKey" }
  }
}
```

### TextBox
```json
{
  "type": "TextBox",
  "properties": {
    "textBox": {
      "label": { "value": "Email" },
      "placeholder": { "value": "Enter email" },
      "bindingPath": { "value": "Page.form.email" }
    }
  }
}
```

### Text
```json
{
  "type": "Text",
  "properties": {
    "text": { "value": "Hello World" }
  }
}
```

### Icon
```json
{
  "type": "Icon",
  "properties": {
    "icon": { "value": "fa-solid fa-user" }
  }
}
```

## Output Format
```json
{
  "reasoning": "Explanation of component choices",
  "components": {
    "componentKey1": {
      "key": "componentKey1",
      "type": "Button",
      "properties": { ... },
      "children": { "childKey": true }
    },
    "componentKey2": {
      "key": "componentKey2",
      "type": "TextBox",
      "properties": { ... }
    }
  }
}
```

## Rules
1. ALL components in a FLAT map - NO nested children objects
2. Children are `{ "childKey": true }` references
3. Use exact component type names: Grid, Button, Text, TextBox, Icon, Image
4. Set meaningful labels and placeholders
5. Configure bindingPath for form inputs
6. DO NOT add detailed styles - Styles agent handles those
"""
    
    def get_relevant_docs(self) -> List[str]:
        return [
            "00-critical-rules",
            "03-component-system",
            "04-property-system",
            "11-data-binding",
            "15-examples-and-patterns",
            "22-component-reference"
        ]
    
    def _build_messages(self, input: AgentInput, rag_context: str) -> List[Dict]:
        """Override to include component analysis in generator context"""
        
        # Get the component analysis from previous outputs
        component_analysis = input.previous_outputs.get("component_analysis", {})
        
        user_content = f"""
## User Request
{input.user_request}

## Components to Generate (from analysis)
```json
{json.dumps(component_analysis, indent=2)}
```

## Existing Page Context
```json
{json.dumps(input.context.get("existingPage", {}), indent=2) if input.context.get("existingPage") else "No existing page"}
```

## Layout Structure (from Layout agent)
```json
{json.dumps(input.previous_outputs.get("layout", {}), indent=2) if input.previous_outputs.get("layout") else "No layout provided"}
```

## Relevant Documentation
{rag_context if rag_context else "No additional documentation available."}

## Your Task
Generate ALL the component definitions listed in the analysis.
Output valid JSON only, wrapped in ```json code blocks.
Include a brief "reasoning" field explaining your decisions.
"""
        
        return [{"role": "user", "content": user_content}]


class ComponentAgent(BaseAgent):
    """
    Two-Phase Component Agent: Analyze then Generate.
    
    Phase 1 (Haiku): Analyze what components are needed
    Phase 2 (Sonnet): Generate the component definitions in batches
    
    This is a facade that orchestrates the two phases.
    """
    
    BATCH_SIZE = 10  # Max components per generation call
    
    def __init__(self):
        super().__init__("Component", model=settings.CLAUDE_SONNET)
        self.analyzer = ComponentAnalyzerAgent()
        self.generator = ComponentGeneratorAgent()
    
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
        """Execute two-phase component generation"""
        try:
            # Phase 1: Analyze with Haiku
            if progress:
                await progress.agent_thinking(
                    "Component", 
                    "Analyzing required components (Phase 1)..."
                )
            
            analysis_result = await self.analyzer.execute(input, progress)
            
            if not analysis_result.success:
                return analysis_result
            
            component_analysis = analysis_result.result
            components_needed = component_analysis.get("components_needed", [])
            
            logger.info(f"Component analysis found {len(components_needed)} components needed")
            
            if not components_needed:
                # No components needed
                return AgentOutput(
                    agent_name="Component",
                    success=True,
                    result={"components": {}},
                    reasoning="No components required for this request"
                )
            
            # Phase 2: Generate with Sonnet (in batches if needed)
            if progress:
                await progress.agent_thinking(
                    "Component", 
                    f"Generating {len(components_needed)} components (Phase 2)..."
                )
            
            if len(components_needed) <= self.BATCH_SIZE:
                # Single batch - generate all at once
                gen_input = AgentInput(
                    user_request=input.user_request,
                    context=input.context,
                    previous_outputs={
                        **input.previous_outputs,
                        "component_analysis": component_analysis
                    }
                )
                
                gen_result = await self.generator.execute(gen_input, progress)
                
                return AgentOutput(
                    agent_name="Component",
                    success=gen_result.success,
                    result=gen_result.result,
                    reasoning=f"Analyzed {len(components_needed)} components, generated in single batch"
                )
            else:
                # Multiple batches
                return await self._generate_in_batches(
                    components_needed, component_analysis, input, progress
                )
            
        except Exception as e:
            logger.error(f"Component agent error: {e}")
            return AgentOutput(
                agent_name="Component",
                success=False,
                result={},
                errors=[str(e)]
            )
    
    async def _generate_in_batches(
        self,
        components_needed: List[Dict],
        full_analysis: Dict,
        input: AgentInput,
        progress: Optional[ProgressCallback]
    ) -> AgentOutput:
        """Generate components in batches to avoid token limits"""
        
        batches = [
            components_needed[i:i+self.BATCH_SIZE] 
            for i in range(0, len(components_needed), self.BATCH_SIZE)
        ]
        
        logger.info(f"Generating {len(components_needed)} components in {len(batches)} batches")
        
        all_components = {}
        
        for i, batch in enumerate(batches):
            if progress:
                await progress.agent_thinking(
                    "Component",
                    f"Generating batch {i+1}/{len(batches)} ({len(batch)} components)..."
                )
            
            # Create analysis for just this batch
            batch_analysis = {
                "reasoning": full_analysis.get("reasoning", ""),
                "components_needed": batch,
                "_batch_info": f"Batch {i+1}/{len(batches)}"
            }
            
            gen_input = AgentInput(
                user_request=input.user_request,
                context=input.context,
                previous_outputs={
                    **input.previous_outputs,
                    "component_analysis": batch_analysis
                }
            )
            
            gen_result = await self.generator.execute(gen_input, progress)
            
            if gen_result.success and gen_result.result:
                # Merge batch results
                batch_components = gen_result.result.get("components", {})
                all_components.update(batch_components)
            else:
                logger.warning(f"Batch {i+1} generation failed: {gen_result.errors}")
        
        return AgentOutput(
            agent_name="Component",
            success=True,
            result={"components": all_components},
            reasoning=f"Generated {len(all_components)} components in {len(batches)} batches"
        )
