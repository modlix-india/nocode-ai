"""Account selection tools — matches the ds/chatv2 two-step flow.

For both Google Ads and Meta, account selection is a two-step pick:

- **Google Ads**: MCC (manager) → customer account.
- **Meta**: Business → ad account.

Each platform has a `*_parent_accounts` tool (step 1) and a `*_accounts(parent_id)`
tool (step 2). Each auto-selects when only one option is available.
"""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.tools._shared import build_ds_headers
from app.agents.adzump.adapters.google.accounts import GoogleAccountsAdapter
from app.agents.adzump.adapters.meta.accounts import MetaAccountsAdapter

logger = logging.getLogger(__name__)


def _format_account(a: dict) -> str:
    cid = a.get("id") or a.get("customer_id", "?")
    name = (a.get("name") or "").strip()
    return f"{name} (ID: {cid})" if name else f"ID: {cid}"


def _remember_names(context: dict, accounts: list[dict], id_key: str) -> None:
    """Record fetched IDs (for guard) and their display names (for rendering).

    Every fetched ID becomes a key in ``account_names`` so the guard in
    ``set_campaign_spec`` can confirm the ID was shown to the user. The value
    is the descriptive name when the API returned one, or an empty string —
    which renderers treat as "name not available, show ID only".
    """
    ctx = context.get("session_context")
    if ctx is None:
        return
    names = ctx.setdefault("account_names", {})
    for a in accounts:
        if aid := a.get(id_key):
            names[str(aid)] = (a.get("name") or "").strip()


def _options_pairs(items: list[dict], id_key: str) -> str:
    def _label(a: dict) -> str:
        return (a.get("name") or "").strip() or f"Account {a.get(id_key, '?')}"
    return "; ".join(f"{_label(a)} → {a.get(id_key, '')}" for a in items)


def _list_summary(
    label: str, items: list[dict], pairs: str, spec_field: str,
) -> str:
    lines = [f"Retrieved {len(items)} {label}(s):"]
    lines.extend(f"- {_format_account(a)}" for a in items)
    lines.append("")
    lines.append(
        f"Pairs (label → value): {pairs}. "
        f"Now call `present_options(question=\"<one short question>\", "
        f"options=[<{{label,value}} dicts from these pairs>])` and STOP — "
        f"no chat text. On user click, `set_campaign_spec({spec_field}=<id>)`."
    )
    return "\n".join(lines)


# ── Google Ads ────────────────────────────────────────────────────────

_GOOGLE_NOT_CONNECTED = (
    "Your Google Ads account isn't connected yet. Tell the user (verbatim): "
    "\"To continue, please connect your Google Ads account from your workspace's "
    "connections/integrations page, then reply 'ready' and we'll pick up here.\" "
    "Do NOT call `set_campaign_spec` for `parent_account` or `account`. "
    "Do NOT invent placeholder IDs. Stop and wait for the user."
)


async def _fetch_google_parent_accounts(params: dict, context: dict) -> ToolResult:
    """Step 1: list Google Ads manager accounts (MCCs). Auto-select if only one."""
    auth_headers = build_ds_headers(context)
    client_code = context.get("client_code", "")
    adapter = GoogleAccountsAdapter()

    try:
        top_level = await adapter.list_top_level_accounts(client_code, auth_headers)
    except Exception as e:
        logger.warning("google_parent_accounts_failed: %s", str(e)[:200])
        return ToolResult(success=False, error=_GOOGLE_NOT_CONNECTED)

    managers = [a for a in top_level if a["is_manager"]]
    direct = [a for a in top_level if not a["is_manager"]]

    if not managers and not direct:
        return ToolResult(success=False, error=_GOOGLE_NOT_CONNECTED)

    _remember_names(context, top_level, "customer_id")

    # Auto-select: exactly 1 manager and no direct accounts → skip to children.
    if len(managers) == 1 and not direct:
        mcc = managers[0]
        return ToolResult(
            success=True,
            data={"parent": mcc, "auto_selected": True},
            summary=(
                f"Only one manager: {_format_account(mcc)}. Store via "
                f"`set_campaign_spec(parent_account='{mcc['customer_id']}')`, then "
                f"call `fetch_google_accounts(parent_id='{mcc['customer_id']}')`."
            ),
        )

    items = managers + direct
    return ToolResult(
        success=True,
        data={"parents": items, "managers": managers, "direct_accounts": direct},
        summary=_list_summary(
            "Google Ads manager account",
            items,
            _options_pairs(items, "customer_id"),
            "parent_account",
        ),
    )


