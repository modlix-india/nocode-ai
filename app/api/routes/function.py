"""Function AI API routes - Explain and Modify functions"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
import json
import logging

from app.services.security import require_ai_access
from app.api.models.auth import ContextAuthentication
from app.services.llm_provider import get_llm_provider
from app.rag.retriever import retrieve_context
from app.services.js2kirun import KIRun2JSConverter, preserve_step_names

logger = logging.getLogger(__name__)

router = APIRouter()

# RAG document filters for function-related queries
FUNCTION_EXPLAIN_DOCS = [
    "07-event-system",
    "08-functions-and-actions", 
    "14-api-reference"
]

FUNCTION_MODIFY_DOCS = [
    "07-event-system",
    "08-functions-and-actions",
    "23-javascript-event-guide",
    "14-api-reference",
    "15-examples-and-patterns"
]


class FunctionExplainRequest(BaseModel):
    """Request to explain a function"""
    functionDefinition: Dict[str, Any]
    functionName: str


class FunctionExplainResponse(BaseModel):
    """Response with function explanation"""
    success: bool
    explanation: str
    summary: str
    steps: List[Dict[str, str]]


class FunctionModifyRequest(BaseModel):
    """Request to modify a function"""
    instruction: str
    functionDefinition: Dict[str, Any]
    functionName: str
    pageContext: Optional[Dict[str, Any]] = None


class FunctionModifyResponse(BaseModel):
    """Response with modified function"""
    success: bool
    functionDefinition: Dict[str, Any]
    reasoning: str
    changes: List[str]


def get_explain_system_prompt() -> str:
    """System prompt for explaining KIRun functions"""
    return """You are an expert at explaining KIRun function definitions.

KIRun is a visual programming system where functions are defined as JSON with:
- `name`: Function name
- `steps`: Dictionary of step definitions, each containing:
  - `statementName`: Unique step identifier
  - `namespace`: Function namespace (e.g., "UIEngine", "System", "System.Math")
  - `name`: Function being called (e.g., "SetStore", "Navigate", "Wait")
  - `parameterMap`: Parameters passed to the function
  - `dependentStatements`: Steps that must complete before this one
  - `position`: Visual position in the editor

You will receive relevant documentation about available functions via RAG.
Use this documentation to understand what each step does.

Your task is to explain what the function does in plain English.

Output JSON format:
```json
{
  "summary": "One sentence summary of what the function does",
  "explanation": "Detailed explanation of the function's behavior",
  "steps": [
    {"name": "stepName", "description": "What this step does"}
  ]
}
```"""


def get_modify_system_prompt() -> str:
    """System prompt for modifying KIRun functions using JavaScript"""
    return """You are an expert at modifying KIRun functions.

You will receive:
1. An existing KIRun function definition
2. A user instruction for what to change
3. RAG documentation about available operations

Your task: Generate JavaScript code that represents the MODIFIED function logic.
The JavaScript will be automatically converted to KIRun format.

## CRITICAL: Preserve Step Comments

The existing code has `// Step: stepName` comments at the end of each line.
These comments are ESSENTIAL for maintaining step references. You MUST:

1. **PRESERVE** all existing `// Step: stepName` comments exactly as they are
2. **ADD** a `// Step: newStepName` comment for any NEW steps you create
3. **KEEP** the same step name in comments even if you modify the line's logic

Example:
- Original: `Page.x = 1;  // Step: initValue`
- Modified: `Page.x = 2;  // Step: initValue`  (KEEP the comment, value changed)
- New step: `wait(5000);  // Step: addedWait`  (ADD comment for new steps)

## JavaScript Rules

1. **NO local variables** - Use Page.* or Store.* for all state
2. **Use simple assignments**: `Page.x = value;`
3. **For API calls**: Use `fetch()` then access `Steps.stepName.output.data`
4. **For navigation**: Use `navigate("/path")`
5. **For conditionals**: Use `if (Page.x) { ... } else { ... }`
6. **For waiting/delays**: Use `wait(milliseconds)` - e.g., `wait(5000)` for 5 seconds
7. **For showing messages**: Use `showMessage("text", "TYPE")`

## Output Format

```json
{
  "reasoning": "What you changed and why",
  "changes": ["List of specific changes made"],
  "javascriptCode": "Page.value = 0;  // Step: initValue\\nwait(5000);  // Step: waitStep\\nfetch('/api/data');  // Step: fetchData"
}
```

## Examples

