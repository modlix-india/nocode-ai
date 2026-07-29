"""VisionAnalyst - single-shot vision BaseAgent for logo + creative picks.

Replaces the direct ``client.beta.chat.completions.parse(...)`` call in
``agents/product/product_assets.py:494-507`` with a properly-named agent
so vision picks show up in trace/cost/observability surfaces alongside
ProductAgent.

Why an agent and not a direct call: same reason as SummaryAgent - Adzump's
convention is "every LLM work unit is a BaseAgent subclass". See
``feedback_traced_llm_not_agent.md`` in workspace memory.

The vision LLM is the sole authority on logo picks - v9 (2026-05-22)
deleted the deterministic logo-FILLING fallback; when the LLM declines,
the pipeline returns ``logos=[]`` and the user uploads via the AD Pilot UI.
One deterministic guard remains, on the CREATIVE bucket:
``_filename_suggests_logo`` drops a logo-named URL the vision pass let
through as a creative (e.g. ``clublogo.png`` - seen in the wild).

Cost: ~same as today (sticking with gpt-4o-mini · see D5b in notes).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.core.agent import BaseAgent
from app.core.session import BaseSession, AuthContext
from app.core.streaming import AgentEventStream

from app.agents.adzump._shared import extract_json
from app.agents.adzump.agents.vision.context import (
    build_select_context,
    build_review_context,
)
from app.agents.adzump.agents.vision.models import (
    AssetSelection,
    ImageVerdict,
    ReviewResult,
    LogoChoice,
)

# DRAFT-NOTE: the public types still live in the product agent's models.py.
# See D4 in implementation-notes.md for the future cross-agent extraction.
from app.agents.adzump.agents.product.models import (
    CreativeCompleteness,
    CreativeRole,
    LogoPick,
    PageContent,
    ProductAssets,
    SiteImage,
)

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────
#
# Staying with gpt-4o-mini for cost parity with the existing direct call
# (~$0.15/1M in, vision-capable). Sonnet 4.6 would be a 20× cost bump for
# the same task. See D5b in implementation-notes.md.
VISION_PROVIDER = "openai"
VISION_MODEL_TIER = "fast"
VISION_MODEL_OVERRIDE = "openai:gpt-4o-mini"

# Old direct call used max_tokens=600. Keeping the same ceiling - the
# output is just a small JSON object.
VISION_MAX_TOKENS = 600

# Single-shot LLM call.
VISION_MAX_TURNS = 1


def _parse_selection(final_text: str) -> AssetSelection:
    """Parse and validate the LLM's final JSON as an ``AssetSelection``.

    Returns an empty ``AssetSelection()`` on any parse / validation failure.
    The caller treats empty as a decline → upload via AD Pilot UI.
    """
    payload = extract_json(final_text)
    if not payload:
        logger.warning("vision_select_no_json final_text=%r", final_text[:300])
        return AssetSelection()
    try:
        return AssetSelection.model_validate(payload)
    except ValidationError as e:
        logger.warning("vision_select_validation_failed err=%s payload=%r",
                       str(e)[:200], payload)
        return AssetSelection()


def _parse_review(final_text: str) -> ReviewResult:
    """Parse the review-each final JSON as a ``ReviewResult``. Empty on any
    parse/validation failure (caller treats empty as 'no usable verdicts')."""
    payload = extract_json(final_text)
    if not payload:
        logger.warning("vision_review_no_json final_text=%r", final_text[:300])
        return ReviewResult()
    try:
        return ReviewResult.model_validate(payload)
    except ValidationError as e:
        logger.warning("vision_review_validation_failed err=%s payload=%r",
                       str(e)[:200], payload)
        return ReviewResult()


_LOGO_FILENAME_TOKENS = ("logo", "wordmark", "brandmark", "monogram")


def _filename_suggests_logo(url: str) -> bool:
    """True if the URL's filename strongly indicates a logo. Used only as a
    post-pick safety net for the 'creative' bucket - content-based selection
    remains primary; this catches the narrow case where the vision pass let
    a sub-brand wordmark through (e.g. ``clublogo.png``)."""
    filename = url.rsplit("/", 1)[-1].lower()
    name_part = filename.rsplit(".", 1)[0]
    return any(tok in name_part for tok in _LOGO_FILENAME_TOKENS)


def _resolve_picks(
    selection: AssetSelection,
    candidates: list[SiteImage],
) -> ProductAssets:
    """Map LLM indices + roles onto ``ProductAssets`` (URLs + metadata).

    Owns the full input→output transform including the creative-bucket
    filename guard. Caller still owns: byte fetching and final persistence.
    """
    n = len(candidates)

    # Logos - dedup by URL, cap at 3 (schema also bounds this).
    logo_picks: list[LogoPick] = []
    logo_urls_seen: set[str] = set()
    for pick in (selection.logos or [])[:3]:
        if not (0 <= pick.idx < n):
            continue
        chosen = candidates[pick.idx]
        if chosen.src in logo_urls_seen:
            continue
        logo_urls_seen.add(chosen.src)
        ext = chosen.src.lower().rsplit("?", 1)[0].rsplit(".", 1)[-1]
        fmt = ext if ext in {"svg", "png", "jpg", "jpeg", "webp", "gif", "avif"} else ""
        if fmt == "jpeg":
            fmt = "jpg"
        bg = (pick.background_hint or "").strip().lower()
        if bg not in ("light", "dark"):
            bg = ""
        logo_picks.append(LogoPick(
            url=chosen.src,
            source=chosen.source,
            role=(pick.role or "").strip()[:32],
            reasoning=(pick.reasoning or "").strip()[:300],
            format=fmt,
            background=bg,
        ))

    # Creatives - preserve LLM ranking, dedup, drop OOB indices.
    # Shift 2 (2026-05-21): prefer the new per-candidate `creatives` field
    # (role-tagged). Fall back to the legacy `creative_idxs` shape so old
    # logs and replays still resolve.
    creative_urls: list[str] = []
    creatives_with_role: list[CreativeRole] = []
    seen_urls: set[str] = set()

    use_role_shape = bool(getattr(selection, "creatives", None))
    if use_role_shape:
        role_iter = [
            (c.idx, (c.role or "").strip().lower(), (c.reasoning or "").strip())
            for c in selection.creatives
        ]
    else:
        role_iter = [(i, "", "") for i in (selection.creative_idxs or [])]

    for idx, role, reasoning in role_iter:
        if not isinstance(idx, int) or not (0 <= idx < n):
            continue
        url = candidates[idx].src
        if not url or url in seen_urls or url in logo_urls_seen:
            continue
        if _filename_suggests_logo(url):
            logger.info("vision_safety_filter: dropped logo-named creative %s", url)
            continue
        if role and role not in {"hero", "amenity", "floor_plan", "unused"}:
            role = ""
        if role != "unused":
            creative_urls.append(url)
        creatives_with_role.append(CreativeRole(
            url=url,
            role=role,
            reasoning=reasoning[:200],
        ))
        seen_urls.add(url)

    # Derive creative_completeness - code computes the launch-readiness
    # verdict, model only labels (Kiran's Q3 pick).
    hero_found = any(c.role == "hero" for c in creatives_with_role)
    amenities_count = sum(1 for c in creatives_with_role if c.role == "amenity")
    floor_plan_found = any(c.role == "floor_plan" for c in creatives_with_role)
    if hero_found and amenities_count >= 1:
        verdict = "complete"
    elif hero_found or amenities_count >= 1:
        verdict = "partial"
    else:
        verdict = "needs_upload"
    # missing_categories must agree with the 'complete' bar (hero AND >=1
    # amenity). floor_plan is tracked but NOT required for launch-readiness -
    # listing it would make a 'complete' campaign still surface a "missing
    # floor plan" ask (v9 I-8; floor plans rarely live on marketing sites).
    missing: list[str] = []
    if not hero_found:
        missing.append("hero")
    if amenities_count < 1:
        missing.append("amenity")
    completeness = CreativeCompleteness(
        hero_found=hero_found,
        amenities_count=amenities_count,
        floor_plan_found=floor_plan_found,
        verdict=verdict,
        missing_categories=missing,
    )

    return ProductAssets(
        logos=logo_picks,
        creative_image_urls=creative_urls,
        creatives_with_role=creatives_with_role,
        creative_completeness=completeness,
        confidence=float(selection.confidence or 0.0),
        note=(selection.note or "")[:300],
    )


def _build_user_message_and_images(
    candidates: list[SiteImage],
    fetched: dict[str, dict],
    summary: str,
    meta_json: str,
    full_page_screenshot_b64: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Build the user-message text + the image content blocks.

    Text-first / images-after shape (matches what BaseAgent's
    ``session.append_user_message`` builds). Candidate descriptions are
    interleaved into the text in index order; images follow in the same
    order so the model correlates by position.

    SVG candidates appear as text-only entries (no thumbnail) - the LLM
    reviews them by metadata + filename.

    Emits image blocks in **Anthropic format** per the
    ``session.append_user_message`` docstring contract - the OpenAI provider
    translates Anthropic ``image`` blocks → Responses-API ``input_image`` in
    ``OpenAIProvider._convert_messages``.
    """
    import base64 as _b64

    # Shift 2 (2026-05-21): if a full-page screenshot is supplied, prepend it
    # as image block #0 so the vision LLM has spatial context (header strip
    # vs partner footer vs hero band) when deciding which candidate is the
    # developer logo, project logo, hero photo, etc. Candidate thumbnails
    # still ship in their original index order (image blocks #1..N).
    # When the screenshot is missing (defensive - should only happen on
    # adapter failure), the agent falls back to the v7 candidate-only shape.
    screenshot_present = bool(full_page_screenshot_b64)

    intro_lines = [
        "Business summary:",
        (summary or "(no summary available)").strip()[:2000],
        "",
    ]
    if screenshot_present:
        intro_lines.extend([
            "Image block #0 below is the FULL-PAGE SCREENSHOT of the live website "
            "(scaled, downsampled JPEG). Use it to locate each candidate "
            "spatially before deciding what it is - header strip = logos, "
            "partner footer strip = partner brands (NOT logos), hero band = "
            "hero creative, etc.",
            "",
            f"Image blocks #1..#{len(candidates)} are the {len(candidates)} candidate crops, "
            f"in the same order as the metadata below.",
        ])
    else:
        intro_lines.append(
            f"Candidates ({len(candidates)} total, in index order - images attached below in the same order):"
        )
    intro_lines.extend([meta_json, "", "Inline candidate descriptions follow in index order:"])

    image_blocks: list[dict[str, Any]] = []
    if screenshot_present:
        image_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": full_page_screenshot_b64,
            },
        })

    for idx, cand in enumerate(candidates):
        entry = fetched.get(cand.src) or {}
        filename = (cand.src or "").rsplit("/", 1)[-1].split("?", 1)[0][:80]
        thumb_bytes = entry.get("thumb_bytes")
        if thumb_bytes:
            intro_lines.append(
                f"  · [Candidate {idx}] source={cand.source} file={filename}"
            )
            image_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": entry.get("thumb_content_type") or "image/jpeg",
                    "data": _b64.b64encode(thumb_bytes).decode("ascii"),
                },
            })
        else:
            # SVG / no thumbnail - text-only entry, no image block.
            intro_lines.append(
                f"  · [Candidate {idx}] source={cand.source} format=SVG "
                f"file={filename} - no thumbnail; review by metadata + filename"
            )

    return "\n".join(intro_lines), image_blocks


