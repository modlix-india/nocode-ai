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
        super().__init__("ComponentAnalyzer", model_tier="fast")
    
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

## Available Component Types (USE ONLY THESE - NO OTHER TYPES EXIST)
- Grid: For layout containers (use this for any container/wrapper/section)
- Text: For displaying text content
- Button: For actions (has label property)
- TextBox: For text/password input
- Checkbox: For boolean inputs
- Dropdown: For selection lists
- RadioButton: For single selection
- Image: For displaying images (use EMPTY src "" for placeholders in import mode)
- Icon: For icons (Font Awesome, Material)
- Link: For navigation links

## CRITICAL: INVALID COMPONENT TYPES
**NEVER USE THESE - THEY DO NOT EXIST:**
- Box (USE Grid INSTEAD)
- Div (USE Grid INSTEAD)
- Container (USE Grid INSTEAD)
- Section (USE Grid INSTEAD)
- Card (USE Grid INSTEAD)
- Flex (USE Grid INSTEAD)
- Row (USE Grid INSTEAD)
- Column (USE Grid INSTEAD)
- Wrapper (USE Grid INSTEAD)
- Header (USE Grid INSTEAD)
- Footer (USE Grid INSTEAD)
- Nav (USE Grid INSTEAD)
- Span (USE Text INSTEAD)
- Paragraph (USE Text INSTEAD)
- Label (USE Text INSTEAD)
- Heading (USE Text INSTEAD)
- H1/H2/H3/H4/H5/H6 (USE Text with textContainer property INSTEAD)
- Input (USE TextBox INSTEAD)
- Anchor (USE Button or Link INSTEAD)

## Image Components in Import Mode
When importing from a website (importMode=true or placeholderImages=true), create Image components with:
- src: EMPTY string "" (placeholder - actual images will be added later)
- alt: Preserve alt text from original if available
- Preserve width/height in styleProperties if known

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
    
    def get_rag_query(self, user_request: str) -> str:
        """Custom RAG query to retrieve valid component types list."""
        return (
            "What UI component types are available? "
            "Valid types: Grid, Text, Button, TextBox, Image, Icon, Checkbox, Dropdown, RadioButton, Link. "
            + user_request
        )


class ComponentGeneratorAgent(BaseAgent):
    """
    Phase 2: Generates component definitions.
    
    Uses Sonnet for detailed component generation.
    Takes the analysis and generates actual component definitions.
    """
    
    def __init__(self):
        super().__init__("ComponentGenerator", model_tier="balanced")
    
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

### Image (Placeholder for Import Mode)
```json
{
  "type": "Image",
  "properties": {
    "src": { "value": "" },
    "alt": { "value": "Image description" }
  }
}
```

**IMPORTANT for Import Mode:** When `importMode` or `placeholderImages` is true in context:
- Create Image components with EMPTY src: `{"value": ""}`
- Preserve alt text if available from the analysis
- The actual images will be added later

## Output Format
```json
{
  "reasoning": "Explanation of component choices",
  "components": {
    "componentKey1": {
      "key": "componentKey1",
      "type": "Button",
      "parent": "parentContainerKey",
      "properties": { ... }
    },
    "componentKey2": {
      "key": "componentKey2",
      "type": "TextBox",
      "parent": "formContainer",
      "properties": { ... }
    },
    "formContainer": {
      "key": "formContainer",
      "type": "Grid",
      "parent": "pageRoot",
      "properties": {},
      "children": { "componentKey2": true }
    }
  }
}
```

## CRITICAL: Parent-Child Relationships
- **EVERY component MUST have a "parent" field** specifying which container it belongs to
- The parent must be an existing Grid container (from Layout agent or a new one you create)
- If you create a new Grid container, also specify ITS parent
- Root level containers should have "parent": "pageRoot"

## Rules
1. ALL components in a FLAT map - NO nested children objects
2. **EVERY component MUST have a "parent" field** (except pageRoot)
3. If a Grid has children, list them: `"children": { "childKey": true }`
4. Use ONLY valid component types: Grid, Button, Text, TextBox, Icon, Image, Checkbox, Dropdown, RadioButton, Link
5. NEVER use "Box", "Container", "Div", "Section", "Card" - use Grid instead
6. NEVER use "Span", "Paragraph", "Label", "H1", "H2" etc. - use Text instead
7. Set meaningful labels and placeholders
8. Configure bindingPath for form inputs
9. DO NOT add detailed styles - Styles agent handles those
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
    
    def get_rag_query(self, user_request: str) -> str:
        """
        Custom RAG query that specifically asks about valid component types.
        This helps retrieve the component reference documentation.
        """
        return (
            "What are all the valid component types available? "
            "List Grid, Text, Button, TextBox, Image, Icon, Checkbox, Dropdown, RadioButton, Link components. "
            "What properties do these components have? " + user_request
        )
    
    def _build_messages(self, input: AgentInput, rag_context: str) -> List[Dict]:
        """Override to include component analysis in generator context"""
        
        # Get the component analysis from previous outputs
        component_analysis = input.previous_outputs.get("component_analysis", {})
        
        # Check if this is import mode with extracted text content
        is_import_mode = input.context.get("importMode", False) or input.context.get("mode") == "import"
        text_content = input.context.get("textContent", [])
        use_exact_content = input.context.get("useExactContent", False)
        
        # Format extracted text if available
        extracted_text_section = ""
        if is_import_mode and text_content and use_exact_content:
            text_lines = []
            for item in text_content[:40]:  # Limit for token efficiency
                el_type = item.get("type", "UNKNOWN")
                text = item.get("text", "")[:150]
                if text:
                    text_lines.append(f'- {el_type}: "{text}"')
            
            if text_lines:
                extracted_text_section = f"""
## CRITICAL: EXACT TEXT CONTENT TO USE

You MUST use these exact text strings from the original website.
Do NOT make up placeholder text - use these verbatim:

{chr(10).join(text_lines)}

For Text components, set the "text" property to the EXACT string above.
For Button components, set the "label" property to the EXACT button text above.
"""
        
        user_content = f"""
## User Request
{input.user_request}
{extracted_text_section}

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
{"Use the EXACT TEXT from the extracted content above for all Text and Button components." if is_import_mode else ""}
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
        super().__init__("Component", model_tier="balanced")
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
