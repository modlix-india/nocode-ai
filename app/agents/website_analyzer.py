"""
Website Analyzer Agent - Analyzes visual data for semantic structure.

Works with multi-viewport extracted data to identify components and hierarchy.
"""
from typing import List, Dict, Any, Optional
from app.agents.base import BaseAgent, AgentInput, AgentOutput
from app.streaming.events import ProgressCallback
import json
import logging

logger = logging.getLogger(__name__)


class WebsiteAnalyzerAgent(BaseAgent):
    """
    Analyzes visual extraction data and produces Nocode component structure.
    
    Receives:
    - Screenshot (desktop viewport)
    - List of visual elements with computed styles at each viewport
    - Root styles per viewport
    - Uploaded image URLs
    
    Produces:
    - Component hierarchy with semantic types
    - Components have pre-computed responsive styleProperties
    """
    
    def __init__(self):
        super().__init__("WebsiteAnalyzer", model_tier="balanced")
    
    def get_system_prompt(self) -> str:
        return """You are a Visual Analyzer for converting website screenshots to Nocode UI components.

## Your Task
Analyze the visual structure of a website and output Nocode components.
You receive:
1. A SCREENSHOT of the website
2. EXTRACTED ELEMENTS with their computed styles at each viewport (desktop, tablet, mobile)
3. ROOT STYLES for the page body

## Output Format

```json
{
  "reasoning": "Brief description of the page structure",
  "rootComponent": "pageRoot",
  "components": [
    {
      "key": "uniqueKey",
      "name": "Human Readable Name",
      "type": "Grid|Text|Button|Image|TextBox",
      "isTopLevel": true,
      "properties": {
        "text": {"value": "Actual text content"},
        "src": {"value": "/path/to/uploaded/image.jpg"}
      },
      "styleProperties": {
        "rootStyle": {
          "resolutions": {
            "ALL": {
              "display": {"value": "flex"},
              "backgroundColor": {"value": "#000000"}
            },
            "TABLET_POTRAIT_SCREEN": {
              "fontSize": {"value": "24px"}
            },
            "MOBILE_POTRAIT_SCREEN": {
              "fontSize": {"value": "18px"}
            }
          }
        }
      },
      "children": {"childKey1": true, "childKey2": true}
    }
  ]
}
```

## Component Types (ONLY USE THESE)
- Grid: Containers, sections, divs, flexbox layouts
- Text: Headings (h1-h6), paragraphs, spans, labels
- Button: Buttons and styled action links
- Image: Images (use uploaded URL from context)
- TextBox: Input fields, textareas
- Checkbox, Dropdown, RadioButton: Form elements

## Style Property Rules
1. ONLY include resolutions with DIFFERENT values from desktop (ALL)
2. Use the EXACT computed styles provided - don't guess colors or fonts
3. Convert RGB values to hex format (#RRGGBB)
4. Keep only essential layout properties: display, flexDirection, justifyContent, alignItems, gap, padding, margin, width, height
5. Keep only essential visual properties: backgroundColor, color, fontSize, fontWeight, fontFamily, borderRadius

## Hierarchy Rules
1. Top-level components should have "isTopLevel": true
2. Child components should be listed in parent's "children" as {"childKey": true}
3. Create logical sections: header, hero, content, footer
4. Match the visual hierarchy from the screenshot

## Critical Instructions
1. Study the SCREENSHOT to understand the visual layout
2. Use EXACT text content from extracted elements
3. Use UPLOADED IMAGE URLs from context (not original URLs)
4. Apply responsive styles only when values DIFFER between viewports
5. Output valid JSON wrapped in ```json blocks
"""
    
    def get_relevant_docs(self) -> List[str]:
        return [
            "00-critical-rules",
            "03-component-system",
            "05-style-system",
            "22-component-reference"
        ]
    
    def _build_messages(self, input: AgentInput, rag_context: str) -> List[Dict]:
        """Build messages with visual data and screenshot"""
        
        visual_data = input.context.get("visualData", {})
        
        # Format elements for prompt (limit size)
        elements = visual_data.get("elements", [])
        elements_summary = self._format_elements(elements)
        
        # Get root styles
        root_styles = visual_data.get("rootStyles", {})
        
        # Get uploaded images mapping
        uploaded_images = visual_data.get("uploadedImages", {})
        
        user_text = f"""
## Website URL
{visual_data.get('url', 'Unknown')}

## Page Title
{visual_data.get('title', 'Unknown')}

## Root Styles (per viewport)
Desktop: {json.dumps(root_styles.get('desktop', {}), indent=2)}
Tablet: {json.dumps(root_styles.get('tablet', {}), indent=2)}
Mobile: {json.dumps(root_styles.get('mobile', {}), indent=2)}

## Uploaded Images (use these URLs in Image components)
{self._format_uploaded_images(uploaded_images)}

## Extracted Elements with Computed Styles
{elements_summary}

## Documentation Reference
{rag_context if rag_context else "No additional documentation."}

## Your Task
1. Study the screenshot to understand visual layout and hierarchy
2. Create components using the EXACT text from extracted elements
3. Apply the EXACT computed styles to each component
4. Use the UPLOADED image URLs (not original URLs)
5. Create responsive styles only when viewport values differ

Output valid JSON only, wrapped in ```json blocks.
"""
        
        # Check for screenshot
        screenshot = visual_data.get("screenshot", "")
        
        if screenshot:
            # Multimodal with screenshot
            image_content = self.provider.format_image_content(
                screenshot,
                media_type="image/png"
            )
            
            # Get theme from root styles
            theme = root_styles.get("desktop", {}).get("theme", "light")
            bg_color = root_styles.get("desktop", {}).get("backgroundColor", "")
            
            content = [
                image_content,
                {
                    "type": "text",
                    "text": f"""
## VISUAL REFERENCE: Desktop Screenshot

This is a {theme.upper()} theme website with background: {bg_color}

Use this screenshot to:
1. Understand the visual hierarchy
2. Identify sections (header, hero, content, footer)
3. Match text and image positions
4. Verify colors and layout

{user_text}
"""
                }
            ]
            return [{"role": "user", "content": content}]
        else:
            logger.warning("No screenshot available - using text-only analysis")
            return [{"role": "user", "content": user_text}]
    
    def _format_elements(self, elements: List[Dict], depth: int = 0, max_depth: int = 4) -> str:
        """Format extracted elements for the prompt (limited depth/count)"""
        if depth > max_depth or not elements:
            return ""
        
        lines = []
        indent = "  " * depth
        
        for i, elem in enumerate(elements[:15]):  # Max 15 elements per level
            tag = elem.get("tag", "div")
            text = elem.get("text", "")[:100]
            elem_id = elem.get("id", "")
            
            # Get desktop styles (primary)
            desktop_styles = elem.get("styles", {}).get("desktop", {})
            
            # Key styles to show
            key_styles = []
            for prop in ["display", "backgroundColor", "color", "fontSize", "fontWeight"]:
                val = desktop_styles.get(prop, "")
                if val and val not in ("none", "normal", "auto", "0px", "transparent"):
                    key_styles.append(f"{prop}:{val[:30]}")
            
            style_str = ", ".join(key_styles[:4]) if key_styles else "default"
            
            lines.append(f"{indent}[{tag}] id={elem_id[:20]} text=\"{text}\" styles=[{style_str}]")
            
            # Recurse into children
            children = elem.get("children", [])
            if children:
                child_text = self._format_elements(children, depth + 1, max_depth)
                if child_text:
                    lines.append(child_text)
        
        if len(elements) > 15:
            lines.append(f"{indent}... and {len(elements) - 15} more elements")
        
        return "\n".join(lines)
    
    def _format_uploaded_images(self, uploaded_images: Dict[str, str]) -> str:
        """Format uploaded images mapping"""
        if not uploaded_images:
            return "No images uploaded"
        
        lines = []
        for i, (original, uploaded) in enumerate(list(uploaded_images.items())[:10]):
            lines.append(f"- {i+1}. {uploaded}")
        
        if len(uploaded_images) > 10:
            lines.append(f"... and {len(uploaded_images) - 10} more images")
        
        return "\n".join(lines)
    
    async def execute(
        self, 
        input: AgentInput,
        progress: Optional[ProgressCallback] = None
    ) -> AgentOutput:
        """Execute visual analysis"""
        try:
            if progress:
                await progress.agent_thinking(
                    self.name,
                    "Analyzing visual structure..."
                )
            
            return await super().execute(input, progress)
            
        except Exception as e:
            logger.error(f"Website analyzer error: {e}")
            return AgentOutput(
                agent_name=self.name,
                success=False,
                result={},
                errors=[str(e)]
            )