def _build_review_message(
    images: list[dict[str, Any]], summary: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """Review-each user message: brief intro + one image block per input image,
    in order. Each ``images`` entry is ``{"data": bytes, "content_type": str}``
    (the upload path holds raw bytes). Mirrors the Anthropic image-block shape
    the provider already translates.

    Bytes-first by design: the upload caller has bytes. A URL input would be
    fetched to bytes by the caller first (as scrape does) - URL-source blocks
    are deferred until a caller needs them (see impl-notes)."""
    import base64 as _b64

    intro = [
        (summary or "").strip()[:1000],
        f"Review each of the {len(images)} image(s) below, in order - one verdict per image.",
    ]
    blocks: list[dict[str, Any]] = []
    for img in images:
        data = img.get("data") or b""
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.get("content_type") or "image/jpeg",
                "data": _b64.b64encode(data).decode("ascii"),
            },
        })
    return "\n".join(p for p in intro if p), blocks


class _SilentEventStream(AgentEventStream):
    """Drops every event except agent lifecycle + data.

    The VisionAnalyst call doesn't surface text or thinking to the user -
    output is just the parsed JSON, consumed by the caller. We still
    forward ``emit_agent_started/finished`` so the agent card lifecycle
    works.
    """

    def __init__(self, parent: AgentEventStream) -> None:
        # Deliberately do NOT call super().__init__().
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
        logger.debug("vision_select_substream_error: %s", message[:200])

    async def emit_done(self, *a, **kw) -> None:
        return

    async def emit_keepalive(self) -> None:
        return

    async def emit_suggestions(self, options, mode="single") -> None:
        return

    async def emit_data(self, data_type: str, payload: dict) -> None:
        await self._parent.emit_data(data_type, payload)

    async def emit_agent_started(self, agent_id, label, parent_id="root",
                                 parent_tool_use_id="",
                                 agent_tool_use_id="") -> None:
        await self._parent.emit_agent_started(
            agent_id, label, parent_id, parent_tool_use_id,
            agent_tool_use_id=agent_tool_use_id,
        )

    async def emit_agent_finished(self, agent_id, status="success",
                                  duration_ms=0, tokens_in=0, tokens_out=0,
                                  step_count=0, summary="") -> None:
        await self._parent.emit_agent_finished(
            agent_id, status, duration_ms, tokens_in, tokens_out,
            step_count, summary,
        )

    async def emit_agent_usage(self, agent_id, tokens_in, tokens_out) -> None:
        await self._parent.emit_agent_usage(agent_id, tokens_in, tokens_out)

    async def emit_craft(self, *a, **kw) -> None:
        return  # VisionAnalyst doesn't render to the craft itself.

    async def emit_craft_text(self, *a, **kw) -> None:
        return

    async def emit_feedback_request(self, session_id, turn_number) -> None:
        return


