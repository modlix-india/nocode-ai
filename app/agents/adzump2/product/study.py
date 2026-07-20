"""ProductStudyAgent — the A2 orchestrator (thin, reuse-heavy).

Turns a seed (url | name | product_id) into a structured ``ProductStudyResult``:
1. **Study** — delegate to the reused legacy CFA ``ProductAgent`` (scrape →
   summarize → profile → asset pass → competitor discovery). That pipeline
   transitively reuses ``SummaryAgent`` (page → pitch) and ``VisionAnalyst``
   (asset classify + gaps), so A2 doesn't re-implement any of it.
2. **Map** — the legacy ``AnalysisOutput`` (raw ``business`` / ``competitive``
   dicts) onto the A2 contract: ``ProductProfile`` + deduped ``Competitor[]`` +
   ``AssetGaps``.
3. **Deduce** — classify the drafted profile into a J5 vertical code +
   confidence (``vertical.py``), then apply the low-confidence→generic+confirm
   policy.

A2 owns only the judgment (summarize/classify/deduce/pick, done inside the
reused agents + the deducer); scraping / geo / storage stay behind the tools.
The legacy ProductAgent is imported LAZILY (its adapter chain evaluates
``X | None`` at import, which fails on the Python 3.9 venv) — offline tests
monkeypatch the ``_resolve_product_agent`` seam and never trigger the import.

NOTE: per the P1 conventions the legacy sub-agents are REUSE-ONLY (import, do
not edit). The A2 doc's "model → M3" migration of ProductAgent itself is a
separate later slice; here ProductAgent runs as-is and only the NET-NEW
``VerticalDeducer`` is on M3.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from app.core.session import AuthContext
from app.core.streaming import AgentEventStream

from app.agents.adzump2.product.models import (
    AssetGaps,
    Competitor,
    ProductProfile,
    ProductStudyResult,
    COMPETITOR_SOURCES,
)
from app.agents.adzump2.product.vertical import apply_confidence_policy, get_vertical_deducer

logger = logging.getLogger(__name__)


def _resolve_product_agent() -> Any:
    """Lazy accessor for the reused legacy ProductAgent singleton.

    Lazy so this module (and its tests) import without the heavy scrape/vision
    adapter chain. Offline tests monkeypatch THIS function.
    """
    from app.agents.adzump.agents.product.agent import get_product_agent
    return get_product_agent()


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.removeprefix("www.").lower()
    except Exception:
        return ""


class ProductStudyAgent:
    """Singleton orchestrator behind the ``analyze_product`` tool."""

    _instance: "ProductStudyAgent | None" = None

    @classmethod
    def get_instance(cls) -> "ProductStudyAgent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("ProductStudyAgent (A2) created")
        return cls._instance

    async def study(
        self,
        *,
        url: str | None = None,
        name: str | None = None,
        product_id: str | None = None,
        event_stream: AgentEventStream | None = None,
        auth: AuthContext | None = None,
        tool_use_id: str = "",
        session_context: dict[str, Any] | None = None,
    ) -> ProductStudyResult:
        """Run the full study and return the structured artifact.

        Requires at least one of ``url`` / ``name`` / ``product_id``.
        """
        primary_url, user_message = self._resolve_seed(url, name, product_id)

        analysis = await _resolve_product_agent().analyze(
            url=primary_url,
            parent_event_stream=event_stream,
            parent_tool_use_id=tool_use_id,
            auth=auth,
            parent_session_context=session_context,
            user_message=user_message,
        )

        profile = self._build_profile(analysis, session_context)
        competitors = self._build_competitors(analysis)
        asset_gaps = AssetGaps.coerce(getattr(analysis, "asset_gaps", None))

        raw_guess = await get_vertical_deducer().deduce(profile, event_stream, auth)
        vertical, needs_confirm = apply_confidence_policy(raw_guess)

        result = ProductStudyResult(
            profile=profile,
            vertical=vertical,
            competitors=competitors,
            asset_gaps=asset_gaps,
            needs_vertical_confirm=needs_confirm,
        )
        logger.info(
            "A2 study done: name=%r vertical=%s conf=%.2f confirm=%s competitors=%d gaps_open=%s",
            profile.name, vertical.code, vertical.confidence, needs_confirm,
            len(competitors), asset_gaps.any_open(),
        )
        return result

    # ── seed → analysis input ────────────────────────────────────────────

    @staticmethod
    def _resolve_seed(
        url: str | None, name: str | None, product_id: str | None,
    ) -> tuple[str, str]:
        """Map the seed to (primary_url, analysis user_message)."""
        url = (url or "").strip()
        name = (name or "").strip()
        product_id = (product_id or "").strip()
        if url:
            return url, (
                f"Analyze this business: {url}. Scrape and profile it, then "
                "discover its direct competitors using web_search and web_fetch."
            )
        if name:
            return "", (
                f"Research the business named '{name}'. Find its official site, "
                "profile it, and discover its direct competitors."
            )
        if product_id:
            return "", (
                f"Analyze the product with id {product_id}: profile it and "
                "discover its direct competitors."
            )
        raise ValueError("study requires one of url / name / product_id")

    # ── AnalysisOutput → ProductProfile ──────────────────────────────────

    @staticmethod
    def _build_profile(
        analysis: Any, session_context: dict[str, Any] | None,
    ) -> ProductProfile:
        """Map the legacy ``business`` dict + discovered assets → ProductProfile."""
        product = getattr(analysis, "product", None) or {}
        competitive = getattr(analysis, "competitive", None) or {}

        value_props = _clean_str_list(product.get("unique_features"))
        for usp in _clean_str_list(competitive.get("our_usps")):
            if usp not in value_props:
                value_props.append(usp)

        geo = _clean_str_list(
            [product.get("location")] + list(product.get("suggested_locations") or [])
        )

        # Assets: the scrape screenshot + any logo / creative URLs the vision
        # pass stashed on product_data (best-effort; storage stays behind J16).
        assets: list[str] = []
        screenshot = getattr(analysis, "screenshot_url", None)
        if screenshot:
            assets.append(screenshot)
        pdata = (session_context or {}).get("product_data") or {}
        for logo in pdata.get("logos") or []:
            u = logo.get("url") if isinstance(logo, dict) else None
            if u:
                assets.append(u)
        for u in pdata.get("creative_image_urls") or []:
            if isinstance(u, str) and u:
                assets.append(u)
        assets = _dedupe_keep_order(assets)

        attributes: dict[str, Any] = {}
        for key in ("business_type", "business_scale", "summary"):
            val = product.get(key)
            if val:
                attributes[key] = val
        contact = product.get("contact")
        if isinstance(contact, dict) and any(contact.values()):
            attributes["contact"] = contact
        pages = product.get("pages_analyzed")
        if pages:
            attributes["pages_analyzed"] = pages

        return ProductProfile(
            name=str(product.get("product_name") or "").strip(),
            pitch=str(product.get("summary") or "").strip(),
            value_props=value_props,
            offerings=_clean_str_list(product.get("products_services")),
            geo=geo,
            price_band=str(product.get("pricing") or "").strip(),
            brand=str(product.get("product_name") or "").strip(),
            tone="",  # legacy profile carries no explicit tone; left for user edit
            assets=assets,
            attributes=attributes,
        )

    # ── AnalysisOutput → deduped Competitor[] ────────────────────────────

    @staticmethod
    def _build_competitors(analysis: Any) -> list[Competitor]:
        """Map + dedupe the legacy ``competitive.competitors`` list.

        Dedupe by normalized name AND by URL host — either collision drops the
        later entry (keeps the first, higher in the model's ranking).
        """
        competitive = getattr(analysis, "competitive", None) or {}
        raw = competitive.get("competitors") or []

        out: list[Competitor] = []
        seen_names: set[str] = set()
        seen_hosts: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            url = item.get("url") or item.get("website") or item.get("link")
            url = str(url).strip() if url else None
            if not name and not url:
                continue

            name_key = name.lower()
            host = _host(url)
            if (name_key and name_key in seen_names) or (host and host in seen_hosts):
                continue
            if name_key:
                seen_names.add(name_key)
            if host:
                seen_hosts.add(host)

            source = str(item.get("source") or "").strip().upper()
            if source not in COMPETITOR_SOURCES:
                source = "WEB" if url else "LLM"

            confidence = item.get("confidence", item.get("score"))
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.7 if url else 0.5
            confidence = max(0.0, min(1.0, confidence))

            note = str(item.get("why_competitor") or item.get("note") or "")[:200]
            out.append(Competitor(
                name=name or host,
                source=source,
                url=url,
                page_id=(str(item["page_id"]).strip() if item.get("page_id") else None),
                confidence=round(confidence, 4),
                note=note,
            ))
        return out


def _clean_str_list(values: Any) -> list[str]:
    """Coerce to a deduped list of non-empty trimmed strings (order-preserving)."""
    if not isinstance(values, (list, tuple)):
        return []
    return _dedupe_keep_order(
        [str(v).strip() for v in values if v is not None and str(v).strip()]
    )


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def get_product_study_agent() -> ProductStudyAgent:
    """Module-level accessor for the shared A2 orchestrator singleton."""
    return ProductStudyAgent.get_instance()
