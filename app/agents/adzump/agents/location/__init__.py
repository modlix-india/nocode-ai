"""Geo-targeting subsystem - the LocationAgent and its supporting modules.

Public surface for the rest of the codebase: ``get_location_agent`` (the
singleton behind the ``manage_targeting_locations`` tool) and
``is_local_business`` (the scale check shared with the orchestrator).
Everything else is internal to this package - see AGENT.md.

Exports are lazy (PEP 562): importing ``location.models`` from adzump-level
modules must not drag in the full agent chain (a circular import via
business_storage otherwise).
"""

__all__ = ["get_location_agent", "is_local_business"]


def __getattr__(name: str):
    if name == "get_location_agent":
        from app.agents.adzump.agents.location.agent import get_location_agent
        return get_location_agent
    if name == "is_local_business":
        from app.agents.adzump.agents.location.models import is_local_business
        return is_local_business
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
