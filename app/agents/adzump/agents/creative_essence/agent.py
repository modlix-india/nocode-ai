"""EssenceAnalyst - single-shot vision BaseAgent extracting creative essence.

Tier-3 of the creative-ingest cascade (see ``creative_intelligence/dedup.py``):
Tiers 1-2 dedup deterministically; this agent looks at the SURVIVORS only and
extracts each one's typed ``Essence`` (strategy / subject / visual reference).
It never culls a creative - dedup is deterministic, vision only adds.

Family shape: a VisionAnalyst clone (``agents/vision/agent.py``) - tools=[],
max_turns=1, gpt-4o-mini, silent sub-stream, fenced-JSON -> pydantic. The
launcher owns ``pre_emit_agent_started``; this agent emits ``agent_finished``
with aggregated usage. Lives OUTSIDE ``creative_intelligence/`` and is injected
into the library's ingest by the tool, so the domain stays model-free.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from io import BytesIO

from pydantic import ValidationError

from app.core.agent import BaseAgent
from app.core.session import AuthContext, BaseSession
from app.core.streaming import AgentEventStream

from app.agents.adzump._shared import extract_json
from app.agents.adzump._uploads import shrink_image_to_jpeg
from app.agents.adzump.agents.creative_essence.context import build_essence_context
from app.agents.adzump.agents.creative_essence.models import (
    CreativeImage,
    EssenceBatch,
)
from app.agents.adzump.creative_intelligence.models import Essence

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────
#
# DeepSeek vision after the 2026-09-10 bench (scripts/bench_essence.py, report
# in logs/bench_essence_report.md): grounded hooks where gpt-4o-mini fabricated
# text not on the image, reads on-image prices/OCR verbatim, ~20x cheaper
# vision input, and streams reasoning for the observability card. Trade-off:
# ~4x slower per batch - acceptable for a background enrich. VisionAnalyst
# (logo/creative picks) stays on gpt-4o-mini pending its own bench.
ESSENCE_PROVIDER = "deepseek"
ESSENCE_MODEL_TIER = "deepseek-v4-flash-vision-exp"
ESSENCE_MODEL_OVERRIDE = "deepseek:deepseek-v4-flash-vision-exp"

# One verdict is ~150-200 output tokens; the chunk cap keeps the whole batch
# well under the ceiling so truncation (-> unparseable JSON) can't happen.
ESSENCE_MAX_TOKENS = 4000
MAX_IMAGES_PER_CALL = 12
# Chunks are independent (fresh session per call), so they run concurrently -
# bounded, or a 60-creative competitor would fire 5 vision calls at once on top
# of the other competitors' pipelines.
MAX_CONCURRENT_CALLS = 3

# Single-shot LLM call per chunk.
ESSENCE_MAX_TURNS = 1

# Vision input cost scales with pixels; ad creatives are ~1080px social sizes,
# so a 1024 long-edge resend is near-native while capping the pathological case.
_MAX_IMAGE_DIM = 1024
_JPEG_QUALITY = 85


class EssenceAnalyst(BaseAgent):
    """Single-shot essence extractor: N creative images in, one typed
    ``Essence`` per unique ``content_hash`` out."""

    display_name = "Essence Analyst"

    _instance: "EssenceAnalyst | None" = None

    def __init__(self) -> None:
        context = build_essence_context()
        context._cached_static_text = context._static_prefix
        super().__init__(
            name="creative_essence",
            tools=[],
            context_builder=context,
            model_tier=ESSENCE_MODEL_TIER,
            max_turns=ESSENCE_MAX_TURNS,
            max_tokens=ESSENCE_MAX_TOKENS,
            provider=ESSENCE_PROVIDER,
            context_management=None,
        )

    @classmethod
    def get_instance(cls) -> "EssenceAnalyst":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("EssenceAnalyst created (essence extraction, single-shot)")
        return cls._instance

    async def extract(
        self,
        images: list[CreativeImage],
        parent_event_stream: AgentEventStream,
        auth: AuthContext,
        parent_session_context: dict | None = None,
        status_tuid: str = "",
        insight_agent_id: str = "",
        competitor_name: str = "",
    ) -> dict[str, Essence]:
        """Extract essence for every unique content_hash in ``images``.

        Chunks the batch (MAX_IMAGES_PER_CALL per LLM call - one call in the
        common post-dedup case), retries an unparseable chunk per-creative,
        and returns ``{content_hash: Essence}``. A creative whose verdict
        never parses is simply absent - its stored ``essence`` stays None and
        a later refetch re-attempts (the essence cache only skips hashes that
        HAVE essence). Never raises: on total failure returns ``{}``.
        """
        unique: dict[str, CreativeImage] = {}
        for ci in images:
            if ci.creative.content_hash and ci.data:
                unique.setdefault(ci.creative.content_hash, ci)
        items = await asyncio.to_thread(_drop_undecodable, list(unique.values()))
        if not items:
            return {}

        run_start = time.monotonic()
        stream = _SilentEventStream(parent_event_stream)
        essences: dict[str, Essence] = {}
        tokens_in = tokens_out = 0
        status = "success"

        # Live narration on the COMPETITOR's card row (the inner stream is
        # silent by design): status line via the row's tuid, per-verdict
        # insight lines as its streamed thinking quote.
        analyzed = {"n": 0}

        async def _status(message: str) -> None:
            if not status_tuid or parent_event_stream is None:
                return
            try:
                await parent_event_stream.emit_tool_update(status_tuid, message)
            except Exception:
                logger.debug("essence_status_emit_failed", exc_info=True)

        async def _insights(got: dict[str, Essence]) -> None:
            if not insight_agent_id or parent_event_stream is None:
                return
            for essence in got.values():
                try:
                    await parent_event_stream.emit_thinking(
                        _insight_line(essence) + "\n",
                        agent_id=insight_agent_id)
                except Exception:
                    logger.debug("essence_insight_emit_failed", exc_info=True)

        await _status(f"Reading {len(items)} ad creatives…")

        sem = asyncio.Semaphore(MAX_CONCURRENT_CALLS)

        async def _do_chunk(chunk: list[CreativeImage]) -> tuple[dict[str, Essence], int, int]:
            got: dict[str, Essence] = {}
            t_in = t_out = 0
            async with sem:
                if stream.is_cancelled:
                    return got, t_in, t_out
                batch, i, o = await self._run_once(
                    chunk, stream, auth, parent_session_context, competitor_name)
                t_in += i
                t_out += o
                if batch is None and len(chunk) > 1:
                    # Unparseable batch JSON - retry each creative alone.
                    logger.warning("essence_batch_unparseable: falling back "
                                   "per-creative n=%d", len(chunk))
                    for ci in chunk:
                        if stream.is_cancelled:
                            break
                        single, i, o = await self._run_once(
                            [ci], stream, auth, parent_session_context,
                            competitor_name)
                        t_in += i
                        t_out += o
                        _collect(single, [ci], got)
                else:
                    _collect(batch, chunk, got)
            analyzed["n"] += len(chunk)
            await _insights(got)
            await _status(f"Analyzed {analyzed['n']}/{len(items)} creatives…")
            return got, t_in, t_out

        chunks = [items[s : s + MAX_IMAGES_PER_CALL]
                  for s in range(0, len(items), MAX_IMAGES_PER_CALL)]
        # One chunk's failure must not abort the others - partials from the
        # healthy chunks are kept.
        for result in await asyncio.gather(*map(_do_chunk, chunks),
                                           return_exceptions=True):
            if isinstance(result, BaseException):
                logger.warning("essence_extract_failed: %s: %s",
                               type(result).__name__, str(result)[:200])
                status = "error"
                continue
            got, t_in, t_out = result
            essences.update(got)
            tokens_in += t_in
            tokens_out += t_out
        if not essences:
            status = "error"  # every verdict failed to parse - not a quiet success

        if insight_agent_id:
            # Nested under a competitor's card row: the launcher owns that
            # span's close (with the rollup) - no separate essence card.
            if status == "error":
                await _status("couldn't read these creatives - will retry "
                              "on the next fetch")
            return essences

        await self._emit_finished(
            parent_event_stream, run_start, status,
            summary=f"essence for {len(essences)}/{len(items)} creatives",
            tokens_in=tokens_in, tokens_out=tokens_out,
        )
        return essences

    async def _run_once(
        self,
        chunk: list[CreativeImage],
        stream: AgentEventStream,
        auth: AuthContext,
        parent_session_context: dict | None,
        competitor_name: str = "",
    ) -> tuple[EssenceBatch | None, int, int]:
        """One LLM call over one chunk. Fresh session per call (a reused
        session would replay the previous chunk's messages into the next).
        Returns (parsed batch or None, tokens_in, tokens_out)."""
        sub_session = BaseSession(agent_name=self.name)
        await sub_session.get_or_create(None, auth)
        if parent_session_context is not None:
            sub_session.context = {
                "url": parent_session_context.get("url", ""),
                "craft_id": parent_session_context.get("craft_id", ""),
            }

        # CPU-bound (PIL decode/shrink + base64 per image) - off the event loop
        # so a 12-image chunk doesn't stall SSE keepalives.
        user_message, image_blocks = await asyncio.to_thread(
            _build_essence_message, chunk, competitor_name)
        try:
            await self.run(
                user_message=user_message,
                session=sub_session,
                event_stream=stream,
                image_blocks=image_blocks,
                model_override=ESSENCE_MODEL_OVERRIDE,
            )
        except Exception as e:
            logger.warning("essence_run_failed: %s: %s",
                           type(e).__name__, str(e)[:200])
            return None, 0, 0

        usage = sub_session.total_usage or {}
        t_in = int(usage.get("input_tokens") or 0)
        t_out = int(usage.get("output_tokens") or 0)
        return _parse_batch(_final_assistant_text(sub_session)), t_in, t_out

    async def _emit_finished(
        self,
        parent_event_stream: AgentEventStream | None,
        run_start: float,
        status: str,
        summary: str,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        """AgentCard close for the whole extract() (usage summed across
        chunk calls). Observability hook - never fails the extraction."""
        if parent_event_stream is None:
            return
        try:
            await parent_event_stream.emit_agent_finished(
                agent_id=self.name,
                status=status,
                duration_ms=int((time.monotonic() - run_start) * 1000),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                step_count=1,
                summary=summary,
            )
        except Exception:
            pass


def _collect(
    batch: EssenceBatch | None,
    chunk: list[CreativeImage],
    essences: dict[str, Essence],
) -> None:
    """Map a chunk's verdicts back onto content_hashes by input-order idx.
    Out-of-range indices are dropped (logged), not guessed."""
    if batch is None:
        return
    for verdict in batch.verdicts:
        if 0 <= verdict.idx < len(chunk):
            essences[chunk[verdict.idx].creative.content_hash] = verdict.to_essence()
        else:
            logger.warning("essence_verdict_idx_oob: idx=%d n=%d",
                           verdict.idx, len(chunk))


def _build_essence_message(
    chunk: list[CreativeImage],
    competitor_name: str = "",
) -> tuple[str, list[dict]]:
    """User-message text + image blocks, text-first / images-after in the same
    index order (the ``session.append_user_message`` contract VisionAnalyst
    uses). Each entry carries the ad copy + landing URL - hook_text /
    copy_framework / offer / classification read them alongside the pixels.
    ``competitor_name`` is the advertiser the ads were collected under - the
    advertiser_role reference (a different developer's project = broker)."""
    lines = [
        f"Extract the essence of each of the {len(chunk)} competitor ad "
        f"creatives below, in order - one verdict per image.",
    ]
    if competitor_name:
        lines.append(f"These ads were collected while researching the "
                     f"competitor {competitor_name!r} - use that name only "
                     f"for advertiser_role, never as a category signal.")
    blocks: list[dict] = []
    for idx, ci in enumerate(chunk):
        c = ci.creative
        copy_bits = []
        if c.headline:
            copy_bits.append(f"headline={c.headline[:200]!r}")
        if c.primary_text:
            copy_bits.append(f"primary_text={c.primary_text[:400]!r}")
        if c.cta:
            copy_bits.append(f"cta={c.cta[:60]!r}")
        if c.landing_url:
            copy_bits.append(f"landing_url={c.landing_url[:160]!r}")
        meta = " ".join(copy_bits) or "(no ad copy captured)"
        note = " (video ad - this is its poster still)" if c.media_type == "video" else ""
        lines.append(f"[Image {idx}] media_type={c.media_type}{note} {meta}")

        data, content_type = _shrink_for_vision(ci.data, ci.content_type)
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": content_type,
                "data": base64.b64encode(data).decode("ascii"),
            },
        })
    return "\n".join(lines), blocks


