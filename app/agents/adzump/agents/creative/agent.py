from app.core.agent import BaseAgent
from app.agents.adzump.context import build_adzump_context
from app.agents.adzump.agents.creative.tools import CREATIVE_TOOLS
from app.config import settings


class CreativeAgent(BaseAgent):
    def __init__(self):
        context = build_adzump_context()

        super().__init__(
            name="creative_agent",
            tools=CREATIVE_TOOLS,
            context_builder=context,
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=10,
            max_tokens=4000,
        )


creative_agent = CreativeAgent()
