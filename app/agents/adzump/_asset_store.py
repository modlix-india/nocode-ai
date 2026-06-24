"""Code disposition for reviewed upload assets — the deterministic HOW behind the
manage_assets front door (design C), acting on the VisionAnalyst's verdicts.

`classify_verdict` is model-led (the model's relevant/needs_user/role own the
call; no confidence threshold — asking is the model's job, not a code ladder).
The store writers below are the canonical product_data write path; the old
separate asset-manager agent that once held them has been removed.
"""

from __future__ import annotations

from hashlib import md5
from typing import Any

from app.agents.adzump.agents.vision.models import ImageVerdict
from app.agents.adzump.agents.product.models import AssetGaps

USABLE_ROLES = {"logo", "hero", "amenity", "floor_plan"}


def _fulfill_gap(sctx: dict, mutate) -> None:
    """Apply a decrement to the open asset-upload elicitation's gap payload.
    The payload rides _pending_elicitation as a JSON-safe dict (persisted across
    turns); rehydrate → mutate → re-store. No-op when no elicitation is open."""
    elicit = (sctx or {}).get("_pending_elicitation") or {}
    gaps = AssetGaps.from_dict(elicit.get("payload"))
    if gaps is None:
        return
    mutate(gaps)
    elicit["payload"] = gaps.to_dict()


def classify_verdict(v: ImageVerdict) -> str:
    """'store' | 'reject' | 'escalate'. Explicit-only escalation — no numeric
    backstop; the model owns 'should I ask?' via needs_user."""
    if v.needs_user:                       # model said it's unsure → ask
        return "escalate"
    if not v.relevant:                     # model: off-product → drop
        return "reject"
    role = (v.role or "").strip().lower()
    if role == "unused":                   # real content, not a usable creative
        return "reject"
    if role in USABLE_ROLES:
        return "store"
    return "escalate"                      # unknown / empty / unexpected → ask, don't guess


def dedup_by_content(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop images whose bytes duplicate an earlier one (md5 of content). The
    same image pasted twice is reviewed + stored once."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for img in images:
        h = md5(img.get("data") or b"").hexdigest()
        if h not in seen:
            seen.add(h)
            unique.append(img)
    return unique


# ── product_data writers (canonical write shape for product_data) ────────────

def store_logo(product_data: dict, res: dict, name: str, sctx: dict) -> None:
    # First logo store of THIS run clears the auto-detected logo (user upload
    # wins); later stores append (developer + project).
    if not sctx.get("_asset_logo_cleared"):
        product_data["logo_urls"] = []
        product_data["logo_displays"] = []
        product_data["logo_meta"] = []
        sctx["_asset_logo_cleared"] = True
    product_data["logo_urls"].append(res["url"])
    product_data["logo_displays"].append({k: v for k, v in res.items() if k != "url"})
    product_data["logo_meta"].append({
        "source_url": "user_upload", "source": "user_upload", "role": "main",
        "reasoning": f"User-uploaded ({name})", "format": res.get("format", ""),
    })
    product_data["logo_url"] = product_data["logo_urls"][0]
    product_data["logo_display"] = product_data["logo_displays"][0]
    product_data["logo_source_url"] = "user_upload"
    product_data["logo_source"] = "user_upload"
    product_data["logo_reasoning"] = "User-uploaded logo"
    product_data["logo_confidence"] = 1.0
    _fulfill_gap(sctx, lambda g: g.fulfill_logo())


def store_creative(product_data: dict, res: dict, role: str, name: str, sctx: dict) -> bool:
    urls = product_data.setdefault("creative_images", [])
    displays = product_data.setdefault("creative_displays", [])
    if res["url"] in set(urls):
        return False
    urls.append(res["url"])
    displays.append({k: v for k, v in res.items() if k != "url"})
    _fulfill_gap(sctx, lambda g: g.fulfill_category(role))
    return True
