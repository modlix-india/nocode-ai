"""Turn a tool call into an edit observation, or decide it is not one.

Lore's most valuable evidence is what the agent actually DID, not what it said
it did. Chat turns are narration; edits are ground truth, they carry a subject,
and there are hundreds of them per build instead of two.

Rather than call `ingest.from_edit` from each of ~190 tools, this classifies a
(tool_name, params, result) at the single point where every tool executes. The
cost of that choice is that classification is by convention rather than by
declaration, so this module is deliberately conservative: a tool it cannot
place produces nothing. A missed edit costs one observation; a wrong one puts a
false claim into an app's permanent knowledge.

Everything here is pure and dependency-free so the rules can be tested against
the real tool registry without a database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.services.lore.context import subject_from_tool_call
from app.services.lore.models import normalise_subject

# Verb prefix -> the action recorded on the observation. Base verbs, because
# `from_edit` renders them as "<actor> <action>d <type>". Checked longest
# first, so `make_user_` wins over any shorter `make` rule that may be added.
_WRITE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("bulk_patch_", "update"),
    ("make_user_", "update"),
    ("apply_transport", "update"),
    ("replace_", "update"),
    ("reset_", "update"),
    ("rename_", "rename"),
    ("rollback_", "update"),
    ("assign_", "update"),
    ("commit_", "update"),
    ("create_", "create"),
    ("delete_", "delete"),
    ("remove_", "delete"),
    ("update_", "update"),
    ("patch_", "update"),
    ("grant_", "update"),
    ("unblock_", "update"),
    ("move_", "update"),
    ("save_", "update"),
    ("add_", "create"),
    ("set_", "update"),
)

# The bare CRUD router tools: the object type is a parameter, not in the name.
_ROUTER_ACTIONS: dict[str, str] = {
    "create": "create",
    "update": "update",
    "delete": "delete",
    "copy": "create",
}

# Write-shaped names that do not touch a definition. Image and file helpers
# work on local paths; cache and session tools change nothing durable. Left
# explicit rather than pattern-matched, because being wrong here means writing
# noise into an app's knowledge forever.
_NOT_A_DEFINITION: frozenset[str] = frozenset({
    "apply_image_filter",
    "clear_cache",
    "close_browser_session",
    "composite_images",
    "convert_image_format",
    "crop_image",
    "generate_image",
    "generate_secured_access_key",
    "make_favicon",
    "pad_image_canvas",
    "recolor_image",
    "reload_auth_token",
    "resize_image_to_path",
    "trim_transparent_borders",
    # Lore's own write tools. Observing them would let lore feed on itself.
    "lore_add",
    "lore_note",
    "lore_correct",
    "propose_kb_update",
    "commit_kb_update",
})

# Tools whose object IS the application, so "app" is the right subject rather
# than no subject at all. Registration wiring is here on purpose: "this app
# auto-assigns profile X on signup" is exactly the kind of fact nobody writes
# down and everybody later needs.
_APP_LEVEL_TOOLS: frozenset[str] = frozenset({
    "create_app",
    "update_app",
    "delete_app",
    "configure_app_for_customer_signup",
    "update_security_app",
    "set_app_page_reference",
    "set_app_property",
    "add_app_reg_entry",
    "delete_app_reg_entry",
})

# Params worth naming in the observation body, in the order a reader wants
# them. These narrow "something changed on this page" to "this changed".
_DETAIL_PARAMS: tuple[str, ...] = (
    "component_key", "target_component_key", "parent_key", "component_type",
    "event_function_name", "step_name", "property_name", "rule_name",
    "role_name", "profile_name", "object_type", "name",
)

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-/{}]{1,120}$")

_MAX_DETAIL = 600


@dataclass(frozen=True)
class EditFact:
    """One definition change, in the shape `ingest.from_edit` wants."""

    object_type: str
    object_name: str
    action: str
    subject: str
    detail: str


def action_for(tool_name: str) -> str | None:
    """The action word for a write tool, or None if it is not a write."""
    if tool_name in _NOT_A_DEFINITION:
        return None
    if tool_name in _ROUTER_ACTIONS:
        return _ROUTER_ACTIONS[tool_name]
    for prefix, action in _WRITE_PREFIXES:
        if tool_name.startswith(prefix):
            return action
    return None


def _subject_for(tool_name: str, params: dict[str, Any]) -> str | None:
    """The object this call is about, as `<type>:<name>`, or "app", or None."""
    subject = subject_from_tool_call(tool_name, params)
    if subject and subject != "app":
        return subject

    # The CRUD router carries its type as a parameter.
    object_type = params.get("object_type")
    if isinstance(object_type, str) and object_type.strip():
        for key in ("page_name", "name", "source_name", "target_name"):
            value = params.get(key)
            if isinstance(value, str) and _NAME_RE.match(value.strip()):
                return normalise_subject(f"{object_type.strip().lower()}:{value.strip()}")

    if tool_name in _APP_LEVEL_TOOLS:
        return "app"
    return None


def _detail_for(tool_name: str, params: dict[str, Any], summary: str) -> str:
    """A one-line description of the change, plus whatever the tool reported.

    The tool's own summary is the most reliable account of what happened, so it
    leads. The parameters follow because a summary rarely names the component.
    """
    bits: list[str] = []
    for key in _DETAIL_PARAMS:
        value = params.get(key)
        if isinstance(value, str) and value.strip() and _NAME_RE.match(value.strip()):
            bits.append(f"{key}={value.strip()}")
    # Property and style edits carry their payload as a dict; the KEYS are the
    # durable fact ("this page sets visibility"), the values usually are not.
    for key in ("properties", "styles", "bindings", "props"):
        value = params.get(key)
        if isinstance(value, dict) and value:
            names = ", ".join(sorted(str(k) for k in list(value)[:8]))
            bits.append(f"{key}: {names}")

    head = f"`{tool_name}`"
    if bits:
        head = f"{head} ({'; '.join(bits)})"
    text = f"{head}\n{summary.strip()}" if summary and summary.strip() else head
    return text[:_MAX_DETAIL]


def classify(
    tool_name: str, params: Any, *, summary: str = "", success: bool = True,
) -> EditFact | None:
    """Classify one tool call. Returns None for anything that is not an edit.

    A failed call is not an edit: nothing changed, and recording the attempt
    would teach the curator that things exist which do not.
    """
    if not success or not isinstance(params, dict):
        return None
    action = action_for(tool_name)
    if action is None:
        return None
    subject = _subject_for(tool_name, params)
    if subject is None:
        return None

    if ":" in subject:
        object_type, _, object_name = subject.partition(":")
    else:
        object_type, object_name = "application", str(params.get("app_code") or "app")

    return EditFact(
        object_type=object_type,
        object_name=object_name,
        action=action,
        subject=subject,
        detail=_detail_for(tool_name, params, summary),
    )