def _insight_line(essence: Essence) -> str:
    """One expert line per verdict for the card's streaming quote:
    'offer-led · "pay 10% now, rest on possession" · static image'."""
    hook = (essence.hook_type or "").replace("_", " ")
    quote = (essence.hook_text or essence.angle or "").strip()
    fmt = (essence.media_format or "").replace("_", " ")
    parts = [p for p in (
        hook,
        f"“{quote[:90]}”" if quote else "",
        fmt,
    ) if p]
    return " · ".join(parts) or "unlabeled creative"


def _drop_undecodable(items: list[CreativeImage]) -> list[CreativeImage]:
    """Exclude creatives whose bytes PIL cannot decode (SVG, truncated
    download, HTML error body). Sent as-is they 400 the whole vision call
    (live 2026-09-10: one corrupt mention-tier image took out its batch).
    Their essence stays None and a later refetch re-attempts."""
    from PIL import Image

    kept: list[CreativeImage] = []
    for ci in items:
        try:
            with Image.open(BytesIO(ci.data)) as img:
                img.verify()
            kept.append(ci)
        except Exception as e:
            logger.warning(
                "essence_skip_undecodable: hash=%s %s: %s",
                (ci.creative.content_hash or "")[:12],
                type(e).__name__, str(e)[:80],
            )
    return kept