async def _fetch_google_accounts(params: dict, context: dict) -> ToolResult:
    """Step 2: list ad accounts under a Google Ads MCC. Auto-select if only one."""
    parent_id = (params.get("parent_id") or "").strip()
    if not parent_id:
        return ToolResult(success=False, error="parent_id is required.")

    auth_headers = build_ds_headers(context)
    client_code = context.get("client_code", "")

    try:
        adapter = GoogleAccountsAdapter()
        accounts = await adapter._list_sub_accounts(parent_id, client_code, auth_headers)
    except Exception as e:
        logger.warning("google_accounts_failed: parent=%s err=%s",
                       parent_id, str(e)[:200])
        return ToolResult(success=False, error=_GOOGLE_NOT_CONNECTED)

    if not accounts:
        return ToolResult(
            success=True,
            data={"accounts": []},
            summary=(
                f"No ad accounts found under MCC {parent_id}. "
                "Ask the user to pick a different manager or check their access."
            ),
        )

    _remember_names(context, accounts, "customer_id")

    if len(accounts) == 1:
        acct = accounts[0]
        return ToolResult(
            success=True,
            data={"account": acct, "auto_selected": True},
            summary=(
                f"Only one ad account: {_format_account(acct)}. Store via "
                f"`set_campaign_spec(account='{acct['customer_id']}')`."
            ),
        )

    return ToolResult(
        success=True,
        data={"accounts": accounts},
        summary=_list_summary(
            "ad account",
            accounts,
            _options_pairs(accounts, "customer_id"),
            "account",
        ),
    )


# ── Meta ──────────────────────────────────────────────────────────────

_META_NOT_CONNECTED = (
    "Your Meta (Facebook/Instagram) account isn't connected yet. Tell the user (verbatim): "
    "\"To continue, please connect your Meta Business account from your workspace's "
    "connections/integrations page, then reply 'ready' and we'll pick up here.\" "
    "Do NOT call `set_campaign_spec` for `parent_account`, `account`, `fb_page`, or `ig_page`. "
    "Do NOT invent placeholder IDs. Stop and wait for the user."
)


_NO_FB_PAGES = (
    "No Facebook pages are linked to this Meta Business. Tell the user to create or "
    "assign a Facebook page to this Business (Meta Business Suite → Accounts → Pages), "
    "then reply 'ready' and we'll pick up here. Do NOT invent a page id. Stop and wait."
)


_NO_IG_ACCOUNTS = (
    "No Instagram Business account is linked to this Facebook page. Tell the user to "
    "connect an Instagram account to the page (Page Settings → Linked Accounts → Instagram) "
    "or pick a different page, then reply 'ready'. Do NOT invent an Instagram id. Stop and wait."
)


async def _fetch_meta_parent_accounts(params: dict, context: dict) -> ToolResult:
    """Step 1: list Meta Business accounts. Auto-select if only one."""
    auth_headers = build_ds_headers(context)
    client_code = context.get("client_code", "")
    adapter = MetaAccountsAdapter()

    try:
        businesses = await adapter.list_business_accounts(client_code, auth_headers)
    except Exception as e:
        logger.warning("meta_parent_accounts_failed: %s", str(e)[:200])
        return ToolResult(success=False, error=_META_NOT_CONNECTED)

    if not businesses:
        return ToolResult(success=False, error=_META_NOT_CONNECTED)

    _remember_names(context, businesses, "id")

    if len(businesses) == 1:
        biz = businesses[0]
        return ToolResult(
            success=True,
            data={"parent": biz, "auto_selected": True},
            summary=(
                f"Only one Meta Business: {_format_account(biz)}. Store via "
                f"`set_campaign_spec(parent_account='{biz['id']}')`, then call "
                f"`fetch_meta_accounts(parent_id='{biz['id']}')`."
            ),
        )

    return ToolResult(
        success=True,
        data={"parents": businesses},
        summary=_list_summary(
            "Meta Business account",
            businesses,
            _options_pairs(businesses, "id"),
            "parent_account",
        ),
    )


