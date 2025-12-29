"""SSE event types and progress callback for real-time updates"""
from enum import Enum
from typing import Optional, Any, Union
from pydantic import BaseModel
import json
import asyncio


class EventType(str, Enum):
    """Types of SSE events"""
    STATUS = "status"                 # Overall status message
    PHASE = "phase"                   # Phase transition
    AGENT_START = "agent_start"       # Sub-agent started
    AGENT_THINKING = "agent_thinking" # Sub-agent reasoning
    AGENT_PROGRESS = "agent_progress" # Sub-agent progress update
    AGENT_COMPLETE = "agent_complete" # Sub-agent finished
    MERGING = "merging"               # Merging outputs
    COMPLETE = "complete"             # Final result
    ERROR = "error"                   # Error occurred
    KEEPALIVE = "keepalive"           # Keepalive to prevent timeout


class ProgressEvent(BaseModel):
    """A single progress event"""
    event: EventType
    agent: Optional[str] = None
    message: str
    data: Optional[dict] = None
    
    def to_sse(self) -> str:
        """Format as Server-Sent Event string"""
        # SSE keepalive uses comment format (colon prefix)
        if self.event == EventType.KEEPALIVE:
            return f": keepalive {self.message}\n\n"
        
        event_data = {
            "agent": self.agent,
            "message": self.message,
        }
        if self.data:
            event_data["data"] = self.data
        return f"event: {self.event.value}\ndata: {json.dumps(event_data)}\n\n"


class ProgressCallback:
    """
    Callback handler for streaming progress updates to SSE.
    
    Usage:
        progress = ProgressCallback()
        
        # Emit events
        await progress.status("Starting generation...")
        await progress.agent_start("Layout")
        await progress.agent_thinking("Layout", "Analyzing requirements...")
        await progress.agent_complete("Layout", success=True)
        
        # Consume events
        async for event in progress.events():
            yield event.to_sse()
    """
    
    def __init__(self):
        self.queue: asyncio.Queue[ProgressEvent] = asyncio.Queue()
        self._closed = False
    
    async def emit(
        self, 
        event: EventType, 
        message: str, 
        agent: Optional[str] = None, 
        data: Optional[dict] = None
    ):
        """Emit a progress event"""
        if not self._closed:
            await self.queue.put(ProgressEvent(
                event=event,
                agent=agent,
                message=message,
                data=data
            ))
    
    async def status(self, message: str):
        """Emit overall status message"""
        await self.emit(EventType.STATUS, message)
    
    async def phase(self, phase_name: str):
        """Emit phase transition"""
        await self.emit(EventType.PHASE, f"Starting {phase_name} phase", data={"phase": phase_name})
    
    async def agent_start(self, agent: str, message: Optional[str] = None):
        """Emit agent started event"""
        await self.emit(
            EventType.AGENT_START, 
            message or f"Starting {agent}...", 
            agent
        )
    
    async def agent_thinking(self, agent: str, message: str):
        """Emit agent thinking/reasoning event"""
        await self.emit(EventType.AGENT_THINKING, message, agent)
    
    async def agent_progress(self, agent: str, message: str, progress: Optional[float] = None):
        """Emit agent progress update"""
        await self.emit(
            EventType.AGENT_PROGRESS, 
            message, 
            agent,
            {"progress": progress} if progress else None
        )
    
    async def agent_complete(
        self, 
        agent: str, 
        success: bool, 
        message: Optional[str] = None
    ):
        """Emit agent completion event"""
        await self.emit(
            EventType.AGENT_COMPLETE,
            message or f"{agent} {'completed' if success else 'failed'}",
            agent,
            {"success": success}
        )
    
    async def merging(self, message: str = "Merging agent outputs..."):
        """Emit merging event"""
        await self.emit(EventType.MERGING, message)
    
    async def keepalive(self, message: str = ""):
        """Emit keepalive to prevent connection timeout"""
        await self.emit(EventType.KEEPALIVE, message)
    
    async def complete(self, result: dict):
        """Emit completion event with final result"""
        await self.emit(EventType.COMPLETE, "Generation complete", data=result)
        self._closed = True
    
    async def error(self, message: str, agent: Optional[str] = None):
        """Emit error event"""
        await self.emit(EventType.ERROR, message, agent)
        self._closed = True
    
    def close(self):
        """Close the callback (no more events)"""
        self._closed = True
    
    @property
    def is_closed(self) -> bool:
        """Check if callback is closed"""
        return self._closed

