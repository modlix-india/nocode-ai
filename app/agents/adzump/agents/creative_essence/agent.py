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
# gpt-4o-mini for the same reason as VisionAnalyst: vision-capable at ~1/20th
# of Sonnet's price, and the task is labeling, not reasoning.
ESSENCE_PROVIDER = "openai"
ESSENCE_MODEL_TIER = "fast"
ESSENCE_MODEL_OVERRIDE = "openai:gpt-4o-mini"

# One verdict is ~150-200 output tokens; the chunk cap keeps the whole batch
# well under the ceiling so truncation (-> unparseable JSON) can't happen.
ESSENCE_MAX_TOKENS = 4000
MAX_IMAGES_PER_CALL = 12

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
        items = list(unique.values())
        if not items:
            return {}

        run_start = time.monotonic()
        stream = _SilentEventStream(parent_event_stream)
        essences: dict[str, Essence] = {}
        tokens_in = tokens_out = 0
        status = "success"

        try:
            for start in range(0, len(items), MAX_IMAGES_PER_CALL):
                if stream.is_cancelled:
                    break
                chunk = items[start : start + MAX_IMAGES_PER_CALL]
                batch, t_in, t_out = await self._run_once(
                    chunk, stream, auth, parent_session_context)
                tokens_in += t_in
                tokens_out += t_out
                if batch is None and len(chunk) > 1:
                    # Unparseable batch JSON - retry each creative alone.
                    logger.warning("essence_batch_unparseable: falling back "
                                   "per-creative n=%d", len(chunk))
                    for ci in chunk:
                        if stream.is_cancelled:
                            break
                        single, t_in, t_out = await self._run_once(
                            [ci], stream, auth, parent_session_context)
                        tokens_in += t_in
                        tokens_out += t_out
                        _collect(single, [ci], essences)
                else:
                    _collect(batch, chunk, essences)
        except Exception as e:
            logger.warning("essence_extract_failed: %s: %s",
                           type(e).__name__, str(e)[:200])
            status = "error"
        if not essences:
            status = "error"  # every verdict failed to parse - not a quiet success

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
            _build_essence_message, chunk)
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
) -> tuple[str, list[dict]]:
    """User-message text + image blocks, text-first / images-after in the same
    index order (the ``session.append_user_message`` contract VisionAnalyst
    uses). Each entry carries the ad copy - hook_text / copy_framework / offer
    read from the copy, not the pixels."""
    lines = [
        f"Extract the essence of each of the {len(chunk)} competitor ad "
        f"creatives below, in order - one verdict per image.",
    ]
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
