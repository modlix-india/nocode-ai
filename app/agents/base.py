"""Base Agent class for all specialized agents"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List
from pydantic import BaseModel
import anthropic
import json
import logging
import asyncio
from app.config import settings
from app.rag.retriever import retrieve_context
from app.streaming.events import ProgressCallback

logger = logging.getLogger(__name__)

# Keepalive interval in seconds (send keepalive every 10 seconds)
KEEPALIVE_INTERVAL = 10


class AgentInput(BaseModel):
    """Input to an agent"""
    user_request: str
    context: Dict[str, Any] = {}
    previous_outputs: Dict[str, Any] = {}


class AgentOutput(BaseModel):
    """Output from an agent"""
    agent_name: str
    success: bool
    result: Dict[str, Any]
    reasoning: Optional[str] = None
    errors: List[str] = []


class BaseAgent(ABC):
    """
    Base class for all specialized agents.
    
    Each agent:
    1. Has a specific responsibility (layout, events, styles, etc.)
    2. Gets relevant documentation via RAG
    3. Calls Claude to generate its portion
    4. Returns structured output
    
    Multi-Model Strategy:
    - HAIKU: Fast, cheap - for analysis, planning, simple tasks
    - SONNET: Balanced - for complex generation
    """
    
    def __init__(self, name: str, model: str = None):
        """
        Initialize the agent.
        
        Args:
            name: Agent name for logging/progress
            model: Claude model to use (defaults to CLAUDE_SONNET)
        """
        self.name = name
        self.model = model or settings.CLAUDE_SONNET
        self.client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    
    @abstractmethod
    def get_system_prompt(self) -> str:
        """Return the agent-specific system prompt"""
        pass
    
    @abstractmethod
    def get_relevant_docs(self) -> List[str]:
        """Return list of doc sections to retrieve for context"""
        pass
    
    async def execute(
        self, 
        input: AgentInput,
        progress: Optional[ProgressCallback] = None
    ) -> AgentOutput:
        """Execute the agent's task"""
        try:
            # Emit thinking progress
            if progress:
                await progress.agent_thinking(
                    self.name, 
                    "Retrieving relevant documentation..."
                )
            
            # Get RAG context for this agent's domain
            # Increased top_k to 10 for better context coverage
            rag_context = await retrieve_context(
                query=input.user_request,
                filter_docs=self.get_relevant_docs(),
                top_k=10
            )
            
            if progress:
                await progress.agent_thinking(
                    self.name,
                    f"Generating {self.name.lower()} structure..."
                )
            
            # Build messages
            messages = self._build_messages(input, rag_context)
            
            # Call Claude with keepalives to prevent timeout
            response = await self._call_claude_with_keepalive(
                messages, progress
            )
            
            # Parse response
            response_text = response.content[0].text
            result = self._parse_response(response_text)
            reasoning = self._extract_reasoning(response_text)
            
            return AgentOutput(
                agent_name=self.name,
                success=True,
                result=result,
                reasoning=reasoning
            )
            
        except Exception as e:
            logger.error(f"{self.name} agent error: {e}")
            return AgentOutput(
                agent_name=self.name,
                success=False,
                result={},
                errors=[str(e)]
            )
    
    async def _call_claude_with_keepalive(
        self,
        messages: List[Dict],
        progress: Optional[ProgressCallback] = None
    ):
        """
        Call Claude API while sending keepalives to prevent connection timeout.
        Runs the synchronous API call in a thread pool while sending periodic keepalives.
        
        Features:
        - Prompt Caching: Reduces token usage by ~90% for repeated system prompts
        - Keepalive: Prevents gateway timeout during long API calls
        """
        # Create task for the Claude API call (runs in thread pool)
        # Use higher max_tokens for Review agent which outputs complete pages
        max_tokens = 16384 if self.name == "Review" else 8192
        
        logger.debug(f"{self.name} agent using model: {self.model}")
        
        # Build system prompt with caching if enabled
        system_prompt = self._get_system_prompt_with_caching()
        
        api_task = asyncio.create_task(
            asyncio.to_thread(
                self.client.messages.create,
                model=self.model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=messages
            )
        )
        
        # Send keepalives while waiting for the API response
        elapsed = 0
        while not api_task.done():
            try:
                # Wait for task with timeout
                await asyncio.wait_for(asyncio.shield(api_task), timeout=KEEPALIVE_INTERVAL)
                break  # Task completed
            except asyncio.TimeoutError:
                # Task still running, send keepalive
                elapsed += KEEPALIVE_INTERVAL
                if progress:
                    await progress.keepalive(f"{self.name} working... {elapsed}s")
        
        # Return the result
        return await api_task
    
    def _get_system_prompt_with_caching(self):
        """
        Get system prompt with Anthropic prompt caching enabled.
        
        Anthropic's prompt caching allows caching of prefixes in the system prompt,
        which significantly reduces token usage for repeated calls with the same
        system prompt. The cache lasts for 5 minutes.
        
        Benefits:
        - Up to 90% reduction in input tokens
        - Faster response times
        - Lower costs
        
        Ref: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
        """
        prompt_text = self.get_system_prompt()
        
        if not settings.PROMPT_CACHING_ENABLED:
            # Return plain string if caching disabled
            return prompt_text
        
        # Return structured prompt with cache control
        # The entire system prompt will be cached for 5 minutes
        return [
            {
                "type": "text",
                "text": prompt_text,
                "cache_control": {"type": "ephemeral"}
            }
        ]
    
    def _build_messages(self, input: AgentInput, rag_context: str) -> List[Dict]:
        """Build the message list for Claude"""
        
        # Format previous outputs
        prev_outputs_text = ""
        if input.previous_outputs:
            prev_outputs_text = "\n".join([
                f"### {name} Output:\n```json\n{json.dumps(output, indent=2)}\n```"
                for name, output in input.previous_outputs.items()
                if output
            ])
        
        # Format existing page context
        existing_page_text = ""
        if input.context.get("existingPage"):
            existing_page_text = f"""
## Existing Page (for modification)
```json
{json.dumps(input.context['existingPage'], indent=2)}
```
"""
        
        # Format selected component context
        selected_component_text = ""
        if input.context.get("selectedComponentKey"):
            selected_component_text = f"""
## Target Component
The user is focused on modifying component with key: `{input.context['selectedComponentKey']}`
"""
            if input.context.get("selectedComponent"):
                selected_component_text += f"""
Current component definition:
```json
{json.dumps(input.context['selectedComponent'], indent=2)}
```
"""
        
        # Build the user text content
        user_text = f"""
## User Request
{input.user_request}

## Mode
{input.context.get('mode', 'create')}

{selected_component_text}

{existing_page_text}

## Relevant Documentation
{rag_context if rag_context else "No additional documentation available."}

## Previous Agent Outputs
{prev_outputs_text if prev_outputs_text else "This is the first agent in the pipeline."}

## Your Task
Generate the {self.name} portion of the page definition.
Output valid JSON only, wrapped in ```json code blocks.
Include a brief "reasoning" field explaining your decisions.
"""
        
        # Check if we have a screenshot for visual feedback (Claude Vision)
        if input.context.get("componentScreenshot"):
            # Use multimodal content for Claude Vision
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": input.context["componentScreenshot"]
                    }
                },
                {
                    "type": "text",
                    "text": f"""
This is a screenshot of the current component rendering. 
The user wants to refine it based on visual feedback.

{user_text}
"""
                }
            ]
            return [{"role": "user", "content": content}]
        else:
            return [{"role": "user", "content": user_text}]
    
    def _parse_response(self, response: str) -> Dict[str, Any]:
        """Parse the LLM response into structured output"""
        try:
            # Try to find JSON block
            if "```json" in response:
                start = response.find("```json") + 7
                end = response.find("```", start)
                json_str = response[start:end].strip()
            elif "```" in response:
                start = response.find("```") + 3
                end = response.find("```", start)
                json_str = response[start:end].strip()
            else:
                # Try to find raw JSON
                start = response.find("{")
                end = response.rfind("}") + 1
                json_str = response[start:end]
            
            return json.loads(json_str)
            
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse {self.name} response as JSON: {e}")
            return {"error": "Failed to parse response", "raw": response[:500]}
    
    def _extract_reasoning(self, response: str) -> Optional[str]:
        """Extract reasoning from response if present"""
        try:
            parsed = self._parse_response(response)
            if isinstance(parsed, dict) and "reasoning" in parsed:
                return parsed.pop("reasoning", None)
        except:
            pass
        return None

