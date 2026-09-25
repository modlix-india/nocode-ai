"""Product library routes - the UI's read access to a client's saved products,
their competitors and those competitors' ads, plus delete.

Plain data, no LLM: routes call ``stores`` directly. Every query is scoped to
the caller's client_code, so another client's id reads as a 404. No create
routes - products and competitors come from the chat's analysis. A product
delete is real (a fresh start); a competitor or ad delete only marks the row
deleted, so research never re-suggests it, a refetch never brings the ad back,
and an explicit re-add restores the competitor with its ads. The competitor
rows are the list's only home, so the delete holds: a resume reads the rows,
and an open chat drops the entry on its next save.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.agents.adzump import stores
from app.agents.adzump.creative_intelligence.models import Creative
from app.agents.adzump.models.product import Product
from app.core.base_auth import require_auth_context
from app.core.session import AuthContext

router = APIRouter(prefix="/products", tags=["Adzump Products"])


class ProductListing(BaseModel):
    id: int
    url: str
    name: str | None = None
    category: str | None = None
    country_code: str | None = None
    summary: str | None = None
    updated_at: datetime


class ProductDetail(BaseModel):
    id: int
    url: str
    product: Product


class CompetitorListing(BaseModel):
    id: int
    name: str
    url: str | None = None  # canonical website; None while none is known
    logo_url: str | None = None
    business_type: str | None = None
    location: str | None = None
    pricing: str | None = None
    key_usps: list[str] = []
    weakness: str | None = None
    why_competitor: str | None = None
    creative_status: str  # pending (never fetched) | ok | empty | error
    total_creatives: int
    active_creatives: int
    creatives_fetched_at: datetime | None = None


class CompetitorCreatives(BaseModel):
    competitor_id: int
    creatives: list[Creative]


@router.get("", response_model=list[ProductListing])
async def list_products(auth: AuthContext = Depends(require_auth_context)):
    return await stores.products.list_products(auth.client_code)


@router.get("/{product_id}", response_model=ProductDetail, response_model_by_alias=False)
async def get_product(product_id: int, auth: AuthContext = Depends(require_auth_context)):
    found = await stores.products.get_product_by_id(auth.client_code, product_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"No product {product_id}")
    url, product = found
    return ProductDetail(id=product_id, url=url, product=product)


@router.delete("/{product_id}", status_code=204)
async def delete_product(product_id: int, auth: AuthContext = Depends(require_auth_context)):
    """Deletes the product with its saved flows, competitors and their ads."""
    if not await stores.products.delete_product(auth.client_code, product_id):
        raise HTTPException(status_code=404, detail=f"No product {product_id}")
    return Response(status_code=204)


@router.get("/{product_id}/competitors", response_model=list[CompetitorListing])
async def list_competitors(product_id: int, auth: AuthContext = Depends(require_auth_context)):
    return await stores.competitors.list_product_competitors(auth.client_code, product_id)


@router.delete("/{product_id}/competitors/{competitor_id}", status_code=204)
async def delete_competitor(
    product_id: int, competitor_id: int, auth: AuthContext = Depends(require_auth_context),
):
    """Marks the competitor deleted; its ads leave every listing with it."""
    if not await stores.competitors.delete_competitor(
            auth.client_code, product_id, competitor_id, auth.user_id):
        raise HTTPException(status_code=404, detail=f"No competitor {competitor_id}")
    return Response(status_code=204)


@router.delete("/{product_id}/competitors/{competitor_id}/creatives/{creative_id}",
               status_code=204)
async def delete_creative(
    product_id: int, competitor_id: int, creative_id: str,
    auth: AuthContext = Depends(require_auth_context),
):
    """Hides one ad; ``creative_id`` is the ad library's id (``creative_id`` in
    the creatives listing)."""
    if not await stores.competitors.delete_creative(
            auth.client_code, product_id, competitor_id, creative_id, auth.user_id):
        raise HTTPException(status_code=404, detail=f"No ad {creative_id}")
    return Response(status_code=204)


@router.get("/{product_id}/creatives", response_model=list[CompetitorCreatives],
            response_model_by_alias=False)
async def list_creatives(
    product_id: int,
    competitor_id: int | None = None,
    auth: AuthContext = Depends(require_auth_context),
):
    """Ads grouped by competitor; ``competitor_id`` narrows to one."""
    grouped = await stores.competitors.list_product_creatives(
        auth.client_code, product_id, competitor_id)
    return [CompetitorCreatives(competitor_id=cid, creatives=creatives)
            for cid, creatives in grouped.items()]
