"""A4 — creative generation (copy + briefs + attributes + lead form).

Public surface:
- ``generate_creatives`` (ToolDefinition) — exposed to the A1 chat agent.
- ``CreativeAgent`` / ``get_creative_agent`` — the M3 reasoning engine.
- ``CreativeSet`` / ``Creative`` / ``Copy`` / ``LeadForm`` — the output contract.
- ``get_taxonomy`` — the J5 creative attribute taxonomy (local until J5 ships).
"""

from app.agents.adzump2.creative.creative import CreativeAgent, get_creative_agent
from app.agents.adzump2.creative.models import (
    Copy,
    Creative,
    CreativeAngle,
    CreativeSet,
    ImageBrief,
    LeadForm,
    LeadFormField,
    PREDICT_TODO,
)
from app.agents.adzump2.creative.taxonomy import get_taxonomy
from app.agents.adzump2.creative.tools import CREATIVE_TOOLS, generate_creatives

__all__ = [
    "CREATIVE_TOOLS",
    "Copy",
    "Creative",
    "CreativeAgent",
    "CreativeAngle",
    "CreativeSet",
    "ImageBrief",
    "LeadForm",
    "LeadFormField",
    "PREDICT_TODO",
    "generate_creatives",
    "get_creative_agent",
    "get_taxonomy",
]
