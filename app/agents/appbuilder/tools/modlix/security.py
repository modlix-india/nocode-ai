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
    r = await client.get("/api/security/applications", headers=headers, params={"page": _page_num(params), "size": _page_size(params)})
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
        "This is the security-side registration: appCode, owner, appAccessType (OWN/ANY/EXPLICIT), status."
    ),
    parameters=[
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
    r = await client.get("/api/security/rolesV2", headers=headers, params=p)
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


async def _execute_create_role(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    body: dict[str, Any] = {"name": name}
    if params.get("description"):
        body["description"] = params["description"]
    if params.get("app_id"):
        body["appId"] = params["app_id"]
    if params.get("parent_role_id"):
        body["parentRoleId"] = params["parent_role_id"]
    client, headers = _client_and_headers(context)
    r = await client.post("/api/security/rolesV2", headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    role_id = (r.data or {}).get("id", "?") if isinstance(r.data, dict) else "?"
    return ToolResult(success=True, summary=f"Created role '{name}' (id={role_id}).")


create_role_tool = ToolDefinition(
    name="create_role",
    description="Create a role. Authority string is auto-generated as Authorities.[APPCODE.]ROLE_<Name>.",
    parameters=[
        ToolParameter(name="name", type="string", description="Role display name"),
        ToolParameter(name="description", type="string", required=False, description="What this role grants"),
        ToolParameter(name="app_id", type="string", required=False, description="Bind to an app (omit = client-scoped)"),
        ToolParameter(name="parent_role_id", type="string", required=False, description="Parent role for inheritance"),
    ],
    execute=_execute_create_role,
)


# ═════════════════════════════════════════════════════════════════════════
#  PROFILES (1 tool)
# ═════════════════════════════════════════════════════════════════════════


async def _execute_list_profiles(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _client_and_headers(context)
    p: dict[str, Any] = {"page": 0, "size": _page_size(params, 100, 500)}
    if params.get("app_id"):
        p["appId"] = params["app_id"]
    r = await client.get("/api/security/profile", headers=headers, params=p)
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
    application_code = (params.get("application_code") or "").strip()
    if not application_code:
        return ToolResult(success=False, error="`application_code` is required")
    client, headers = _client_and_headers(context)
    r = await client.get("/api/security/transports/makeTransport", headers=headers, params={"applicationCode": application_code})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Security bundle for app '{application_code}':\n{json.dumps(r.data, indent=2, default=str)}")


export_security_app_tool = ToolDefinition(
    name="export_security_app",
    description="Export an app's security setup (users, clients, roles, profiles) as a JSON bundle. Pass the result to a peer env's security createAndApply to clone the auth setup. Treat as sensitive — includes credentials/auth setup.",
    parameters=[
        ToolParameter(name="application_code", type="string", description="appCode of the app to export from the security service"),
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
