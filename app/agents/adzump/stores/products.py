"""adzump_products - one row per (client, business url).

`data` is the typed Product, the source of truth; the promoted columns are
projections written from the same object in the same statement, so they can
never drift from the JSON.
"""

from __future__ import annotations

import json

from app.db.connection import execute_query
from app.agents.adzump.models.product import Product
from app.agents.adzump._shared import normalize_business_url


async def upsert_product(
    client_code: str, product: Product, url: str, user_id: int = 0
) -> int:
    """Insert or update the product row keyed by (client_code, url). `url` is the
    resolved+normalized business url (the storage key), NOT product.primary_url
    which can diverge. Returns the row id."""
    url = normalize_business_url(url)
    if not url:
        raise ValueError("upsert_product: empty business url")
    columns = _project(product)
    await execute_query(
        """
        INSERT INTO adzump_products
            (client_code, url, name, scale, category, country_code,
             summary, data, created_by, updated_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS new
        ON DUPLICATE KEY UPDATE
            name=new.name, scale=new.scale,
            category=new.category, country_code=new.country_code,
            summary=new.summary, data=new.data,
            updated_by=new.updated_by
        """,
        (client_code, url, columns["name"], columns["scale"],
         columns["category"], columns["country_code"], columns["summary"],
         json.dumps(product.model_dump(mode="json")), user_id, user_id),
    )
    return await product_id(client_code, url)


async def get_product(client_code: str, url: str) -> Product | None:
    """Hydrate the typed Product back from `data`, or None on miss."""
    rows = await execute_query(
        "SELECT data FROM adzump_products WHERE client_code=%s AND url=%s",
        (client_code, url),
    )
    if not rows:
        return None
    raw = rows[0]["data"]
    return Product.model_validate(raw if isinstance(raw, dict) else json.loads(raw))


async def product_id(client_code: str, url: str) -> int | None:
    """The row id for a (client, url), or None if absent - the FK the competitor
    and flow rows hang off."""
    rows = await execute_query(
        "SELECT id FROM adzump_products WHERE client_code=%s AND url=%s",
        (client_code, url),
    )
    return rows[0]["id"] if rows else None


def _project(product: Product) -> dict:
    """The queried scalars lifted out of the typed Product - the ONLY place
    columns are derived from it. `url` is not here: it is the storage key the
    caller passes."""
    return {
        "name": product.product_name,
        "scale": product.business_scale,
        "category": product.category,
        "country_code": product.place.country_code,
        "summary": product.summary,
    }
