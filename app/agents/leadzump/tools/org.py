"""Team and org administration, reused from the AppBuilder agent.

Not rewritten. `app/agents/appbuilder/tools/modlix/security.py` already wraps the
security service's user, profile, role and org endpoints, its tools are plain
``ToolDefinition`` objects with no AppBuilder-only dependencies (they read only
``context["headers"]``), and reusing another agent's tools is the established
pattern here — adzump2 imports `web_fetch` and `present_options` from adzump the
same way. Importing them is not "using the AppBuilder agent in LeadZump":
`ALLOWED_AI_APPS` is untouched and that agent is still not reachable from this
app.

**Why these are the safe ones to reuse.** The security service carries 122
`@PreAuthorize` annotations across 29 controllers, so the caller's own token
really is the gate: a CRM user without `Authorities.User_UPDATE` gets a 403 from
the backend, not a tool that quietly works. That is emphatically not true of
entity-processor, where the whole LeadZump surface has nine annotations across
fifty-nine controllers and none on any path this agent touches.

**Nine of the thirty taken, plus two written here, and the rest left on purpose.** The exclusions
are not squeamishness; they are a different product. A relationship manager's
assistant administers the sales team it can already see. It does not author
privilege structures, register apps, or move definitions between environments:

* `create_role`, `create_profile`, `build_authority` — authoring privilege
  *structures* rather than applying existing ones. Assigning a profile someone
  already designed is team admin; inventing a new authority set is not.
* `grant_app_access`, `list_security_apps`, `update_security_app` — platform app
  administration, one level above the tenant.
* `set_app_property`, `list_app_properties`, `add_app_reg_entry`,
  `list_app_reg_entries`, `delete_app_reg_entry`,
  `configure_app_for_customer_signup` — app-definition and signup machinery.
* `export_security_app`, `list_transport_types`, `apply_transport_by_id`,
  `apply_transport_by_code` — environment transport. An agent that can apply a
  transport can replace a tenant's security configuration wholesale.
* `list_clients` — enumerates tenants beyond the CRM's own organisation.
* `get_client_by_code` — its only argument is a client code, which is precisely
  the value this agent must never let a model choose. A tool whose whole input
  is the thing you have to strip is not worth keeping; `list_departments` and
  `list_designations` already describe the caller's own organisation.
* `list_roles`, `assign_role`, `remove_role` — **removed after the API said no.**
  A LeadZump admin holds `Authorities.Profile_{READ,CREATE,UPDATE,DELETE}` but
  **not** `Authorities.Role_READ`, so `/api/security/rolev2` answers 403 for the
  people who would use this panel. Roles are the platform primitive; *profiles*
  are what LeadZump actually assigns, and `assign_profile` is exactly what its
  own `users` page calls. Keeping three tools that 403 for nearly every caller
  would spend prompt budget inviting the model to fail.
* `verify_token` — a diagnostic, and this agent has already authenticated.

**Two of the survivors were carrying a tenant argument.** `list_users` takes
`client_code` and `list_roles` takes `app_code`, both optional, both fine in
AppBuilder where a developer legitimately works across apps and tenants. Here
they are a way for a model to ask about somewhere it was not signed in to. The
security service would scope the answer by ClientHierarchy anyway, so this is
belt rather than braces — but the invariant that *no tool in this agent takes a
tenant* is worth more than the one case where it might have been harmless, and
a unit test enforces it. :func:`_pin` removes them from the advertised schema:
`list_users` then falls through to the caller's own client. `list_profiles` gets
the same treatment for a different reason: it needs a numeric `app_id`, which
differs per environment and which a CRM user cannot know, so
:func:`_resolve_app_id` looks it up from the app code the caller is signed in to
and caches it for the conversation.

Adding any of them back is one line here plus a confirmation message, so the
decision stays cheap to revisit.
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
from typing import Any

from app.agents.appbuilder.tools.modlix.security import (
    assign_profile_tool,
    get_user_tool,
    list_departments_tool,
    list_designations_tool,
    list_profiles_tool,
    list_users_tool,
    make_user_active_tool,
    make_user_inactive_tool,
    unblock_user_tool,
)
from app.agents.leadzump.tools._client import client, headers, ok
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

USERS = "/api/security/users"

def _caller_app(context: dict) -> str:
    """The app the caller is signed in to, for tools that would otherwise ask."""
    auth = context.get("auth")
    return getattr(auth, "access_app_code", "") or context.get("app_code") or ""


async def _resolve_app_id(context: dict) -> str:
    """The caller's app's numeric id, resolved once per conversation.

    `list_profiles` needs it because the platform exposes profiles per app at
    `/api/security/app/{appId}/profiles`, and a CRM user has no way to know
    LeadZump's id — it differs per environment (270 on local). Resolved from
    the app code the caller is signed in to and cached in the session, because
    it cannot change within one conversation.
    """
    session_ctx = context.setdefault("session_context", {})
    cached = session_ctx.get("app_id")
    if cached:
        return str(cached)

    app_code = _caller_app(context)
    if not app_code:
        return ""
    result = await client().get(
        "/api/security/applications",
        headers=headers(context),
        params={"appCode": app_code, "size": 1},
    )
    rows = (result.data or {}).get("content") if isinstance(result.data, dict) else None
    if not result.success or not rows:
        # Expected for a user without security-service read permission, so
        # logged at info rather than warning — it is the API doing its job.
        logger.info("app id for %s not resolvable by this caller: %s", app_code, result.error)
        return ""
    app_id = rows[0].get("id")
    if app_id:
        session_ctx["app_id"] = app_id
    return str(app_id or "")


def _pin(tool, drop: tuple[str, ...] = (), inject: dict | None = None):
    """A copy of `tool` with `drop` removed from its schema and `inject` forced.

    Removed from the advertised parameters rather than filtered at dispatch, so
    the model never sees an argument it is not allowed to set — `BaseAgent`
    already rejects undeclared arguments, which makes the schema the real
    boundary. Anything in `inject` is resolved from the request context at call
    time and overwrites whatever the model sent.

    Copies, for the same reason :func:`_confirmable` does: these objects belong
    to AppBuilder's registry too.
    """
    kept = [p for p in tool.parameters if p.name not in drop]
    original = tool.execute
    forced = inject or {}

    async def execute(params: dict, context: dict):
        merged = {k: v for k, v in params.items() if k not in drop}
        for key, resolve in forced.items():
            value = resolve(context)
            if inspect.isawaitable(value):
                value = await value
            if value:
                merged[key] = value
        return await original(merged, context)

    return dataclasses.replace(tool, parameters=kept, execute=execute)


# ── two written here, because AppBuilder has neither ────────────────────────
#
# The first live conversation over this band asked "who is on my team and what
# profiles do they hold", and the agent had to answer that it could list the
# profiles that exist and grant one, but could not say who held what. It was
# right: neither `list_users` nor `get_user` returns an assignment, and the user
# DTO carries no profile field at all. The read does exist, just not where you
# would look for it — `GET users/{userId}/app/{appId}/assignedProfiles` — and
# without it `assign_profile` is a write with no way to check the result.


PROFILE_FIELDS = ("id", "name", "description", "defaultProfile")


async def _user_profiles(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """The profiles one user holds in this app.

    Slimmed hard on purpose: each profile carries an `arrangement` tree of
    nested roles that runs to thousands of characters, and none of it answers
    the question being asked.
    """
    user_id = str(params.get("user_id") or "").strip()
    if not user_id:
        return ToolResult(success=False, error="user_id is required, from list_users.")

    app_id = await _resolve_app_id(context)
    if not app_id:
        # Almost always a permission, not a fault: resolving the id reads
        # `/api/security/applications`, which needs an authority a plain CRM
        # user does not hold. Saying "could not resolve" alone reads as a
        # broken tool and invites a retry that will fail the same way.
        return ToolResult(
            success=False,
            error=(
                "Cannot read this user's profiles: looking up the application "
                "requires permission on the security service that the signed-in "
                "user does not have. This is a permissions limit, not a fault — "
                "ask an administrator rather than retrying."
            ),
        )

    result = await client().get(
        f"{USERS}/{user_id}/app/{app_id}/assignedProfiles", headers=headers(context)
    )
    if not result.success:
        return ToolResult(
            success=False, error=f"Could not read profiles for user {user_id}: {result.error}"
        )

    entries = result.data if isinstance(result.data, list) else []
    rows = [
        {k: e.get(k) for k in PROFILE_FIELDS if e.get(k) not in (None, "")}
        for e in entries
        if isinstance(e, dict)
    ]
    if not rows:
        return ok(
            {"userId": user_id, "profiles": []},
            f"user {user_id} holds no profile in this app, so they have no access to it",
        )
    return ok(
        {"userId": user_id, "profiles": rows},
        f"user {user_id} holds: " + ", ".join(str(r.get("name")) for r in rows),
        max_chars=4000,
    )


async def _remove_profile(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Take a profile away from a user.

    The counterpart to `assign_profile`, and a GET that mutates — the platform
    exposes it as `GET {userId}/removeProfile/{profileId}`, not a DELETE. Worth
    naming because a reader who assumes GETs are safe would be wrong here.
    """
    user_id = str(params.get("user_id") or "").strip()
    profile_id = str(params.get("profile_id") or "").strip()
    if not user_id or not profile_id:
        return ToolResult(
            success=False,
            error="Both user_id and profile_id are required. Read user_profiles first.",
        )

    result = await client().get(
        f"{USERS}/{user_id}/removeProfile/{profile_id}", headers=headers(context)
    )
    if not result.success:
        return ToolResult(
            success=False,
            error=f"Could not remove profile {profile_id} from user {user_id}: {result.error}",
        )
    return ok(
        {"userId": user_id, "removedProfileId": profile_id, "result": result.data},
        f"profile {profile_id} removed from user {user_id}",
    )


