"""Parsing and configuration helpers for creative generation splits."""

from __future__ import annotations

import re


def parse_creative_counts(config_val: str) -> tuple[int, int]:
    """Parse configuration split (e.g., '2 competitor and 1 own') or standard config values.

    Returns:
        tuple: (own_count, competitor_count)
    """
    val = str(config_val).strip()

    if val == "1":
        return 1, 0
    if val == "2":
        return 1, 1
    if val == "3":
        return 2, 1

    text_lower = val.lower()
    own_match = re.search(r"(\d+)\s*own", text_lower)
    comp_match = re.search(r"(\d+)\s*competitor", text_lower)

    own_count = int(own_match.group(1)) if own_match else 0
    competitor_count = int(comp_match.group(1)) if comp_match else 0

    if own_count == 0 and competitor_count == 0:
        num_match = re.search(r"\d+", text_lower)
        own_count = int(num_match.group(0)) if num_match else 1

    return own_count, competitor_count


def filter_competitor_images(competitors: list[dict]) -> list[str]:
    """Filter crawled competitor images to exclude icons, logos, and badges.

    TODO: This is a placeholder until the competitor image crawl pipeline is
    complete. Expected session key once populated:
        session.context["competitor_analysis"]["competitors"][i]["creative_images"]
    Currently falls back to homepage URLs when creative_images is empty,
    which will be replaced once the crawl pipeline provides real image URLs.
    """
    competitor_images: list[str] = []
    exclude_keywords = ("icon", "logo", "header", "footer", "badge", "social", "avatar")
    
    for c in competitors:
        # Check crawled creative_images list first
        images = c.get("creative_images") or []
        if isinstance(images, list):
            for img in images:
                if img and not any(k in img.lower() for k in exclude_keywords):
                    competitor_images.append(img)
        else:
            img_url = c.get("logoUrl") or c.get("url") or ""
            if img_url and not any(k in img_url.lower() for k in exclude_keywords):
                if any(img_url.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp")):
                    competitor_images.append(img_url)
    return competitor_images
