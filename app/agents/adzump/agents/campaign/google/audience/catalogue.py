"""The targetable segment catalogue: three taxonomies, filtered to what can serve this
channel and country, flattened to JSON-safe dicts.

One loader for the build tool, the panel mutation and the manage agent, because a segment
reference is opaque - ``customers/1/userInterests/80071`` carries no label, kind or ancestry,
so anything accepting a ref must resolve it against the same catalogue. A second loader that
filtered differently would let a ref pass in one path and fail in another.

The fetch is process-cached for 24h (see the adapter), so resolving one ref is a dict lookup.
"""

from __future__ import annotations

import logging

from app.agents.adzump.adapters.google import audience_taxonomy as taxonomy
from app.agents.adzump.agents.campaign.google.audience import constants
from app.agents.adzump.agents.campaign.google.audience.models import SignalKind

logger = logging.getLogger(__name__)

_KIND_BY_RESOURCE = {
    taxonomy.LIFE_EVENT: SignalKind.LIFE_EVENT,
    taxonomy.DETAILED_DEMOGRAPHIC: SignalKind.DETAILED_DEMOGRAPHIC,
}

_RESOURCES = (taxonomy.INTEREST, taxonomy.LIFE_EVENT, taxonomy.DETAILED_DEMOGRAPHIC)


async def load(
    *,
    customer_id: str,
    channel_type: str,
    country_code: str,
    login_customer_id: str = "",
    client_code: str = "",
    auth_headers: dict[str, str] | None = None,
) -> list[dict]:
    """Every segment that can serve, as ``{id, ref, label, kind, path}``."""
    fetched: list[taxonomy.TaxonomyEntry] = []
    targetable: list[taxonomy.TaxonomyEntry] = []
    for resource in _RESOURCES:
        types = (
            constants.SEGMENT_TAXONOMY_TYPES if resource == taxonomy.INTEREST else ()
        )
        try:
            rows = await taxonomy.fetch(
                resource,
                customer_id=customer_id,
                taxonomy_types=types,
                login_customer_id=login_customer_id,
                client_code=client_code,
                auth_headers=auth_headers or {},
            )
        except Exception as exc:
            # Fail soft per resource: a thinner candidate set beats no campaign.
            logger.warning(
                "audience taxonomy fetch failed: %s - %s",
                resource,
                str(exc)[: constants.LOG_ERROR_MAX_CHARS],
            )
            continue
        fetched.extend(rows)
        targetable.extend(
            e
            for e in rows
            if taxonomy.is_targetable(
                e, channel_type=channel_type, country_code=country_code
            )
        )

    # Ancestors resolve against everything fetched, not just the targetable ones. A parent
    # that cannot serve is still the parent, and indexing only the targetable entries
    # truncates its children's path - the ancestry that separates "people buying this" from
    # "people who work in this".
    index = taxonomy.by_resource_name(fetched)
    return [
        {
            "id": e.entry_id,
            "ref": e.resource_name,
            "label": e.name,
            # user_interest splits on taxonomy_type: "buying this" and "into this" reach
            # different people. The other two resources ARE the kind.
            "kind": (
                e.taxonomy_type
                if e.resource == taxonomy.INTEREST
                else _KIND_BY_RESOURCE[e.resource].value
            ),
            "path": taxonomy.path_of(e, index),
        }
        for e in targetable
    ]


def by_key(candidates: list[dict]) -> dict[str, dict]:
    """id and resource name -> the candidate. Both, because the agent sees ids in the tree
    while the panel sends resource names."""
    index: dict[str, dict] = {}
    for c in candidates:
        index[str(c["id"])] = c
        index[c["ref"]] = c
    return index


def as_tree(candidates: list[dict]) -> str:
    """The catalogue as an indented tree, for a model only - the panel reads the flat
    candidates. Every byte is resent with each turn's history, so indentation carries the
    ancestry, ids stand in for resource names, and the kind is a heading, not a suffix."""
    lines: list[str] = []
    kind = None
    for c in sorted(candidates, key=lambda x: (x["kind"], x["path"])):
        if c["kind"] != kind:
            kind = c["kind"]
            lines.append(f"[{kind}]")
        depth = len(c["path"]) - 1
        lines.append(f"{'  ' * depth}{c['id']} {c['label']}")
    return "\n".join(lines)


def as_lines(candidates: list[dict]) -> str:
    """Search hits, for a model only. Full ancestry per line - a match is pulled out of the
    tree, so as_tree's indentation would sit under nothing."""
    return "\n".join(
        f"{c['id']} {' > '.join(c['path'])} [{c['kind']}]" for c in candidates
    )
