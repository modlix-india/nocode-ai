"""Core implementation of creative copy generation and image creation services."""

from __future__ import annotations

import logging

from app.core.tools.base import ToolResult
from app.agents.appbuilder.tools._shared import get_saas_client
from app.agents.adzump._shared import build_ds_headers
from app.agents.adzump.agents.creative_generator.fresh_generation import (
    generate_fresh_creatives_workflow,
)
from app.agents.adzump.agents.creative_generator.modification import (
    modify_existing_creative_workflow,
)

logger = logging.getLogger(__name__)


class CreativeGenerationService:
    """Service to handle the orchestration of creative copywriting and image generation."""

    def __init__(self, context: dict) -> None:
        self.context = context
        self.session = context.get("_session")
        self.sctx = (
            self.session.context
            if self.session
            else (context.get("session_context") or {})
        )
        self.product_data = self.sctx.get("product_data") or {}
        self.competitor_analysis = self.sctx.get("competitor_analysis") or {}
        self.spec = self.sctx.get("campaign_spec") or {}
        self.auth = context.get("auth")
        self.stream = context.get("event_stream")
        self.tool_use_id = context.get("tool_use_id", "")
        self.client = get_saas_client()
        self.headers = build_ds_headers(context)

    async def generate_fresh_creatives(self, params: dict) -> ToolResult:
        """Generate fresh ad copy and square creatives from scratch."""
        return await generate_fresh_creatives_workflow(self, params)

    async def modify_existing_creative(self, params: dict) -> ToolResult:
        """Modify, update, or regenerate formats for a specific existing creative."""
        return await modify_existing_creative_workflow(self, params)
