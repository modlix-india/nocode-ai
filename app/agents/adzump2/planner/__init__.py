"""A3 — the planner/critic/repair generation engine for Adzump2.

Public surface:
- ``draft`` / ``critique`` / ``repair`` — the three roles (design A3 §5.1).
- ``PlanGenerator`` / ``generate_plan`` — the bounded, monotonic loop.
- ``draft_plan`` — the ToolDefinition exposed to the A1 chat agent.
- The structured contracts in ``models`` (PlanContext, PlanPatch, PlanCritique,
  Issue, ValidationResult, GenerateResult).
"""

from app.agents.adzump2.planner.critic import Critic, critique, get_critic
from app.agents.adzump2.planner.loop import (
    MAX_REPAIR,
    DEFAULT_THRESHOLD,
    PlanGenerator,
    PlanIO,
    draft_plan,
    generate_plan,
    get_plan_generator,
    ground_patch,
)
from app.agents.adzump2.planner.models import (
    GenerateResult,
    Issue,
    PlanContext,
    PlanCritique,
    PlanPatch,
    ValidationResult,
)
from app.agents.adzump2.planner.planner import Planner, draft, get_planner
from app.agents.adzump2.planner.repair import Repairer, get_repairer, repair

__all__ = [
    "Planner",
    "Critic",
    "Repairer",
    "PlanGenerator",
    "PlanIO",
    "draft",
    "critique",
    "repair",
    "generate_plan",
    "get_planner",
    "get_critic",
    "get_repairer",
    "get_plan_generator",
    "ground_patch",
    "draft_plan",
    "PlanContext",
    "PlanPatch",
    "PlanCritique",
    "Issue",
    "ValidationResult",
    "GenerateResult",
    "MAX_REPAIR",
    "DEFAULT_THRESHOLD",
]