class VisionAnalyst(BaseAgent):
    """Single-shot vision agent. Two modes, one engine:

    - select-subset (scrape): pick logos + creatives from scraped candidates
      → ``AssetSelection`` → ``_resolve_picks`` → ``ProductAssets`` (``pick()``).
    - review-each (upload): one verdict per image → ``ReviewResult`` (``review()``).

    The system prompt can't be swapped per ``run()`` (it's built from the
    context at construction), so each mode is its own configured instance:
    ``get_selector()`` (select) and ``get_reviewer()`` (review)."""

    display_name = "Vision Analyst"

    _instance: "VisionAnalyst | None" = None

    def __init__(self, *, name: str = "vision_select", context_builder=None) -> None:
        context = context_builder or build_select_context()
        context._cached_static_text = context._static_prefix

        super().__init__(
            name=name,
            tools=[],
            context_builder=context,
            model_tier=VISION_MODEL_TIER,
            max_turns=VISION_MAX_TURNS,
            max_tokens=VISION_MAX_TOKENS,
            provider=VISION_PROVIDER,
            context_management=None,
        )

    @classmethod
    def get_instance(cls) -> "VisionAnalyst":
        # The select-subset (scrape) singleton - unchanged behavior.
        if cls._instance is None:
            cls._instance = cls()
            logger.info("VisionAnalyst created (select-subset / scrape, single-shot)")
        return cls._instance

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        return super().build_tool_context(session)

    async def pick(
        self,
        candidates: list[SiteImage],
        fetched: dict[str, dict],
        summary: str,
        meta_json: str,
        parent_event_stream: AgentEventStream,
        auth: AuthContext,
        parent_session_context: dict | None = None,
        full_page_screenshot_b64: str | None = None,
    ) -> ProductAssets:
        """Run one vision pick and return resolved ``ProductAssets``.

        Caller (``product_assets._select_assets_with_llm``) still owns:
        candidate prefiltering, parallel fetching of thumbnails, and the
        bytes dict for the persister.

        Returns an empty ``ProductAssets()`` if the LLM call fails or the
        JSON is unparseable. Downstream treats empty as a decline → upload.
        """
        if not candidates:
            return ProductAssets()

        import time as _time
        run_start = _time.monotonic()

        sub_session = BaseSession(agent_name="vision_select")
        await sub_session.get_or_create(None, auth)

        if parent_session_context is not None:
            sub_session.context = {
                "url": parent_session_context.get("url", ""),
                "craft_id": parent_session_context.get("craft_id", ""),
            }

        user_message, image_blocks = _build_user_message_and_images(
            candidates, fetched, summary, meta_json,
            full_page_screenshot_b64=full_page_screenshot_b64,
        )

        wrapped_stream = _SilentEventStream(parent_event_stream)

        try:
            await self.run(
                user_message=user_message,
                session=sub_session,
                event_stream=wrapped_stream,
                image_blocks=image_blocks,
                model_override=VISION_MODEL_OVERRIDE,
            )
        except Exception as e:
            logger.warning(
                "vision_select_run_failed: %s: %s",
                type(e).__name__, str(e)[:200],
            )
            await self._emit_finished(parent_event_stream, run_start, sub_session, status="error", summary=type(e).__name__)
            return ProductAssets()

        # Find the last assistant message - that's the JSON output.
        final_text = ""
        for m in reversed(sub_session.get_messages()):
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            if isinstance(content, str):
                final_text = content
                break
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                if any(parts):
                    final_text = "\n".join(p for p in parts if p)
                    break

        selection = _parse_selection(final_text)
        result = _resolve_picks(selection, candidates)
        await self._emit_finished(
            parent_event_stream, run_start, sub_session,
            status="success",
            summary=f"logos={len(result.logos)} creatives={len(result.creative_image_urls)}",
        )
        return result

    async def review(
        self,
        images: list[dict[str, Any]],
        parent_event_stream: AgentEventStream,
        auth: AuthContext,
        summary: str = "",
        parent_session_context: dict | None = None,
    ) -> ReviewResult:
        """Review-each: one verdict per image (the upload path). Returns an empty
        ``ReviewResult`` if the LLM call fails or the JSON is unparseable.

        Each ``images`` entry is ``{"data": bytes, "content_type": str}``. Use
        the review-each instance (``get_reviewer()``) - it carries the
        review-each system prompt.
        """
        if not images:
            return ReviewResult()

        import time as _time
        run_start = _time.monotonic()

        sub_session = BaseSession(agent_name=self.name)
        await sub_session.get_or_create(None, auth)
        if parent_session_context is not None:
            sub_session.context = {
                "url": parent_session_context.get("url", ""),
                "craft_id": parent_session_context.get("craft_id", ""),
            }

        user_message, image_blocks = _build_review_message(images, summary)
        wrapped_stream = _SilentEventStream(parent_event_stream)

        try:
            await self.run(
                user_message=user_message,
                session=sub_session,
                event_stream=wrapped_stream,
                image_blocks=image_blocks,
                model_override=VISION_MODEL_OVERRIDE,
            )
        except Exception as e:
            logger.warning("vision_review_run_failed: %s: %s",
                           type(e).__name__, str(e)[:200])
            await self._emit_finished(parent_event_stream, run_start, sub_session,
                                      status="error", summary=type(e).__name__)
            return ReviewResult()

        # Last assistant message = the JSON output. (Same shape as pick(); kept
        # inline rather than shared so pick()'s hot path stays byte-identical.)
        final_text = ""
        for m in reversed(sub_session.get_messages()):
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            if isinstance(content, str):
                final_text = content
                break
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                if any(parts):
                    final_text = "\n".join(p for p in parts if p)
                    break

        result = _parse_review(final_text)
        await self._emit_finished(
            parent_event_stream, run_start, sub_session,
            status="success", summary=f"verdicts={len(result.verdicts)}",
        )
        return result

    async def _emit_finished(
        self,
        parent_event_stream: AgentEventStream | None,
        run_start: float,
        sub_session: BaseSession,
        status: str,
        summary: str,
    ) -> None:
        """Emit agent_finished with token usage from sub_session.total_usage.

        Adds the observability hook the VisionAnalyst call previously lacked -
        production tool wrappers and the eval CapturingEventStream both
        consume this. Defensive: the VisionAnalyst has historically run in
        contexts where parent_event_stream is None (eval before the
        2026-05-21 CapturingEventStream).
        """
        if parent_event_stream is None:
            return
        import time as _time
        try:
            duration_ms = int((_time.monotonic() - run_start) * 1000)
            usage = sub_session.total_usage or {}
            await parent_event_stream.emit_agent_finished(
                agent_id=self.name,
                status=status,
                duration_ms=duration_ms,
                tokens_in=int(usage.get("input_tokens") or 0),
                tokens_out=int(usage.get("output_tokens") or 0),
                step_count=1,
                summary=summary,
            )
        except Exception:
            # Observability hook - never fail the pick on emit error.
            pass


_REVIEW_INSTANCE: "VisionAnalyst | None" = None


def get_selector() -> VisionAnalyst:
    """Accessor for the shared select-subset (scrape) singleton - unchanged."""
    return VisionAnalyst.get_instance()


def get_reviewer() -> VisionAnalyst:
    """Accessor for the shared review-each (upload) singleton. Its own instance
    because review-each needs a different system prompt than select-subset (the
    prompt can't be swapped per run)."""
    global _REVIEW_INSTANCE
    if _REVIEW_INSTANCE is None:
        _REVIEW_INSTANCE = VisionAnalyst(name="vision_review", context_builder=build_review_context())
        logger.info("VisionAnalyst created (review-each / upload, single-shot)")
    return _REVIEW_INSTANCE
