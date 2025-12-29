"""Layout Agent - Two-Phase: Analyze (Haiku) + Generate (Sonnet)"""
from typing import List, Dict, Any, Optional
from app.agents.base import BaseAgent, AgentInput, AgentOutput
from app.config import settings
from app.streaming.events import ProgressCallback
import json
import logging

logger = logging.getLogger(__name__)


class LayoutAnalyzerAgent(BaseAgent):
    """
    Phase 1: Analyzes what layout structure is needed.
    
    Uses Haiku for fast, cheap analysis.
    Outputs a layout plan with sections and hierarchy.
    """
    
    def __init__(self):
        super().__init__("LayoutAnalyzer", model=settings.CLAUDE_HAIKU)
    
    def get_system_prompt(self) -> str:
        return """You are a Layout Analyzer for the Nocode UI system.

Your job is to ANALYZE what layout structure is needed, NOT generate detailed definitions.

## Output Format

Plan the layout structure:

```json
{
  "reasoning": "Brief explanation of layout decisions",
  "layout_plan": {
    "rootKey": "pageRoot",
    "sections": [
      {
        "key": "header",
        "purpose": "Navigation and branding",
        "layout_type": "ROWLAYOUT",
        "children_hint": ["logo", "navLinks", "userMenu"]
      },
      {
        "key": "main",
        "purpose": "Main content area",
        "layout_type": "grid",
        "children_hint": ["sidebar", "content"]
      },
      {
        "key": "footer",
        "purpose": "Footer with links and copyright",
        "layout_type": "ROWLAYOUT",
        "children_hint": ["footerLinks", "copyright"]
      }
    ],
    "responsive_notes": "Sidebar collapses on mobile"
  }
}
```

## Layout Types
- `ROWLAYOUT`: Horizontal flex layout (for navbars, rows)
- `grid`: CSS Grid for complex layouts
- Default (no layout property): Vertical stack

## Rules
1. Plan semantic sections (header, main, sidebar, footer)
2. Identify layout type for each section
3. List expected children (hints for Component agent)
4. Note responsive considerations
5. Keep analysis concise
"""
    
    def get_relevant_docs(self) -> List[str]:
        return [
            "00-critical-rules",
            "02-application-and-page-definitions",
            "03-component-system",
            "22-component-reference"
        ]


