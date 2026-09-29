"""Step engine: steps with dependencies -> this turn's step states and missing list.

    Step       one thing to collect: when it applies, which steps it requires,
               when it is done, what to prescribe while owed, what State shows
    Journey    an agent's steps over its own per-turn read-model
    walk()     resolves each step's Status from its dependencies:
               off / done / blocked / waiting / open
    StepState  one step's result this turn: label, value, status, age
    Progress   the result: every StepState, the missing list, whether it is complete

A step opens only once every step it requires is done (or off). When several
are open at once, declaration order decides which is asked first - so a
``requires`` may only name an earlier step.

State and the missing list come from the same walk, so they never disagree.
Pure: no I/O, no session writes, nothing specific to one agent. The
orchestrator's journey is ``workflow.NEW_CAMPAIGN``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

C = TypeVar("C")  # the agent's frozen per-turn read-model


class Status(str, Enum):
    OFF = "off"          # does not apply now: satisfies dependents
    DONE = "done"
    BLOCKED = "blocked"  # an earlier step it requires is still owed
    WAITING = "waiting"  # its ask is on screen: never re-prescribed, blocks completion
    OPEN = "open"        # owed now: prescribed


@dataclass(frozen=True)
class Journey(Generic[C]):
    name: str
    steps: tuple[Step[C], ...]
    finish: str  # the one prescription once every step is off or done

    # Runs once, when the journey is created (at app startup). Catches editing
    # mistakes that would otherwise fail silently mid-chat:
    #   - two steps with the same name
    #   - a `requires` with a typo, or naming a later step - walk() goes top
    #     to bottom, so that step would stay blocked forever
    def __post_init__(self) -> None:
        seen: set[str] = set()
        for step in self.steps:
            if step.name in seen:
                raise ValueError(f"{self.name}: duplicate step {step.name!r}")
            if unknown := set(step.requires) - seen:
                raise ValueError(f"{self.name}.{step.name} requires {sorted(unknown)}, "
                                 "which is not an earlier step")
            seen.add(step.name)

    # Called every turn. Goes through the steps top to bottom and gives each one
    # a status:
    #   - off or done       -> satisfied, so later steps that need it can open
    #   - open              -> its prescription joins the missing list
    #   - blocked / waiting -> nothing to ask now, but the journey isn't complete
    # Every step also gets a StepState (what its State line shows). Once every
    # step is off or done, the missing list is just `finish`. `set_at` (field ->
    # turn it was saved) and `turn` give each its "set N turns ago". Pure read:
    # the same snapshot always gives the same answer.
    def walk(self, ctx: C, set_at: Mapping[str, int] | None = None, turn: int = 0) -> Progress:
        step_states: list[StepState] = []
        missing: list[str] = []
        satisfied: set[str] = set()
        for step in self.steps:
            status = _status(step, ctx, satisfied)
            if status in (Status.OFF, Status.DONE):
                satisfied.add(step.name)
            elif status is Status.OPEN:
                missing.append(step.prescribe(ctx))
            step_states.append(StepState(step.label, step.value(ctx), status,
                                         _turns_ago(step.fields, set_at or {}, turn)))
        complete = len(satisfied) == len(self.steps)
        return Progress(tuple(step_states), (self.finish,) if complete else tuple(missing), complete)


# One thing an agent needs. Each field answers one question about it:
#   name       its id, e.g. "budget"
#   label      its name in State, e.g. "Daily Budget"
#   done       is it finished?
#   prescribe  owed now -> the exact instruction for the model (the tool call)
#   value      what its State row shows; None = nothing yet
#   applies    does it matter right now? False -> off: hidden, and counts as
#              satisfied for the steps that require it
#   ready      can it be asked now? False -> waiting: its question is already
#              on screen, so it isn't asked again, but the journey isn't complete
#   requires   earlier steps that must be off or done first; until then blocked
#   fields     the saved answers that belong to it, for "set N turns ago"
@dataclass(frozen=True)
class Step(Generic[C]):
    name: str
    label: str
    done: Callable[[C], bool]
    prescribe: Callable[[C], str]
    value: Callable[[C], str | None] = lambda ctx: None
    applies: Callable[[C], bool] = lambda ctx: True
    ready: Callable[[C], bool] = lambda ctx: True
    requires: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()


# What walk() found for one step this turn - the facts its State line is
# printed from. Several answers in one message are fine: each step is judged
# on its own, and every answer saved this turn reads "just set".
@dataclass(frozen=True)
class StepState:
    label: str
    value: str | None
    status: Status
    turns_ago: int | None  # since its newest field write; None = never written


# What walk() returns: how far the journey has got this turn.
#   step_states  one per step - the facts behind each State line
#   missing      instructions for the steps owed now, next one first
#                (just `finish` once everything is done)
#   complete     every step is off or done - the review card waits for this
# state_section() / missing_section() turn it into the text the model reads.
@dataclass(frozen=True)
class Progress:
    step_states: tuple[StepState, ...]
    missing: tuple[str, ...]
    complete: bool

    def state_section(self) -> str:
        lines = [line for state in self.step_states if (line := _state_line(state))]
        return "\n".join(["## State", *lines])

    def missing_section(self, note: str = "") -> str:
        """``note`` is the agent's own line above the numbered list."""
        if not self.missing:
            # Reachable exactly when a step is WAITING (its ask is on screen and
            # blocks completion) - never claim review-readiness here or the
            # review-over-open-ask behavior the ready gate kills leaks back in.
            return ("\n## What's still missing\n(nothing to ask - an answer is "
                    "pending on screen; wait for the user's reply)")
        lines = ["\n## What's still missing (in order - do the top item first)"]
        if note:
            lines.append(note)
        lines += [f"{i}. {item}" for i, item in enumerate(self.missing, 1)]
        return "\n".join(lines)


def _status(step: Step[C], ctx: C, satisfied: set[str]) -> Status:
    if not step.applies(ctx):
        return Status.OFF
    if step.done(ctx):
        return Status.DONE
    if not set(step.requires) <= satisfied:
        return Status.BLOCKED
    if not step.ready(ctx):
        return Status.WAITING
    return Status.OPEN


def _turns_ago(fields: tuple[str, ...], set_at: Mapping[str, int], turn: int) -> int | None:
    stamps = [int(set_at[f]) for f in fields if f in set_at]
    return max(0, turn - max(stamps)) if stamps else None


def _state_line(state: StepState) -> str | None:
    if state.status in (Status.OFF, Status.BLOCKED):
        # Not owed now - shown only when it already holds a value.
        return f"- {state.label}: {state.value}" if state.value else None
    line = f"- {state.label}: {state.value or '-'}"
    if state.status is Status.WAITING:
        return f"{line} (asked - waiting on the reply)"
    if state.status is Status.DONE:
        return f"{line} ✓{_age(state.turns_ago)}"
    return line


def _age(turns_ago: int | None) -> str:
    """' - set 2 turns ago', so the model can tell a fresh answer from an old one."""
    if turns_ago is None:
        return ""
    if turns_ago == 0:
        return " - just set"
    return f" - set {turns_ago} turn{'s' if turns_ago > 1 else ''} ago"