async def _fetch_meta_accounts(params: dict, context: dict) -> ToolResult:
    """Step 2: list ad accounts under a Meta Business. Auto-select if only one."""
    parent_id = (params.get("parent_id") or "").strip()
    if not parent_id:
        return ToolResult(success=False, error="parent_id is required.")

    auth_headers = build_ds_headers(context)
    client_code = context.get("client_code", "")

    try:
        adapter = MetaAccountsAdapter()
        accounts = await adapter.list_ad_accounts(parent_id, client_code, auth_headers)
    except Exception as e:
        logger.warning("meta_accounts_failed: parent=%s err=%s",
                       parent_id, str(e)[:200])
        return ToolResult(success=False, error=_META_NOT_CONNECTED)

    if not accounts:
        return ToolResult(
            success=True,
            data={"accounts": []},
            summary=(
                f"No ad accounts under business {parent_id}. "
                "Ask the user to pick a different business."
            ),
        )

    _remember_names(context, accounts, "id")

    if len(accounts) == 1:
        acct = accounts[0]
        return ToolResult(
            success=True,
            data={"account": acct, "auto_selected": True},
            summary=(
                f"Only one Meta ad account: {_format_account(acct)}. Store via "
                f"`set_campaign_spec(account='{acct['id']}')`."
            ),
        )

    return ToolResult(
        success=True,
        data={"accounts": accounts},
        summary=_list_summary(
            "Meta ad account",
            accounts,
            _options_pairs(accounts, "id"),
            "account",
        ),
    )


async def _fetch_meta_fb_pages(params: dict, context: dict) -> ToolResult:
    """List Facebook pages under a Meta Business. Auto-select if only one."""
    business_id = (params.get("parent_id") or "").strip()
    if not business_id:
        return ToolResult(success=False, error="parent_id is required.")

    auth_headers = build_ds_headers(context)
    client_code = context.get("client_code", "")

    try:
        adapter = MetaAccountsAdapter()
        pages = await adapter.list_fb_pages(business_id, client_code, auth_headers)
    except Exception as e:
        logger.warning("meta_fb_pages_failed: business=%s err=%s",
                       business_id, str(e)[:200])
        return ToolResult(success=False, error=_META_NOT_CONNECTED)

    if not pages:
        return ToolResult(success=False, error=_NO_FB_PAGES)

    _remember_names(context, pages, "id")

    if len(pages) == 1:
        page = pages[0]
        return ToolResult(
            success=True,
            data={"page": page, "auto_selected": True},
            summary=(
                f"Only one Facebook page: {_format_account(page)}. Store via "
                f"`set_campaign_spec(fb_page='{page['id']}')`, then call "
                f"`fetch_meta_ig_accounts(page_id='{page['id']}')`."
            ),
        )

    return ToolResult(
        success=True,
        data={"pages": pages},
        summary=_list_summary(
            "Facebook page",
            pages,
            _options_pairs(pages, "id"),
            "fb_page",
        ),
    )


async def _fetch_meta_ig_accounts(params: dict, context: dict) -> ToolResult:
    """List Instagram Business accounts under a Facebook page. Auto-select if only one."""
    page_id = (params.get("page_id") or "").strip()
    if not page_id:
        return ToolResult(success=False, error="page_id is required.")

    auth_headers = build_ds_headers(context)
    client_code = context.get("client_code", "")

    try:
        adapter = MetaAccountsAdapter()
        accounts = await adapter.list_ig_accounts(page_id, client_code, auth_headers)
    except Exception as e:
        logger.warning("meta_ig_accounts_failed: page=%s err=%s",
                       page_id, str(e)[:200])
        return ToolResult(success=False, error=_META_NOT_CONNECTED)

    if not accounts:
        return ToolResult(success=False, error=_NO_IG_ACCOUNTS)

    _remember_names(context, accounts, "id")

    if len(accounts) == 1:
        acct = accounts[0]
        return ToolResult(
            success=True,
            data={"account": acct, "auto_selected": True},
            summary=(
                f"Only one Instagram account: {_format_account(acct)}. Store via "
                f"`set_campaign_spec(ig_page='{acct['id']}')`."
            ),
        )

    return ToolResult(
        success=True,
        data={"accounts": accounts},
        summary=_list_summary(
            "Instagram account",
            accounts,
            _options_pairs(accounts, "id"),
            "ig_page",
        ),
    )