class LayoutGeneratorAgent(BaseAgent):
    """
    Phase 2: Generates layout definitions.
    
    Uses Sonnet for detailed layout generation.
    Takes the analysis and generates actual layout Grid components.
    """
    
    def __init__(self):
        super().__init__("LayoutGenerator", model=settings.CLAUDE_SONNET)
    
    def get_system_prompt(self) -> str:
        return """You are a Layout Generator for the Nocode UI system.

You receive a layout plan. Generate the actual layout Grid definitions.

## CRITICAL: Page Structure

The page uses a **FLAT** componentDefinition map. DO NOT nest children as objects.

**WRONG ❌:**
```json
{
  "rootComponent": {
    "key": "root",
    "children": {
      "child1": { "key": "child1", "type": "Grid" }
    }
  }
}
```

**CORRECT ✅:**
```json
{
  "rootComponent": "root",
  "componentDefinition": {
    "root": {
      "key": "root",
      "type": "Grid",
      "children": { "header": true, "content": true }
    },
    "header": {
      "key": "header",
      "type": "Grid",
      "children": {}
    },
    "content": {
      "key": "content", 
      "type": "Grid",
      "children": {}
    }
  }
}
```

## Component Types for Layout
- Grid: For grid-based layouts with templateColumns, templateRows, gap
- Box: Simple container (like a div)

## Output Format
```json
{
  "reasoning": "Brief explanation of layout decisions",
  "rootComponent": "rootKey",
  "componentDefinition": {
    "rootKey": {
      "key": "rootKey",
      "type": "Grid",
      "properties": {
        "layout": { "value": "ROWLAYOUT" }
      },
      "children": { "header": true, "main": true, "footer": true },
      "displayOrder": 0
    },
    "header": {
      "key": "header",
      "type": "Grid",
      "children": {},
      "displayOrder": 0
    },
    "main": {
      "key": "main",
      "type": "Grid", 
      "children": {},
      "displayOrder": 1
    },
    "footer": {
      "key": "footer",
      "type": "Grid",
      "children": {},
      "displayOrder": 2
    }
  }
}
```

## Grid Properties
- `layout`: "ROWLAYOUT" (flex row) or default (grid)
- For Grid type, styles control templateColumns, templateRows, gap

## Rules
1. Use semantic key names (header, sidebar, content, footer)
2. Use FLAT componentDefinition - children are `{ "childKey": true }` ONLY
3. Every component in children MUST have its own entry in componentDefinition
4. Use Grid for layouts
5. DO NOT add events, detailed styles, or data binding - other agents handle those
6. Focus ONLY on structure and spatial organization
7. Keep keys simple and descriptive (camelCase)
"""
    
    def get_relevant_docs(self) -> List[str]:
        return [
            "00-critical-rules",
            "02-application-and-page-definitions",
            "03-component-system",
            "15-examples-and-patterns",
            "22-component-reference"
        ]
    
    def _build_messages(self, input: AgentInput, rag_context: str) -> List[Dict]:
        """Override to include layout plan in generator context"""
        
        # Get the layout plan from previous outputs
        layout_plan = input.previous_outputs.get("layout_plan", {})
        
        user_content = f"""
## User Request
{input.user_request}

## Layout Plan (from analysis)
```json
{json.dumps(layout_plan, indent=2)}
```

## Existing Page Context
```json
{json.dumps(input.context.get("existingPage", {}), indent=2) if input.context.get("existingPage") else "No existing page"}
```

## Relevant Documentation
{rag_context if rag_context else "No additional documentation available."}

## Your Task
Generate the layout Grid definitions based on the plan.
Output valid JSON only, wrapped in ```json code blocks.
Include a brief "reasoning" field explaining your decisions.
"""
        
        return [{"role": "user", "content": user_content}]


class LayoutAgent(BaseAgent):
    """
    Two-Phase Layout Agent: Analyze then Generate.
    
    Phase 1 (Haiku): Analyze what layout structure is needed
    Phase 2 (Sonnet): Generate the layout Grid definitions
    
    This is a facade that orchestrates the two phases.
    Layout typically doesn't need batching as layouts are simpler.
    """
    
    def __init__(self):
        super().__init__("Layout", model=settings.CLAUDE_SONNET)
        self.analyzer = LayoutAnalyzerAgent()
        self.generator = LayoutGeneratorAgent()
    
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
        """Execute two-phase layout generation"""
        try:
            # Phase 1: Analyze with Haiku
            if progress:
                await progress.agent_thinking(
                    "Layout", 
                    "Analyzing layout structure (Phase 1)..."
                )
            
            analysis_result = await self.analyzer.execute(input, progress)
            
            if not analysis_result.success:
                return analysis_result
            
            layout_plan = analysis_result.result
            
            logger.info(f"Layout analysis: {layout_plan.get('layout_plan', {}).get('sections', [])}")
            
            # Phase 2: Generate with Sonnet
            if progress:
                await progress.agent_thinking(
                    "Layout", 
                    "Generating layout structure (Phase 2)..."
                )
            
            gen_input = AgentInput(
                user_request=input.user_request,
                context=input.context,
                previous_outputs={"layout_plan": layout_plan}
            )
            
            gen_result = await self.generator.execute(gen_input, progress)
            
            return AgentOutput(
                agent_name="Layout",
                success=gen_result.success,
                result=gen_result.result,
                reasoning=f"Analyzed layout, generated {len(gen_result.result.get('componentDefinition', {}))} sections"
            )
            
        except Exception as e:
            logger.error(f"Layout agent error: {e}")
            return AgentOutput(
                agent_name="Layout",
                success=False,
                result={},
                errors=[str(e)]
            )
