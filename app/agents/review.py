"""Review Agent - Basic validation without LLM"""
from typing import List, Dict, Any, Optional
from app.agents.base import BaseAgent, AgentInput, AgentOutput
from app.streaming.events import ProgressCallback
import json
import logging

logger = logging.getLogger(__name__)


class ReviewAgent(BaseAgent):
    """
    Reviews and validates the merged page output.
    
    Performs basic validation without LLM calls:
    - Checks if all agent steps executed successfully
    - Validates page structure
    - Fixes onClick property format to ensure it's {"value": "eventKey"}
    """
    
    def __init__(self):
        # Don't need model tier for basic validation
        super().__init__("Review", model_tier="balanced")
    
    def get_system_prompt(self) -> str:
        """Not used for basic validation, but required by base class"""
        return ""
    
    def get_relevant_docs(self) -> List[str]:
        """Not used for basic validation"""
        return []
    
    def _fix_onclick_properties(self, page: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fix onClick properties to ensure they follow the format:
        "onClick": {"value": "eventFunctionKey"}
        """
        fixed_page = json.loads(json.dumps(page))  # Deep copy
        
        component_def = fixed_page.get("componentDefinition", {})
        
        for comp_key, comp in component_def.items():
            if not isinstance(comp, dict):
                continue
            
            properties = comp.get("properties", {})
            if not isinstance(properties, dict):
                continue
            
            # Fix onClick property
            if "onClick" in properties:
                onclick_value = properties["onClick"]
                
                # If it's already in correct format, skip
                if isinstance(onclick_value, dict) and "value" in onclick_value:
                    # Check if value is a string (correct) or array (wrong)
                    if isinstance(onclick_value["value"], list):
                        # Take first element if it's an array
                        if len(onclick_value["value"]) > 0:
                            properties["onClick"] = {"value": str(onclick_value["value"][0])}
                        else:
                            # Empty array, remove onClick
                            del properties["onClick"]
                    elif isinstance(onclick_value["value"], str):
                        # Already correct format
                        pass
                    else:
                        # Convert to string
                        properties["onClick"] = {"value": str(onclick_value["value"])}
                elif isinstance(onclick_value, list):
                    # Array format - take first element
                    if len(onclick_value) > 0:
                        properties["onClick"] = {"value": str(onclick_value[0])}
                    else:
                        # Empty array, remove onClick
                        del properties["onClick"]
                elif isinstance(onclick_value, str):
                    # Direct string - wrap in value object
                    properties["onClick"] = {"value": onclick_value}
                else:
                    # Unknown format, try to convert to string
                    properties["onClick"] = {"value": str(onclick_value)}
        
        return fixed_page
    
    def _validate_page_structure(self, page: Dict[str, Any]) -> tuple[bool, List[str]]:
        """
        Basic validation of page structure.
        Returns (is_valid, list_of_issues)
        """
        issues = []
        
        # Check rootComponent
        if "rootComponent" not in page:
            issues.append("Missing rootComponent")
        elif not isinstance(page["rootComponent"], str):
            issues.append("rootComponent must be a string (component key)")
        
        # Check componentDefinition
        if "componentDefinition" not in page:
            issues.append("Missing componentDefinition")
        elif not isinstance(page["componentDefinition"], dict):
            issues.append("componentDefinition must be a dictionary")
        else:
            comp_def = page["componentDefinition"]
            
            # Check if rootComponent exists in componentDefinition
            root_comp = page.get("rootComponent")
            if root_comp and root_comp not in comp_def:
                issues.append(f"rootComponent '{root_comp}' not found in componentDefinition")
            
            # Validate each component
            for comp_key, comp in comp_def.items():
                if not isinstance(comp, dict):
                    issues.append(f"Component '{comp_key}' is not a dictionary")
                    continue
                
                # Check key matches
                if comp.get("key") != comp_key:
                    issues.append(f"Component '{comp_key}' has mismatched key: {comp.get('key')}")
                
                # Check type
                if "type" not in comp:
                    issues.append(f"Component '{comp_key}' missing type")
                
                # Check children format (should be { "childKey": true })
                if "children" in comp:
                    children = comp["children"]
                    if isinstance(children, dict):
                        for child_key, child_value in children.items():
                            if child_value is not True:
                                issues.append(f"Component '{comp_key}' has invalid child reference: '{child_key}' should be true, got {type(child_value).__name__}")
        
        return len(issues) == 0, issues
    
    async def execute(
        self, 
        input: AgentInput,
        progress: Optional[ProgressCallback] = None
    ) -> AgentOutput:
        """Execute basic validation without LLM"""
        
        if progress:
            await progress.agent_thinking(
                self.name,
                "Validating page structure and fixing onClick properties..."
            )
        
        merged_page = input.context.get("merged_page", {})
        
        if not merged_page:
            return AgentOutput(
                agent_name=self.name,
                success=False,
                result={},
                errors=["No merged page to review"]
            )
        
        # Check if previous agents succeeded
        previous_outputs = input.previous_outputs
        failed_agents = []
        
        for agent_name, output in previous_outputs.items():
            if not output:
                failed_agents.append(f"{agent_name} (no output)")
            elif isinstance(output, dict) and output.get("error"):
                failed_agents.append(f"{agent_name} ({output.get('error')})")
        
        if failed_agents:
            logger.warning(f"Some agents failed: {failed_agents}")
            # Continue anyway - we'll validate what we have
        
        # Fix onClick properties
        fixed_page = self._fix_onclick_properties(merged_page)
        
        # Validate structure
        is_valid, issues = self._validate_page_structure(fixed_page)
        
        if issues:
            logger.warning(f"Page structure issues found: {issues}")
        
        # Build reasoning
        reasoning_parts = []
        if failed_agents:
            reasoning_parts.append(f"Note: Some agents had issues: {', '.join(failed_agents)}")
        if issues:
            reasoning_parts.append(f"Found {len(issues)} structure issues: {', '.join(issues[:5])}")
        else:
            reasoning_parts.append("Page structure validated successfully")
        reasoning_parts.append("Fixed onClick properties to ensure correct format: {'value': 'eventKey'}")
        
        reasoning = ". ".join(reasoning_parts) + "."
        
        return AgentOutput(
            agent_name=self.name,
            success=is_valid and len(failed_agents) == 0,
            result=fixed_page,
            reasoning=reasoning,
            errors=issues if issues else []
        )
