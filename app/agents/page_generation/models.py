"""Data models for page generation agents"""
from enum import Enum
from typing import Dict, Any, Optional, List
from pydantic import BaseModel


class PageAgentMode(str, Enum):
    CREATE = "create"      # Generate new page from scratch
    MODIFY = "modify"      # Modify specific aspects of existing page
    ENHANCE = "enhance"    # Add features to existing page
    IMPORT = "import"      # Import from external website URL


class PageAgentOptions(BaseModel):
    """Options for page generation"""
    mode: PageAgentMode = PageAgentMode.CREATE
    preserveEvents: bool = False   # Keep existing events when modifying
    preserveStyles: bool = False   # Keep existing styles when modifying
    preserveLayout: bool = False   # Keep existing layout when modifying


class DeviceScreenshots(BaseModel):
    """Screenshots from different device viewports"""
    desktop: Optional[str] = None  # Base64 encoded desktop viewport screenshot
    tablet: Optional[str] = None   # Base64 encoded tablet viewport screenshot
    mobile: Optional[str] = None   # Base64 encoded mobile viewport screenshot


class RequestFile(BaseModel):
    """File data for AI requests"""
    name: str
    type: str
    content: str  # Base64 encoded file content


class RequestTheme(BaseModel):
    """Theme information for AI requests"""
    themeName: str


class RequestFontPack(BaseModel):
    """Font pack information"""
    name: str
    code: str  # HTML link tag code for font loading


class PageAgentRequest(BaseModel):
    """Request to generate or modify a page"""
    instruction: str
    page: Optional[Dict[str, Any]] = None  # Existing page definition
    selectedComponentKey: Optional[str] = None  # Component to focus on
    componentScreenshot: Optional[str] = None  # Base64 image for visual feedback (specific component)
    deviceScreenshots: Optional[DeviceScreenshots] = None  # Screenshots from all device viewports
    file: Optional[RequestFile] = None  # Uploaded file (non-image) as base64
    theme: Optional[RequestTheme] = None  # Theme information
    iconPacks: Optional[List[str]] = None  # List of available icon pack names
    fontPacks: Optional[List[RequestFontPack]] = None  # List of available font packs with names and loading codes
    sourceUrl: Optional[str] = None  # URL to import from (for IMPORT mode)
    clientCode: Optional[str] = None  # Client code for file uploads (passed from auth)
    options: PageAgentOptions = PageAgentOptions()

    # Session tracking fields
    sessionId: Optional[str] = None  # Existing session to continue (for multi-turn context)
    newSession: bool = False  # Force creation of new session even if sessionId provided
    pageName: Optional[str] = None  # Page name for session ID generation

    # Alias for backward compatibility
    @property
    def existingPage(self) -> Optional[Dict[str, Any]]:
        return self.page


class AgentLogEntry(BaseModel):
    """Log entry for agent execution"""
    status: str  # "success" | "failed" | "running"
    reasoning: Optional[str] = None
    errors: List[str] = []
    model: Optional[str] = None  # Track which model was used
    error: Optional[str] = None  # Single error message

    class Config:
        extra = "ignore"  # Ignore extra fields like startedAt/finishedAt


class TokenUsageByAgent(BaseModel):
    """Token usage for a single agent"""
    inputTokens: int = 0
    outputTokens: int = 0
    cacheReadTokens: int = 0
    cacheCreationTokens: int = 0
    model: Optional[str] = None
    latencyMs: Optional[int] = None


class TokenUsageSummary(BaseModel):
    """Aggregated token usage for a request"""
    totalInputTokens: int = 0
    totalOutputTokens: int = 0
    totalCacheReadTokens: int = 0
    totalCacheCreationTokens: int = 0
    byAgent: Dict[str, TokenUsageByAgent] = {}


class ContextUsageInfo(BaseModel):
    """Context usage information for a session"""
    used: int = 0  # Total tokens used in session so far
    limit: int = 184000  # Model's context limit
    percentage: float = 0.0  # Percentage used
    turnsInContext: int = 0  # How many turns are in active context
    warning: Optional[str] = None  # "approaching_limit" when > 80%


class PageAgentResponse(BaseModel):
    """Response from page generation"""
    success: bool
    page: Dict[str, Any]
    agentLogs: Dict[str, AgentLogEntry]
    sessionId: Optional[str] = None  # Session ID for multi-turn context
    turnNumber: Optional[int] = None  # Which turn in conversation
    tokenUsage: Optional[TokenUsageSummary] = None  # Token usage for this request
    contextUsage: Optional[ContextUsageInfo] = None  # Session context status
