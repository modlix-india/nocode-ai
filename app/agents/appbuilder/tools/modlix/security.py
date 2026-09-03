"""Security CRUD + transports — users, clients, apps, roles, profiles,
departments, designations, authority grammar, transport bundle apply.

Ports modlix-mcp/modlix_mcp/tools/{security,transports}.py — 23 tools total
(19 security + 4 transport).

Hits `/api/security/*` (MySQL-backed identity layer) and the cross-service
`/api/{ui|core|security}/transports` bundle endpoints. ClientHierarchy is
enforced server-side on every call (@PreAuthorize) — this module just calls
the endpoints; the platform decides what the JWT can see/do.

Destructive ops (assign/remove role, grant_app_access, apply_transport_*)
are flagged in their tool descriptions — the propose-then-confirm pattern
the CFA agent loop applies to CRUD names catches them.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from . import _conventions as c


# Shared param-description constants.
_DESC_USER_ID = "User id (numeric, as string)"
_DESC_SIZE = "Max rows"
_DESC_PAGE = "Zero-indexed page"
_DESC_SCOPE_UI_CORE = "'ui' or 'core' — which service to target"


def _client_and_headers(context: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context.get("headers") or {}


def _page_size(params: dict[str, Any], default: int = 100, cap: int = 1000) -> int:
    try:
        return max(1, min(int(params.get("size") or default), cap))
    except (TypeError, ValueError):
        return default


def _page_num(params: dict[str, Any]) -> int:
    try:
        return max(0, int(params.get("page") or 0))
    except (TypeError, ValueError):
        return 0


# ═════════════════════════════════════════════════════════════════════════
#  AUTH CHECK (1 tool)
# ═════════════════════════════════════════════════════════════════════════


async def _execute_verify_token(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _client_and_headers(context)
    r = await client.get("/api/security/verifyToken", headers=headers)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=json.dumps(r.data, indent=2, default=str))


verify_token_tool = ToolDefinition(
    name="verify_token",
    description="Verify the caller's bearer token and report the auth context (user, clientCode, verifiedAppCode, expiry). Useful first call before any destructive security op.",
    parameters=[],
    execute=_execute_verify_token,
)


# ═════════════════════════════════════════════════════════════════════════
#  USERS (8 tools)
# ═════════════════════════════════════════════════════════════════════════


async def _execute_list_users(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _client_and_headers(context)
    p: dict[str, Any] = {"page": _page_num(params), "size": _page_size(params, 100, 1000)}
    if params.get("client_code"):
        p["clientCode"] = params["client_code"]
    r = await client.get("/api/security/users", headers=headers, params=p)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "id": u.get("id"), "userName": u.get("userName"), "emailId": u.get("emailId"),
        "phoneNumber": u.get("phoneNumber"), "clientId": u.get("clientId"),
        "clientCode": u.get("clientCode"), "statusCode": u.get("statusCode"),
    } for u in content]
    total = (r.data or {}).get("totalElements", len(rows)) if isinstance(r.data, dict) else len(rows)
    return ToolResult(success=True, summary=f"Users (page {p['page']}, {len(rows)} of {total}):\n{json.dumps(rows, indent=2, default=str)}")


list_users_tool = ToolDefinition(
    name="list_users",
    description="List users visible to the caller (respects ClientHierarchy). Returns id + userName + emailId + clientCode + statusCode.",
    parameters=[
        ToolParameter(name="client_code", type="string", required=False, description="Filter to users in this client (omit = caller's)"),
        ToolParameter(name="size", type="integer", required=False, default=100, description=_DESC_SIZE),
        ToolParameter(name="page", type="integer", required=False, default=0, description=_DESC_PAGE),
    ],
    execute=_execute_list_users,
)


async def _execute_get_user(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    user_id = (params.get("user_id") or "").strip()
    if not user_id:
        return ToolResult(success=False, error="`user_id` is required")
    client, headers = _client_and_headers(context)
    r = await client.get(f"/api/security/users/{user_id}", headers=headers)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    d = r.data if isinstance(r.data, dict) else {}
    # Defensive password redaction.
    for k in ("password", "pin"):
        if k in d:
            d[k] = "<REDACTED>"
    return ToolResult(success=True, summary=json.dumps(d, indent=2, default=str))


get_user_tool = ToolDefinition(
    name="get_user",
    description="Read a user's full profile. Defensive password/pin redaction applied to the response.",
    parameters=[
        ToolParameter(name="user_id", type="string", description=_DESC_USER_ID),
    ],
    execute=_execute_get_user,
)


async def _user_action_call(
    context: dict[str, Any], method: str, path: str, json_body: dict[str, Any] | None,
    success_msg: str,
) -> ToolResult:
    """Shared shape for user-mutation calls (assign/remove role, etc.)."""
    client, headers = _client_and_headers(context)
    if method == "GET":
        r = await client.get(path, headers=headers)
    elif method == "POST":
        r = await client.post(path, headers=headers, json=json_body)
    else:
        r = await client.patch(path, headers=headers, json=json_body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=success_msg)


async def _execute_assign_role(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    user_id = (params.get("user_id") or "").strip()
    role_id = (params.get("role_id") or "").strip()
    if not user_id or not role_id:
        return ToolResult(success=False, error="`user_id` and `role_id` are required")
    return await _user_action_call(context, "GET", f"/api/security/users/{user_id}/assignRole/{role_id}", None, f"Assigned role {role_id} to user {user_id}.")


assign_role_tool = ToolDefinition(
    name="assign_role",
    description="Grant a role to a user. Caller needs Role_UPDATE + ability to manage the target user.",
    parameters=[
        ToolParameter(name="user_id", type="string", description="Target user id"),
        ToolParameter(name="role_id", type="string", description="Role id to assign"),
    ],
    execute=_execute_assign_role,
)


async def _execute_remove_role(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    user_id = (params.get("user_id") or "").strip()
    role_id = (params.get("role_id") or "").strip()
    if not user_id or not role_id:
        return ToolResult(success=False, error="`user_id` and `role_id` are required")
    return await _user_action_call(context, "GET", f"/api/security/users/{user_id}/removeRole/{role_id}", None, f"Removed role {role_id} from user {user_id}.")


remove_role_tool = ToolDefinition(
    name="remove_role",
    description="Revoke a role from a user.",
    parameters=[
        ToolParameter(name="user_id", type="string", description="Target user id"),
        ToolParameter(name="role_id", type="string", description="Role id to remove"),
    ],
    execute=_execute_remove_role,
)


async def _execute_assign_profile(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    user_id = (params.get("user_id") or "").strip()
    profile_id = (params.get("profile_id") or "").strip()
    if not user_id or not profile_id:
        return ToolResult(success=False, error="`user_id` and `profile_id` are required")
    return await _user_action_call(context, "GET", f"/api/security/users/{user_id}/assignProfile/{profile_id}", None, f"Assigned profile {profile_id} to user {user_id}.")


assign_profile_tool = ToolDefinition(
    name="assign_profile",
    description="Attach a profile (bundle of roles) to a user.",
    parameters=[
        ToolParameter(name="user_id", type="string", description="Target user id"),
        ToolParameter(name="profile_id", type="string", description="Profile id to assign"),
    ],
    execute=_execute_assign_profile,
)


async def _execute_unblock_user(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    user_id = (params.get("user_id") or "").strip()
    if not user_id:
        return ToolResult(success=False, error="`user_id` is required")
    return await _user_action_call(context, "POST", "/api/security/users/unblockUser", {"userId": user_id}, f"Unblocked user {user_id}.")


unblock_user_tool = ToolDefinition(
    name="unblock_user",
    description="Clear the account lockout flag (typically set after repeated failed login attempts). Status returns to ACTIVE.",
    parameters=[
        ToolParameter(name="user_id", type="string", description="Locked user id (statusCode=LOCKED)"),
    ],
    execute=_execute_unblock_user,
)


async def _execute_make_user_active(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    user_id = (params.get("user_id") or "").strip()
    if not user_id:
        return ToolResult(success=False, error="`user_id` is required")
    return await _user_action_call(context, "PATCH", "/api/security/users/makeUserActive", {"userId": user_id}, f"Activated user {user_id}.")


make_user_active_tool = ToolDefinition(
    name="make_user_active",
    description="Set user statusCode=ACTIVE.",
    parameters=[ToolParameter(name="user_id", type="string", description=_DESC_USER_ID)],
    execute=_execute_make_user_active,
)


async def _execute_make_user_inactive(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    user_id = (params.get("user_id") or "").strip()
    if not user_id:
        return ToolResult(success=False, error="`user_id` is required")
    return await _user_action_call(context, "PATCH", "/api/security/users/makeUserInActive", {"userId": user_id}, f"Deactivated user {user_id}.")


make_user_inactive_tool = ToolDefinition(
    name="make_user_inactive",
    description="Set user statusCode=INACTIVE. Soft-disable — user can't log in but data is preserved.",
    parameters=[ToolParameter(name="user_id", type="string", description=_DESC_USER_ID)],
    execute=_execute_make_user_inactive,
)


# ═════════════════════════════════════════════════════════════════════════
#  CLIENTS (2 tools)
# ═════════════════════════════════════════════════════════════════════════


async def _execute_list_clients(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _client_and_headers(context)
    r = await client.get("/api/security/clients", headers=headers, params={"page": _page_num(params), "size": _page_size(params)})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "id": x.get("id"), "code": x.get("code"), "name": x.get("name"),
        "typeCode": x.get("typeCode"), "levelType": x.get("levelType"),
        "statusCode": x.get("statusCode"), "managerId": x.get("managerId"),
    } for x in content]
    return ToolResult(success=True, summary=f"Clients ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_clients_tool = ToolDefinition(
    name="list_clients",
    description="List clients (tenants) visible via ClientHierarchy.",
    parameters=[
        ToolParameter(name="size", type="integer", required=False, default=100, description=_DESC_SIZE),
        ToolParameter(name="page", type="integer", required=False, default=0, description=_DESC_PAGE),
    ],
    execute=_execute_list_clients,
)


async def _execute_get_client_by_code(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    code = (params.get("client_code") or "").strip()
    if not code:
        return ToolResult(success=False, error="`client_code` is required")
    client, headers = _client_and_headers(context)
    r = await client.get("/api/security/clients/internal/getClientByCode", headers=headers, params={"clientCode": code})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=json.dumps(r.data, indent=2, default=str))


get_client_by_code_tool = ToolDefinition(
    name="get_client_by_code",
    description="Read a client by its code (e.g. 'SYSTEM', 'CITYV').",
    parameters=[ToolParameter(name="client_code", type="string", description="Client code")],
    execute=_execute_get_client_by_code,
)


# ═════════════════════════════════════════════════════════════════════════
#  SECURITY APPS (2 tools)
# ═════════════════════════════════════════════════════════════════════════


async def _execute_list_security_apps(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _client_and_headers(context)
    qp: dict[str, Any] = {"page": _page_num(params), "size": _page_size(params)}
    if params.get("app_code"):
        qp["appCode"] = params["app_code"]
    r = await client.get("/api/security/applications", headers=headers, params=qp)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "id": a.get("id"), "appCode": a.get("appCode"), "appName": a.get("appName"),
        "appType": a.get("appType"), "appAccessType": a.get("appAccessType"),
        "clientId": a.get("clientId"), "status": a.get("status"),
    } for a in content]
    return ToolResult(success=True, summary=f"Security apps ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_security_apps_tool = ToolDefinition(
    name="list_security_apps",
    description=(
        "List app records in the SECURITY service (distinct from /api/ui/applications which lists UI Application definitions). "
        "This is the security-side registration: appCode, owner, appAccessType (OWN/ANY/EXPLICIT), status. "
        "Pass `app_code` for an exact-match lookup (returns 0 or 1 row) — much faster than paginating through hundreds of apps."
    ),
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description="Exact appCode filter — returns just that app's security row"),
        ToolParameter(name="size", type="integer", required=False, default=100, description=_DESC_SIZE),
        ToolParameter(name="page", type="integer", required=False, default=0, description=_DESC_PAGE),
    ],
    execute=_execute_list_security_apps,
)


async def _execute_grant_app_access(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_id = (params.get("app_id") or "").strip()
    client_id = (params.get("client_id") or "").strip()
    if not app_id or not client_id:
        return ToolResult(success=False, error="`app_id` and `client_id` are required")
    write_access = bool(params.get("write_access", False))
    client, headers = _client_and_headers(context)
    r = await client.post(
        f"/api/security/applications/{app_id}/access",
        headers=headers,
        json={"clientId": client_id, "writeAccess": write_access},
    )
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Granted client {client_id} {'write' if write_access else 'read'} access to app {app_id}.")


grant_app_access_tool = ToolDefinition(
    name="grant_app_access",
    description="Grant a client EXPLICIT access to an app (only applicable when appAccessType='EXPLICIT').",
    parameters=[
        ToolParameter(name="app_id", type="string", description="App id"),
        ToolParameter(name="client_id", type="string", description="Client id to grant access to"),
        ToolParameter(name="write_access", type="boolean", required=False, default=False, description="Write access (true) vs read-only (false)"),
    ],
    execute=_execute_grant_app_access,
)


# ═════════════════════════════════════════════════════════════════════════
#  ROLES (2 tools)
# ═════════════════════════════════════════════════════════════════════════


async def _execute_list_roles(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _client_and_headers(context)
    p: dict[str, Any] = {"page": 0, "size": _page_size(params, 100, 500)}
    if params.get("app_code"):
        p["appCode"] = params["app_code"]
    r = await client.get("/api/security/rolev2", headers=headers, params=p)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "id": x.get("id"), "name": x.get("name"), "shortName": x.get("shortName"),
        "authority": x.get("authority"), "appId": x.get("appId"),
        "appName": x.get("appName"), "parentRoleId": x.get("parentRoleId"),
    } for x in content]
    return ToolResult(success=True, summary=f"Roles ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_roles_tool = ToolDefinition(
    name="list_roles",
    description="List roles. Optional app_code filters to roles assignable in that app.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description="Filter roles assignable in this app"),
        ToolParameter(name="size", type="integer", required=False, default=100, description=_DESC_SIZE),
    ],
    execute=_execute_list_roles,
)


# Client-scoped role names whose generated authority collides with a token the
# platform itself gates on (@PreAuthorize across the security service). A
# client-scoped role called "Owner" under any client literally becomes
# Authorities.ROLE_Owner, the client-owner super-authority: assigning it hands
# out client-admin powers. The Chit Fund run created exactly that role.
_RESERVED_CLIENT_ROLE_AUTHORITIES: frozenset[str] = frozenset({
    "Authorities.ROLE_Owner",
    "Authorities.ROLE_Client_MANAGE",
    "Authorities.ROLE_ClientManager",
})


async def _resolve_role_app(
    params: dict[str, Any], context: dict[str, Any], client: Any, headers: dict[str, str],
) -> tuple[str | None, str | None, ToolResult | None]:
    """Decide which app (if any) a new role binds to.

    Returns (app_id, app_code, error). Precedence: explicit `app_id` >
    `client_scoped=true` > `app_code` (parameter, then the session app).
    Every other modlix tool defaults to the session app; create_role used to
    be the one that silently ignored it, which is how the Chit Fund build
    ended up with client-scoped roles behind app-scoped page gates.
    """
    explicit_id = params.get("app_id")
    if explicit_id:
        # Never assume the session app here: an explicit id may belong to a
        # different app, and echoing the session app's authority would hand
        # the model a token the platform never bound. If app_code was ALSO
        # given, make sure the two agree.
        code = str(params.get("app_code") or "").strip()
        if code:
            from .app_admin import _find_security_app_by_code  # lazy: avoids an import cycle
            row = await _find_security_app_by_code(client, headers, code)
            if row and str(row.get("id")) != str(explicit_id):
                return None, None, ToolResult(
                    success=False,
                    error=(
                        f"app_id {explicit_id} does not belong to appCode '{code}' (that app's id is {row.get('id')}). "
                        "Pass one or the other."
                    ),
                )
        return str(explicit_id), (code or None), None
    if params.get("client_scoped"):
        return None, None, None
    from app.agents.appbuilder.tools._shared import resolve_app_code
    app_code = resolve_app_code(params, context)
    if not app_code:
        return None, None, None
    from .app_admin import _find_security_app_by_code  # lazy: avoids an import cycle
    row = await _find_security_app_by_code(client, headers, app_code)
    if not row or row.get("id") is None:
        return None, None, ToolResult(
            success=False,
            error=(
                f"No active security app with appCode '{app_code}'. Run create_app first, "
                "or pass client_scoped=true if you really want a client-level role."
            ),
        )
    return str(row["id"]), app_code, None


async def _execute_create_role(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    client, headers = _client_and_headers(context)
    app_id, app_code, err = await _resolve_role_app(params, context, client, headers)
    if err is not None:
        return err

    # Mirror the platform's AuthoritiesNameUtil.makeRoleName so the caller
    # sees the exact token pages/storages must reference. The platform never
    # populates RoleV2.authority on the wire, so this is the only place the
    # model can learn it.
    authority = c.make_role_authority(name, app_code if app_id else None)
    if app_id is None and authority in _RESERVED_CLIENT_ROLE_AUTHORITIES:
        return ToolResult(
            success=False,
            error=(
                f"A client-scoped role named '{name}' would carry {authority}, a platform-reserved "
                "authority (client owner/manager powers). Scope it to your app instead: "
                "create_role(name=..., app_code=<appcode>) gives Authorities.<APPCODE>.ROLE_" + name.replace(" ", "_") + "."
            ),
        )

    body: dict[str, Any] = {"name": name}
    if params.get("description"):
        body["description"] = params["description"]
    if app_id:
        body["appId"] = app_id
    if params.get("parent_role_id"):
        body["parentRoleId"] = params["parent_role_id"]
    r = await client.post("/api/security/rolev2", headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    role_id = (r.data or {}).get("id", "?") if isinstance(r.data, dict) else "?"

    if app_id:
        scope = f"app={app_code}" if app_code else f"app_id={app_id}"
        shown = authority if app_code else "Authorities.<APPCODE>.ROLE_" + name.replace(" ", "_")
        return ToolResult(
            success=True,
            summary=(
                f"Created role '{name}' (id={role_id}, authority={shown}, {scope}). "
                "Use this exact token in page `permission` and component `visibility`. "
                f"Grant it with assign_role(user_id=..., role_id={role_id}); provision at least one test user per role."
            ),
        )
    return ToolResult(
        success=True,
        summary=(
            f"Created CLIENT-SCOPED role '{name}' (id={role_id}, authority={authority}). "
            "It will NOT satisfy an Authorities.<APPCODE>.ROLE_ gate. If this role guards pages in an app, "
            "re-create it with app_code=<appcode>."
        ),
    )


create_role_tool = ToolDefinition(
    name="create_role",
    description=(
        "Create a role. Defaults to APP-SCOPED for the session app (or `app_code`), producing the authority "
        "Authorities.<APPCODE>.ROLE_<Name>, which is the only form an app's page/storage gates can match. "
        "The result echoes the exact authority string: paste it into `permission` / `visibility`, never hand-write it. "
        "Pass client_scoped=true only for a deliberate client-level role (Authorities.ROLE_<Name>)."
    ),
    parameters=[
        ToolParameter(name="name", type="string", description="Role display name (spaces become underscores in the authority)"),
        ToolParameter(name="description", type="string", required=False, description="What the role grants"),
        ToolParameter(name="app_code", type="string", required=False, description="App to scope the role to; defaults to the session app. Resolved to the numeric security app id for you."),
        ToolParameter(name="client_scoped", type="boolean", required=False, default=False, description="True = client-level role (Authorities.ROLE_<Name>); never satisfies an <APPCODE>.-prefixed gate. Reserved names (Owner, Client_MANAGE, ClientManager) are refused."),
        ToolParameter(name="app_id", type="string", required=False, description="Escape hatch: numeric security app id (NOT a Mongo ObjectId). Prefer app_code."),
        ToolParameter(name="parent_role_id", type="string", required=False, description="Parent role for inheritance"),
    ],
    execute=_execute_create_role,
)


# ═════════════════════════════════════════════════════════════════════════
#  PROFILES (1 tool)
# ═════════════════════════════════════════════════════════════════════════


async def _execute_list_profiles(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _client_and_headers(context)
    app_id = params.get("app_id")
    if not app_id:
        return ToolResult(
            success=False,
            error="`app_id` is required — the platform exposes profiles per-app at /api/security/app/{appId}/profiles",
        )
    p: dict[str, Any] = {"page": 0, "size": _page_size(params, 100, 500)}
    r = await client.get(f"/api/security/app/{app_id}/profiles", headers=headers, params=p)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "id": x.get("id"), "name": x.get("name"), "title": x.get("title"),
        "appId": x.get("appId"), "clientId": x.get("clientId"),
        "defaultProfile": x.get("defaultProfile"), "rootProfileId": x.get("rootProfileId"),
    } for x in content]
    return ToolResult(success=True, summary=f"Profiles ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_profiles_tool = ToolDefinition(
    name="list_profiles",
    description="List profiles (named bundles of roles assignable to users).",
    parameters=[
        ToolParameter(name="app_id", type="string", required=False, description="Filter to profiles in this app"),
        ToolParameter(name="size", type="integer", required=False, default=100, description=_DESC_SIZE),
    ],
    execute=_execute_list_profiles,
)


# ═════════════════════════════════════════════════════════════════════════
#  ORG STRUCTURE (2 tools)
# ═════════════════════════════════════════════════════════════════════════


async def _execute_list_departments(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _client_and_headers(context)
    r = await client.get("/api/security/departments", headers=headers, params={"page": 0, "size": _page_size(params, 100, 500)})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{"id": d.get("id"), "name": d.get("name"), "description": d.get("description"), "parentDepartmentId": d.get("parentDepartmentId")} for d in content]
    return ToolResult(success=True, summary=f"Departments ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_departments_tool = ToolDefinition(
    name="list_departments",
    description="List departments in the caller's client.",
    parameters=[ToolParameter(name="size", type="integer", required=False, default=100, description=_DESC_SIZE)],
    execute=_execute_list_departments,
)


async def _execute_list_designations(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _client_and_headers(context)
    r = await client.get("/api/security/designations", headers=headers, params={"page": 0, "size": _page_size(params, 100, 500)})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "id": d.get("id"), "name": d.get("name"),
        "parentDesignationId": d.get("parentDesignationId"),
        "departmentId": d.get("departmentId"),
        "profileId": d.get("profileId"),
    } for d in content]
    return ToolResult(success=True, summary=f"Designations ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_designations_tool = ToolDefinition(
    name="list_designations",
    description="List designations (job titles + reporting chain) in the caller's client.",
    parameters=[ToolParameter(name="size", type="integer", required=False, default=100, description=_DESC_SIZE)],
    execute=_execute_list_designations,
)


# ═════════════════════════════════════════════════════════════════════════
#  AUTHORITY HELPER (1 tool — pure local utility)
# ═════════════════════════════════════════════════════════════════════════


async def _execute_build_authority(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    kind = (params.get("kind") or "").strip()
    name = (params.get("name") or "").strip()
    if not kind or not name:
        return ToolResult(success=False, error="`kind` and `name` are required")
    app_code = params.get("app_code") or None
    if kind == "role":
        return ToolResult(success=True, summary=c.make_role_authority(name, app_code))
    if kind == "profile":
        return ToolResult(success=True, summary=c.make_profile_authority(name, app_code))
    if kind == "permission":
        return ToolResult(success=True, summary=c.make_permission_authority(name, app_code))
    return ToolResult(success=False, error=f"kind must be 'role', 'profile', or 'permission', got {kind!r}")


build_authority_tool = ToolDefinition(
    name="build_authority",
    description="Construct a properly-formatted Authority string. Use the result on Storage createAuth / readAuth / etc. fields. No API call.",
    parameters=[
        ToolParameter(name="kind", type="string", description="role | profile | permission", enum=["role", "profile", "permission"]),
        ToolParameter(name="name", type="string", description="Role/profile/permission name (spaces become underscores)"),
        ToolParameter(name="app_code", type="string", required=False, description="App code prefix (uppercased); omit for client-scoped"),
    ],
    execute=_execute_build_authority,
)


# ═════════════════════════════════════════════════════════════════════════
#  TRANSPORTS (4 tools)
# ═════════════════════════════════════════════════════════════════════════


async def _execute_export_security_app(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    # `application_code` was this tool's original spelling and the only one of
    # its kind across 149 app-code parameters; it is accepted as a fallback so
    # an in-flight session that already fetched the old schema still dispatches.
    app_code = (params.get("app_code") or params.get("application_code") or "").strip()
    if not app_code:
        return ToolResult(success=False, error="`app_code` is required")
    client, headers = _client_and_headers(context)
    r = await client.get("/api/security/transports/makeTransport", headers=headers, params={"applicationCode": app_code})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Security bundle for app '{app_code}':\n{json.dumps(r.data, indent=2, default=str)}")


export_security_app_tool = ToolDefinition(
    name="export_security_app",
    description="Export an app's security setup (users, clients, roles, profiles) as a JSON bundle. Pass the result to a peer env's security createAndApply to clone the auth setup. Treat as sensitive — includes credentials/auth setup.",
    parameters=[
        ToolParameter(name="app_code", type="string", description="appCode of the app to export from the security service"),
    ],
    execute=_execute_export_security_app,
)


def _validate_scope(scope: str) -> str | None:
    if scope not in ("ui", "core"):
        return f"`scope` must be 'ui' or 'core', got {scope!r}"
    return None


async def _execute_list_transport_types(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    scope = (params.get("scope") or "ui").strip()
    err = _validate_scope(scope)
    if err:
        return ToolResult(success=False, error=err)
    client, headers = _client_and_headers(context)
    r = await client.get(f"/api/{scope}/transports/transportTypes", headers=headers)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    names = r.data if isinstance(r.data, list) else []
    return ToolResult(success=True, summary=f"Transport types ({scope}, {len(names)}): {names}")


list_transport_types_tool = ToolDefinition(
    name="list_transport_types",
    description="List object types that the named service can (de)serialize for transports. Use when planning an export.",
    parameters=[
        ToolParameter(name="scope", type="string", required=False, default="ui", description=_DESC_SCOPE_UI_CORE),
    ],
    execute=_execute_list_transport_types,
)


async def _execute_apply_transport_by_id(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    transport_id = (params.get("transport_id") or "").strip()
    scope = (params.get("scope") or "ui").strip()
    if not transport_id:
        return ToolResult(success=False, error="`transport_id` is required")
    err = _validate_scope(scope)
    if err:
        return ToolResult(success=False, error=err)
    client, headers = _client_and_headers(context)
    r = await client.get(f"/api/{scope}/transports/applyTransport/{transport_id}", headers=headers)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Applied {scope} transport id={transport_id}: result={r.data}")


apply_transport_by_id_tool = ToolDefinition(
    name="apply_transport_by_id",
    description="Apply a previously-uploaded transport bundle to the current app/client context (by transport id). DESTRUCTIVE — overwrites matching definitions. Read the transport via the web UI first.",
    parameters=[
        ToolParameter(name="transport_id", type="string", description="Transport document id (Mongo _id) from a prior web-UI upload"),
        ToolParameter(name="scope", type="string", required=False, default="ui", description=_DESC_SCOPE_UI_CORE),
    ],
    execute=_execute_apply_transport_by_id,
)


async def _execute_apply_transport_by_code(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    code = (params.get("code") or "").strip()
    scope = (params.get("scope") or "ui").strip()
    if not code:
        return ToolResult(success=False, error="`code` is required")
    err = _validate_scope(scope)
    if err:
        return ToolResult(success=False, error=err)
    client, headers = _client_and_headers(context)
    r = await client.get(f"/api/{scope}/transports/applyTransportCode/{code}", headers=headers)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Applied {scope} transport code={code}: result={r.data}")


apply_transport_by_code_tool = ToolDefinition(
    name="apply_transport_by_code",
    description="Apply a transport bundle by its uniqueTransportCode (shorter than the id). DESTRUCTIVE — overwrites matching definitions.",
    parameters=[
        ToolParameter(name="code", type="string", description="uniqueTransportCode from the transport doc"),
        ToolParameter(name="scope", type="string", required=False, default="ui", description=_DESC_SCOPE_UI_CORE),
    ],
    execute=_execute_apply_transport_by_code,
)


# ═════════════════════════════════════════════════════════════════════════
#  APP PROPERTIES + APP-REGISTRATION (customer-signup machinery)
# ═════════════════════════════════════════════════════════════════════════
#
# Customer-facing apps need MORE than just create_app to be usable:
#
#  • appUsageType on the security_app row must be one of B2C/B2B/B2X (NOT S which
#    blocks all customer registration), set via update_security_app.
#  • REGISTRATION_TYPE app property must be set to REGISTRATION_TYPE_NO_VERIFICATION
#    (or _VERIFICATION / _CODE_IMMEDIATE_LOGIN_IMMEDIATE) via set_app_property.
#  • A user-profile entry in security_app_reg_user_profile must exist so a
#    profile gets auto-assigned on registration — without it the registered user
#    has zero permissions in the app.
#  • A file-access entry in security_app_reg_file_access grants the registered
#    user STATIC/SECURED file paths — typically Authorities.Logged_IN.
#  • An app-access entry in security_app_reg_access lets the registered user
#    actually reach the app (allow_app_id pointing back to the app itself).
#
# The convenience tool `configure_app_for_customer_signup` does ALL of this in
# one call. Prefer it over the granular set_/add_ tools for the common case;
# fall back to the granular tools for special shapes (B2B, multi-profile, etc.).


_REG_OBJECT_TYPES = ("userProfile", "userRole", "fileAccess", "appAccess",
                     "department", "designation", "userDesignation", "profileRestriction")
_LEVELS = ("CLIENT", "CUSTOMER", "CONSUMER")
_USAGE_TYPES = ("S", "B", "B2C", "B2B", "B2X", "X")
_REG_TYPES = (
    "REGISTRATION_TYPE_NO_REGISTRATION",
    "REGISTRATION_TYPE_NO_VERIFICATION",
    "REGISTRATION_TYPE_VERIFICATION",
    "REGISTRATION_TYPE_CODE_IMMEDIATE_LOGIN_IMMEDIATE",
)
_APP_PROPERTY_API = "/api/security/applications/property"
_DESC_TARGET_APP_CODE = "Target appCode"


async def _execute_set_app_property(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_id = (params.get("app_id") or "").strip()
    name = (params.get("name") or "").strip()
    value = params.get("value")
    if not app_id or not name:
        return ToolResult(success=False, error="`app_id` and `name` are required")
    body: dict[str, Any] = {"appId": app_id, "name": name, "value": value}
    if params.get("client_id"):
        body["clientId"] = params["client_id"]
    client, headers = _client_and_headers(context)
    r = await client.post(_APP_PROPERTY_API, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Set property {name}={value!r} on app id={app_id}.")


set_app_property_tool = ToolDefinition(
    name="set_app_property",
    description=(
        "Set a security-app property (security_app_property table) — distinct from `update_app` "
        "which patches the UI-side application override doc.\n\n"
        "Common property names:\n"
        "  • REGISTRATION_TYPE — REGISTRATION_TYPE_NO_VERIFICATION | _VERIFICATION | _CODE_IMMEDIATE_LOGIN_IMMEDIATE | _NO_REGISTRATION.\n"
        "    Required for customer signup. Without it, /api/security/clients/register returns 'Feature not supported'.\n"
        "  • Anything else app-team-defined (read first via list_app_properties to see what's already set).\n\n"
        "Prefer `configure_app_for_customer_signup` for the common case — it sets REGISTRATION_TYPE "
        "along with the other 4 pieces customer onboarding needs."
    ),
    parameters=[
        ToolParameter(name="app_id", type="string", description="security_app id (numeric, from list_security_apps or list_apps)"),
        ToolParameter(name="name", type="string", description="Property name, e.g. REGISTRATION_TYPE"),
        ToolParameter(name="value", type="string", description="Property value"),
        ToolParameter(name="client_id", type="string", required=False, description="Owning client_id (defaults to the app's owning client)"),
    ],
    execute=_execute_set_app_property,
)


async def _execute_list_app_properties(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    qp: dict[str, Any] = {}
    if params.get("app_id"):
        qp["appId"] = params["app_id"]
    if params.get("app_code"):
        qp["appCode"] = params["app_code"]
    if params.get("client_id"):
        qp["clientId"] = params["client_id"]
    if params.get("name"):
        qp["propName"] = params["name"]
    client, headers = _client_and_headers(context)
    r = await client.get(_APP_PROPERTY_API, headers=headers, params=qp)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"App properties:\n{json.dumps(r.data, indent=2, default=str)}")


list_app_properties_tool = ToolDefinition(
    name="list_app_properties",
    description=(
        "Read security_app_property rows. Filter by `app_code`, `app_id`, and/or property `name`. "
        "Use to confirm REGISTRATION_TYPE etc. before configuring."
    ),
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description="App code filter"),
        ToolParameter(name="app_id", type="string", required=False, description="App id filter"),
        ToolParameter(name="name", type="string", required=False, description="Specific property name"),
        ToolParameter(name="client_id", type="string", required=False, description="Client id filter"),
    ],
    execute=_execute_list_app_properties,
)


async def _execute_update_security_app(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_id = (params.get("app_id") or "").strip()
    if not app_id:
        return ToolResult(success=False, error="`app_id` is required")
    patch: dict[str, Any] = {}
    if params.get("app_usage_type"):
        ut = params["app_usage_type"].upper()
        if ut not in _USAGE_TYPES:
            return ToolResult(success=False, error=f"app_usage_type must be one of {_USAGE_TYPES}")
        patch["appUsageType"] = ut
    if params.get("app_access_type"):
        patch["appAccessType"] = params["app_access_type"].upper()
    if params.get("app_type"):
        patch["appType"] = params["app_type"].upper()
    if params.get("app_name"):
        patch["appName"] = params["app_name"]
    if not patch:
        return ToolResult(success=False, error="No mutable fields supplied (app_usage_type / app_access_type / app_type / app_name)")
    client, headers = _client_and_headers(context)
    r = await client.patch(f"/api/security/applications/{app_id}", headers=headers, json=patch)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Patched security app id={app_id} with {list(patch.keys())}.")


update_security_app_tool = ToolDefinition(
    name="update_security_app",
    description=(
        "PATCH the security_app row's metadata. Use this to flip `app_usage_type` from the create_app default "
        "(S=Standalone, rejects registration) to a multi-tenant variant:\n"
        "  • B2C — individual consumers register themselves (typical for customer-facing apps like POS, e-commerce, social)\n"
        "  • B2B — businesses register (each customer is itself a business)\n"
        "  • B2X — open to both individuals and businesses\n"
        "  • B — business-only, INTERNAL (still blocks registration; only valid for sub-tenants admins onboard manually)\n"
        "  • S — standalone (sites, internal tools — no signup possible)\n"
        "  • X — wildcard / any\n\n"
        "Distinct from `update_app` which patches the UI override doc (themes, properties.defaultPage, languages)."
    ),
    parameters=[
        ToolParameter(name="app_id", type="string", description="security_app id (numeric)"),
        ToolParameter(name="app_usage_type", type="string", required=False, description="S | B | B2C | B2B | B2X | X"),
        ToolParameter(name="app_access_type", type="string", required=False, description="OWN | ANY | EXPLICIT"),
        ToolParameter(name="app_type", type="string", required=False, description="APP | SITE | POSTER"),
        ToolParameter(name="app_name", type="string", required=False, description="Internal display name (must equal app_code)"),
    ],
    execute=_execute_update_security_app,
)


async def _execute_add_app_reg_entry(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_code = (params.get("app_code") or "").strip()
    kind = (params.get("kind") or "").strip()
    body = params.get("body")
    if not app_code or kind not in _REG_OBJECT_TYPES:
        return ToolResult(success=False, error=f"`app_code` and `kind` (one of {_REG_OBJECT_TYPES}) are required")
    if not isinstance(body, dict):
        return ToolResult(success=False, error="`body` must be an object — see description for required fields per kind")
    client, headers = _client_and_headers(context)
    r = await client.post(f"/api/security/applications/reg/{app_code}/{kind}", headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Added app reg {kind} for {app_code}: id={(r.data or {}).get('id', '?')}")


add_app_reg_entry_tool = ToolDefinition(
    name="add_app_reg_entry",
    description=(
        "Add a row to one of the app-registration tables — used to set up what an app does on registration "
        "(auto-assign profile/role, grant file paths, grant cross-app access, etc.).\n\n"
        "Endpoint: POST /api/security/applications/reg/{appCode}/{kind}\n\n"
        "Body shape varies by kind. Required common fields: `clientId`, `appId`, `clientType` ('BUS' or 'IND'), "
        "`level` (CLIENT/CUSTOMER/CONSUMER), `businessType` (default 'COMMON').\n\n"
        "Per-kind extras (top-level in body):\n"
        "  • userProfile      → `profileId`\n"
        "  • userRole         → `roleId`\n"
        "  • fileAccess       → `resourceType` (STATIC|SECURED), `accessName`, `writeAccess` (bool), `path` (string), `allowSubPathAccess` (bool)\n"
        "  • appAccess        → `allowAppId`, `writeAccess`, `register` (bool — can register users into the target app)\n"
        "  • department       → `name`, optional `parentDepartmentId`\n"
        "  • designation      → `name`, optional parent/next/department ids\n"
        "  • userDesignation  → `designationId`\n"
        "  • profileRestriction → `profileId`, restriction fields\n\n"
        "Prefer `configure_app_for_customer_signup` for the standard customer-signup set; this raw tool is for unusual shapes."
    ),
    parameters=[
        ToolParameter(name="app_code", type="string", description=_DESC_TARGET_APP_CODE),
        ToolParameter(name="kind", type="string", description=f"One of {_REG_OBJECT_TYPES}"),
        ToolParameter(name="body", type="object", description="Full registration record body — see description for per-kind fields"),
    ],
    execute=_execute_add_app_reg_entry,
)


async def _execute_list_app_reg_entries(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_code = (params.get("app_code") or "").strip()
    kind = (params.get("kind") or "").strip()
    if not app_code or kind not in _REG_OBJECT_TYPES:
        return ToolResult(success=False, error=f"`app_code` and `kind` (one of {_REG_OBJECT_TYPES}) are required")
    query: dict[str, Any] = {"page": _page_num(params), "size": _page_size(params, 50, 200)}
    for k_src, k_dst in (("client_code", "clientCode"), ("client_id", "clientId"), ("client_type", "clientType"),
                        ("level", "level"), ("business_type", "businessType")):
        if params.get(k_src):
            query[k_dst] = params[k_src]
    client, headers = _client_and_headers(context)
    r = await client.post(f"/api/security/applications/reg/{app_code}/{kind}/query", headers=headers, json=query)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    return ToolResult(success=True, summary=f"{kind} entries for {app_code} ({len(content)}):\n{json.dumps(content, indent=2, default=str)}")


list_app_reg_entries_tool = ToolDefinition(
    name="list_app_reg_entries",
    description="List app-registration rows of a given kind. Useful to confirm what's already configured before adding more.",
    parameters=[
        ToolParameter(name="app_code", type="string", description=_DESC_TARGET_APP_CODE),
        ToolParameter(name="kind", type="string", description=f"One of {_REG_OBJECT_TYPES}"),
        ToolParameter(name="client_code", type="string", required=False, description="Filter by client"),
        ToolParameter(name="client_id", type="string", required=False, description="Filter by client id"),
        ToolParameter(name="client_type", type="string", required=False, description="BUS | IND"),
        ToolParameter(name="level", type="string", required=False, description="CLIENT | CUSTOMER | CONSUMER"),
        ToolParameter(name="business_type", type="string", required=False, default="COMMON", description="Business type code, default COMMON"),
        ToolParameter(name="size", type="integer", required=False, default=50, description=_DESC_SIZE),
        ToolParameter(name="page", type="integer", required=False, default=0, description=_DESC_PAGE),
    ],
    execute=_execute_list_app_reg_entries,
)


async def _execute_delete_app_reg_entry(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    kind = (params.get("kind") or "").strip()
    entry_id = (params.get("entry_id") or "").strip()
    if kind not in _REG_OBJECT_TYPES or not entry_id:
        return ToolResult(success=False, error=f"`kind` (one of {_REG_OBJECT_TYPES}) and `entry_id` are required")
    client, headers = _client_and_headers(context)
    r = await client.delete(f"/api/security/applications/reg/{kind}/{entry_id}", headers=headers)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Deleted {kind} entry id={entry_id}.")


delete_app_reg_entry_tool = ToolDefinition(
    name="delete_app_reg_entry",
    description="Delete an app-registration row by kind + id. Destructive — confirm before calling.",
    parameters=[
        ToolParameter(name="kind", type="string", description=f"One of {_REG_OBJECT_TYPES}"),
        ToolParameter(name="entry_id", type="string", description="Registration row id"),
    ],
    execute=_execute_delete_app_reg_entry,
)


# ── Profile creation ─────────────────────────────────────────────────────


def _build_arrangement(params: dict[str, Any]) -> dict[str, Any]:
    """Render `role_ids` into the platform's arrangement map.

    ProfileDAO.getRoleIdsFromArrangements walks the arrangement values looking
    for `roleId` on each entry (skipping `assignable: false`) and writes the
    security_profile_role rows from it. An empty arrangement therefore yields a
    profile that grants nothing, which in turn makes every app-scoped role in it
    unassignable (UserService.assignRoleToUser -> hasAccessToRoles -> 403).
    An explicit `arrangement` wins; `role_ids` is the friendly form.
    """
    explicit = params.get("arrangement")
    if isinstance(explicit, dict) and explicit:
        return explicit
    role_ids = params.get("role_ids") or []
    if isinstance(role_ids, (str, int)):
        role_ids = [role_ids]
    arrangement: dict[str, Any] = {}
    for order, rid in enumerate(role_ids):
        if rid is None or str(rid).strip() == "":
            continue
        arrangement[f"r{rid}"] = {
            "roleId": int(str(rid).strip()),
            "assignable": True,
            "order": order,
        }
    return arrangement


def _build_profile_body(name: str, app_id: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    # The platform's hasReadAccess(appId, entity.clientId) runs BEFORE the late-fixup
    # that sets clientId from the caller. If clientId is null on the entity, the access
    # check returns empty Mono → 403 "Cannot create Profile for the selected client".
    # Default to the caller's clientId (SYSTEM=1). `arrangement` is also required to
    # avoid an NPE in ProfileDAO.getRoleIdsFromArrangements; empty {} is fine.
    body: dict[str, Any] = {
        "name": name,
        "appId": app_id,
        "clientId": params.get("client_id") or context.get("client_id") or "1",
        "arrangement": _build_arrangement(params),
    }
    if params.get("description"):
        body["description"] = params["description"]
    if params.get("root_role_id"):
        body["rootProfileId"] = params["root_role_id"]
    if params.get("default_profile") is not None:
        body["defaultProfile"] = bool(params["default_profile"])
    return body


async def _find_existing_profile(client: Any, headers: dict, app_id: str, name: str) -> dict | None:
    existing = await client.get(f"/api/security/app/{app_id}/profiles", headers=headers, params={"page": 0, "size": 200})
    if not (existing.success and isinstance(existing.data, dict)):
        return None
    for p in existing.data.get("content") or []:
        if p.get("name") == name and str(p.get("appId")) == str(app_id):
            return p
    return None


async def _execute_create_profile(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    app_id = (params.get("app_id") or "").strip()
    if not name or not app_id:
        return ToolResult(success=False, error="`name` and `app_id` are required")
    client, headers = _client_and_headers(context)
    found = await _find_existing_profile(client, headers, app_id, name)
    if found is not None:
        return ToolResult(
            success=True,
            summary=f"Profile '{name}' already exists (id={found.get('id')}) for app_id={app_id} — reusing.",
        )
    body = _build_profile_body(name, app_id, params, context)
    r = await client.post("/api/security/app/profiles", headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    granted = sorted(
        v["roleId"] for v in (body["arrangement"] or {}).values()
        if isinstance(v, dict) and v.get("roleId") is not None
    )
    if granted:
        note = (
            f" Grants role ids {granted}. Assign to a user with "
            f"assign_profile(user_id, {(r.data or {}).get('id', '?')})."
        )
    else:
        note = (
            " WARNING: this profile grants NO roles, so it is inert and the app-scoped "
            "roles it should carry stay unassignable (assign_role will 403). Re-create "
            "it with role_ids=[...]."
        )
    return ToolResult(
        success=True,
        summary=(
            f"Created profile '{name}' (id={(r.data or {}).get('id', '?')}) for "
            f"app_id={app_id} (clientId={body['clientId']}).{note}"
        ),
    )


create_profile_tool = ToolDefinition(
    name="create_profile",
    description=(
        "Create a profile (bundle of roles) for an app. Profiles are app-scoped and assigned to users via "
        "`assign_profile` (post-registration) OR via `add_app_reg_entry(kind=userProfile)` "
        "(auto-assigned on registration).\n\n"
        "PASS `role_ids`. A profile with no roles is inert, and an app-scoped role that sits in no "
        "profile can never be granted to anyone: `assign_role` fails the platform's "
        "`hasAccessToRoles` check (which resolves roles THROUGH profiles) and returns "
        "403 'role forbidden for the user'. So the working provisioning chain is: "
        "create_role(app_code=...) -> create_profile(role_ids=[...]) -> assign_profile(user, profile). "
        "Without role_ids the app's own role-gated pages are unreachable by every user.\n\n"
        "Endpoint: POST /api/security/app"
    ),
    parameters=[
        ToolParameter(name="name", type="string", description="Profile name"),
        ToolParameter(name="app_id", type="string", description="security_app id this profile belongs to"),
        ToolParameter(name="description", type="string", required=False, description="Profile description"),
        ToolParameter(name="default_profile", type="boolean", required=False, description="Mark as default-on-registration"),
        ToolParameter(
            name="role_ids", type="array", required=False,
            description=(
                "Role ids this profile grants, e.g. [295, 296] from create_role. "
                "Rendered into the platform's `arrangement` map for you."
            ),
        ),
        ToolParameter(
            name="arrangement", type="object", required=False,
            description=(
                "Raw platform arrangement map, for nested/ordered layouts. Shape: "
                '{"<key>": {"roleId": 295, "assignable": true, "name": "Owner", '
                '"subArrangements": {...}}}. Prefer role_ids unless you need nesting.'
            ),
        ),
        ToolParameter(name="client_id", type="string", required=False, description="Owning client id (defaults to the session client)"),
    ],
    execute=_execute_create_profile,
)


# ── Convenience: configure app for customer signup ───────────────────────


def _validate_signup_params(params: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    app_code = (params.get("app_code") or "").strip()
    app_id = (params.get("app_id") or "").strip()
    profile_id = (params.get("profile_id") or "").strip()
    usage_type = (params.get("app_usage_type") or "B2C").upper()
    reg_type = params.get("registration_type") or "REGISTRATION_TYPE_NO_VERIFICATION"
    level = (params.get("level") or ("CONSUMER" if usage_type == "B2C" else "CUSTOMER")).upper()
    if not app_code or not app_id or not profile_id:
        return "`app_code`, `app_id` (security_app id), and `profile_id` are all required", None
    if usage_type not in _USAGE_TYPES:
        return f"app_usage_type must be one of {_USAGE_TYPES}", None
    if reg_type not in _REG_TYPES:
        return f"registration_type must be one of {_REG_TYPES}", None
    return None, {
        "app_code": app_code, "app_id": app_id, "profile_id": profile_id,
        "usage_type": usage_type, "reg_type": reg_type, "level": level,
        "client_id": (params.get("client_id") or "1").strip(),
        "client_type": (params.get("client_type") or "INDV").upper(),
        "business_type": (params.get("business_type") or "COMMON").upper(),
    }


async def _signup_step_set_usage_type(client: Any, headers: dict, p: dict[str, Any]) -> str | None:
    r = await client.patch(f"/api/security/applications/{p['app_id']}", headers=headers, json={"appUsageType": p["usage_type"]})
    return None if r.success else f"step 1 (PATCH appUsageType): {r.error}"


async def _signup_step_set_reg_property(client: Any, headers: dict, p: dict[str, Any]) -> str | None:
    r = await client.post(_APP_PROPERTY_API, headers=headers,
        json={"appId": p["app_id"], "clientId": p["client_id"], "name": "REGISTRATION_TYPE", "value": p["reg_type"]})
    return None if r.success else f"step 2 (REGISTRATION_TYPE): {r.error}"


async def _signup_step_add_user_profile(client: Any, headers: dict, p: dict[str, Any], common: dict[str, Any]) -> str | None:
    r = await client.post(f"/api/security/applications/reg/{p['app_code']}/userProfile", headers=headers,
        json={**common, "profileId": p["profile_id"]})
    return None if r.success else f"step 3 (userProfile reg): {r.error}"


async def _signup_step_add_file_access(client: Any, headers: dict, app_code: str, common: dict[str, Any]) -> str | None:
    for resource_type in ("STATIC", "SECURED"):
        r = await client.post(f"/api/security/applications/reg/{app_code}/fileAccess", headers=headers,
            json={**common, "resourceType": resource_type, "accessName": "Authorities.Logged_IN",
                  "writeAccess": True, "path": "", "allowSubPathAccess": True})
        if not r.success:
            return f"step 4 (fileAccess {resource_type}): {r.error}"
    return None


async def _signup_step_add_self_access(client: Any, headers: dict, p: dict[str, Any], common: dict[str, Any]) -> str | None:
    r = await client.post(f"/api/security/applications/reg/{p['app_code']}/appAccess", headers=headers,
        json={**common, "allowAppId": p["app_id"], "writeAccess": False, "register": False})
    return None if r.success else f"step 5 (appAccess self): {r.error}"


async def _execute_configure_app_for_customer_signup(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    err, p = _validate_signup_params(params)
    if err or p is None:
        return ToolResult(success=False, error=err or "invalid params")
    client, headers = _client_and_headers(context)
    common = {
        "clientId": p["client_id"], "appId": p["app_id"], "clientType": p["client_type"],
        "level": p["level"], "businessType": p["business_type"],
    }
    steps = (
        lambda: _signup_step_set_usage_type(client, headers, p),
        lambda: _signup_step_set_reg_property(client, headers, p),
        lambda: _signup_step_add_user_profile(client, headers, p, common),
        lambda: _signup_step_add_file_access(client, headers, p["app_code"], common),
        lambda: _signup_step_add_self_access(client, headers, p, common),
    )
    for step_fn in steps:
        e = await step_fn()
        if e:
            return ToolResult(success=False, error=e)
    done = [
        f"appUsageType → {p['usage_type']}",
        f"REGISTRATION_TYPE → {p['reg_type']}",
        f"userProfile reg → profile_id={p['profile_id']} @ level={p['level']}",
        "fileAccess reg → STATIC + SECURED Authorities.Logged_IN",
        f"appAccess reg → self-allow ({p['app_code']})",
    ]
    return ToolResult(
        success=True,
        summary=(
            f"Configured '{p['app_code']}' for customer signup ({p['usage_type']}/{p['reg_type']}/level={p['level']}):\n  - "
            + "\n  - ".join(done)
            + f"\n\nUsers can now self-register via POST /api/security/clients/register with appCode={p['app_code']}; "
              f"they'll be auto-assigned profile_id={p['profile_id']}."
        ),
    )


configure_app_for_customer_signup_tool = ToolDefinition(
    name="configure_app_for_customer_signup",
    description=(
        "**Required step for any customer-facing app** — after `create_app` + `create_profile`, call this to "
        "enable self-service registration. Does all 5 platform-side wires in one call:\n"
        "  1. PATCH appUsageType → B2C (or whatever you pass)\n"
        "  2. Set REGISTRATION_TYPE app property (default: REGISTRATION_TYPE_NO_VERIFICATION)\n"
        "  3. Add `userProfile` reg entry → auto-assigns the given profile to every new registrant\n"
        "  4. Add `fileAccess` reg entries (STATIC + SECURED, Authorities.Logged_IN)\n"
        "  5. Add `appAccess` reg entry (self-reference) so the user can reach the app\n\n"
        "Without this, the app's `create_app` defaults leave it as appUsageType=S (Standalone), which "
        "BLOCKS /api/security/clients/register with 'Not allowed for Standalone Applications'. The only "
        "users who can log in are pre-provisioned sysadmins, which is fine for marketing sites but "
        "fundamentally broken for any product with customers.\n\n"
        "When to skip: only for internal tools / marketing sites / appbuilder-class apps. If the user "
        "describes anything customer-facing (POS, ticketing, e-commerce, social, etc.), call this."
    ),
    parameters=[
        ToolParameter(name="app_code", type="string", description=_DESC_TARGET_APP_CODE),
        ToolParameter(name="app_id", type="string", description="security_app id (numeric) — get from list_security_apps"),
        ToolParameter(name="profile_id", type="string", description="Profile to auto-assign on registration"),
        ToolParameter(name="app_usage_type", type="string", required=False, default="B2C", description="B2C (individuals) | B2B (business clients) | B2X (both) | X"),
        ToolParameter(name="registration_type", type="string", required=False, default="REGISTRATION_TYPE_NO_VERIFICATION", description="One of REGISTRATION_TYPE_NO_VERIFICATION | _VERIFICATION | _CODE_IMMEDIATE_LOGIN_IMMEDIATE"),
        ToolParameter(name="level", type="string", required=False, description="CLIENT | CUSTOMER | CONSUMER — defaults from usage_type (B2C→CONSUMER, others→CUSTOMER)"),
        ToolParameter(name="client_id", type="string", required=False, default="1", description="Owning client_id (default 1 = SYSTEM)"),
        ToolParameter(name="client_type", type="string", required=False, default="INDV", description="IND for individual consumers, BUS for business clients"),
        ToolParameter(name="business_type", type="string", required=False, default="COMMON", description="Business type code, default COMMON"),
    ],
    execute=_execute_configure_app_for_customer_signup,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    # Auth
    verify_token_tool,
    # Users
    list_users_tool,
    get_user_tool,
    assign_role_tool,
    remove_role_tool,
    assign_profile_tool,
    unblock_user_tool,
    make_user_active_tool,
    make_user_inactive_tool,
    # Clients
    list_clients_tool,
    get_client_by_code_tool,
    # Security apps
    list_security_apps_tool,
    grant_app_access_tool,
    # Roles + profiles
    list_roles_tool,
    create_role_tool,
    list_profiles_tool,
    create_profile_tool,
    # App properties + app-registration (customer-signup machinery)
    set_app_property_tool,
    list_app_properties_tool,
    update_security_app_tool,
    add_app_reg_entry_tool,
    list_app_reg_entries_tool,
    delete_app_reg_entry_tool,
    configure_app_for_customer_signup_tool,
    # Org structure
    list_departments_tool,
    list_designations_tool,
    # Authority helper
    build_authority_tool,
    # Transports
    export_security_app_tool,
    list_transport_types_tool,
    apply_transport_by_id_tool,
    apply_transport_by_code_tool,
]
