"""LeadZumpAgent — a CRM assistant over LeadZump's own leads and deals.

Deliberately not the AppBuilder agent with a different prompt. AppBuilder
authors applications: storages, pages, functions, schemas. A relationship
manager wants to ask about the people in their pipeline, and handing them a
tool that can rewrite the app they work in would be the wrong product as well
as a privilege they never asked for. So this is its own agent, with its own
tools and its own gate (see ``router.py``).

What it adds over the base loop:

* a turn reminder carrying today's date, because every "this week" question
  needs one and the model has no clock; and the record the user is working on,
  so a follow-up question does not start from nothing;
* confirmation prompts written in the CRM's terms rather than the framework's
  generic "Confirm: Update Deal" — a stage move can send a customer a message,
  and the person approving it should be told that before they approve.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.agents.leadzump.context import build_leadzump_context
from app.agents.leadzump.tools.registry import ALL_TOOLS, MUTATING_TOOLS
from app.config import settings
from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.core.tools.base import ToolResult

logger = logging.getLogger(__name__)

# The records a tool call was about, remembered so the next turn's reminder can
# name them. Keyed by the argument the tool takes.
_FOCUS_ARGS = ("code", "deal_code", "lead_code")


def _subject(tool_input: dict[str, Any]) -> str:
    """The record a confirmation prompt is about."""
    for arg in _FOCUS_ARGS:
        if tool_input.get(arg):
            return str(tool_input[arg])
    return "this record"


def _confirm_move_stage(code: str, args: dict[str, Any]) -> str:
    status = f", status {args['status_id']}" if args.get("status_id") else ""
    note = f" Note: {args['comment']}" if args.get("comment") else ""
    return (
        f"Move deal {code} to stage {args.get('stage_id')}{status}. This re-runs "
        f"assignment, queues the new stage's messaging rules (which can send the "
        f"customer a WhatsApp message), and may report a conversion to Meta or "
        f"Google.{note}"
    )


def _confirm_update(entity: str):
    def build(code: str, args: dict[str, Any]) -> str:
        fields = ", ".join(sorted(k for k in args if k not in ("code", "confirmed")))
        return f"Update {entity} {code}: change {fields or 'nothing'}."

    return build


def _confirm_note(code: str, args: dict[str, Any]) -> str:
    preview = str(args.get("content") or "").strip()
    if len(preview) > 160:
        preview = preview[:157] + "..."
    return f'Add a note to {code}, visible to the whole team: "{preview}"'


def _confirm_task(code: str, args: dict[str, Any]) -> str:
    kind = args.get("task_type") or args.get("task_type_id") or "task"
    due = f", due {args['due_date']}" if args.get("due_date") else ""
    return f"Create a {kind} on {code}{due}. It will appear in the assignee's task list."


def _confirm_create(code: str, args: dict[str, Any]) -> str:
    who = args.get("phone_number") or args.get("email") or "no contact given"
    return (
        f"Create deal '{args.get('name')}' on product {args.get('product_id')} for {who}. "
        f"The assignee is notified, and the backend refuses it if a deal already "
        f"exists for that number on that product."
    )


def _confirm_tag(code: str, args: dict[str, Any]) -> str:
    why = f" Reason: {args['comment']}" if args.get("comment") else ""
    return f"Tag deal {code} as '{args.get('tag')}'.{why}"


def _confirm_task_complete(code: str, args: dict[str, Any]) -> str:
    done = args.get("completed", True)
    return f"Mark task {code} as {'completed' if done else 'reopened'}."


_CONFIRMATIONS = {
    "deal_move_stage": _confirm_move_stage,
    "lead_update": _confirm_update("lead"),
    "deal_update": _confirm_update("deal"),
    "note_add": _confirm_note,
    "task_create": _confirm_task,
    "deal_create": _confirm_create,
    "deal_tag": _confirm_tag,
    "task_complete": _confirm_task_complete,
}


class LeadZumpAgent(BaseAgent):
    """Chat agent over the LeadZump CRM's leads, deals, pipeline and follow-ups."""

    _instance: "LeadZumpAgent | None" = None

    CONFIRMATION_TOOLS: set[str] = set(MUTATING_TOOLS)

    def __init__(self) -> None:
        super().__init__(
            name="leadzump",
            tools=ALL_TOOLS,
            context_builder=build_leadzump_context(),
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=settings.MAX_AGENT_TURNS,
            provider=getattr(settings, "LEADZUMP_PROVIDER", settings.LLM_PROVIDER),
        )

    @classmethod
    def get_instance(cls) -> "LeadZumpAgent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("LeadZumpAgent created with %d tools", len(ALL_TOOLS))
        return cls._instance

    # ── BaseAgent override hooks ──

    async def build_turn_reminder(self, session: BaseSession, turn: int) -> str:
        """Today's date, and what the user is looking at. No I/O."""
        now = datetime.now(timezone.utc)
        lines = [
            "## Now",
            f"- Current time: {now.strftime('%Y-%m-%dT%H:%M:%SZ')} (UTC). "
            f"Today is {now.strftime('%A %d %B %Y')} in UTC.",
            "- Every date you send to a tool and every date you read back is UTC. "
            "Say which day you mean when the user says 'today' or 'this week'.",
        ]

        recent = session.context.get("recent_records") if session.context else None
        if isinstance(recent, list) and recent:
            lines.append("")
            lines.append("## In this conversation so far")
            for entry in recent[-5:]:
                lines.append(f"- {entry}")
            lines.append(
                "Re-read a record before quoting it — these are what was looked at, "
                "not what it currently says."
            )
        return "\n".join(lines)

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session
        if session.auth:
            ctx["auth"] = session.auth
        return ctx

    def _build_confirmation_message(
        self, tool_name: str, display_name: str, tool_input: dict[str, Any],
    ) -> str:
        """Say what is about to change, in the CRM's own terms.

        The default is "Confirm: Move Deal Stage", which tells the person
        approving it nothing about the messages it may send. Approving a
        consequence you were not shown is not consent, so each of these names
        the record and the effect.
        """
        builder = _CONFIRMATIONS.get(tool_name)
        if builder is None:
            return f"Confirm: {display_name}"
        return builder(_subject(tool_input), tool_input)

    def note_tool_outcome(
        self,
        tool_name: str,
        tool_input: Any,
        result: ToolResult,
        session: BaseSession,
    ) -> None:
        """Remember which records this turn touched, for the next turn's reminder.

        Here rather than in each tool because it is the same three lines
        thirteen times, and a tool that forgot them would silently stop being
        part of the conversation's memory.
        """
        if not result.success or not isinstance(tool_input, dict):
            return
        for arg in _FOCUS_ARGS:
            code = tool_input.get(arg)
            if not code:
                continue
            entry = f"{tool_name} → {code}"
            recent = session.context.setdefault("recent_records", [])
            if entry in recent:
                recent.remove(entry)
            recent.append(entry)
            del recent[:-10]
