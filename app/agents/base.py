"""Base Agent class for all specialized agents"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List
from pydantic import BaseModel
import json
import logging
import asyncio
import time
from app.config import settings
from app.rag.retriever import retrieve_context
from app.streaming.events import ProgressCallback
from app.services.llm_provider import get_llm_provider, LLMProvider

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
    token_usage: Optional[Dict[str, Any]] = None  # Token usage from LLM call


class BaseAgent(ABC):
    """
    Base class for all specialized agents.
    
    Each agent:
    1. Has a specific responsibility (layout, events, styles, etc.)
    2. Gets relevant documentation via RAG
    3. Calls the configured LLM (Claude or GPT) to generate its portion
    4. Returns structured output
    
    Multi-Model Strategy:
    - FAST tier: Quick, cheap - for analysis, planning, simple tasks
      (Claude Haiku / GPT-4o-mini)
    - BALANCED tier: Capable - for complex generation
      (Claude Sonnet / GPT-4o)
    """
    
    def __init__(self, name: str, model_tier: str = "balanced"):
        """
        Initialize the agent.
        
        Args:
            name: Agent name for logging/progress
            model_tier: "fast" or "balanced" (maps to appropriate model per provider)
        """
        self.name = name
        self.model_tier = model_tier
        self._provider: Optional[LLMProvider] = None
    
    @property
    def provider(self) -> LLMProvider:
        """Lazy-load the LLM provider"""
        if self._provider is None:
            self._provider = get_llm_provider()
        return self._provider
    
    @abstractmethod
    def get_system_prompt(self) -> str:
        """Return the agent-specific system prompt"""
        pass
    
    @abstractmethod
    def get_relevant_docs(self) -> List[str]:
        """Return list of doc sections to retrieve for context"""
        pass
    
    def get_rag_query(self, user_request: str) -> str:
        """
        Return the query to use for RAG retrieval.
        Override this to customize the query for specific agents.
        
        By default, returns the user's request as-is.
        """
        return user_request
    
    async def execute(
        self,
        input: AgentInput,
        progress: Optional[ProgressCallback] = None
    ) -> AgentOutput:
        """Execute the agent's task"""
        start_time = time.time()
        try:
            # Emit thinking progress
            if progress:
                await progress.agent_thinking(
                    self.name,
                    "Retrieving relevant documentation..."
                )

            # Get RAG context for this agent's domain
            # Increased top_k to 10 for better context coverage
            # Use custom query if agent provides one
            rag_query = self.get_rag_query(input.user_request)
            rag_context = await retrieve_context(
                query=rag_query,
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

            # Call LLM with keepalives to prevent timeout
            response = await self._call_llm_with_keepalive(
                messages, progress
            )

            # Calculate latency
            latency_ms = int((time.time() - start_time) * 1000)

            # Extract token usage from response
            token_usage = None
            if "usage" in response:
                usage = response["usage"]
                token_usage = {
                    "agent_type": self.name,
                    "model": response.get("model", "unknown"),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                    "latency_ms": latency_ms,
                    "success": True,
                }

            # Parse response - response is now a dict with "content" key
            response_text = response["content"]
            result = self._parse_response(response_text)
            reasoning = self._extract_reasoning(response_text)

            return AgentOutput(
                agent_name=self.name,
                success=True,
                result=result,
                reasoning=reasoning,
                token_usage=token_usage
            )

        except Exception as e:
            logger.error(f"{self.name} agent error: {e}")
            latency_ms = int((time.time() - start_time) * 1000)
            return AgentOutput(
                agent_name=self.name,
                success=False,
                result={},
                errors=[str(e)],
                token_usage={
                    "agent_type": self.name,
                    "model": "unknown",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "latency_ms": latency_ms,
                    "success": False,
                    "error_message": str(e),
                }
            )
    
    async def _call_llm_with_keepalive(
        self,
        messages: List[Dict],
        progress: Optional[ProgressCallback] = None
    ) -> Dict[str, Any]:
        """
        Call LLM API while sending keepalives to prevent connection timeout.
        
        Uses the configured LLM provider (Anthropic or OpenAI).
        Runs the API call while sending periodic keepalives.
        
        Features:
        - Prompt Caching: Reduces token usage (Anthropic only)
        - Keepalive: Prevents gateway timeout during long API calls
        """
        # Use higher max_tokens for agents that output large structures
        # Review: outputs complete pages
        # LayoutGenerator/ComponentGenerator: can generate many components
        # WebsiteAnalyzer: analyzes entire websites with many components
        if self.name == "Review":
            max_tokens = 16384
        elif self.name in ["LayoutGenerator", "ComponentGenerator", "WebsiteAnalyzer"]:
            max_tokens = 16384  # Large layouts/components can need more tokens
        else:
            max_tokens = 8192
        
        model_name = self.provider.get_model(self.model_tier)
        logger.debug(f"{self.name} agent using {self.provider.name} model: {model_name}")
        
        # Create task for the LLM API call
        api_task = asyncio.create_task(
            self.provider.create_completion(
                system_prompt=self.get_system_prompt(),
                messages=messages,
                model_tier=self.model_tier,
                max_tokens=max_tokens,
                use_cache=True
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
    
    def _build_messages(self, input: AgentInput, rag_context: str) -> List[Dict]:
        """Build the message list for the LLM"""

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

        # Collect all images to send to the LLM
        images = []

        # Add device screenshots if available (full page context at different viewports)
        if input.context.get("deviceScreenshots"):
            device_shots = input.context["deviceScreenshots"]
            # Add desktop screenshot
            if device_shots.get("desktop"):
                images.append({
                    "data": device_shots["desktop"],
                    "label": "Desktop viewport screenshot of the current page"
                })
            # Add tablet screenshot
            if device_shots.get("tablet"):
                images.append({
                    "data": device_shots["tablet"],
                    "label": "Tablet viewport screenshot of the current page"
                })
            # Add mobile screenshot
            if device_shots.get("mobile"):
                images.append({
                    "data": device_shots["mobile"],
                    "label": "Mobile viewport screenshot of the current page"
                })

        # Add component screenshot if available (specific component capture)
        if input.context.get("componentScreenshot"):
            images.append({
                "data": input.context["componentScreenshot"],
                "label": "Screenshot of the selected component to modify"
            })

        # If we have images, create multimodal content
        if images:
            content = []

            # Add all images with labels
            for img in images:
                image_content = self.provider.format_image_content(
                    img["data"],
                    media_type="image/png"
                )
                content.append(image_content)

            # Build description of what images are included
            image_descriptions = [img["label"] for img in images]
            images_text = "\n".join(f"- {desc}" for desc in image_descriptions)

            content.append({
                "type": "text",
                "text": f"""
## Visual Context
The following screenshots show the current state of the page:
{images_text}

Use these images to understand:
- The current visual layout and design
- What changes the user is asking for
- How your modifications should integrate with the existing design

{user_text}
"""
            })
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
                if end == -1:
                    # Truncated - try to find last complete brace
                    json_str = response[start:].strip()
                    # Try to find last complete JSON object
                    last_brace = json_str.rfind('}')
                    if last_brace > 0:
                        json_str = json_str[:last_brace+1]
                    else:
                        raise json.JSONDecodeError("Truncated response - no complete JSON found", json_str, len(json_str))
                else:
                    json_str = response[start:end].strip()
            elif "```" in response:
                start = response.find("```") + 3
                end = response.find("```", start)
                if end == -1:
                    # Truncated
                    json_str = response[start:].strip()
                    last_brace = json_str.rfind('}')
                    if last_brace > 0:
                        json_str = json_str[:last_brace+1]
                    else:
                        raise json.JSONDecodeError("Truncated response - no complete JSON found", json_str, len(json_str))
                else:
                    json_str = response[start:end].strip()
            else:
                # Try to find raw JSON
                start = response.find("{")
                if start == -1:
                    raise json.JSONDecodeError("No JSON found in response", response, 0)
                end = response.rfind("}") + 1
                if end == 0:
                    raise json.JSONDecodeError("Truncated response - no closing brace", response, len(response))
                json_str = response[start:end]
            
            return json.loads(json_str)
            
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse {self.name} response as JSON: {e}")
            # Check if it's likely a truncation issue
            if "Unterminated string" in str(e) or "Truncated" in str(e):
                logger.error(f"{self.name} response appears truncated. Consider increasing max_tokens.")
            return {"error": "Failed to parse response", "raw": response[:1000], "error_detail": str(e)}
    
    def _extract_reasoning(self, response: str) -> Optional[str]:
        """Extract reasoning from response if present"""
        try:
            parsed = self._parse_response(response)
            if isinstance(parsed, dict) and "reasoning" in parsed:
                return parsed.pop("reasoning", None)
        except:
            pass
        return None