### Add a print step (preserving existing comments)
```javascript
fetch('/api/data');  // Step: fetchData
console.log("API called");  // Step: printLog  <-- NEW step with comment
if (Steps.fetchData.output.success) { ... }  // Step: checkSuccess
Page.result = Steps.fetchData.output.data;  // Step: setResult
```

### Modify existing step (preserve its comment)
```javascript
// Original:  wait(5000);  // Step: waitBeforeApi
// Modified:  wait(10000);  // Step: waitBeforeApi  <-- Same comment, different value
```

## What NOT to Do
- NO local variables (const, let, var)
- NO arrow functions
- NO destructuring
- NO Promise.all or complex async patterns
- NO closures
- DO NOT remove or change existing `// Step: xxx` comments

Analyze the existing function, understand its logic, then output the COMPLETE modified function as JavaScript with ALL step comments preserved.
"""


@router.post("/explain", response_model=FunctionExplainResponse)
async def explain_function(
    request: FunctionExplainRequest,
    auth: ContextAuthentication = Depends(require_ai_access)
):
    """
    Explain a KIRun function definition.
    
    Uses AI with RAG to generate a human-readable explanation of what the function does.
    """
    try:
        provider = get_llm_provider()
        
        # Get RAG context for understanding KIRun functions
        rag_context = await retrieve_context(
            query=f"Explain KIRun function steps: {request.functionName}",
            filter_docs=FUNCTION_EXPLAIN_DOCS,
            top_k=5
        )
        
        # Build user message with function definition
        user_message = f"""Please explain this KIRun function:

Function Name: {request.functionName}

Function Definition:
```json
{json.dumps(request.functionDefinition, indent=2)}
```

## Relevant Documentation
{rag_context if rag_context else "No additional documentation available."}

Provide a clear explanation of what this function does, including a summary, detailed explanation, and breakdown of each step."""

        # Call LLM
        response = await provider.create_completion(
            messages=[{"role": "user", "content": user_message}],
            system_prompt=get_explain_system_prompt(),
            model_tier="fast",  # Use fast model for explanations
            max_tokens=2000
        )
        
        # Parse response
        response_text = response.get("content", "")
        
        try:
            # Extract JSON from response
            if "```json" in response_text:
                start = response_text.find("```json") + 7
                end = response_text.find("```", start)
                json_str = response_text[start:end].strip()
            elif "```" in response_text:
                start = response_text.find("```") + 3
                end = response_text.find("```", start)
                json_str = response_text[start:end].strip()
            else:
                start = response_text.find("{")
                end = response_text.rfind("}") + 1
                json_str = response_text[start:end]
            
            result = json.loads(json_str)
            
            return FunctionExplainResponse(
                success=True,
                summary=result.get("summary", ""),
                explanation=result.get("explanation", ""),
                steps=result.get("steps", [])
            )
        except json.JSONDecodeError:
            # If JSON parsing fails, return raw text as explanation
            return FunctionExplainResponse(
                success=True,
                summary="Function explanation",
                explanation=response_text,
                steps=[]
            )
            
    except Exception as e:
        logger.error(f"Error explaining function: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Global converter instance for reuse
_kirun2js_converter = KIRun2JSConverter()


def _kirun_to_pseudo_js(func_def: Dict[str, Any]) -> str:
    """
    Convert a KIRun function definition to pseudo-JavaScript for LLM understanding.
    Uses the template-based KIRun2JSConverter for accurate representation.
    """
    result = _kirun2js_converter.convert(func_def)
    logger.info(f"[KIRun->JS] Input steps: {list(func_def.get('steps', {}).keys())}")
    logger.info(f"[KIRun->JS] Output JS:\n{result}")
    return result


def _convert_js_to_kirun(js_code: str, func_name: str, original_func: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert JavaScript code to KIRun format using the JS2KIRun converter.
    
    Preserves:
    - Original step names by parsing '// Step: stepName' comments
    - Original step positions from the original function definition
    
    Falls back to returning original function if conversion fails.
    """
    logger.info(f"[JS->KIRun] Input JS code:\n{js_code}")
    logger.info(f"[JS->KIRun] Original steps: {list(original_func.get('steps', {}).keys())}")
    
    try:
        from app.services.js2kirun.converter import EventFunctionConverter
        
        converter = EventFunctionConverter()
        kirun_def = converter.convert(js_code, func_name)
        
        logger.info(f"[JS->KIRun] Converted steps (before preserve): {list(kirun_def.get('steps', {}).keys())}")
        
        # Preserve step names from JS comments AND positions from original function
        # This parses '// Step: stepName' comments and remaps step names,
        # then copies positions from original_func for matching steps
        kirun_def = preserve_step_names(js_code, kirun_def, original_func)
        
        logger.info(f"[JS->KIRun] Final steps (after preserve): {list(kirun_def.get('steps', {}).keys())}")
        
        # Preserve original function metadata
        kirun_def["name"] = original_func.get("name", func_name)
        if "namespace" in original_func:
            kirun_def["namespace"] = original_func["namespace"]
        
        logger.info(f"Successfully converted JS to KIRun for function '{func_name}'")
        return kirun_def
        
    except ImportError as e:
        logger.error(f"JS2KIRun converter not available: {e}")
        raise ValueError(f"Converter not available: {e}")
    except Exception as e:
        logger.error(f"Failed to convert JS to KIRun: {e}", exc_info=True)
        raise ValueError(f"Conversion failed: {e}")


