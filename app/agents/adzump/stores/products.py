"""adzump_products - one row per (client, business url).

`data` is the typed Product, the source of truth; the promoted columns are
projections written from the same object in the same statement, so they can
never drift from the JSON. Analysis creates the row (insert_product); after
that each change writes only its own fields (update_product_fields).
"""

from __future__ import annotations

import json
from typing import Any

from app.db.connection import execute_query, get_connection
from app.agents.adzump.models.product import Product, apply_product_fields
from app.agents.adzump._shared import normalize_business_url


# Create the product row for (client_code, url) from its first analysis; an
# existing row is never overwritten. `url` is the storage key (the resolved,
# normalized business url), not product.primary_url. Returns the row id.
async def insert_product(
    client_code: str, url: str, product: Product, user_id: int = 0,
) -> int:
    url = normalize_business_url(url)
    if not url:
        raise ValueError("insert_product: empty business url")
    columns = _project(product)
    await execute_query(
        """
        INSERT INTO adzump_products
            (client_code, url, name, scale, category, country_code,
             summary, data, created_by, updated_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE id=id
        """,
        (client_code, url, columns["name"], columns["scale"],
         columns["category"], columns["country_code"], columns["summary"],
         json.dumps(product.model_dump(mode="json")), user_id, user_id),
    )
    return await product_id(client_code, url)


# Write only the given fields of one product (`apply_product_fields` keys),
# leaving every other field as stored. The row is locked while it is read and
# rewritten, so two chats changing different fields both land. False when the
# client has no such row.
async def update_product_fields(
    client_code: str, product_id: int, fields: dict[str, Any], user_id: int = 0,
) -> bool:
    async with get_connection() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT data FROM adzump_products "
                    "WHERE client_code=%s AND id=%s FOR UPDATE",
                    (client_code, product_id))
                row = await cur.fetchone()
                if row is None:
                    await conn.rollback()
                    return False
                data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                apply_product_fields(data, fields)
                product = Product.model_validate(data)
                columns = _project(product)
                await cur.execute(
                    """
                    UPDATE adzump_products SET name=%s, scale=%s, category=%s,
                        country_code=%s, summary=%s, data=%s, updated_by=%s
                    WHERE id=%s
                    """,
                    (columns["name"], columns["scale"], columns["category"],
                     columns["country_code"], columns["summary"],
                     json.dumps(product.model_dump(mode="json")), user_id, product_id))
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return True


async def delete_product(client_code: str, product_id: int) -> bool:
    """Delete one product of the client; its flows, competitors, creatives and
    assets go with it (V19 cascades). False when the client has no such row."""
    return bool(await execute_query(
        "DELETE FROM adzump_products WHERE client_code=%s AND id=%s",
        (client_code, product_id),
    ))


async def get_product(client_code: str, url: str) -> Product | None:
    """Hydrate the typed Product back from `data`, or None on miss."""
    rows = await execute_query(
        "SELECT data FROM adzump_products WHERE client_code=%s AND url=%s",
        (client_code, url),
    )
    return _product_from(rows[0]["data"]) if rows else None


async def get_product_by_id(
    client_code: str, product_id: int,
) -> tuple[str, Product] | None:
    """(storage url, typed Product) for one row id of the client, or None."""
    rows = await execute_query(
        "SELECT url, data FROM adzump_products WHERE client_code=%s AND id=%s",
        (client_code, product_id),
    )
    return (rows[0]["url"], _product_from(rows[0]["data"])) if rows else None


async def list_products(client_code: str) -> list[dict]:
    """The client's products, most recently updated first - promoted columns
    only, never the `data` blob."""
    return await execute_query(
        "SELECT id, url, name, category, country_code, summary, updated_at "
        "FROM adzump_products WHERE client_code=%s "
        "ORDER BY updated_at DESC, id DESC",
        (client_code,),
    )


async def product_id(client_code: str, url: str) -> int | None:
    """The row id for a (client, url), or None if absent - the FK the competitor
    and flow rows hang off."""
    rows = await execute_query(
        "SELECT id FROM adzump_products WHERE client_code=%s AND url=%s",
        (client_code, url),
    )
    return rows[0]["id"] if rows else None


def _product_from(raw) -> Product:
    return Product.model_validate(raw if isinstance(raw, dict) else json.loads(raw))


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