def _shrink_for_vision(data: bytes, content_type: str) -> tuple[bytes, str]:
    """Bound vision-input cost: re-encode anything over the long-edge cap to a
    1024px JPEG (the shared downscale rule). Small images and undecodable bytes
    pass through unchanged - a failed shrink degrades to the original, never
    drops the image."""
    out = shrink_image_to_jpeg(
        data, long_edge=_MAX_IMAGE_DIM, quality=_JPEG_QUALITY,
        exif=True, only_if_larger=True,
    )
    return (out, "image/jpeg") if out else (data, content_type)


def _final_assistant_text(session: BaseSession) -> str:
    """The last assistant message's text - the model's JSON output."""
    for m in reversed(session.get_messages()):
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if c.get("type") == "text"]
            if any(parts):
                return "\n".join(p for p in parts if p)
    return ""


def _parse_batch(final_text: str) -> EssenceBatch | None:
    """Parse the final text as an ``EssenceBatch``. None on any parse or
    validation failure - the caller decides whether to fall back per-creative."""
    payload = extract_json(final_text)
    if payload is None:
        logger.warning("essence_no_json final_text=%r", final_text[:300])
        return None
    try:
        return EssenceBatch.model_validate(payload)
    except ValidationError as e:
        logger.warning("essence_validation_failed err=%s", str(e)[:200])
        return None


