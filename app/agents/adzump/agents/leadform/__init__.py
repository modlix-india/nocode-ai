"""Lead form generation and management subsystem."""

__all__ = ["get_leadform_agent", "LeadFormAgent", "SUGGEST_LEAD_FORM"]


def __getattr__(name: str):
    if name in ("get_leadform_agent", "LeadFormAgent"):
        from app.agents.adzump.agents.leadform.agent import get_leadform_agent, LeadFormAgent
        if name == "get_leadform_agent":
            return get_leadform_agent
        return LeadFormAgent
    if name == "SUGGEST_LEAD_FORM":
        from app.agents.adzump.agents.leadform.parent_tool import SUGGEST_LEAD_FORM
        return SUGGEST_LEAD_FORM
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

