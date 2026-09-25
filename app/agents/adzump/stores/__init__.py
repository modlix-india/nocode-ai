"""The adzump data-access layer: SQL for the migration V19 tables, nothing else.

One module per table group. Stores take and return typed models plus plain
keys (client_code, product url / id); session context and business rules stay
in the services that call them.

    from app.agents.adzump import stores
    product = await stores.products.get_product(client_code, url)
"""

from app.agents.adzump.stores import competitors, flows, products

__all__ = ["competitors", "flows", "products"]