class _SilentEventStream(AgentEventStream):
    """Drops everything except agent lifecycle + data (the VisionAnalyst
    matrix): the essence pass surfaces no text or thinking - only the
    AgentCard span the launcher opened. Local copy by design; consolidation
    is parked with the generic sub-agent call tool."""

    def __init__(self, parent: AgentEventStream) -> None:
        # Deliberately no super().__init__() - nothing consumes a local queue.
        self._parent = parent

    @property
    def is_cancelled(self) -> bool:
        return getattr(self._parent, "is_cancelled", False)

    def cancel(self) -> None:
        try:
            self._parent.cancel()
        except Exception:
            pass

    async def emit_text(self, text: str) -> None:
        return

    async def emit_thinking(self, reasoning: str) -> None:
        return

    async def emit_tool_start(self, *a, **kw) -> None:
        return

    async def emit_tool_update(self, *a, **kw) -> None:
        return

    async def emit_tool_result(self, *a, **kw) -> None:
        return

    async def emit_error(self, message: str) -> None:
        logger.debug("essence_substream_error: %s", message[:200])

    async def emit_done(self, *a, **kw) -> None:
        return

    async def emit_keepalive(self) -> None:
        return

    async def emit_suggestions(self, options, mode="single") -> None:
        return

    async def emit_data(self, data_type: str, payload: dict) -> None:
        if self._parent is not None:  # None parent = headless (eval) run
            await self._parent.emit_data(data_type, payload)

    async def emit_agent_started(self, agent_id, label, parent_id="root",
                                 parent_tool_use_id="",
                                 agent_tool_use_id="") -> None:
        if self._parent is not None:
            await self._parent.emit_agent_started(
                agent_id, label, parent_id, parent_tool_use_id,
                agent_tool_use_id=agent_tool_use_id,
            )

    async def emit_agent_finished(self, agent_id, status="success",
                                  duration_ms=0, tokens_in=0, tokens_out=0,
                                  step_count=0, summary="") -> None:
        if self._parent is not None:
            await self._parent.emit_agent_finished(
                agent_id, status, duration_ms, tokens_in, tokens_out,
                step_count, summary,
            )

    async def emit_agent_usage(self, agent_id, tokens_in, tokens_out) -> None:
        if self._parent is not None:
            await self._parent.emit_agent_usage(agent_id, tokens_in, tokens_out)

    async def emit_craft(self, *a, **kw) -> None:
        return  # The launcher owns craft rendering, not the essence pass.

    async def emit_craft_text(self, *a, **kw) -> None:
        return

    async def emit_feedback_request(self, session_id, turn_number) -> None:
        return


def get_essence_analyst() -> EssenceAnalyst:
    """Accessor for the shared singleton."""
    return EssenceAnalyst.get_instance()
