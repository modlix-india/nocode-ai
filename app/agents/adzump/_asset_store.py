"""Code disposition for judged upload assets — the deterministic HOW that
replaces the AssetManagerAgent tool-loop (design C).

`classify_verdict` is model-led (the model's relevant/needs_user/role own the
call; no confidence threshold — asking is the model's job, not a code ladder).
The store writers are ported byte-for-byte from asset_manager so product_data's
write shape is unchanged; asset_manager itself is deleted in slice 5.
"""

from __future__ import annotations

from hashlib import md5
from typing import Any

from app.agents.adzump.agents.asset_picker.models import ImageVerdict

USABLE_ROLES = {"logo", "hero", "amenity", "floor_plan"}


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
    same image pasted twice is judged + stored once."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for img in images:
        h = md5(img.get("data") or b"").hexdigest()
        if h not in seen:
            seen.add(h)
            unique.append(img)
    return unique


# ── product_data writers (ported from asset_manager — keep the shape) ────────

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
    sig = product_data.get("_shift3_signal")
    if isinstance(sig, dict):
        sig["logo_missing"] = False


def store_creative(product_data: dict, res: dict, role: str, name: str) -> bool:
    urls = product_data.setdefault("creative_images", [])
    displays = product_data.setdefault("creative_displays", [])
    if res["url"] in set(urls):
        return False
    urls.append(res["url"])
    displays.append({k: v for k, v in res.items() if k != "url"})
    sig = product_data.get("_shift3_signal")
    if isinstance(sig, dict):
        sig["creative_missing_categories"] = [
            c for c in (sig.get("creative_missing_categories") or []) if c != role
        ]
    return True
