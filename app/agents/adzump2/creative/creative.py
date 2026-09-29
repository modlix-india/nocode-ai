"""A4 creative engine — strategy → best-of-N copy → image brief → attribute tag,
with a pre-spend critic/repair gate. MiniMax M3 for the reasoning; scoring +
prediction is Java (J20, later). A4 emits creatives; it does not score performance.

Flow (A4-creative.md §5):
  1. strategy    — pick angles from the product value props (P1: explore-only;
                   TODO(J19/J20) for market attributes + known winners).           [M3]
  2. copy        — best-of-N copy variants per angle, as slot POOLS.               [M3]
  3. image brief — a brief per visual angle; image gen via MCP generate_image is a
                   P1 TODO stub → brief + asset placeholders. Reuses VisionAnalyst
                   / web_fetch by import for the prod enrichment paths.
  4. attributes  — tag each creative with J5 taxonomy attributes (deterministic).
  5. critic gate — an M3 critic scores copy/brief vs the J5 rubric with bounded
                   repair (like A3). The PREDICT gate (J20/ML) is STUBBED in P1:
                   predict_score = None; nothing is hard-dropped on predict.

The single LLM seam is ``_llm_json`` — offline tests monkeypatch it with canned
outputs, so the whole pipeline is provable with no live model and no network.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.agents.adzump2.creative.leadform import build_lead_form
from app.agents.adzump2.creative.models import (
    EXPLORE,
    LAUNCH,
    PREDICT_TODO,
    Copy,
    Creative,
    CreativeAngle,
    CreativeSet,
    ImageBrief,
)
from app.agents.adzump2.creative.taxonomy import (
    KNOWN_FORMATS,
    VISUAL_FORMATS,
    FORMAT_SLOTS,
    VerticalTaxonomy,
    get_taxonomy,
    normalize_pools,
    validate_attributes,
)
from app.config import settings
from app.core.agent import BaseAgent
from app.core.context import BaseContext
from app.core.session import AuthContext, BaseSession
from app.core.streaming import AgentEventStream

# NOTE: the reused legacy pieces — VisionAnalyst (`get_selector`, asset classify)
# and `web_fetch` — are imported LAZILY inside the prod enrichment helpers
# (`_reference_scan`, `_classify_assets_via_vision`) below, never at module load.
# That keeps A4's offline path (and its tests) free of the legacy package's
# heavier import graph. Reuse-only: those modules are never modified.

logger = logging.getLogger(__name__)


# ── tunables (bounded — M3-thrash guard) ─────────────────────────────────────

N_ANGLES_DEFAULT = 3
BEST_OF_N_DEFAULT = 3
N_ANGLES_MAX = 6
BEST_OF_N_MAX = 5

CRITIC_THRESHOLD = 0.70       # below → bounded repair, then flag EXPLORE
MAX_CRITIC_REPAIR = 2         # ≤2 repair rounds per creative (monotonic, like A3)
MAX_LLM_CALLS = 24            # hard per-generate LLM budget (M3-thrash guard)

CREATIVE_MAX_TOKENS = 4000

_RUBRIC_AXES = ("clarity", "angle_fit", "compliance_safe", "hook_strength", "cta_fit")

_ASPECT_RATIOS = {
    "IMAGE": ["1:1", "4:5"],
    "VIDEO": ["9:16", "1:1"],
    "CAROUSEL": ["1:1"],
    "DEMAND_GEN": ["1:1", "1.91:1"],
}

_STRATEGY_TODO = (
    "TODO(J19/J20): strategy is explore-only in P1 (angles from product value "
    "props). Exploit known winners (J20) + market attribute distribution (J19) "
    "is a later phase."
)

_IMAGE_TODO = (
    "TODO(J16/generate_image): image generation via MCP generate_image / "
    "compositing (or appbuilder delegation) is stubbed in P1. The brief + "
    "placeholder asset refs are emitted so J7 can compile once J16 stores assets."
)


AGENT_PERSONA = """You are a senior performance-marketing creative strategist and copywriter for the Adzump platform.

