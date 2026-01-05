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
        super().__init__("LayoutAnalyzer", model_tier="fast")
    
    def get_system_prompt(self) -> str:
        return """You are a Layout Analyzer for the Nocode UI system.

Your job is to ANALYZE what HIGH-LEVEL layout structure is needed, NOT detailed definitions.

## IMPORTANT: Focus on CONTAINERS, Not Content

Plan ONLY the structural containers. DO NOT plan individual content items.

**GOOD container examples:** header, navContainer, heroSection, featureGrid, footer, sidebar, contentArea
**BAD - too granular:** navItem1, navItem2, featureTitle, featureDescription, buttonText

The Component agent will fill the containers with actual content (Text, Button, Image, etc.)

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
    
    Supports batching for large layouts to avoid token limits.
    """
    
    BATCH_SIZE = 8  # Max sections per generation call
    
    def __init__(self):
        super().__init__("LayoutGenerator", model_tier="balanced")
    
    def get_system_prompt(self) -> str:
        return """You are a Layout Generator for the Nocode UI system.

You receive a layout plan. Generate the actual layout Grid definitions.

## CRITICAL: Create ONLY Container Components

Your job is to create the STRUCTURAL CONTAINERS (Grid components) that organize the page.
DO NOT create leaf components like Text, Button, Image, Icon - the Component agent handles those.

Create Grid containers for:
- Page sections (header, main, footer, sidebar)
- Groups of related items (navContainer, heroSection, featureGrid)
- Form containers

DO NOT create containers for:
- Individual text items (use Text component instead - handled by Component agent)
- Individual buttons (use Button component - handled by Component agent)
- Individual images (use Image component - handled by Component agent)

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

## IMPORTANT: Grid Default Styles
Grid components have DEFAULT styles: `display: flex` and `flexDirection: column`.
This means Grid renders as a vertical flex container by default.
To change this behavior, you MUST override these in styleProperties:
- For horizontal layout: set `flexDirection: "row"`
- For CSS Grid: set `display: "grid"` with `gridTemplateColumns`
- The Styles agent will handle the actual style values

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
        super().__init__("Layout", model_tier="balanced")
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
            
            # Phase 2: Generate with Sonnet (in batches if needed)
            if progress:
                await progress.agent_thinking(
                    "Layout", 
                    "Generating layout structure (Phase 2)..."
                )
            
            layout_plan_data = layout_plan.get("layout_plan", {})
            sections = layout_plan_data.get("sections", [])
            
            # Check if we need batching
            if len(sections) <= self.generator.BATCH_SIZE:
                # Single batch - generate all at once
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
            else:
                # Multiple batches
                return await self._generate_in_batches(
                    sections, layout_plan, input, progress
                )
            
        except Exception as e:
            logger.error(f"Layout agent error: {e}")
            return AgentOutput(
                agent_name="Layout",
                success=False,
                result={},
                errors=[str(e)]
            )
    
    async def _generate_in_batches(
        self,
        sections: List[Dict],
        full_layout_plan: Dict,
        input: AgentInput,
        progress: Optional[ProgressCallback]
    ) -> AgentOutput:
        """Generate layout in batches to avoid token limits"""
        
        batches = [
            sections[i:i+self.generator.BATCH_SIZE] 
            for i in range(0, len(sections), self.generator.BATCH_SIZE)
        ]
        
        logger.info(f"Generating {len(sections)} layout sections in {len(batches)} batches")
        
        all_components = {}
        root_component = None
        
        for i, batch in enumerate(batches):
            if progress:
                await progress.agent_thinking(
                    "Layout",
                    f"Generating batch {i+1}/{len(batches)} ({len(batch)} sections)..."
                )
            
            # Create layout plan for just this batch
            batch_layout_plan = {
                "reasoning": full_layout_plan.get("reasoning", ""),
                "layout_plan": {
                    "rootKey": full_layout_plan.get("layout_plan", {}).get("rootKey", "pageRoot"),
                    "sections": batch,
                    "responsive_notes": full_layout_plan.get("layout_plan", {}).get("responsive_notes", "")
                },
                "_batch_info": f"Batch {i+1}/{len(batches)}"
            }
            
            gen_input = AgentInput(
                user_request=input.user_request,
                context=input.context,
                previous_outputs={"layout_plan": batch_layout_plan}
            )
            
            gen_result = await self.generator.execute(gen_input, progress)
            
            if gen_result.success and gen_result.result:
                # Merge batch results
                batch_components = gen_result.result.get("componentDefinition", {})
                all_components.update(batch_components)
                
                # Keep root component from first batch
                if i == 0:
                    root_component = gen_result.result.get("rootComponent")
            else:
                logger.warning(f"Layout batch {i+1} generation failed: {gen_result.errors}")
        
        # Build final result
        result = {
            "rootComponent": root_component or "pageRoot",
            "componentDefinition": all_components
        }
        
        return AgentOutput(
            agent_name="Layout",
            success=True,
            result=result,
            reasoning=f"Generated {len(all_components)} layout sections in {len(batches)} batches"
            )
