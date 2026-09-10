"""Rule-9 repair sweep: re-verify every stored creative through its served URL.

Static files can be deleted, paths migrated, or (pre-hardening) written broken
- the store is the fix, never the read path. Each failing creative is removed
from ``creatives[]`` with a diagnostic appended to ``dropped[]``; a competitor
left with zero renderable creatives becomes fetchStatus "empty" (searched,
none renderable) - never "ok" with an empty array.

Run once after deploying the hardened ingest, then weekly:
    python scripts/sweep_creative_library.py [--dry-run]
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.agents.adzump.creative_intelligence import store, verify
from app.agents.adzump.creative_intelligence.library import MAX_DROPPED_ENTRIES
from app.agents.adzump.creative_intelligence.models import Competitor

logger = logging.getLogger(__name__)


async def sweep_library(ctx: dict, *, dry_run: bool = False) -> dict:
    """Verify every stored creative; repair records in place. Returns the
    report: records scanned/updated, creatives checked, removals by reason."""
    report = {
        "records_scanned": 0,
        "records_updated": 0,
        "creatives_checked": 0,
        "removed_by_reason": {},
        "dry_run": dry_run,
    }
    for competitor in await store.list_competitors(ctx):
        report["records_scanned"] += 1
        changed = await _repair_record(competitor, report)
        if changed and not dry_run:
            if await store.upsert_competitor(competitor, ctx):
                report["records_updated"] += 1
        elif changed:
            report["records_updated"] += 1  # would have written
    logger.info("creative_library_sweep: %s", report)
    return report


async def _repair_record(competitor: Competitor, report: dict) -> bool:
    kept = []
    changed = False
    for creative in competitor.creatives:
        report["creatives_checked"] += 1
        ok, reason = await verify.verify_creative(creative)
        if ok:
            kept.append(creative)
            continue
        changed = True
        by_reason = report["removed_by_reason"]
        by_reason[reason] = by_reason.get(reason, 0) + 1
        competitor.dropped.insert(0, {
            "creativeId": creative.creative_id,
            "fileUrl": creative.file_url or creative.source_asset_url,
            "reason": reason,
            "droppedAt": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("sweep_removed: key=%s id=%s reason=%s",
                    competitor.competitor_key, creative.creative_id, reason)
    if not changed:
        return False
    competitor.creatives = kept
    competitor.dropped = competitor.dropped[:MAX_DROPPED_ENTRIES]
    if not kept and competitor.fetch_status == "ok":
        competitor.fetch_status = "empty"  # searched, none renderable
    return True
