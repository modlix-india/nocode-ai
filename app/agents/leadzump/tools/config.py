"""Configuring the CRM itself: products, pipeline stages, sources, task types.

The third of the three things "everything else" turned out to mean, and the one
AppBuilder cannot help with — it authors *applications*, and this is
entity-processor configuration. `catalog.py` reads this surface; this module
changes it.

Two traps here, both the same shape as the ones in `deals.py` and both worse:

1. **`POST sources` replaces the whole taxonomy.** `SourceService.upsertTree`
   upserts what it is given and then calls
   `deleteByAppAndClientExcludingIds(appCode, clientCode, savedIds)` — every
   source *not* in the payload is deleted. A tool that posted one new source
   would wipe the tenant's entire source list and answer 200. So `source_add`
   reads the tree, appends to it, and sends the whole thing back.

2. **`PUT products/code/{code}` re-reads and re-assigns.**
   `ProductService.updatableEntity` assigns `productTemplateId`, `forPartner`,
   the two override flags, `productWalkInFormId`, both file details and
   `whatsappSessionCode` from the body with no null check — the source even
   carries a comment saying a PUT silently drops anything not listed. Same
   read-modify-write requirement as a deal.

Everything here changes behaviour for every existing deal on the product, so
all four writes confirm.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.leadzump.tools._client import (
    PRODUCTS,
    SOURCES,
    STAGES,
    TASKS,
    TEMPLATES,
    client,
    headers,
    not_found,
    ok,
    require_code,
)
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

TEMPLATE_FIELDS = ("id", "code", "name", "description")
TASK_TYPE_FIELDS = ("id", "code", "name", "description", "contentEntitySeries")


async def _product_template_list(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """The pipeline blueprints a product can be built on."""
    result = await client().get(
        TEMPLATES,
        headers=headers(context),
        params={"size": max(1, min(int(params.get("size") or 50), 100)), "sort": "name,ASC"},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Could not list product templates: {result.error}")

    page = result.data if isinstance(result.data, dict) else {}
    rows = [
        {k: t.get(k) for k in TEMPLATE_FIELDS if t.get(k) not in (None, "")}
        for t in (page.get("content") or [])
        if isinstance(t, dict)
    ]
    return ok(
        {"templates": rows, "total": page.get("totalElements", len(rows))},
        f"{len(rows)} product template(s)",
        max_chars=6000,
    )


async def _product_create(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Create a product (a project deals are raised against).

    The product template is what gives it a pipeline. A product created without
    one has no stages, and `deal_move_stage` on its deals fails with
    "product template missing" — so this asks for one rather than letting that
    surface later as a puzzling error.
    """
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="name is required.")
    if params.get("product_template_id") in (None, "", 0):
        return ToolResult(
            success=False,
            error=(
                "product_template_id is required — it is what gives the product a "
                "pipeline. Call product_template_list to see the options. A product "
                "without one has no stages and its deals cannot be moved."
            ),
        )

    body: dict[str, Any] = {
        "name": name,
        "productTemplateId": params["product_template_id"],
    }
    if params.get("description"):
        body["description"] = params["description"]
    if params.get("for_partner") is not None:
        body["forPartner"] = bool(params["for_partner"])

    result = await client().post(f"{PRODUCTS}/req", headers=headers(context), json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Could not create the product: {result.error}")

    saved = result.data if isinstance(result.data, dict) else {}
    return ok(
        {k: saved.get(k) for k in ("id", "code", "name", "productTemplateId")},
        f"product '{name}' created",
    )


_PRODUCT_WRITABLE = {
    "name": "name",
    "description": "description",
    "for_partner": "forPartner",
    "product_template_id": "productTemplateId",
}


async def _product_update(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Change a product, read-modify-write.

    A partial PUT would blank `whatsappSessionCode`, both file details and the
    override flags, and answer 200 — the service's own comment says so.
    """
    code, err = require_code(params)
    if err:
        return err

    changes = {
        _PRODUCT_WRITABLE[k]: params[k]
        for k in _PRODUCT_WRITABLE
        if k in params and params[k] is not None
    }
    if not changes:
        return ToolResult(
            success=False,
            error="Nothing to change. Pass at least one of: "
            + ", ".join(sorted(_PRODUCT_WRITABLE))
            + ".",
        )

    current = await client().get(f"{PRODUCTS}/code/{code}", headers=headers(context))
    if not current.success or not isinstance(current.data, dict):
        return not_found("product", code)

    body = dict(current.data)
    body.update(changes)

    result = await client().put(
        f"{PRODUCTS}/code/{code}", headers=headers(context), json=body
    )
    if not result.success:
        return ToolResult(success=False, error=f"Could not update product {code}: {result.error}")

    saved = result.data if isinstance(result.data, dict) else {}
    return ok(
        {k: saved.get(k) for k in ("id", "code", "name", "productTemplateId")},
        f"product {saved.get('name') or code}: {', '.join(sorted(changes))} updated",
    )


async def _stage_create(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Add a stage, or a status under an existing stage.

    A status is a *child stage*: pass `parent_stage_id` and it becomes one of
    that stage's statuses. Nothing else distinguishes the two, which is why
    `pipeline_describe` presents them nested.

    This changes the pipeline for **every deal already on the template**, not
    just future ones — hence the confirmation.
    """
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="name is required.")
    if params.get("product_template_id") in (None, "", 0):
        return ToolResult(
            success=False,
            error=(
                "product_template_id is required — a stage belongs to one template. "
                "Call pipeline_describe or product_template_list first."
            ),
        )

    body: dict[str, Any] = {
        "name": name,
        "productTemplateId": params["product_template_id"],
        "platform": params.get("platform") or "PRE_QUALIFICATION",
    }
    if params.get("parent_stage_id") not in (None, "", 0):
        body["parentId"] = params["parent_stage_id"]
    if params.get("stage_type"):
        body["stageType"] = str(params["stage_type"]).upper()
    if params.get("order") is not None:
        body["order"] = params["order"]
    if params.get("is_success") is not None:
        body["isSuccess"] = bool(params["is_success"])
    if params.get("is_failure") is not None:
        body["isFailure"] = bool(params["is_failure"])
    if params.get("description"):
        body["description"] = params["description"]

    result = await client().post(f"{STAGES}/req", headers=headers(context), json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Could not create the stage: {result.error}")

    what = "status" if params.get("parent_stage_id") else "stage"
    return ok(
        {"created": what, "name": name, "result": result.data},
        f"{what} '{name}' added to template {params['product_template_id']}",
    )


async def _source_add(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Add a source (or a sub-source under one), preserving the rest.

    Read-modify-write is mandatory here, not stylistic: the endpoint deletes
    every source absent from the payload. Posting just the new one would leave
    the tenant with a single source and every existing deal's source orphaned.
    """
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="name is required.")

    current = await client().get(
        SOURCES, headers=headers(context), params={"onlyActive": "false"}
    )
    if not current.success or not isinstance(current.data, list):
        return ToolResult(
            success=False,
            error=(
                f"Could not read the existing source tree, so the new source was NOT "
                f"added: posting without it would delete every source this tenant has. "
                f"({current.error})"
            ),
        )

    tree = [dict(s) for s in current.data if isinstance(s, dict)]
    parent_name = (params.get("parent_source") or "").strip().lower()

    if parent_name:
        parent = next((s for s in tree if str(s.get("name") or "").lower() == parent_name), None)
        if parent is None:
            names = ", ".join(str(s.get("name")) for s in tree if s.get("name"))
            return ToolResult(
                success=False,
                error=f"No source called '{params['parent_source']}'. Existing sources: {names}.",
            )
        kids = list(parent.get("children") or [])
        if any(str(k.get("name") or "").lower() == name.lower() for k in kids):
            return ToolResult(
                success=False, error=f"'{name}' already exists under '{parent.get('name')}'."
            )
        kids.append({"name": name, "active": True})
        parent["children"] = kids
    else:
        if any(str(s.get("name") or "").lower() == name.lower() for s in tree):
            return ToolResult(success=False, error=f"A source called '{name}' already exists.")
        tree.append({"name": name, "active": True, "children": []})

    result = await client().post(SOURCES, headers=headers(context), json=tree)
    if not result.success:
        return ToolResult(success=False, error=f"Could not save the source tree: {result.error}")

    saved = result.data if isinstance(result.data, list) else []
    where = f" under '{params['parent_source']}'" if parent_name else ""
    return ok(
        {"added": name, "sources": [s.get("name") for s in saved if isinstance(s, dict)]},
        f"source '{name}' added{where}; {len(saved)} source(s) now configured",
    )


async def _task_type_list(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """The task types this tenant has, and what each one attaches to."""
    result = await client().get(
        f"{TASKS}/types", headers=headers(context), params={"size": 100, "sort": "name,ASC"}
    )
    if not result.success:
        return ToolResult(success=False, error=f"Could not list task types: {result.error}")

    page = result.data if isinstance(result.data, dict) else {}
    rows = [
        {k: t.get(k) for k in TASK_TYPE_FIELDS if t.get(k) not in (None, "")}
        for t in (page.get("content") or [])
        if isinstance(t, dict)
    ]
    return ok(
        {"taskTypes": rows, "total": page.get("totalElements", len(rows))},
        f"{len(rows)} task type(s)",
        max_chars=6000,
    )


async def _task_type_create(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Create a task type.

    `contentEntitySeries` decides what a task of this type hangs off, and
    `TaskService.checkEntity` refuses a task whose type does not match the
    parent it was given — so getting this wrong makes every later
    `task_create` on the type fail, not this call.
    """
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="name is required.")

    body: dict[str, Any] = {
        "name": name,
        "contentEntitySeries": (params.get("attaches_to") or "TICKET").upper(),
    }
    if params.get("description"):
        body["description"] = params["description"]

    result = await client().post(f"{TASKS}/types", headers=headers(context), json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Could not create the task type: {result.error}")

    saved = result.data if isinstance(result.data, dict) else {}
    return ok(
        {k: saved.get(k) for k in ("id", "code", "name", "contentEntitySeries")},
        f"task type '{name}' created",
    )


# ── tool definitions ────────────────────────────────────────────────────────

product_template_list = ToolDefinition(
    name="product_template_list",
    display_name="List Product Templates",
    description=(
        "The pipeline blueprints a product can be built on. A template defines "
        "the ordered stages and their statuses, so read this before creating a "
        "product or adding a stage."
    ),
    parameters=[
        ToolParameter(
            name="size", type="integer", description="Rows, 1-100 (default 50).",
            required=False, default=50,
        )
    ],
    execute=_product_template_list,
)

product_create = ToolDefinition(
    name="product_create",
    display_name="Create Product",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Create a product (a project deals are raised against). Requires a "
        "product template, which is what gives it a pipeline. Pauses for the "
        "user's confirmation."
    ),
    parameters=[
        ToolParameter(name="name", type="string", description="Product name.", required=True),
        ToolParameter(
            name="product_template_id",
            type="integer",
            description="Numeric template id, from product_template_list.",
            required=True,
        ),
        ToolParameter(name="description", type="string", description="Free-text description.", required=False),
        ToolParameter(
            name="for_partner",
            type="boolean",
            description="Whether channel partners may work deals on it.",
            required=False,
        ),
    ],
    execute=_product_create,
)

product_update = ToolDefinition(
    name="product_update",
    display_name="Update Product",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Change a product's name, description, partner visibility or template. "
        "Fields you do not pass are left as they are. Changing the template "
        "changes the pipeline for every deal on the product. Pauses for the "
        "user's confirmation."
    ),
    parameters=[
        ToolParameter(name="code", type="string", description="The product's 22-character code.", required=True),
        ToolParameter(name="name", type="string", description="New name.", required=False),
        ToolParameter(name="description", type="string", description="New description.", required=False),
        ToolParameter(name="for_partner", type="boolean", description="Partner visibility.", required=False),
        ToolParameter(
            name="product_template_id",
            type="integer",
            description="New template id. Changes the pipeline for existing deals.",
            required=False,
        ),
    ],
    execute=_product_update,
)

stage_create = ToolDefinition(
    name="stage_create",
    display_name="Add Pipeline Stage",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Add a pipeline stage to a product template, or a status under an "
        "existing stage by passing parent_stage_id. This changes the pipeline "
        "for every deal already on the template, not just new ones. Read "
        "pipeline_describe first so the new stage lands in the right place. "
        "Pauses for the user's confirmation."
    ),
    parameters=[
        ToolParameter(name="name", type="string", description="Stage or status name.", required=True),
        ToolParameter(
            name="product_template_id",
            type="integer",
            description="Numeric template id the stage belongs to.",
            required=True,
        ),
        ToolParameter(
            name="parent_stage_id",
            type="integer",
            description=(
                "Omit for a top-level stage. Pass a stage id to add a STATUS "
                "under that stage — a status is simply a child stage."
            ),
            required=False,
        ),
        ToolParameter(
            name="platform",
            type="string",
            description="Which half of the funnel. Defaults to PRE_QUALIFICATION.",
            required=False,
            enum=["PRE_QUALIFICATION", "POST_QUALIFICATION"],
        ),
        ToolParameter(
            name="stage_type", type="string", description="Stage type, e.g. OPEN.", required=False
        ),
        ToolParameter(
            name="order", type="integer", description="Position in the ordered pipeline.", required=False
        ),
        ToolParameter(
            name="is_success", type="boolean", description="Marks a won outcome.", required=False
        ),
        ToolParameter(
            name="is_failure", type="boolean", description="Marks a lost outcome.", required=False
        ),
        ToolParameter(name="description", type="string", description="Free-text description.", required=False),
    ],
    execute=_stage_create,
)

