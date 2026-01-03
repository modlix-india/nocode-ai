"""Styles Agent - Handles visual styling and theming with Batching"""
from typing import List, Dict, Any, Optional
from app.agents.base import BaseAgent, AgentInput, AgentOutput
from app.streaming.events import ProgressCallback
from app.config import settings
import json
import logging

logger = logging.getLogger(__name__)


class StylesAgent(BaseAgent):
    """
    Specializes in visual styling and design.
    
    Uses Haiku model for faster, cheaper style generation.
    Styles are simpler CSS-like outputs that don't need heavy reasoning.
    
    Supports batching for large pages to avoid token limits.
    """
    
    BATCH_SIZE = 12  # Max components per generation call
    
    def __init__(self):
        # Use Haiku for simpler style generation
        super().__init__("Styles", model_tier="fast")
    
    def get_system_prompt(self) -> str:
        return """You are a Styles Agent for the Nocode UI system.

Your responsibility is to create beautiful, consistent visual styles:
- Colors and color schemes
- Typography (fonts, sizes, weights)
- Spacing (padding, margins)
- Borders and shadows
- Background styles
- Theme integration

## CRITICAL: ONE STYLE ENTRY PER COMPONENT

**IMPORTANT:** Each component can only have ONE style entry (one styleId). Multiple style entries will overwrite each other!

Put ALL styles for a component under a SINGLE styleId (use "rootStyle" as the default):

```json
{
  "componentStyles": {
    "myButton": {
      "rootStyle": {
        "resolutions": {
          "ALL": {
            "backgroundColor": { "value": "#4F46E5" },
            "color": { "value": "#FFFFFF" },
            "padding": { "value": "12px 24px" },
            "backgroundColor:hover": { "value": "#4338CA" }
          },
          "MOBILE_POTRAIT_SCREEN": {
            "padding": { "value": "8px 16px" }
          }
        }
      }
    }
  }
}
```

## Style Properties Structure

Styles are applied via `styleProperties` on each component definition.
The structure uses **resolutions** for responsive design and **pseudo-state suffixes** for interactions.

### Screen Size Resolutions (from smallest to largest):
- `MOBILE_POTRAIT_SCREEN` - Mobile portrait (320px+)
- `MOBILE_LANDSCAPE_SCREEN` - Mobile landscape (480px+)
- `TABLET_POTRAIT_SCREEN` - Tablet portrait (768px+)
- `TABLET_LANDSCAPE_SCREEN` - Tablet landscape (1024px+)
- `DESKTOP_SCREEN` - Desktop (1280px+)
- `WIDE_SCREEN` - Wide screens (1920px+)
- `ALL` - Base styles that apply to all screen sizes (ALWAYS include this)

### Pseudo-State Suffixes:
Add pseudo-states as suffixes with colon `:` directly on property names:
- `backgroundColor:hover` - Mouse hover state
- `outline:focus` - Keyboard focus state
- `transform:active` - Active/pressed state
- `opacity:disabled` - Disabled state

Example:
```json
{
  "rootStyle": {
    "resolutions": {
      "ALL": {
        "backgroundColor": { "value": "#4F46E5" },
        "backgroundColor:hover": { "value": "#4338CA" },
        "transform:hover": { "value": "translateY(-1px)" },
        "outline:focus": { "value": "2px solid #818cf8" }
      }
    }
  }
}
```

### Sub-Component Styles:
For components with sub-components, prefix with component name and dash `-`:
```json
{
  "rootStyle": {
    "resolutions": {
      "ALL": {
        "icon-color": { "value": "#6366f1" },
        "icon-fontSize": { "value": "20px" },
        "label-fontWeight": { "value": "600" },
        "label-color:hover": { "value": "#4338CA" }
      }
    }
  }
}
```

### Your Output Format:
```json
{
  "reasoning": "Explanation of design decisions",
  "componentStyles": {
    "<componentKey>": {
      "rootStyle": {
        "resolutions": {
          "ALL": {
            "property": { "value": "value" },
            "property:hover": { "value": "hover-value" }
          }
        }
      }
    }
  }
}
```

## Design Guidelines
1. Use a consistent color palette
2. Ensure sufficient contrast for accessibility (WCAG AA: 4.5:1 for text)
3. Use appropriate font sizes (16px base, 14px minimum)
4. Add hover/focus states for ALL interactive elements
5. Use spacing consistently (8px grid: 4px, 8px, 12px, 16px, 24px, 32px)
6. Consider responsive breakpoints for mobile-first design

## Theme Variables
Reference theme variables for consistency using bindings:
- `{ "location": "Theme.colorPrimary" }` instead of hardcoded colors
- Common theme paths: colorPrimary, colorSecondary, colorBackground, colorSurface, colorText

## IMPORT MODE: Override Default Styles

When `importMode=true` in context, you MUST override default component styles.
Components have default styles that will interfere. Always set these explicitly:

### For Grid containers:
- `display`, `flexDirection`, `alignItems`, `justifyContent`
- `padding`, `margin`, `gap`
- `backgroundColor`, `borderRadius`, `boxShadow`
- Reset: `border: "none"`, `outline: "none"`

### For Text:
- `fontSize`, `fontWeight`, `fontFamily`, `lineHeight`, `letterSpacing`
- `color`, `margin`, `padding`
- Reset: `textDecoration: "none"` (unless needed)

### For Button:
- `backgroundColor`, `color`, `border`, `borderRadius`
- `padding`, `fontSize`, `fontWeight`
- `cursor: "pointer"`
- Reset: `outline: "none"`, `boxShadow: "none"` (add custom if needed)

### For Image:
- `width`, `height`, `objectFit`
- Reset: `border: "none"`

## Rules
1. Create visually appealing, modern designs
2. Ensure accessibility (contrast, focus states, touch targets)
3. Use consistent spacing and sizing
4. Apply pseudo-states for ALL interactive elements
5. Use responsive styles when appropriate
6. **USE ONLY ONE styleId per component** (use "rootStyle")
7. **In import mode**: Always set explicit values to override defaults
"""
    
    def get_relevant_docs(self) -> List[str]:
        return [
            "05-style-system",
            "17-theme-definitions",
            "18-style-definitions",
            "15-examples-and-patterns",
            "22-component-reference"
        ]
    
    def _build_messages(self, input: AgentInput, rag_context: str) -> List[Dict]:
        """Override to filter components when batching and handle import mode"""
        import json
        
        # Get batch info if present
        batch_components = input.previous_outputs.get("_batch_components", [])
        batch_info = input.previous_outputs.get("_batch_info", "")
        
        # Check if this is import mode
        is_import_mode = input.context.get("mode") == "import" or input.context.get("importMode", False)
        
        # Build base messages using parent class
        messages = super()._build_messages(input, rag_context)
        
        # Prepare additional instructions
        additional_instructions = []
        
        # If batching, add instruction to only style specific components
        if batch_components and batch_info:
            additional_instructions.append(
                f"{batch_info}: IMPORTANT - Only generate styles for these specific components: {', '.join(batch_components)}. Do NOT style any other components."
            )
        
        # Check for inspired-by mode (has styleHints with overrideTheme)
        style_hints = input.context.get("styleHints", {})
        is_inspired_mode = style_hints.get("overrideTheme", False)
        
        # Handle inspired-by mode with explicit light/dark theme
        if is_inspired_mode and not is_import_mode:
            theme = style_hints.get("theme", "light")
            bg_color = style_hints.get("backgroundColor", "#ffffff")
            text_color = style_hints.get("textColor", "#1a1a1a")
            
            inspired_instructions = f"""
## INSPIRED-BY MODE: Use Clean {theme.upper()} Theme

### Theme Settings:
- Background Color: {bg_color}
- Text Color: {text_color}
- Theme: {theme.upper()}

### IMPORTANT:
- Use a CLEAN {theme} color scheme
- The root component (pageRoot) MUST have backgroundColor: "{bg_color}"
- Default text color should be {text_color}
- Sections can have accent colors, but avoid making everything dark
- Use good contrast between text and backgrounds
- Hero sections can have colored backgrounds but ensure text is readable
"""
            additional_instructions.append(inspired_instructions)
        
        # If import mode, add override instructions with EXACT values
        if is_import_mode:
            # Get EXACT computed styles from context (extracted from browser)
            exact_styles = input.context.get("exactStyles", {})
            computed_styles = input.context.get("computedStyles", {})
            
            # Prefer exactStyles, fallback to computedStyles
            styles_to_use = exact_styles if exact_styles else computed_styles
            
            # Get theme and colors
            theme = styles_to_use.get("theme", "light")
            bg_color = styles_to_use.get("backgroundColor", "#ffffff" if theme == "light" else "#0f0f0f")
            text_color = styles_to_use.get("textColor", "#000000" if theme == "light" else "#ffffff")
            font_family = styles_to_use.get("fontFamily", "Inter, -apple-system, sans-serif")
            
            # Get element-specific styles
            h1_styles = styles_to_use.get("h1Styles", {})
            h2_styles = styles_to_use.get("h2Styles", {})
            button_styles = styles_to_use.get("buttonStyles", {})
            
            # Also get from website analysis as backup
            website_analysis = input.previous_outputs.get("website_analysis", {})
            color_palette = website_analysis.get("colorPalette", {})
            
            import_instructions = f"""
## IMPORT MODE: USE EXACT CSS VALUES

### CRITICAL: Use these EXACT values extracted from the source website:

Theme: {theme.upper()}
Root Background: {bg_color} 
Default Text Color: {text_color}
Font Family: {font_family}

### Element-Specific Styles:
H1: {json.dumps(h1_styles) if h1_styles else 'Use default heading styles'}
H2: {json.dumps(h2_styles) if h2_styles else 'Use default heading styles'}
Button: {json.dumps(button_styles) if button_styles else 'Use theme button styles'}

### MANDATORY: Apply to Root Container
The ROOT component (first Grid) MUST have:
- backgroundColor: {{ "value": "{bg_color}" }}
- color: {{ "value": "{text_color}" }}
- fontFamily: {{ "value": "{font_family}" }}
- minHeight: {{ "value": "100vh" }}
- margin: {{ "value": "0" }}
- padding: {{ "value": "0" }}

### MANDATORY: Override Defaults for ALL Components
Every component must explicitly set:
- Grid: backgroundColor (use "{bg_color}" unless it's a section with different color)
- Text: color (use "{text_color}" unless highlighted), fontFamily, margin: "0"
- Button: backgroundColor, color, border: "none", cursor: "pointer"
"""
            if color_palette:
                import_instructions += f"\n\nColor Palette Reference: {json.dumps(color_palette)}"
            
            additional_instructions.append(import_instructions)
        
        # Apply additional instructions
        if additional_instructions:
            extra_text = "\n\n".join(additional_instructions)
            user_message = messages[0]["content"]
            if isinstance(user_message, str):
                messages[0]["content"] = user_message + "\n\n" + extra_text
            elif isinstance(user_message, list):
                # Handle multimodal content
                for item in user_message:
                    if isinstance(item, dict) and item.get("type") == "text":
                        item["text"] += "\n\n" + extra_text
                        break
        
        return messages
    
    def _get_component_keys(self, input: AgentInput) -> List[str]:
        """Extract all component keys from previous agent outputs"""
        component_keys = []
        
        # Get components from component agent output
        component_output = input.previous_outputs.get("component", {})
        if isinstance(component_output, dict):
            components = component_output.get("components", {})
            if isinstance(components, dict):
                component_keys.extend(components.keys())
        
        # Get components from layout agent output
        layout_output = input.previous_outputs.get("layout", {})
        if isinstance(layout_output, dict):
            comp_def = layout_output.get("componentDefinition", {})
            if isinstance(comp_def, dict):
                component_keys.extend(comp_def.keys())
        
        # Get components from existing page
        existing_page = input.context.get("existingPage", {})
        if isinstance(existing_page, dict):
            comp_def = existing_page.get("componentDefinition", {})
            if isinstance(comp_def, dict):
                component_keys.extend(comp_def.keys())
        
        # Remove duplicates while preserving order
        seen = set()
        unique_keys = []
        for key in component_keys:
            if key not in seen:
                seen.add(key)
                unique_keys.append(key)
        
        return unique_keys
    
    async def execute(
        self, 
        input: AgentInput,
        progress: Optional[ProgressCallback] = None
    ) -> AgentOutput:
        """Execute with batching for large pages"""
        
        # Get all component keys that need styling
        component_keys = self._get_component_keys(input)
        
        logger.info(f"Styles agent: found {len(component_keys)} components to style")
        
        # Check if we need batching
        if len(component_keys) <= self.BATCH_SIZE:
            # Single batch - use base class execute
            return await super().execute(input, progress)
        else:
            # Multiple batches
            return await self._generate_in_batches(component_keys, input, progress)
    
    async def _generate_in_batches(
        self,
        component_keys: List[str],
        input: AgentInput,
        progress: Optional[ProgressCallback]
    ) -> AgentOutput:
        """Generate styles in batches to avoid token limits"""
        
        batches = [
            component_keys[i:i+self.BATCH_SIZE] 
            for i in range(0, len(component_keys), self.BATCH_SIZE)
        ]
        
        logger.info(f"Generating styles for {len(component_keys)} components in {len(batches)} batches")
        
        all_component_styles = {}
        
        for i, batch in enumerate(batches):
            if progress:
                await progress.agent_thinking(
                    "Styles",
                    f"Generating batch {i+1}/{len(batches)} ({len(batch)} components)..."
                )
            
            # Create input with only this batch of components
            # We'll filter in the prompt by mentioning which components to style
            batch_input = AgentInput(
                user_request=f"{input.user_request}\n\n[Batch {i+1}/{len(batches)}] Style these components: {', '.join(batch)}",
                context=input.context,
                previous_outputs={
                    **input.previous_outputs,
                    "_batch_components": batch,
                    "_batch_info": f"Batch {i+1}/{len(batches)}"
                }
            )
            
            batch_result = await super().execute(batch_input, progress)
            
            if batch_result.success and batch_result.result:
                # Merge batch results
                batch_styles = batch_result.result.get("componentStyles", {})
                all_component_styles.update(batch_styles)
            else:
                logger.warning(f"Styles batch {i+1} generation failed: {batch_result.errors}")
        
        return AgentOutput(
            agent_name="Styles",
            success=True,
            result={"componentStyles": all_component_styles},
            reasoning=f"Generated styles for {len(all_component_styles)} components in {len(batches)} batches"
        )