# ── Tool definitions ──────────────────────────────────────────────────

fetch_google_parent_accounts = ToolDefinition(
    name="fetch_google_parent_accounts",
    description=(
        "Step 1 of Google Ads account selection. Lists the user's manager "
        "accounts (MCCs). If only one exists, auto-selects it. Returned data "
        "has `parents` (or `parent` + `auto_selected=true`)."
    ),
    display_name="Fetch Google Parent Accounts",
    parameters=[],
    execute=_fetch_google_parent_accounts,
)

fetch_google_accounts = ToolDefinition(
    name="fetch_google_accounts",
    description=(
        "Step 2 of Google Ads account selection. Lists the customer (ad) "
        "accounts under a specific manager (MCC). If only one exists, "
        "auto-selects it. Call only after the user has picked a manager from "
        "fetch_google_parent_accounts."
    ),
    display_name="Fetch Google Accounts",
    parameters=[
        ToolParameter(
            name="parent_id",
            type="string",
            description="The selected MCC's customer_id.",
            required=True,
        ),
    ],
    execute=_fetch_google_accounts,
)

fetch_meta_parent_accounts = ToolDefinition(
    name="fetch_meta_parent_accounts",
    description=(
        "Step 1 of Meta account selection. Lists the user's Business accounts. "
        "If only one exists, auto-selects it. Returned data has `parents` (or "
        "`parent` + `auto_selected=true`)."
    ),
    display_name="Fetch Meta Parent Accounts",
    parameters=[],
    execute=_fetch_meta_parent_accounts,
)

fetch_meta_accounts = ToolDefinition(
    name="fetch_meta_accounts",
    description=(
        "Step 2 of Meta account selection. Lists the ad accounts under a "
        "specific Business. If only one exists, auto-selects it. Call only "
        "after the user has picked a business from fetch_meta_parent_accounts."
    ),
    display_name="Fetch Meta Accounts",
    parameters=[
        ToolParameter(
            name="parent_id",
            type="string",
            description="The selected Meta Business id.",
            required=True,
        ),
    ],
    execute=_fetch_meta_accounts,
)

fetch_meta_fb_pages = ToolDefinition(
    name="fetch_meta_fb_pages",
    description=(
        "Step 3 of Meta account selection (after parent business + ad account). "
        "Lists the Facebook pages accessible to the chosen Business. If only one "
        "exists, auto-selects it. Call only after `account` is stored."
    ),
    display_name="Fetch Facebook Pages",
    parameters=[
        ToolParameter(
            name="parent_id",
            type="string",
            description="The selected Meta Business id (parent_account).",
            required=True,
        ),
    ],
    execute=_fetch_meta_fb_pages,
)

fetch_meta_ig_accounts = ToolDefinition(
    name="fetch_meta_ig_accounts",
    description=(
        "Step 4 of Meta account selection. Lists Instagram Business accounts "
        "linked to a specific Facebook page. If only one exists, auto-selects it. "
        "Call only after `fb_page` is stored."
    ),
    display_name="Fetch Instagram Accounts",
    parameters=[
        ToolParameter(
            name="page_id",
            type="string",
            description="The selected Facebook page id (fb_page).",
            required=True,
        ),
    ],
    execute=_fetch_meta_ig_accounts,
)

ACCOUNT_TOOLS = [
    fetch_google_parent_accounts,
    fetch_google_accounts,
    fetch_meta_parent_accounts,
    fetch_meta_accounts,
    fetch_meta_fb_pages,
    fetch_meta_ig_accounts,
]
