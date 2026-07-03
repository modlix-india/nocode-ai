"""A2 — product study + competitor discovery (the artifact that gates building).

Thin orchestrator over the reused legacy CFA sub-agents (ProductAgent /
SummaryAgent / VisionAnalyst / geo) that returns a STRUCTURED
``ProductStudyResult``: an agent-drafted, user-editable ``ProductProfile``, the
deduced J5 ``VerticalGuess`` (selects the whole playbook downstream), a deduped
``Competitor`` list (for J19), and ``AssetGaps`` (what the site couldn't supply).

Boundary: A2 does the LLM reasoning (summarize / classify / deduce / pick);
scraping, geo, and storage stay behind the reused tools. See
``modules/A2-product-study.md``.
"""

from __future__ import annotations

from app.agents.adzump2.product.models import (
    AssetGaps,
    Competitor,
    ProductProfile,
    ProductStudyResult,
    VerticalGuess,
)

__all__ = [
    "AssetGaps",
    "Competitor",
    "ProductProfile",
    "ProductStudyResult",
    "VerticalGuess",
]