@router.post("/modify", response_model=FunctionModifyResponse)
async def modify_function(
    request: FunctionModifyRequest,
    auth: ContextAuthentication = Depends(require_ai_access)
):
    """
    Modify a KIRun function based on natural language instruction.
    
    Uses:
    1. RAG to retrieve relevant documentation
    2. LLM to generate modified JavaScript
    3. JS-to-KIRun converter to produce the final function
    """
    try:
        provider = get_llm_provider()
        
        # Get relevant RAG context
        rag_context = await retrieve_context(
            query=request.instruction,
            filter_docs=FUNCTION_MODIFY_DOCS,
            top_k=8
        )
        
        # Convert existing function to pseudo-JS for LLM understanding
        existing_js = _kirun_to_pseudo_js(request.functionDefinition)
        
        # Build context about available store paths
        context_info = ""
        if request.pageContext:
            if request.pageContext.get("storePaths"):
                context_info += f"\n\nAvailable store paths: {', '.join(request.pageContext['storePaths'][:20])}"
            if request.pageContext.get("componentDefinition"):
                comp_keys = list(request.pageContext["componentDefinition"].keys())[:10]
                context_info += f"\n\nComponents on page: {', '.join(comp_keys)}"
        
        # Build user message
        user_message = f"""Modify this function according to the instruction.

## Instruction
{request.instruction}

## Current Function: {request.functionName}

### Existing Logic (as JavaScript):
```javascript
{existing_js}
```

### Original KIRun Definition:
```json
{json.dumps(request.functionDefinition, indent=2)}
```
{context_info}

## Reference Documentation
{rag_context if rag_context else "No additional documentation available."}

## Task
1. Understand the current function logic
2. Apply the requested changes
3. Output the COMPLETE modified function as JavaScript code

Remember: Output JavaScript that will be converted to KIRun. Use wait(milliseconds) for delays, fetch() for API calls, navigate() for navigation."""

        # Call LLM with balanced model for modifications
        response = await provider.create_completion(
            messages=[{"role": "user", "content": user_message}],
            system_prompt=get_modify_system_prompt(),
            model_tier="balanced",  # Use balanced model for complex modifications
            max_tokens=4000
        )
        
        # Parse response
        response_text = response.get("content", "")
        
        try:
            # Extract JSON from response
            if "```json" in response_text:
                start = response_text.find("```json") + 7
                end = response_text.find("```", start)
                json_str = response_text[start:end].strip()
            elif "```" in response_text:
                start = response_text.find("```") + 3
                end = response_text.find("```", start)
                json_str = response_text[start:end].strip()
            else:
                start = response_text.find("{")
                end = response_text.rfind("}") + 1
                json_str = response_text[start:end]
            
            result = json.loads(json_str)
            
            # Get JavaScript code from response
            js_code = result.get("javascriptCode", "")
            logger.info(f"[LLM Response] Reasoning: {result.get('reasoning', 'N/A')[:200]}")
            logger.info(f"[LLM Response] JavaScript code:\n{js_code}")
            
            if not js_code:
                raise ValueError("No JavaScript code in response")
            
            # Convert JavaScript to KIRun
            func_def = _convert_js_to_kirun(
                js_code, 
                request.functionName, 
                request.functionDefinition
            )
            
            return FunctionModifyResponse(
                success=True,
                functionDefinition=func_def,
                reasoning=result.get("reasoning", ""),
                changes=result.get("changes", [])
            )
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Failed to parse/convert modify response: {e}")
            raise HTTPException(
                status_code=500, 
                detail=f"Failed to parse AI response: {str(e)}"
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error modifying function: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