source_add = ToolDefinition(
    name="source_add",
    display_name="Add Lead Source",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Add a lead source, or a sub-source under an existing one. The tool "
        "reads the current taxonomy and appends to it, because the underlying "
        "endpoint replaces the whole list. Pauses for the user's confirmation."
    ),
    parameters=[
        ToolParameter(name="name", type="string", description="The new source or sub-source name.", required=True),
        ToolParameter(
            name="parent_source",
            type="string",
            description=(
                "Name of an existing source to nest this under. Omit for a "
                "top-level source."
            ),
            required=False,
        ),
    ],
    execute=_source_add,
)

task_type_list = ToolDefinition(
    name="task_type_list",
    display_name="List Task Types",
    description=(
        "The task types this tenant has configured and what each attaches to "
        "(a deal, a lead or a user). task_create matches its `task_type` "
        "against these."
    ),
    parameters=[],
    execute=_task_type_list,
)

task_type_create = ToolDefinition(
    name="task_type_create",
    display_name="Create Task Type",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Create a task type. `attaches_to` decides what tasks of this type hang "
        "off, and a mismatch makes later task_create calls fail rather than "
        "this one. Pauses for the user's confirmation."
    ),
    parameters=[
        ToolParameter(name="name", type="string", description="Task type name, e.g. 'Site visit'.", required=True),
        ToolParameter(
            name="attaches_to",
            type="string",
            description="What a task of this type belongs to. Defaults to TICKET (a deal).",
            required=False,
            enum=["TICKET", "OWNER", "USER"],
        ),
        ToolParameter(name="description", type="string", description="Free-text description.", required=False),
    ],
    execute=_task_type_create,
)

CONFIG_TOOLS = [
    product_template_list,
    product_create,
    product_update,
    stage_create,
    source_add,
    task_type_list,
    task_type_create,
]

CONFIG_MUTATING = {
    "product_create",
    "product_update",
    "stage_create",
    "source_add",
    "task_type_create",
}
