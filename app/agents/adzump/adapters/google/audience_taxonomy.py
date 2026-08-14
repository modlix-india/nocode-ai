"""Google's audience taxonomies — user_interest, life_event, detailed_demographic.

Fetches and caches them, and reads the shapes Google returns. Decides nothing about campaign
types: the same taxonomy serves Demand Gen, Performance Max and App campaigns, so which types
to ask for and which channel to check against are the caller's.

Fetched whole rather than searched — the set is small enough that one cached call lets a
ranker see all of it. Measurements and sources: docs/demand-gen-audience-mechanisms.md.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import TypeVar

from app.agents.adzump.adapters.google.client import google_ads_client

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The taxonomy is global — two accounts returned byte-identical sets — so one process-wide
# cache serves every advertiser. Google revises it on the order of months, and a stale entry
# only means "we didn't suggest something new", never a broken campaign.
# Per process, so each worker pays for one cold fetch. Left that way deliberately: three
# queries a day per worker is not worth a shared store and its invalidation.
_CACHE_TTL_SECONDS = 24 * 3600
_cache: dict[str, tuple[float, list[TaxonomyEntry]]] = {}
# One lock per resource so parallel campaigns starting cold fetch once rather than N times.
# Created lazily: a module-level Lock would bind to whichever loop imported this first.
_locks: dict[str, asyncio.Lock] = {}

INTEREST = "user_interest"
LIFE_EVENT = "life_event"
DETAILED_DEMOGRAPHIC = "detailed_demographic"

# Locale modes that admit any country: scoped by language, or not scoped at all.
# These are the values the API actually sends — the proto's field comments name a different,
# non-existent set, and matching those excludes every entry.
_COUNTRY_AGNOSTIC_LOCALES = frozenset({"ALL_LOCALES", "LANGUAGE_AND_ALL_COUNTRIES"})


@dataclass(frozen=True)
class _Resource:
    name: str
    id_field: str
    parent_field: str
    extra_fields: tuple[str, ...] = ()


# user_interest names its key fields differently from the other two, so field names are
# configuration rather than a shared template.
_RESOURCES: dict[str, _Resource] = {
    INTEREST: _Resource(
        INTEREST, "user_interest_id", "user_interest_parent", ("taxonomy_type",)
    ),
    LIFE_EVENT: _Resource(LIFE_EVENT, "id", "parent"),
    DETAILED_DEMOGRAPHIC: _Resource(DETAILED_DEMOGRAPHIC, "id", "parent"),
}


@dataclass(frozen=True)
class TaxonomyEntry:
    resource: str
    resource_name: (
        str  # what a segment references, e.g. customers/1/userInterests/80071
    )
    entry_id: int
    name: str
    parent: str
    launched_to_all: bool = False
    availabilities: list[dict] = field(default_factory=list)
    taxonomy_type: str = ""  # user_interest only: AFFINITY | IN_MARKET


def _camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(w.title() for w in rest)


def _query(res: _Resource, taxonomy_types: tuple[str, ...]) -> str:
    fields = [
        res.id_field,
        "name",
        res.parent_field,
        "launched_to_all",
        "availabilities",
        *res.extra_fields,
    ]
    select = ", ".join(f"{res.name}.{f}" for f in fields)
    query = f"SELECT {select} FROM {res.name}"
    if taxonomy_types and "taxonomy_type" in res.extra_fields:
        values = ", ".join(f"'{t}'" for t in taxonomy_types)
        query += f" WHERE {res.name}.taxonomy_type IN ({values})"
    return query


def _to_entry(res: _Resource, row: dict) -> TaxonomyEntry | None:
    data = row.get(_camel(res.name)) or {}
    resource_name = data.get("resourceName") or ""
    name = (data.get("name") or "").strip()
    if not resource_name or not name:
        return None
    raw_id = data.get(_camel(res.id_field))
    return TaxonomyEntry(
        resource=res.name,
        resource_name=resource_name,
        entry_id=int(raw_id) if raw_id is not None else 0,
        name=name,
        parent=data.get(_camel(res.parent_field)) or "",
        launched_to_all=bool(data.get("launchedToAll")),
        availabilities=data.get("availabilities") or [],
        taxonomy_type=data.get("taxonomyType") or "",
    )


def _rescope_name(resource_name: str, customer_id: str) -> str:
    head, _, tail = resource_name.partition("/")
    _, _, rest = tail.partition("/")
    if head != "customers" or not rest:
        return resource_name
    return f"customers/{customer_id}/{rest}"


def _rescope(entry: TaxonomyEntry, customer_id: str) -> TaxonomyEntry:
    """Repoint a cached entry at this customer. Ids are global but resource names carry the
    account they were fetched under. ``parent`` is a resource name too and must move with it,
    or ``path_of`` stops resolving and the hierarchy silently disappears."""
    return replace(
        entry,
        resource_name=_rescope_name(entry.resource_name, customer_id),
        parent=_rescope_name(entry.parent, customer_id) if entry.parent else "",
    )


async def fetch(
    resource: str,
    *,
    customer_id: str,
    taxonomy_types: tuple[str, ...] = (),
    login_customer_id: str = "",
    client_code: str = "",
    auth_headers: dict[str, str] | None = None,
) -> list[TaxonomyEntry]:
    """One taxonomy, as Google returns it.

    ``taxonomy_types`` narrows user_interest server-side. Cached per (resource, types), so
    only the first caller of a cold key pays for the query.
    """
    key = f"{resource}:{','.join(taxonomy_types)}"
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return [_rescope(e, customer_id) for e in cached[1]]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Another caller may have populated it while we waited on the lock.
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
            return [_rescope(e, customer_id) for e in cached[1]]

        res = _RESOURCES[resource]
        rows = await google_ads_client.search_stream(
            _query(res, taxonomy_types),
            customer_id,
            login_customer_id,
            client_code,
            auth_headers or {},
        )
        entries = [e for e in (_to_entry(res, r) for r in rows) if e is not None]
        _cache[key] = (time.monotonic(), entries)
        logger.info("taxonomy fetched: %s rows=%d (cached)", key, len(entries))
        return [_rescope(e, customer_id) for e in entries]


def clear_cache() -> None:
    _cache.clear()


def path_of(entry: TaxonomyEntry, index: dict[str, TaxonomyEntry]) -> list[str]:
    """Ancestor names, root first, ending in the entry's own name — what makes "Apartments
    (For Sale)" unambiguous. ``index`` is the whole set, from ``by_resource_name``."""
    names = [entry.name]
    seen = {entry.resource_name}
    current = entry
    while current.parent and current.parent not in seen:
        seen.add(current.parent)
        parent = index.get(current.parent)
        if parent is None:
            break
        names.append(parent.name)
        current = parent
    return list(reversed(names))


