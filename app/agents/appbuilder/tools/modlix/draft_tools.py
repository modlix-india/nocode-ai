"""Tools for the draft surface: hand over the review link, and publish.

These are the two things the agent has to be able to do once its edits stop
going live. Without the link there is nothing to review; without publish there
is no way to finish.
"""

from __future__ import annotations

from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from . import _draft_surface as drafts
from . import _page_ops as p_ops


def _client_and_headers(context: dict[str, Any]) -> tuple:
    from app.core.tools.http_client import SaasClient
    from app.config import settings

    client: SaasClient = context.get("saas_client") or SaasClient(settings.GATEWAY_URL)
    return client, context.get("headers", {}) or {}


def _app_code(params: dict[str, Any], context: dict[str, Any]) -> str:
    from app.agents.appbuilder.tools._shared import resolve_app_code
    return resolve_app_code(params, context)


def _unsupported(app_code: str) -> ToolResult:
    return ToolResult(
        success=False,
        error=(
            f"This deployment has no draft surface, so there is nothing to review for "
            f"'{app_code}'. Your edits went live as they always have. Say so plainly "
            f"rather than offering the user a review step that does not exist."
        ),
    )


# ── get_draft_link ────────────────────────────────────────────────────────────


async def _execute_get_draft_link(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_code = _app_code(params, context)
    if not app_code:
        return ToolResult(success=False, error="No appCode. Pass `app_code` or set it on the session.")

    client, headers = _client_and_headers(context)
    if not await drafts.supported(client, headers, app_code):
        return _unsupported(app_code)

    url, err = await drafts.ensure_draft_url(client, headers, app_code)
    if err:
        return ToolResult(
            success=False,
            error=(
                f"Could not get a draft link for '{app_code}': {err}. Minting needs write "
                f"access to the app, and the environment needs `security.draftUrlSuffix` set; "
                f"without it there is no hostname to mint."
            ),
        )
    if not url:
        return ToolResult(success=False, error=f"No draft link for '{app_code}' and minting returned nothing.")

    return ToolResult(
        success=True,
        summary=(
            f"Draft surface for '{app_code}': {url}\n"
            f"Unpublished changes are visible there and nowhere else. Anyone with this "
            f"link can see them, so treat it as a credential."
        ),
        data={"app_code": app_code, "draft_url": url},
    )


get_draft_link_tool = ToolDefinition(
    name="get_draft_link",
    description=(
        "The URL where this app's unpublished changes can be seen, minting one if the app "
        "has none yet. Give it to the user whenever you have left work in the draft, and "
        "use it as the host when you screenshot your own unpublished changes: the ordinary "
        "page URL renders the LIVE app and will not show them."
    ),
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to the app this session is working in"),
    ],
    execute=_execute_get_draft_link,
)


# ── list_pending_changes ──────────────────────────────────────────────────────


async def _execute_list_pending_changes(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_code = _app_code(params, context)
    if not app_code:
        return ToolResult(success=False, error="No appCode. Pass `app_code` or set it on the session.")

    client, headers = _client_and_headers(context)
    if not await drafts.supported(client, headers, app_code):
        return _unsupported(app_code)

    r = await client.get(f"{drafts.PUBLISH_API}/{app_code}/pending", headers=dict(headers))
    if not r.success:
        return ToolResult(success=False, error=r.error)

    import json as _json
    return ToolResult(
        success=True,
        summary=f"Unpublished changes in '{app_code}':\n{_json.dumps(r.data, indent=2, default=str)}",
        data=r.data,
    )


list_pending_changes_tool = ToolDefinition(
    name="list_pending_changes",
    description="Everything in this app that is drafted and not yet published, grouped by object type.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to the app this session is working in"),
    ],
    execute=_execute_list_pending_changes,
)


# ── publish ───────────────────────────────────────────────────────────────────


async def _execute_publish_app(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_code = _app_code(params, context)
    if not app_code:
        return ToolResult(success=False, error="No appCode. Pass `app_code` or set it on the session.")
    if not params.get("confirmed"):
        return ToolResult(
            success=False,
            error=(
                "Publishing makes every drafted change live for everyone, and that is the "
                "user's call, not yours. Ask them, then call again with confirmed=true. Do "
                "not publish just because you finished the work."
            ),
        )

    client, headers = _client_and_headers(context)
    if not await drafts.supported(client, headers, app_code):
        return _unsupported(app_code)

    r = await client.post(f"{drafts.PUBLISH_API}/{app_code}", headers=dict(headers))
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Published every pending change in '{app_code}'. It is now live.")


publish_app_tool = ToolDefinition(
    name="publish_app",
    description=(
        "Promote every drafted change in the app to live. Requires confirmed=true, and the "
        "user has to be the one who decides: finishing the work is not the same as being "
        "asked to ship it."
    ),
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to the app this session is working in"),
        ToolParameter(name="confirmed", type="boolean", required=False, default=False, description="The user explicitly asked to publish"),
    ],
    execute=_execute_publish_app,
)


async def _execute_discard_page_draft(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_code = _app_code(params, context)
    page_name = (params.get("page_name") or "").strip()
    if not page_name:
        return ToolResult(success=False, error="`page_name` is required")

    client, headers = _client_and_headers(context)
    if not await drafts.supported(client, headers, app_code):
        return _unsupported(app_code)

    page, err = await p_ops.fetch_page_by_name(client, page_name, app_code, headers)
    if err:
        return ToolResult(success=False, error=err)
    page_id = (page or {}).get("id")
    if not page_id:
        return ToolResult(success=False, error=f"Page '{page_name}' has no id.")

    r = await client.delete(f"{p_ops.API_PREFIX}/{page_id}/draft", headers=dict(headers))
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Discarded the unpublished changes on '{page_name}'. Live is untouched.")


discard_page_draft_tool = ToolDefinition(
    name="discard_page_draft",
    description="Throw away a page's unpublished changes and go back to what is live.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to the app this session is working in"),
    ],
    execute=_execute_discard_page_draft,
)


DRAFT_TOOLS = [
    get_draft_link_tool,
    list_pending_changes_tool,
    publish_app_tool,
    discard_page_draft_tool,
]
