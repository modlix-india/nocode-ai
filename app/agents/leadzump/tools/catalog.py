"""Catalog and shape-of-the-business tools: products, pipeline, stage counts.

These are the tools that turn a person's words into the ids everything else
needs. A stage is not a string in this CRM: it is a row belonging to one
product template, and `deal_move_stage` refuses one from any other template. So
`pipeline_describe` is a precondition for moving a deal, not a nicety.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.leadzump.tools._client import (
    ANALYTICS,
    PRODUCTS,
    SOURCES,
    STAGES,
    budget_rows,
    client,
    headers,
    ok,
    to_epoch,
)
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

# Stages are partitioned across two platforms and a deal walks the first before
# the second. Describing only the default one would show half a pipeline and
# read as the whole of it.
PLATFORMS = ("PRE_QUALIFICATION", "POST_QUALIFICATION")

PRODUCT_FIELDS = ("id", "code", "name", "description", "productTemplateId")


async def _product_list(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    query: dict[str, Any] = {
        "size": max(1, min(int(params.get("size") or 50), 100)),
        "page": max(0, int(params.get("page") or 0)),
        "sort": "name,ASC",
    }
    if params.get("active_only", True):
        # The filter takes the DTO's field name (`isActive`), while the same
        # column comes back on the wire as `active` — Lombok's getter for a
        # boolean `isActive` is `isActive()`, which Jackson names `active`.
        query["isActive"] = 1

    result = await client().get(PRODUCTS, headers=headers(context), params=query)
    if not result.success:
        return ToolResult(success=False, error=f"Could not list products: {result.error}")

    page = result.data if isinstance(result.data, dict) else {}
    rows = [
        {k: p.get(k) for k in PRODUCT_FIELDS if p.get(k) not in (None, "")}
        for p in (page.get("content") or [])
        if isinstance(p, dict)
    ]
    return ok(
        {"products": rows, "total": page.get("totalElements", len(rows))},
        f"{len(rows)} product(s)",
        max_chars=6000,
    )


async def _resolve_template_id(
    params: dict[str, Any], context: dict[str, Any]
) -> tuple[Any, ToolResult | None]:
    """The product template behind whichever handle the caller gave."""
    template_id = params.get("product_template_id")
    if template_id not in (None, "", 0):
        return template_id, None

    product = params.get("product_id") or params.get("product_code")
    if product in (None, "", 0):
        return None, ToolResult(
            success=False,
            error=(
                "Pass product_id (or product_code) so the pipeline can be read "
                "for that product, or product_template_id directly. Call "
                "product_list first if you have neither."
            ),
        )

    path = f"{PRODUCTS}/code/{product}" if not str(product).isdigit() else f"{PRODUCTS}/{product}"
    result = await client().get(path, headers=headers(context))
    if not result.success or not isinstance(result.data, dict):
        return None, ToolResult(
            success=False, error=f"Could not read product '{product}': {result.error}"
        )

    template_id = result.data.get("productTemplateId")
    if not template_id:
        return None, ToolResult(
            success=False,
            error=(
                f"Product '{result.data.get('name') or product}' has no product "
                f"template, so it has no pipeline. Deals on it cannot be moved "
                f"between stages until one is set in the app."
            ),
        )
    return template_id, None


async def _pipeline_describe(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    template_id, err = await _resolve_template_id(params, context)
    if err:
        return err

    stages: list[dict[str, Any]] = []
    failures: list[str] = []
    for platform in PLATFORMS:
        result = await client().get(
            f"{STAGES}/values/ordered",
            headers=headers(context),
            params={"productTemplateId": template_id, "platform": platform},
        )
        if not result.success:
            # A 404 here means "this template has no stages on this platform",
            # which is ordinary — a template may be entirely pre-qualification.
            failures.append(f"{platform}: {result.error}")
            continue
        for entry in result.data or []:
            if not isinstance(entry, dict):
                continue
            parent = entry.get("parent") or {}
            stages.append(
                {
                    "platform": platform,
                    "id": parent.get("id"),
                    "name": parent.get("name"),
                    "order": parent.get("order"),
                    "stageType": parent.get("stageType"),
                    "isSuccess": parent.get("isSuccess"),
                    "isFailure": parent.get("isFailure"),
                    "statuses": [
                        {"id": c.get("id"), "name": c.get("name"), "order": c.get("order")}
                        for c in (entry.get("child") or [])
                        if isinstance(c, dict)
                    ],
                }
            )

    if not stages:
        detail = "; ".join(failures) if failures else "the template defines none"
        return ToolResult(
            success=False,
            error=f"No pipeline stages found for product template {template_id} ({detail}).",
        )

    return ok(
        {"productTemplateId": template_id, "stages": stages},
        f"{len(stages)} stage(s) across {len({s['platform'] for s in stages})} platform(s)",
        max_chars=8000,
    )


def _bucket_filter(params: dict[str, Any]) -> dict[str, Any]:
    """A `TicketBucketFilter` body.

    `startDate` / `endDate` are `LocalDateTime`, so they go over the wire as
    epoch seconds like every other timestamp here. `timezone` is left unset on
    purpose: setting it would make the service reinterpret those instants as
    wall-clock in that zone.
    """
    body: dict[str, Any] = {"includeTotal": True, "includePercentage": False}
    for arg, key in (
        ("product_ids", "productIds"),
        ("stage_ids", "stageIds"),
        ("assigned_user_ids", "assignedUserIds"),
    ):
        value = params.get(arg)
        if value:
            body[key] = value
    for arg, key in (("sources", "sources"), ("sub_sources", "subSources")):
        value = params.get(arg)
        if value:
            body[key] = value
    start = to_epoch(params.get("start_date"))
    if start is not None:
        body["startDate"] = start
    end = to_epoch(params.get("end_date"))
    if end is not None:
        body["endDate"] = end
    return body


_GROUPINGS = {
    "product": "/stage-counts/products",
    "assigned_user": "/stage-counts/assigned-users",
    "created_by": "/stage-counts/created-bys",
    "client": "/stage-counts/clients",
}


async def _stage_counts(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    grouping = params.get("group_by") or "product"
    path = _GROUPINGS.get(grouping)
    if not path:
        return ToolResult(
            success=False,
            error=f"group_by must be one of: {', '.join(sorted(_GROUPINGS))}.",
        )

    try:
        body = _bucket_filter(params)
    except ValueError as exc:
        return ToolResult(success=False, error=str(exc))

    result = await client().post(
        f"{ANALYTICS}{path}",
        headers=headers(context),
        params={"page": 0, "size": max(1, min(int(params.get("size") or 25), 50))},
        json=body,
    )
    if not result.success:
        return ToolResult(success=False, error=f"Stage counts failed: {result.error}")

    page = result.data if isinstance(result.data, dict) else {}
    rows = []
    for entry in page.get("content") or []:
        if not isinstance(entry, dict):
            continue
        counts = {}
        for pair in entry.get("perCount") or []:
            if not isinstance(pair, dict):
                continue
            value = pair.get("value") or {}
            counts[str(pair.get("id"))] = value.get("count") if isinstance(value, dict) else value
        rows.append({"id": entry.get("id"), "name": entry.get("name"), "counts": counts})

    kept = budget_rows(rows, max_rows=25)
    shaped: dict[str, Any] = {
        "groupedBy": grouping,
        "rows": kept,
        "total": page.get("totalElements", len(rows)),
    }
    if len(kept) < len(rows):
        shaped["note"] = (
            f"showing {len(kept)} of {len(rows)} groups (result size limit); "
            f"narrow with product_ids or assigned_user_ids"
        )
    return ok(shaped, f"stage counts by {grouping} for {len(rows)} group(s)", max_chars=7000)


# ── tool definitions ────────────────────────────────────────────────────────

product_list = ToolDefinition(
    name="product_list",
    display_name="List Products",
    description=(
        "The products (projects) deals are raised against, with their numeric "
        "ids and the product template that defines their pipeline. Call it "
        "before any tool that takes a product_id."
    ),
    parameters=[
        ToolParameter(
            name="active_only",
            type="boolean",
            description="Only active products (default true).",
            required=False,
            default=True,
        ),
        ToolParameter(
            name="page", type="integer", description="Zero-based page number.", required=False, default=0
        ),
        ToolParameter(
            name="size", type="integer", description="Rows per page, 1-100 (default 50).",
            required=False, default=50,
        ),
    ],
    execute=_product_list,
)

pipeline_describe = ToolDefinition(
    name="pipeline_describe",
    display_name="Describe Pipeline",
    description=(
        "The ordered pipeline for a product: its stages, the statuses under "
        "each, and which stages count as success or failure. Stages belong to "
        "one product template — a stage id from another product is refused by "
        "deal_move_stage — so read this for the specific product before "
        "proposing or making a move."
    ),
    parameters=[
        ToolParameter(
            name="product_id",
            type="integer",
            description="Numeric product id, from product_list or a deal.",
            required=False,
        ),
        ToolParameter(
            name="product_code",
            type="string",
            description="The product's 22-character code, if you have that instead.",
            required=False,
        ),
        ToolParameter(
            name="product_template_id",
            type="integer",
            description="Product template id directly, skipping the product lookup.",
            required=False,
        ),
    ],
    execute=_pipeline_describe,
)

stage_counts = ToolDefinition(
    name="stage_counts",
    display_name="Stage Counts",
    description=(
        "How many deals sit in each pipeline stage, grouped by product, "
        "assignee, creator or client. This is the tool for 'how is the funnel "
        "looking' questions — do not try to answer them by paging deal_search."
    ),
    parameters=[
        ToolParameter(
            name="group_by",
            type="string",
            description="What each row counts across. Defaults to product.",
            required=False,
            enum=sorted(_GROUPINGS),
            default="product",
        ),
        ToolParameter(
            name="product_ids",
            type="array",
            description="Restrict to these numeric product ids.",
            required=False,
            items={"type": "integer"},
        ),
        ToolParameter(
            name="stage_ids",
            type="array",
            description="Restrict to these numeric stage ids.",
            required=False,
            items={"type": "integer"},
        ),
        ToolParameter(
            name="assigned_user_ids",
            type="array",
            description="Restrict to deals held by these numeric user ids.",
            required=False,
            items={"type": "integer"},
        ),
        ToolParameter(
            name="sources",
            type="array",
            description="Restrict to these lead sources.",
            required=False,
            items={"type": "string"},
        ),
        ToolParameter(
            name="sub_sources",
            type="array",
            description="Restrict to these lead sub-sources.",
            required=False,
            items={"type": "string"},
        ),
        ToolParameter(
            name="start_date",
            type="string",
            description="ISO-8601 UTC start of the window, e.g. 2026-08-01.",
            required=False,
        ),
        ToolParameter(
            name="end_date",
            type="string",
            description="ISO-8601 UTC end of the window.",
            required=False,
        ),
        ToolParameter(
            name="size", type="integer", description="Groups per page, 1-50 (default 25).",
            required=False, default=25,
        ),
    ],
    execute=_stage_counts,
)


async def _source_list(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """The lead-source taxonomy this tenant has configured.

    Needed because `source` and `sub_source` are matched EXACTLY on search and
    stored verbatim on create. A model guessing "Facebook" where the tenant
    configured "Meta" gets zero rows and no hint that the value was wrong,
    which reads as "no such leads" rather than "no such source".
    """
    result = await client().get(
        SOURCES,
        headers=headers(context),
        params={"onlyActive": str(bool(params.get("active_only", True))).lower()},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Could not list sources: {result.error}")

    entries = result.data if isinstance(result.data, list) else []
    rows = [
        {k: e.get(k) for k in ("id", "code", "name", "parentLevel0") if e.get(k) not in (None, "")}
        for e in entries
        if isinstance(e, dict)
    ]
    return ok({"sources": rows, "total": len(rows)}, f"{len(rows)} source(s)", max_chars=6000)


source_list = ToolDefinition(
    name="source_list",
    display_name="List Sources",
    description=(
        "The lead sources and sub-sources this tenant uses. Source is matched "
        "exactly, so read this before filtering or creating by source rather "
        "than guessing a name — a wrong one silently returns nothing."
    ),
    parameters=[
        ToolParameter(
            name="active_only",
            type="boolean",
            description="Only active sources (default true).",
            required=False,
            default=True,
        )
    ],
    execute=_source_list,
)

CATALOG_TOOLS = [product_list, pipeline_describe, stage_counts, source_list]
