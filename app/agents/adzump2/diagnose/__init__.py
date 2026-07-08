"""A5 — diagnose (the loop's qualitative half).

Public surface:
- ``diagnose`` / ``propose_action`` (ToolDefinitions) — exposed to the A1 loop.
- ``DiagnoseAgent`` / ``get_diagnose_agent`` — the M3 reasoning engine.
- ``Diagnosis`` / ``AnnotatedAction`` / ``TestProposal`` / ``WatchItem`` — output.

A5 reads J10 (snapshot) + J12 (ActionSet) + J20 (attribute map), NARRATES +
PRIORITIZES the gated actions, proposes creative tests grounded in real J20
gaps, and watchlists thin/immature grains. It recomputes no numbers and applies
nothing (LLM reasoning only; the numbers/gates are Java).
"""

from app.agents.adzump2.diagnose.diagnose import DiagnoseAgent, get_diagnose_agent
from app.agents.adzump2.diagnose.models import (
    AnnotatedAction,
    Diagnosis,
    TestProposal,
    WatchItem,
)
from app.agents.adzump2.diagnose.tools import DIAGNOSE_TOOLS, diagnose, propose_action

__all__ = [
    "AnnotatedAction",
    "DIAGNOSE_TOOLS",
    "Diagnosis",
    "DiagnoseAgent",
    "TestProposal",
    "WatchItem",
    "diagnose",
    "get_diagnose_agent",
    "propose_action",
]
