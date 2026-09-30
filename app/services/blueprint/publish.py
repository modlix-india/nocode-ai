"""Putting what was built on the site.

── Why this is a separate, deliberate act ───────────────────────────────

The build never publishes. Everything it creates is created unpublished and
everything the builder authors lands on a draft, because "the objects exist" and
"the public can see them" are two different decisions and only the first one is
the machine's to make.

That was right, and it had a consequence nobody priced in: a build could finish
correctly and **the person who pressed it still could not look at the result.**
A page created unpublished carries `published=False`, and the engine answers 404
for it on the live surface AND on the draft host:

    /api/ui/page/home       200   published=None
    /api/ui/page/blogList   404   published=False
    /api/ui/page/blogList?draft=true   404

So the board offered View site and View draft, and both were blank for exactly
the pages the build had just made. Publishing is the missing step, not the
authoring — the work was there the whole time.

── Both services, storages first ────────────────────────────────────────

A site is pages and the storages behind them, and they publish through two
different controllers (`/api/ui/publish` and `/api/core/publish`). Publishing
only the pages is how a live form ends up posting into a schema nobody has
published, which fails at the one moment it matters — a real customer's first
enquiry.

Core goes first for the same reason: a page that goes live against an
unpublished storage is broken for as long as the gap lasts, and the other order
is merely slower.

── What it refuses to do ────────────────────────────────────────────────

It publishes an APP, not a selection, because that is what the platform's
publish surface offers and pretending otherwise would mean a per-object loop
that cannot be made atomic anyway. So the count is stated first: a person should
know they are putting five things on the site, not one.

It never runs on its own. Nothing here is called by the build, by the sweep, or
by an agent — only by a person pressing Publish.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.appbuilder.tools._shared import get_saas_client

logger = logging.getLogger(__name__)

#: The two publish surfaces, in the order they must be published.
#:
#: Core before UI: a page that goes live against a storage nobody has published
#: is broken until the storage catches up, and nothing on screen would say why.
SURFACES: tuple[tuple[str, str], ...] = (
    ("core", "/api/core/publish"),
    ("ui", "/api/ui/publish"),
)

#: What each object type is called when it is shown to a person. The platform
#: answers in its own vocabulary — PAGE, STORAGE, APPLICATION — which is not
#: what somebody looking at their own site calls them.
PLAIN: dict[str, str] = {
    "PAGE": "page",
    "APPLICATION": "the site's settings",
    "STORAGE": "store",
    "STYLE": "style",
    "THEME": "theme",
    "URI_PATH": "address",
    "TEMPLATE": "template",
    "NOTIFICATION": "notification",
    "FUNCTION": "function",
    "CONNECTION": "connection",
}


class PublishError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _plain(object_type: str) -> str:
    return PLAIN.get((object_type or "").upper(), (object_type or "object").lower())


async def pending(
    app_code: str, headers: dict[str, str], client_code: str = "",
) -> dict[str, Any]:
    """Everything waiting to go on the site, across both services.

    Answers with a flat list AND a count, because the count is what a button
    says and a list is what a person checks it against. A caller that had to
    derive the count by walking a map keyed by object type would get it wrong
    the first time a new type drafted.

    A surface that cannot be reached is reported rather than counted as empty:
    "nothing to publish" and "we could not find out" are different, and only one
    of them means the button should be quiet.
    """
    client = get_saas_client()
    items: list[dict[str, Any]] = []
    unreachable: list[str] = []

    for service, base in SURFACES:
        params = {"clientCode": client_code} if client_code else {}
        result = await client.get(f"{base}/app/{app_code}/pending", headers=headers, params=params)
        if not result.success:
            logger.info("blueprint publish: %s pending unreadable: %s", service, result.error)
            unreachable.append(service)
            continue
        data = result.data if isinstance(result.data, dict) else {}
        for object_type, rows in data.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                items.append({
                    "service": service,
                    "type": str(object_type),
                    "what": _plain(str(object_type)),
                    "name": str(row.get("name") or row.get("objectName") or ""),
                    "message": str(row.get("message") or ""),
                })

    items.sort(key=lambda i: (i["type"], i["name"]))
    return {
        "appCode": app_code,
        "count": len(items),
        "items": items,
        "unreachable": unreachable,
    }


async def publish_all(
    app_code: str, headers: dict[str, str], client_code: str = "",
) -> dict[str, Any]:
    """Put everything drafted for this app on the site.

    Both services are attempted even when the first one fails, and the result
    says what happened to each. Stopping at the first failure would leave the
    site half published with no record of which half — the state that is hardest
    to reason about afterwards and the easiest to produce.
    """
    client = get_saas_client()
    published: list[str] = []
    failures: dict[str, str] = {}

    # Which pages were waiting, read BEFORE publishing. Afterwards their drafts
    # are gone, so this is the only moment the list exists — and it is what says
    # whose plan to link once the components are live.
    waiting = await pending(app_code, headers, client_code)
    names = [
        item["name"] for item in waiting["items"]
        if item["type"] == "PAGE" and item["name"]
    ]

    for service, base in SURFACES:
        params = {"clientCode": client_code} if client_code else {}
        result = await client.post(f"{base}/app/{app_code}", headers=headers, params=params)
        if not result.success:
            failures[service] = str(result.error)[:300]
            continue
        data = result.data if isinstance(result.data, dict) else {}
        published.append(f"{service}: {_summarise(data)}")

    if failures and not published:
        raise PublishError(
            "Nothing could be published: " + "; ".join(
                f"{where} — {why}" for where, why in failures.items()
            )
        )

    # The plan is linked HERE, not at the end of the build.
    #
    # A blueprint write bumps the page's document version, and the builder's
    # draft carries the version it was taken from — so stamping the plan while a
    # draft is outstanding makes that draft unpublishable ("Please reload to get
    # the new version before making changes"). The build therefore matches
    # without writing, and the link is made once the components are on the site.
    #
    # It is also the more honest moment. A section that exists only in an
    # unpublished draft is not built yet, whatever the draft says.
    linked = await _link_plans(app_code, headers, client_code, names)

    return {
        "appCode": app_code,
        "published": published,
        "failed": failures,
        "linked": linked,
        # Asked again AFTER publishing, rather than assumed to be zero. A
        # publish that silently skipped something must not leave the board
        # claiming the site is up to date.
        "remaining": (await pending(app_code, headers, client_code))["count"],
    }


async def _link_plans(
    app_code: str, headers: dict[str, str], client_code: str, names: list[str],
) -> dict[str, int]:
    """Point each published page's plan at the components now on the site.

    Per page and guarded per page: a plan that cannot be linked is a card that
    goes on reading "to build", which is wrong but visible. Letting it fail the
    publish would be worse — the site is already live by this point and the
    caller would be told the whole thing failed.
    """
    from app.services.blueprint.build_job import _reconcile_built

    linked: dict[str, int] = {}
    for name in names:
        try:
            matched, _ = await _reconcile_built(app_code, name, headers, client_code)
        except Exception:  # noqa: BLE001 — the site is live; a plan link is not worth failing it
            logger.info("blueprint publish: could not link the plan for %s", name, exc_info=True)
            continue
        if matched:
            linked[name] = matched
    return linked


def _summarise(report: dict[str, Any]) -> str:
    """The platform's publishAll report, in one line — INCLUDING what it refused.

    `publishAll` attempts every pending draft and reports per object, so a run
    can come back `attempted=2, published=0` with the reason sitting in
    `results`. The first version of this read the count and dropped the rest,
    and reported "2 published" over a publish that had published nothing and
    said exactly why:

        {"name": "blogList", "published": false,
         "error": "Please reload to get the new version before making changes"}

    Which is how a self-inflicted version conflict looked, for a while, like the
    platform quietly ignoring two pages.
    """
    if not report:
        return "nothing to publish"

    results = report.get("results")
    refused: list[str] = []
    if isinstance(results, list):
        for row in results:
            if not isinstance(row, dict) or row.get("published"):
                continue
            why = str(row.get("error") or "refused").strip()
            refused.append(f"{row.get('name') or 'an object'} — {why}")

    count = report.get("published")
    attempted = report.get("attempted")
    if isinstance(count, int):
        line = f"{count} of {attempted} published" if isinstance(attempted, int) \
            else f"{count} published"
    else:
        line = ", ".join(f"{k}={v}" for k, v in list(report.items())[:4])

    # Named, not counted. "2 were refused" sends somebody looking; the reason
    # was already in the answer.
    return line + (" | refused: " + "; ".join(refused[:4]) if refused else "")
