"""Campaign craft panel builder.

Single responsibility: assemble and emit the campaign side-panel craft card.
Platform-aware via explicit dispatch (_google_campaign_blocks / _meta_campaign_blocks);
new platforms add a section builder + one dispatch branch here — nowhere else.

Callers:
  keyword_research.py  — emit_campaign_craft() after initial research completes
  keyword_update.py    — emit_section_update() for add/delete/edit (append=True, no flash)
  Future campaign tools add their own sections inside the platform section builders.
"""

from __future__ import annotations

import logging

from app.agents.adzump.platform import is_google as _is_google, is_meta as _is_meta

logger = logging.getLogger(__name__)

_LOG_TRUNCATE = 160


# Block builders


def _spec_blocks(spec: dict) -> list[dict]:
    """Common campaign summary: platform badge + key-value row."""
    blocks: list[dict] = []
    platform = str(spec.get("platform") or "")
    if platform:
        blocks.append({"type": "badge", "label": platform})
    kv: list[dict] = []
    if spec.get("channel"):
        kv.append({"key": "Channel", "value": str(spec["channel"])})
    if spec.get("duration"):
        kv.append({"key": "Duration", "value": str(spec["duration"])})
    if spec.get("budget"):
        budget = str(spec["budget"])
        if spec.get("budget_currency"):
            budget = f"{spec['budget_currency']} {budget}"
        kv.append({"key": "Daily Budget", "value": budget})
    if kv:
        blocks.append({"type": "key_value", "items": kv})
    return blocks


def keyword_review_block(dump: dict) -> dict:
    """Exported so update handlers can re-emit only this block (keyed upsert, no panel flash)."""
    tabs: list[dict] = []
    for key, label in (("brand", "Brand"), ("generic", "Generic")):
        kset = dump.get(key)
        if not kset:
            continue
        pos_rows = [
            {
                "keyword": p.get("keyword", ""),
                "volume": p.get("volume", 0),
                "match_type": p.get("match_type", "PHRASE"),
                "intent": p.get("intent", ""),
            }
            for p in (kset.get("positives") or [])
        ]
        neg_rows = [
            {
                "keyword": n.get("keyword", ""),
                "volume": n.get("volume", 0),
                "match_type": n.get("match_type", "PHRASE"),
                "reason": n.get("reason", ""),
            }
            for n in (kset.get("negatives") or [])
        ]
        tabs.append(
            {
                "key": key,
                "label": label,
                "sections": [
                    {
                        "key": "positives",
                        "label": f"Positives ({len(pos_rows)})",
                        "columns": ["keyword", "volume", "match_type", "intent"],
                        "rows": pos_rows,
                        "actions": ["add", "edit", "delete"],
                    },
                    {
                        "key": "negatives",
                        "label": f"Negatives ({len(neg_rows)})",
                        "columns": ["keyword", "volume", "match_type", "reason"],
                        "rows": neg_rows,
                        "actions": ["add", "edit", "delete"],
                    },
                ],
            }
        )
    return {"id": "keyword_review", "type": "keyword_review", "tabs": tabs}


# Platform section builders


def _google_campaign_blocks(session_ctx: dict) -> list[dict]:
    """All Google campaign craft sections.

    Add new Google-specific sections here as they land (ad copy review,
    quality score panel, etc.). Returns [] when the session has no content yet
    for a section — callers never get empty dividers.
    """
    blocks: list[dict] = []

    dump = session_ctx.get("keyword_research") or {}
    if dump:
        blocks.append({"type": "divider"})
        blocks.append({"type": "heading", "text": "Keyword Suggestions"})
        blocks.append(keyword_review_block(dump))

    # Future sections (ad copy, quality score …) extend blocks here.
    return blocks


def _meta_campaign_blocks(session_ctx: dict) -> list[dict]:
    """All Meta campaign craft sections — placeholder for future features."""
    return []


#  Emitters


async def emit_campaign_craft(stream, craft_id: str, session_ctx: dict) -> None:
    """Full campaign craft rebuild (append=False).

    Called once after the initial research / creation step. Use
    emit_section_update() for incremental updates (add/delete/edit) to avoid
    a full-panel flash.
    """
    if stream is None:
        logger.info(
            "emit_campaign_craft: stream is None — craft NOT emitted (craft_id=%s)",
            craft_id,
        )
        return
    spec = session_ctx.get("campaign_spec") or {}
    platform = str(spec.get("platform") or "")

    blocks = _spec_blocks(spec)

    if _is_google(platform):
        blocks.extend(_google_campaign_blocks(session_ctx))
    elif _is_meta(platform):
        blocks.extend(_meta_campaign_blocks(session_ctx))

    if not blocks:
        logger.info(
            "emit_campaign_craft: no blocks — craft NOT emitted (craft_id=%s)", craft_id
        )
        return
    try:
        await stream.emit_craft(craft_id, "Campaign", blocks, append=False)
        logger.info(
            "emit_campaign_craft: emitted craft_id=%s blocks=%d types=%s",
            craft_id,
            len(blocks),
            [b.get("type") for b in blocks],
        )
    except Exception as exc:
        logger.debug("emit_campaign_craft failed: %s", str(exc)[:_LOG_TRUNCATE])


async def emit_section_update(stream, craft_id: str, block: dict) -> None:
    """Keyed upsert — block MUST carry a stable ``id`` so LazyPrompt finds and replaces it."""
    if stream is None or not craft_id:
        return
    try:
        await stream.emit_craft(craft_id, "Campaign", [block], append=True)
    except Exception as exc:
        logger.debug("emit_section_update failed: %s", str(exc)[:_LOG_TRUNCATE])