You ground every angle, headline and hook STRICTLY on the product profile you are given — you never invent facts, prices, awards, or claims that aren't in the profile. You write compliance-safe copy (no guaranteed-returns language for regulated verticals like real estate/finance unless the profile states it, no discriminatory targeting language).

You are precise about output: when a task asks for JSON, you output ONLY the JSON object or array requested — no prose, no explanations, no markdown code fences.
"""


# ── JSON extraction (mirrors the vision agent's tolerant parser) ──────────────

_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


def _extract_json(text: str) -> Any | None:
    """Extract a JSON object/array from model text (fenced, bare, or embedded)."""
    if not text:
        return None
    m = _JSON_FENCE_RE.search(text)
    raw = m.group(1) if m else text.strip()
    raw = re.sub(r"^```[a-z]*\s*", "", raw)
    raw = re.sub(r"\s*```\s*$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        for open_c, close_c in (("{", "}"), ("[", "]")):
            s = raw.find(open_c)
            e = raw.rfind(close_c)
            if s != -1 and e != -1 and e > s:
                try:
                    return json.loads(raw[s : e + 1])
                except json.JSONDecodeError:
                    continue
        return None


@dataclass
class _Critique:
    """A critic verdict for one copy variant."""

    score: float = 0.0
    by_axis: dict[str, float] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


def _clamp(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _derive_hook(copy: Copy) -> str:
    """The hook is a free-text taxonomy axis: prefer a number-led headline."""
    lines = list(copy.headlines) + list(copy.primary_texts)
    for ln in lines:
        if re.search(r"[₹$%]|\b\d", ln):
            return ln.strip()[:120]
    return (lines[0].strip()[:120] if lines else "")


# ── quiet event stream for prod sub-agent runs ───────────────────────────────


class _QuietStream(AgentEventStream):
    """Drops chat/thinking/tool events; forwards agent-lifecycle to a parent if
    present. Mirrors the vision agent's ``_SilentEventStream`` but tolerates
    ``parent=None`` (the eval / offline path)."""

    def __init__(self, parent: AgentEventStream | None = None) -> None:
        self._parent = parent  # intentionally no super().__init__()

    @property
    def is_cancelled(self) -> bool:
        return getattr(self._parent, "is_cancelled", False)

    def cancel(self) -> None:
        try:
            if self._parent:
                self._parent.cancel()
        except Exception:
            pass

    async def emit_text(self, *a, **kw) -> None: return
    async def emit_thinking(self, *a, **kw) -> None: return
    async def emit_tool_start(self, *a, **kw) -> None: return
    async def emit_tool_update(self, *a, **kw) -> None: return
    async def emit_tool_result(self, *a, **kw) -> None: return
    async def emit_error(self, message: str) -> None:
        logger.debug("creative_substream_error: %s", str(message)[:200])
    async def emit_done(self, *a, **kw) -> None: return
    async def emit_keepalive(self) -> None: return
    async def emit_suggestions(self, *a, **kw) -> None: return
    async def emit_craft(self, *a, **kw) -> None: return
    async def emit_craft_text(self, *a, **kw) -> None: return
    async def emit_feedback_request(self, *a, **kw) -> None: return

    async def emit_data(self, data_type: str, payload: dict) -> None:
        if self._parent:
            await self._parent.emit_data(data_type, payload)

    async def emit_agent_started(self, *a, **kw) -> None:
        if self._parent:
            await self._parent.emit_agent_started(*a, **kw)

    async def emit_agent_finished(self, *a, **kw) -> None:
        if self._parent:
            await self._parent.emit_agent_finished(*a, **kw)

    async def emit_agent_usage(self, *a, **kw) -> None:
        if self._parent:
            await self._parent.emit_agent_usage(*a, **kw)


# ── the agent ─────────────────────────────────────────────────────────────────


class CreativeAgent(BaseAgent):
    """Single-shot M3 reasoning engine for A4 creative generation.

    Each reasoning step is one bounded LLM call routed through ``_llm_json`` —
    the single seam offline tests monkeypatch. Not wired into the chat loop;
    A1 reaches it via the ``generate_creatives`` tool.
    """

    display_name = "Creative Strategist"

    _instance: "CreativeAgent | None" = None

    def __init__(self) -> None:
        context = BaseContext(doc_paths=[], static_prefix=AGENT_PERSONA)
        context._cached_static_text = context._static_prefix
        super().__init__(
            name="adzump2_creative",
            tools=[],
            context_builder=context,
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=1,  # single-shot per reasoning call
            max_tokens=CREATIVE_MAX_TOKENS,
            provider=getattr(settings, "ADZUMP2_PROVIDER", settings.LLM_PROVIDER),
            context_management=None,
        )

    @classmethod
    def get_instance(cls) -> "CreativeAgent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("CreativeAgent created (single-shot M3, no tools)")
        return cls._instance

    # ── the LLM seam (offline tests monkeypatch THIS) ────────────────────────

    async def _complete_text(
        self,
        task: str,
        *,
        purpose: str,
        auth: AuthContext | None = None,
        event_stream: AgentEventStream | None = None,
    ) -> str:
        """Run one single-shot M3 completion and return the assistant text.

        Production path. Offline tests never reach here — they monkeypatch
        ``_llm_json``. Live-M3 proving of this path is a documented P1+ TODO.
        """
        if auth is None:
            logger.warning("creative %s: no auth — cannot run live M3", purpose)
            return ""
        sub_session = BaseSession(agent_name=self.name)
        await sub_session.get_or_create(None, auth)
        stream = _QuietStream(event_stream)
        try:
            await self.run(user_message=task, session=sub_session, event_stream=stream)
        except Exception as e:  # noqa: BLE001 — never let a step crash the pipeline
            logger.warning("creative %s run failed: %s: %s", purpose, type(e).__name__, str(e)[:200])
            return ""
        for m in reversed(sub_session.get_messages()):
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

    async def _llm_json(
        self,
        task: str,
        *,
        purpose: str,
        auth: AuthContext | None = None,
        event_stream: AgentEventStream | None = None,
    ) -> Any | None:
        """Single-shot M3 completion parsed as JSON. THE monkeypatch seam."""
        raw = await self._complete_text(task, purpose=purpose, auth=auth, event_stream=event_stream)
        return _extract_json(raw)

    async def _json_step(
        self, task: str, *, purpose: str, budget: dict[str, int], auth, event_stream
    ) -> Any | None:
        """Call the seam + count it against the per-generate LLM budget.

        HARD cap (M3-thrash guard): once the budget is spent, return None instead
        of calling the model — callers degrade gracefully (defaults / shortfall /
        fallback), and the total LLM-call count can never exceed ``budget['max']``.
        """
        if budget["calls"] >= budget["max"]:
            logger.warning("creative %s: LLM budget (%d) exhausted — skipping", purpose, budget["max"])
            return None
        budget["calls"] += 1
        return await self._llm_json(task, purpose=purpose, auth=auth, event_stream=event_stream)

    # ── prod enrichment (uses the reused imports; guarded, never hit offline) ──

    async def _reference_scan(
        self, reference_url: str | None, auth, event_stream
    ) -> str:
        """Optional competitor/reference read via the reused ``web_fetch`` tool."""
        if not reference_url or auth is None:
            return ""
        try:
            from app.agents.adzump.tools.research import web_fetch  # reuse (lazy)

            ctx = {"headers": auth.to_headers(), "event_stream": event_stream}
            res = await web_fetch.execute(
                {
                    "url": reference_url,
                    "question": "What ad angles, offers, and hooks does this page emphasize?",
                },
                ctx,
            )
            if res.success and isinstance(res.data, dict):
                return str(res.data.get("answer") or "")[:1500]
        except Exception as e:  # noqa: BLE001
            logger.debug("creative reference_scan failed: %s", e)
        return ""

    # ── step 1: strategy ─────────────────────────────────────────────────────

    async def _strategy(
        self,
        profile: dict[str, Any],
        taxonomy: VerticalTaxonomy,
        n_angles: int,
        market_signal: str,
        budget: dict[str, int],
        auth,
        event_stream,
    ) -> list[CreativeAngle]:
        angle_axis = taxonomy.axis("angle")
        allowed = ", ".join(angle_axis.values) if angle_axis else ""
        profile_json = json.dumps(
            {
                "name": profile.get("name"),
                "pitch": profile.get("pitch"),
                "value_props": profile.get("value_props"),
                "offerings": profile.get("offerings"),
                "price_band": profile.get("price_band"),
                "geo": profile.get("geo"),
                "tone": profile.get("tone"),
            },
            default=str,
        )[:2500]
        axes_help = (
            "visualSubject, offer, cta, audiencePairing (single values from the vertical vocab), "
            "and copyAttributes (a list)"
        )
        task = (
            f"Pick {n_angles} distinct creative ANGLES to advertise this product "
            f"(vertical: {taxonomy.code}).\n"
            f"Product profile: {profile_json}\n"
            + (f"Competitor/market signal: {market_signal}\n" if market_signal else "")
            + f"\nEach angle's `angle` MUST be one of: {allowed}.\n"
            f"For each angle also propose taxonomy attributes: {axes_help}.\n"
            "Ground each angle in the product's real value props — do not invent claims.\n\n"
            "Output ONLY a JSON array (no prose, no fences):\n"
            '[{"angle":"...","rationale":"...","attributes":{"visualSubject":"...","offer":"...",'
            '"cta":"...","audiencePairing":"...","copyAttributes":["..."]}}]'
        )
        payload = await self._json_step(
            task, purpose="strategy", budget=budget, auth=auth, event_stream=event_stream
        )
        angles = self._parse_angles(payload, taxonomy, n_angles)
        if not angles:
            angles = self._default_angles(taxonomy, n_angles)
        return angles

    @staticmethod
    def _as_list(payload: Any, key: str) -> list[Any]:
        """A model payload as a list: a bare array, or the array under ``key``."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get(key), list):
            return payload[key]
        return []

    def _parse_angles(
        self, payload: Any, taxonomy: VerticalTaxonomy, n_angles: int
    ) -> list[CreativeAngle]:
        angle_axis = taxonomy.axis("angle")
        vocab = set(angle_axis.values) if angle_axis else set()
        out: list[CreativeAngle] = []
        seen: set[str] = set()
        for it in self._as_list(payload, "angles"):
            if not isinstance(it, dict):
                continue
            angle = str(it.get("angle") or "").strip()
            # unknown angle → drop (strategy stays inside the taxonomy); dedupe
            if (vocab and angle not in vocab) or angle in seen or not angle:
                continue
            seen.add(angle)
            attrs = it.get("attributes") if isinstance(it.get("attributes"), dict) else {}
            out.append(
                CreativeAngle(
                    angle=angle,
                    rationale=str(it.get("rationale") or "")[:300],
                    strategy="explore",
                    attributes=attrs,
                )
            )
            if len(out) >= n_angles:
                break
        return out

    def _default_angles(self, taxonomy: VerticalTaxonomy, n_angles: int) -> list[CreativeAngle]:
        return [
            CreativeAngle(angle=a, rationale="default vertical angle", strategy="explore")
            for a in taxonomy.default_angles[:n_angles]
        ]

    # ── step 2: best-of-N copy ───────────────────────────────────────────────

    def _slot_help(self, fmt: str) -> str:
        specs = FORMAT_SLOTS.get(fmt, ())
        return "; ".join(
            f"{s.field}: {s.min}-{s.max} entries, <= {s.char_limit} chars each" for s in specs
        )

    async def _copy_variants(
        self,
        profile: dict[str, Any],
        angle: CreativeAngle,
        fmt: str,
        taxonomy: VerticalTaxonomy,
        n: int,
        budget: dict[str, int],
        auth,
        event_stream,
        *,
        repair_of: dict[str, Any] | None = None,
        issues: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        profile_json = json.dumps(
            {
                "name": profile.get("name"),
                "pitch": profile.get("pitch"),
                "value_props": profile.get("value_props"),
                "price_band": profile.get("price_band"),
                "geo": profile.get("geo"),
                "tone": profile.get("tone"),
            },
            default=str,
        )[:2000]
        slot_help = self._slot_help(fmt)
        if repair_of is not None:
            task = (
                f"Improve this ad copy for the '{angle.angle}' angle ({fmt} format) — fix the issues.\n"
                f"Current copy: {json.dumps(repair_of, default=str)[:1500]}\n"
                f"Issues to fix: {json.dumps(issues or [], default=str)[:800]}\n"
                f"Product profile: {profile_json}\n"
                f"Slot rules — {slot_help}.\n"
                "Ground on the profile; no invented claims; compliance-safe.\n\n"
                'Output ONLY: {"variants":[{"headlines":["..."],"primary_texts":["..."],'
                '"descriptions":["..."],"cta":"..."}]}  (exactly 1 variant)'
            )
            want = 1
        else:
            task = (
                f"Write {n} DISTINCT ad-copy variants for the '{angle.angle}' angle "
                f"({fmt} format).\n"
                f"Product profile: {profile_json}\n"
                f"Angle rationale: {angle.rationale}\n"
                f"Slot rules — fill these pools: {slot_help}.\n"
                "For RSA fill headlines + descriptions (no primary_texts). For visual "
                "formats fill primary_texts + headlines + descriptions.\n"
                "Ground on the profile; no invented claims; compliance-safe.\n\n"
                'Output ONLY: {"variants":[{"headlines":["..."],"primary_texts":["..."],'
                '"descriptions":["..."],"cta":"..."}]}'
            )
            want = n
        payload = await self._json_step(
            task, purpose="copy", budget=budget, auth=auth, event_stream=event_stream
        )
        return self._parse_variants(payload, want)

    def _parse_variants(self, payload: Any, want: int) -> list[dict[str, Any]]:
        items = self._as_list(payload, "variants")
        if not items and isinstance(payload, dict) and any(
            k in payload for k in ("headlines", "primary_texts", "descriptions")
        ):
            items = [payload]  # a single bare copy object
        out: list[dict[str, Any]] = []
        for it in items:
            if isinstance(it, dict):
                out.append(
                    {
                        "headlines": it.get("headlines") or [],
                        "primary_texts": it.get("primary_texts") or it.get("primaryTexts") or [],
                        "descriptions": it.get("descriptions") or [],
                        "cta": str(it.get("cta") or "").strip(),
                    }
                )
            if len(out) >= max(1, want):
                break
        return out

    # ── step 5a: critic ──────────────────────────────────────────────────────

    async def _critique(
        self,
        angle: CreativeAngle,
        fmt: str,
        variants: list[dict[str, Any]],
        taxonomy: VerticalTaxonomy,
        budget: dict[str, int],
        auth,
        event_stream,
    ) -> list[_Critique]:
        axes = ", ".join(_RUBRIC_AXES)
        task = (
            f"Score each ad-copy variant below for the '{angle.angle}' angle "
            f"({fmt} format), vertical {taxonomy.code}.\n"
            f"Variants (index order): {json.dumps(variants, default=str)[:2500]}\n\n"
            f"Score each on these axes 0.0-1.0: {axes}. Give an overall `score` "
            "(0.0-1.0) and list concrete `issues` to fix.\n"
            "Be a strict reviewer — reward clear, on-angle, compliance-safe copy; "
            "penalize vague or invented claims.\n\n"
            'Output ONLY: {"scores":[{"index":0,"score":0.0,"by_axis":{"clarity":0.0},'
            '"issues":["..."]}]}'
        )
        payload = await self._json_step(
            task, purpose="critique", budget=budget, auth=auth, event_stream=event_stream
        )
        return self._parse_critiques(payload, len(variants))

    def _parse_critiques(self, payload: Any, n: int) -> list[_Critique]:
        if isinstance(payload, dict) and isinstance(payload.get("scores"), list):
            rows = payload["scores"]
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        out: list[_Critique] = [_Critique() for _ in range(max(1, n))]
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            idx = row.get("index")
            idx = idx if isinstance(idx, int) and 0 <= idx < len(out) else i
            if idx >= len(out):
                continue
            try:
                score = float(row.get("score"))
            except (TypeError, ValueError):
                score = 0.0
            by_axis = {
                k: float(v)
                for k, v in (row.get("by_axis") or {}).items()
                if isinstance(v, (int, float))
            }
            issues = [str(x) for x in (row.get("issues") or [])][:8]
            out[idx] = _Critique(score=max(0.0, min(1.0, score)), by_axis=by_axis, issues=issues)
        return out

    # ── step 3: image brief (deterministic; gen is a P1 stub) ────────────────

    async def _image_brief(
        self,
        profile: dict[str, Any],
        angle: CreativeAngle,
        fmt: str,
        copy: Copy,
        taxonomy: VerticalTaxonomy,
        auth,
        event_stream,
    ) -> tuple[ImageBrief, list[str]]:
        visual_subject = str(angle.attributes.get("visualSubject") or "").strip()
        # Reference an existing profile asset that matches the subject/role. The
        # metadata match is offline-safe; when the profile carries raw scraped
        # candidates, the prod VisionAnalyst reuse classifies them (guarded).
        picked = self._pick_existing_asset(profile, visual_subject)
        if not picked and profile.get("asset_candidates") and auth is not None:
            picked = await self._classify_assets_via_vision(profile, auth, event_stream)
        overlay = ""
        if copy.headlines:
            overlay = copy.headlines[0]
        elif copy.primary_texts:
            overlay = copy.primary_texts[0]
        brief = ImageBrief(
            scene=visual_subject or "product",
            subject=str(profile.get("name") or "the product"),
            style=str(profile.get("tone") or "clean, modern, high-contrast"),
            overlay_text=overlay[:60],
            aspect_ratios=list(_ASPECT_RATIOS.get(fmt, ["1:1"])),
            route="PICK_EXISTING" if picked else "GENERATE",
            # PICKED = referencing an existing profile asset; STUBBED = generation
            # is a P1 TODO (MCP generate_image / J16), a placeholder ref is emitted.
            status="PICKED" if picked else "STUBBED",
            todo=_IMAGE_TODO,
        )
        if picked:
            asset_refs = picked
        else:
            asset_refs = [f"IMG_TODO::{taxonomy.code}/{angle.angle}/{fmt.lower()}"]
        return brief, asset_refs

    @staticmethod
    def _pick_existing_asset(profile: dict[str, Any], visual_subject: str) -> list[str]:
        """Deterministically reference a profile asset matching the visual subject.

        The full VisionAnalyst classify + J16 store pipeline is the prod path
        (imported ``get_selector`` — TODO(J16): wire raw candidate classification
        here). Offline we match on any role/type hint already on the profile.
        """
        assets = profile.get("assets")
        if not isinstance(assets, list):
            return []
        subj = (visual_subject or "").lower()
        matches: list[str] = []
        for a in assets:
            if not isinstance(a, dict):
                continue
            url = str(a.get("url") or a.get("src") or "").strip()
            if not url:
                continue
            role = str(a.get("role") or a.get("type") or "").lower()
            if not subj or subj in role or role in subj or role in ("hero", "amenity"):
                matches.append(f"existing::{url}")
            if len(matches) >= 2:
                break
        return matches

    async def _classify_assets_via_vision(
        self, profile: dict[str, Any], auth, event_stream
    ) -> list[str]:
        """Prod-only: classify the profile's raw scraped image candidates with the
        reused VisionAnalyst and reference the ones matching the visual subject.

        TODO(J16): the full pipeline fetches candidate thumbnails to bytes and
        stores/registers picks via J16 before referencing them. Here we do a
        best-effort classify with what the profile carries; any gap returns [].
        Never reached offline (guarded by ``asset_candidates`` + auth).
        """
        try:
            from app.agents.adzump.agents.vision.agent import get_selector  # reuse (lazy)
            from app.agents.adzump.agents.product.models import SiteImage

            raw = profile.get("asset_candidates") or []
            candidates = [SiteImage(**c) for c in raw if isinstance(c, dict) and c.get("src")]
            if not candidates:
                return []
            assets = await get_selector().pick(
                candidates=candidates,
                fetched={},
                summary=str(profile.get("pitch") or ""),
                meta_json="",
                parent_event_stream=event_stream,
                auth=auth,
                parent_session_context=None,
            )
            urls = list(getattr(assets, "creative_image_urls", []) or [])
            return [f"existing::{u}" for u in urls[:2]]
        except Exception as e:  # noqa: BLE001 — prod enrichment must never crash gen
            logger.debug("creative vision classify skipped: %s", e)
            return []

    # ── step 4: attributes (deterministic; validated to the J5 taxonomy) ─────

    @staticmethod
    def _build_attributes(
        angle: CreativeAngle, copy: Copy, taxonomy: VerticalTaxonomy
    ) -> tuple[dict[str, Any], list[str]]:
        proposed: dict[str, Any] = {"angle": angle.angle}
        proposed.update(angle.attributes or {})
        hook = _derive_hook(copy)
        if hook:
            proposed["hook"] = hook
        return validate_attributes(proposed, taxonomy)

    # ── per-creative build (copy → critic/repair → attrs → brief) ────────────

    async def _build_one(
        self,
        profile: dict[str, Any],
        angle: CreativeAngle,
        fmt: str,
        taxonomy: VerticalTaxonomy,
        best_of_n: int,
        budget: dict[str, int],
        auth,
        event_stream,
        *,
        cr_id: str,
    ) -> Creative:
        variants = await self._copy_variants(
            profile, angle, fmt, taxonomy, best_of_n, budget, auth, event_stream
        )
        if not variants:
            variants = [{"headlines": [], "primary_texts": [], "descriptions": [], "cta": ""}]

        crits = await self._critique(
            angle, fmt, variants, taxonomy, budget, auth, event_stream
        )
        best_i = max(range(len(variants)), key=lambda i: crits[i].score if i < len(crits) else 0.0)

        rounds = 0
        while (
            crits[best_i].score < CRITIC_THRESHOLD
            and rounds < MAX_CRITIC_REPAIR
            and budget["calls"] + 2 <= budget["max"]  # room for repair copy + re-critique
        ):
            rounds += 1
            repaired = await self._copy_variants(
                profile, angle, fmt, taxonomy, 1, budget, auth, event_stream,
                repair_of=variants[best_i], issues=crits[best_i].issues,
            )
            if repaired:
                variants[best_i] = repaired[0]
            new_crit = await self._critique(
                angle, fmt, [variants[best_i]], taxonomy, budget, auth, event_stream
            )
            if new_crit:
                crits[best_i] = new_crit[0]

        chosen = variants[best_i]
        crit = crits[best_i]

        pools, shortfalls = normalize_pools(chosen, fmt)
        cta = pools["cta"] or str(angle.attributes.get("cta") or "")
        copy = Copy(
            headlines=pools["headlines"],
            primary_texts=pools["primary_texts"],
            descriptions=pools["descriptions"],
            cta=cta,
        )
        attributes, attr_warn = self._build_attributes(angle, copy, taxonomy)

        image_brief = None
        asset_refs: list[str] = []
        if fmt in VISUAL_FORMATS:
            image_brief, asset_refs = await self._image_brief(
                profile, angle, fmt, copy, taxonomy, auth, event_stream
            )

        disposition = LAUNCH if crit.score >= CRITIC_THRESHOLD else EXPLORE
        return Creative(
            id=cr_id,
            format=fmt,
            copy=copy,
            attributes=attributes,
            asset_refs=asset_refs,
            predict_score=None,  # STUBBED in P1 — J20/ML scores later
            source="GENERATED",
            image_brief=image_brief,
            critic_score=crit.score,
            critic_issues=crit.issues,
            attribute_warnings=attr_warn,
            pool_shortfalls=shortfalls,
            disposition=disposition,
            predict_note=PREDICT_TODO,
        )

    # ── the public entrypoint ────────────────────────────────────────────────

    async def generate(
        self,
        *,
        profile: dict[str, Any],
        vertical: str | None = None,
        formats: list[str] | None = None,
        n_angles: int = N_ANGLES_DEFAULT,
        best_of_n: int = BEST_OF_N_DEFAULT,
        reference_url: str | None = None,
        auth: AuthContext | None = None,
        event_stream: AgentEventStream | None = None,
    ) -> CreativeSet:
        """Produce attribute-tagged, critic-gated creatives + a lead form.

        ``profile`` is the A2 ProductProfile (dict). ``vertical`` selects the J5
        taxonomy (defaults to ``profile['vertical']`` → ``generic``). ``formats``
        default to ``["RSA","IMAGE"]``.
        """
        profile = profile or {}
        taxonomy = get_taxonomy(vertical or profile.get("vertical"))
        fmts = [f for f in (formats or []) if f in KNOWN_FORMATS] or ["RSA", "IMAGE"]
        # dedupe, keep order
        fmts = list(dict.fromkeys(fmts))
        n_angles = _clamp(n_angles, 1, N_ANGLES_MAX, N_ANGLES_DEFAULT)
        best_of_n = _clamp(best_of_n, 1, BEST_OF_N_MAX, BEST_OF_N_DEFAULT)

        budget = {"calls": 0, "max": MAX_LLM_CALLS}
        warnings: list[str] = [_STRATEGY_TODO]

        market_signal = await self._reference_scan(reference_url, auth, event_stream)

        angles = await self._strategy(
            profile, taxonomy, n_angles, market_signal, budget, auth, event_stream
        )

        creatives: list[Creative] = []
        for a_i, angle in enumerate(angles):
            for fmt in fmts:
                if budget["calls"] >= budget["max"]:
                    warnings.append(f"LLM budget ({budget['max']}) reached — stopped early")
                    break
                creative = await self._build_one(
                    profile, angle, fmt, taxonomy, best_of_n, budget, auth, event_stream,
                    cr_id=f"cr_{a_i + 1}_{fmt.lower()}",
                )
                creatives.append(creative)
            else:
                continue
            break

        lead_form = await build_lead_form(
            self, profile, taxonomy, auth=auth, event_stream=event_stream
        )

        for c in creatives:
            warnings.extend(c.attribute_warnings)
            warnings.extend(c.pool_shortfalls)

        predict = {
            "status": "STUBBED",
            "scored": False,
            "floor": None,
            "todo": PREDICT_TODO,
        }
        return CreativeSet(
            vertical=taxonomy.code,
            creatives=creatives,
            lead_form=lead_form,
            angles=angles,
            predict=predict,
            warnings=warnings,
            llm_calls=budget["calls"],
        )


def get_creative_agent() -> CreativeAgent:
    """Module-level accessor for the shared CreativeAgent singleton."""
    return CreativeAgent.get_instance()
