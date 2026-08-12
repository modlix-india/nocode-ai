"""How the business is described to a campaign sub-agent.

Channel-neutral: both read only ``product_data`` and ``session_ctx``, so every channel's
agent describes the business the same way. Channel-specific framing belongs in that
channel's package.
"""

from __future__ import annotations


def business_text(product: dict) -> str:
    """The business the campaign must target."""
    parts = [
        f"Business: {product.get('product_name', '')}",
        f"Type: {product.get('business_type', '')}",
    ]
    if features := product.get("unique_features"):
        parts.append("Offerings / USPs: " + "; ".join(str(f) for f in features))
    if services := product.get("products_services"):
        parts.append("Products / services: " + "; ".join(str(s) for s in services))
    if summary := product.get("summary"):
        parts.append("Summary: " + str(summary))
    return "\n".join(p for p in parts if p.strip())


def conversation_text(session_ctx: dict) -> str:
    """business_text plus what a *question* may reach for that generation never needs.

    A build targets the offering; a question can be about the competition or the budget
    ("are we covering what competitors bid on?"), so those are added here rather than
    widening the build prompt.
    """
    product = session_ctx.get("product_data") or {}
    parts = [business_text(product)]

    # analyze_competitors' shape (tools/competitor.py): {"competitors": [{"name": ...}]}
    competitive = session_ctx.get("competitor_analysis") or {}
    names = [
        str(c.get("name")).strip()
        for c in (competitive.get("competitors") or [])
        if isinstance(c, dict) and c.get("name")
    ]
    if names:
        parts.append("Competitors: " + ", ".join(names))

    if budget := (session_ctx.get("campaign_spec") or {}).get("budget"):
        parts.append(f"Daily budget: {budget}")
    return "\n".join(p for p in parts if p.strip())