user_profiles = ToolDefinition(
    name="user_profiles",
    display_name="User's Profiles",
    description=(
        "Which profiles one user holds in this app — the answer to 'what access "
        "does this person have'. Neither list_users nor get_user carries it. An "
        "empty result means they hold no profile here, so they cannot use the "
        "app at all."
    ),
    parameters=[
        ToolParameter(
            name="user_id", type="string", description="Numeric user id, from list_users.",
            required=True,
        )
    ],
    execute=_user_profiles,
)

remove_profile = ToolDefinition(
    name="remove_profile",
    display_name="Remove Profile",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Take a profile away from a user, narrowing what they can do. Read "
        "user_profiles first so you remove the one you mean. Removing their only "
        "profile leaves them unable to use the app. Pauses for the user's "
        "confirmation."
    ),
    parameters=[
        ToolParameter(name="user_id", type="string", description="Numeric user id.", required=True),
        ToolParameter(
            name="profile_id", type="string", description="Numeric profile id, from user_profiles.",
            required=True,
        ),
    ],
    execute=_remove_profile,
)


ORG_TOOLS = [
    # Who is on the team, and what they hold.
    _pin(list_users_tool, drop=("client_code",)),
    get_user_tool,
    _pin(list_profiles_tool, drop=("app_id",), inject={"app_id": _resolve_app_id}),
    user_profiles,
    # The org chart the deal hierarchy is built on: entity-processor scopes a
    # deal read to `assignedUserId IN (subOrg)`, and subOrg is exactly the
    # reporting tree these two describe. Without them the agent can say "you
    # can see 40 deals" and not why.
    list_departments_tool,
    list_designations_tool,
    # Joiners and leavers, and the profile/role grants that go with them.
    make_user_active_tool,
    make_user_inactive_tool,
    unblock_user_tool,
    assign_profile_tool,
    remove_profile,
]

# The writes among them. Every one changes who can do what, or whether someone
# can sign in at all, so each pauses for the user before it runs.
ORG_MUTATING = {
    "make_user_active",
    "make_user_inactive",
    "unblock_user",
    "assign_profile",
    "remove_profile",
}


def _confirmable(tools: list) -> list:
    """Copies of the write tools declared as blocking elicitations.

    `BaseAgent` lints that every `CONFIRMATION_TOOLS` entry declares
    `kind='elicitation'`, and these arrive from AppBuilder as plain tools
    because AppBuilder does not confirm them.

    Copied rather than edited in place, and that is the whole point of this
    function: a `ToolDefinition` is a mutable dataclass and these are the *same
    objects* AppBuilder's own registry holds, so setting `kind` on them would
    silently start pausing AppBuilder's user administration for confirmation
    too. `dataclasses.replace` gives this agent its own instances and leaves
    that agent exactly as it was.
    """
    return [
        dataclasses.replace(t, kind="elicitation", elicit_mode="blocking")
        if t.name in ORG_MUTATING
        else t
        for t in tools
    ]


ORG_TOOLS = _confirmable(ORG_TOOLS)