def by_resource_name(entries: list[TaxonomyEntry]) -> dict[str, TaxonomyEntry]:
    return {e.resource_name: e for e in entries}


def query_patterns(query: str) -> list[re.Pattern[str]]:
    """Compiled matchers for a free-text query, or empty if nothing usable. Prefix-at-word-
    boundary, not substring: "loan" must reach "Loans", but containment makes "real" match
    "Mont-real" and surface trips to Canada."""
    words = [w for w in query.casefold().split() if len(w) > 2]
    return [re.compile(rf"\b{re.escape(w)}") for w in words]


def match_score(name: str, patterns: list[re.Pattern[str]]) -> int:
    """How many of the query's words this name carries."""
    folded = name.casefold()
    return sum(1 for p in patterns if p.search(folded))


def rank_by_name(items: Sequence[T], query: str, name: Callable[[T], str]) -> list[T]:
    """Items whose name matches the query, best first.

    Scored per word, not per phrase: no entry contains "home loan", but "Home Purchase Loans"
    carries both words. Ties go to shorter names, which are the broader parents. Generic over
    the item because callers hold entries here and plain dicts once in session state.
    """
    patterns = query_patterns(query)
    if not patterns:
        return []
    scored = [
        (score, -len(name(i)), i)
        for i in items
        if (score := match_score(name(i), patterns))
    ]
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [i for _, _, i in scored]


def search(entries: Sequence[TaxonomyEntry], query: str) -> list[TaxonomyEntry]:
    return rank_by_name(entries, query, lambda e: e.name)


def is_targetable(
    entry: TaxonomyEntry, *, channel_type: str, country_code: str
) -> bool:
    """Whether this entry can serve for the given channel and country.

    An entry carries ``launched_to_all`` OR ``availabilities``, never both - life events use
    the first, detailed demographics the second, so checking one drops a whole resource. An
    entry with neither is excluded: Google accepts an unavailable criterion, then never spends.
    """
    if entry.launched_to_all:
        return True
    return any(
        _channel_admits(a.get("channel") or {}, channel_type)
        and _locale_admits(a.get("locale") or [], country_code)
        for a in entry.availabilities
    )


def _channel_admits(channel: dict, channel_type: str) -> bool:
    if channel.get("availabilityMode") == "ALL_CHANNELS":
        return True
    # CHANNEL_TYPE_AND_ALL_SUBTYPES and ..._SUBSET_SUBTYPES both name the channel type.
    # Sub-types are not modelled here, so a type match is as far as this can check.
    return channel.get("advertisingChannelType") == channel_type


def _locale_admits(locales: list[dict], country_code: str) -> bool:
    return any(
        loc.get("availabilityMode") in _COUNTRY_AGNOSTIC_LOCALES
        or loc.get("countryCode") == country_code
        for loc in locales
    )
