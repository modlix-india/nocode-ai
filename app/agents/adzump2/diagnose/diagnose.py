"""A5 diagnose engine — the loop's qualitative half.

Reads the J10 ``PerformanceSnapshot`` + the J12 ``ActionSet`` + the J20 attribute
map (+ the leadzump tags/notes the snapshot's CRM rows carry) and reasons over
them in ONE unified, vertical-aware M3 pass, emitting a ``Diagnosis``:

  * a plain-language NARRATIVE (which angle wins, where junk concentrates, why),
  * J12's actions PRIORITIZED + business-framed (``ranked_actions``),
  * qualitative creative/audience TEST proposals grounded in real J20 gaps,
  * a WATCHLIST of thin/immature grains to observe, not act on.

Boundary (A5-diagnose.md §5.6): A5 is **LLM reasoning only**. It produces no
numbers and applies nothing — the numbers, gates, objective and prediction are
Java (J10/J12/J20). So this engine:

  * NARRATES + PRIORITIZES J12's actions — it never recomputes their numbers.
    Every ``AnnotatedAction`` carries J12's expectedDelta/confidence/verdicts
    verbatim, and ``ranked_actions`` is a strict subset of the J12 ``ActionSet``.
  * RESPECTS J12's gates — a thin / FAST_ONLY grain is put on the ``watchlist``,
    NEVER surfaced as an act-now recommendation (fast signal proposes, slow
    signal disposes; §5.3). This is enforced in CODE, not left to the model.
  * GROUNDS test proposals in real gaps in the J20 attribute map (under-explored
    or junk-concentrated axis+values) — a random creative guess is dropped.

The single LLM seam is ``_llm_json`` — offline tests monkeypatch it with a canned
``Diagnosis`` payload, so the whole pipeline is provable with no live model and no
network. The per-dimension judgment from the legacy optimization agents
(age/gender/keyword/location/search-term) is folded into the ONE prompt as
guidance (§5.5), not ported as five separate agents.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.agents.adzump2.diagnose.models import (
    AnnotatedAction,
    Diagnosis,
    TestProposal,
    WatchItem,
)
from app.config import settings
from app.core.agent import BaseAgent
from app.core.context import BaseContext
from app.core.session import AuthContext, BaseSession
from app.core.streaming import AgentEventStream

logger = logging.getLogger(__name__)


# ── tunables (bounded — M3-thrash guard) ─────────────────────────────────────

MAX_LLM_CALLS = 2          # ONE unified diagnose call + at most one reparse retry
DIAGNOSE_MAX_TOKENS = 4000

FAST_ONLY = "FAST_ONLY"
MATURE = "MATURE"

# A grain whose signal isn't MATURE is "thin": A5 defers it to the watchlist and
# never surfaces an act-now on it, regardless of what the model proposes.
_THIN_MATURITIES = {FAST_ONLY, "PARTIAL", ""}

# junk-source detection (snapshot CRM) + leadzump junk tag markers.
JUNK_RATE_HIGH = 0.30
_JUNK_TAG_MARKERS = ("junk", "budget-mismatch", "budget_mismatch", "out-of-budget", "irrelevant", "spam")

# J20 attribute-gap detection — a value worth TESTING (explore) rather than a
# proven winner: under-explored (thin volume / low confidence) OR junk-concentrated.
GAP_VOLUME = 50
GAP_CONFIDENCE = 0.40
GAP_JUNK_CORR = 0.35

# J20 winning-attribute detection (exploit) — the "which angle wins" story.
WIN_MIN_LIFT = 1.2
WIN_MIN_VOLUME = 50
WIN_MIN_CONFIDENCE = 0.50


AGENT_PERSONA = """You are a senior performance-marketing analyst for the Adzump platform. You explain WHY a campaign is performing the way it is and what to do about it, in plain language a founder can act on.

You reason over three given facts and NEVER recompute them:
- a PerformanceSnapshot (platform + leadzump CRM metrics joined at the ad grain, with a blended score and a signal-maturity per grain),
- the engine's proposed ActionSet (already significance-gated), and
- an attribute performance map (which creative angle/audience/offer wins vs baseline).

Your discipline:
- You NARRATE and PRIORITIZE the engine's actions; you never invent your own numbers, never restate a different expectedDelta, and never propose acting on a grain the engine left alone.
- You respect signal maturity: a FAST_ONLY / immature grain has spend but not enough downstream CRM outcome to trust — it goes on the WATCHLIST, never into act-now recommendations. Fast signal proposes, slow signal disposes.
- The leadzump tags/notes are your moat: they tell you lead QUALITY (which angle books site-visits, where the junk / budget-mismatch leads concentrate) that platform metrics can't. Read them as direction.
- Your test proposals must target a REAL gap in the attribute map (an under-explored or junk-concentrated angle/audience/offer), not a random guess.

When asked for JSON you output ONLY the JSON object requested — no prose, no markdown fences.
"""


# ── JSON extraction (mirrors the creative agent's tolerant parser) ────────────

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


def _f(v: Any) -> float | None:
    """Best-effort float, else None."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── quiet event stream for prod sub-agent runs (mirrors A4) ───────────────────


class _QuietStream(AgentEventStream):
    """Drops chat/thinking/tool events; forwards agent-lifecycle to a parent if
    present. Tolerates ``parent=None`` (the eval / offline path)."""

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
        logger.debug("diagnose_substream_error: %s", str(message)[:200])
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


class DiagnoseAgent(BaseAgent):
    """Single-shot M3 reasoning engine for A5 diagnosis.

    ONE unified vertical-aware diagnose pass routed through ``_llm_json`` — the
    single seam offline tests monkeypatch. Not wired into the chat loop; A1
    reaches it via the ``diagnose`` tool.
    """

    display_name = "Campaign Diagnostician"

    _instance: "DiagnoseAgent | None" = None

    def __init__(self) -> None:
        context = BaseContext(doc_paths=[], static_prefix=AGENT_PERSONA)
        context._cached_static_text = context._static_prefix
        super().__init__(
            name="adzump2_diagnose",
            tools=[],
            context_builder=context,
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=1,  # single-shot per reasoning call
            max_tokens=DIAGNOSE_MAX_TOKENS,
            provider=getattr(settings, "ADZUMP2_PROVIDER", settings.LLM_PROVIDER),
            context_management=None,
        )

    @classmethod
    def get_instance(cls) -> "DiagnoseAgent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("DiagnoseAgent created (single-shot M3, no tools)")
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
        ``_llm_json``. Live-M3 proving of this path is the P4.5 integration gate.
        """
        if auth is None:
            logger.warning("diagnose %s: no auth — cannot run live M3", purpose)
            return ""
        sub_session = BaseSession(agent_name=self.name)
        await sub_session.get_or_create(None, auth)
        stream = _QuietStream(event_stream)
        try:
            await self.run(user_message=task, session=sub_session, event_stream=stream)
        except Exception as e:  # noqa: BLE001 — never let a step crash the pipeline
            logger.warning("diagnose %s run failed: %s: %s", purpose, type(e).__name__, str(e)[:200])
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
        """Call the seam + count it against the per-diagnose LLM budget.

        HARD cap (M3-thrash guard): once the budget is spent, return None instead
        of calling the model — the caller degrades to a deterministic diagnosis,
        and the total call count can never exceed ``budget['max']``.
        """
        if budget["calls"] >= budget["max"]:
            logger.warning("diagnose %s: LLM budget (%d) exhausted — skipping", purpose, budget["max"])
            return None
        budget["calls"] += 1
        return await self._llm_json(task, purpose=purpose, auth=auth, event_stream=event_stream)

    # ── deterministic signal extraction (NOT LLM — the numbers stay Java's) ──

    @staticmethod
    def _rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        rows = snapshot.get("rows") or snapshot.get("grainRows") or []
        return [r for r in rows if isinstance(r, dict)]

    @classmethod
    def _index_rows(cls, snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """entityId → row. The action ``targetId`` joins to a row by entityId."""
        out: dict[str, dict[str, Any]] = {}
        for r in cls._rows(snapshot):
            eid = str(r.get("entityId") or r.get("adGrainId") or "").strip()
            if eid:
                out[eid] = r
        return out

    @classmethod
    def _thin_grains(cls, snapshot: dict[str, Any]) -> set[str]:
        """Grains whose signal isn't MATURE — deferred to the watchlist."""
        thin: set[str] = set()
        for eid, r in cls._index_rows(snapshot).items():
            if str(r.get("signalMaturity") or "").strip().upper() in _THIN_MATURITIES:
                thin.add(eid)
        return thin

    @staticmethod
    def _row_junk_rate(row: dict[str, Any]) -> float:
        crm = row.get("crm") or {}
        rate = _f(crm.get("junkRate"))
        if rate is not None:
            return rate
        leads = _f(crm.get("leads")) or 0.0
        junk = _f(crm.get("junk")) or 0.0
        return (junk / leads) if leads > 0 else 0.0

    @staticmethod
    def _row_tags(row: dict[str, Any]) -> list[str]:
        crm = row.get("crm") or {}
        tags = crm.get("tags") or row.get("tags") or []
        return [str(t) for t in tags if t]

    @classmethod
    def _junk_sources(cls, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Grains where junk concentrates — high junkRate OR a junk/budget-mismatch
        leadzump tag. This is the lead-QUALITY signal only leadzump carries."""
        out: list[dict[str, Any]] = []
        for eid, r in cls._index_rows(snapshot).items():
            rate = cls._row_junk_rate(r)
            tags = cls._row_tags(r)
            tagged = any(any(m in t.lower() for m in _JUNK_TAG_MARKERS) for t in tags)
            if rate >= JUNK_RATE_HIGH or tagged:
                out.append({
                    "targetId": eid,
                    "grain": str(r.get("grain") or ""),
                    "junkRate": round(rate, 4),
                    "tags": tags,
                    "notes": str((r.get("crm") or {}).get("notes") or ""),
                })
        out.sort(key=lambda x: x["junkRate"], reverse=True)
        return out

    @staticmethod
    def _attribute_entries(attribute_map: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(attribute_map, list):
            entries = attribute_map
        else:
            entries = attribute_map.get("attributes") or attribute_map.get("entries") or []
        return [e for e in entries if isinstance(e, dict)]

    @classmethod
    def _gap_attributes(cls, attribute_map: dict[str, Any]) -> list[dict[str, Any]]:
        """Axis+values worth TESTING: under-explored (thin volume / low confidence)
        or junk-concentrated. A proven winner (high volume + confidence) is NOT a
        gap — that's exploit, not explore."""
        gaps: list[dict[str, Any]] = []
        for e in cls._attribute_entries(attribute_map):
            axis = str(e.get("axis") or "").strip()
            value = str(e.get("value") or "").strip()
            if not axis or not value:
                continue
            vol = _f(e.get("volume")) or 0.0
            conf = _f(e.get("confidence")) or 0.0
            junk = _f(e.get("junkCorrelation")) or 0.0
            if vol < GAP_VOLUME or conf < GAP_CONFIDENCE or junk >= GAP_JUNK_CORR:
                gaps.append({
                    "axis": axis, "value": value,
                    "volume": vol, "confidence": conf, "junkCorrelation": junk,
                    "outcomeLift": _f(e.get("outcomeLift")),
                })
        return gaps

    @classmethod
    def _winning_attributes(cls, attribute_map: dict[str, Any]) -> list[dict[str, Any]]:
        """Proven winners (exploit): high outcome lift, enough volume + confidence
        to trust. This is the "which angle wins" story A5 tells."""
        wins: list[dict[str, Any]] = []
        for e in cls._attribute_entries(attribute_map):
            axis = str(e.get("axis") or "").strip()
            value = str(e.get("value") or "").strip()
            lift = _f(e.get("outcomeLift"))
            vol = _f(e.get("volume")) or 0.0
            conf = _f(e.get("confidence")) or 0.0
            if not axis or not value or lift is None:
                continue
            if lift >= WIN_MIN_LIFT and vol >= WIN_MIN_VOLUME and conf >= WIN_MIN_CONFIDENCE:
                wins.append({
                    "axis": axis, "value": value, "outcomeLift": lift,
                    "volume": vol, "confidence": conf,
                })
        wins.sort(key=lambda x: x["outcomeLift"], reverse=True)
        return wins

    def _signals(
        self, snapshot: dict[str, Any], attribute_map: dict[str, Any]
    ) -> dict[str, Any]:
        """The deterministic grounding A5 reasons over (and injects into the
        prompt): winning attributes, junk sources, thin grains, attribute gaps."""
        return {
            "winningAttributes": self._winning_attributes(attribute_map),
            "junkSources": self._junk_sources(snapshot),
            "thinGrains": sorted(self._thin_grains(snapshot)),
            "attributeGaps": self._gap_attributes(attribute_map),
        }

    # ── prompt (ONE unified vertical-aware pass — §5.5) ──────────────────────

    def _build_prompt(
        self,
        *,
        vertical: str,
        snapshot: dict[str, Any],
        action_set: dict[str, Any],
        signals: dict[str, Any],
        actions: list[dict[str, Any]],
    ) -> str:
        rows_view = [
            {
                "entityId": r.get("entityId"),
                "grain": r.get("grain"),
                "blendedScore": r.get("blendedScore"),
                "signalMaturity": r.get("signalMaturity"),
                "platform": r.get("platform"),
                "crm": r.get("crm"),
            }
            for r in self._rows(snapshot)
        ]
        actions_view = [
            {
                "index": i,
                "type": a.get("type"),
                "targetId": a.get("targetId"),
                "expectedDelta": a.get("expectedDelta"),
                "confidence": a.get("confidence"),
                "risk": a.get("risk"),
                "rationale": a.get("rationale"),
            }
            for i, a in enumerate(actions)
        ]
        return (
            f"Diagnose this {vertical or 'generic'} ad campaign. Reason across budget, "
            "bid, audience, keyword and creative TOGETHER (they interact) — do not treat "
            "them as five separate problems.\n\n"
            f"PERFORMANCE SNAPSHOT rows (platform + leadzump CRM, joined at the ad grain):\n"
            f"{json.dumps(rows_view, default=str)[:3500]}\n\n"
            f"The engine's proposed ACTIONS (already significance-gated — narrate + "
            f"prioritize these, do NOT invent your own or change their numbers):\n"
            f"{json.dumps(actions_view, default=str)[:2000]}\n\n"
            f"Grounding signals the platform computed for you:\n"
            f"- winning attributes (exploit): {json.dumps(signals['winningAttributes'], default=str)[:900]}\n"
            f"- junk sources (leads tagged junk/budget-mismatch): {json.dumps(signals['junkSources'], default=str)[:900]}\n"
            f"- thin / immature grains (WATCH only, never act): {json.dumps(signals['thinGrains'], default=str)[:400]}\n"
            f"- attribute GAPS worth testing: {json.dumps(signals['attributeGaps'], default=str)[:900]}\n\n"
            "Legacy judgment to apply as guidance (folded from the per-dimension optimizers):\n"
            "- Audience (age/gender/geo): trim segments with poor CTR / high cost-per-outcome; scale a "
            "segment only once it shows real converting volume — never enable a weak audience on thin data.\n"
            "- Keyword/search-term (Google): negative-keyword the wasteful/irrelevant/competitor terms "
            "first (cheapest, safest); a term is only 'own-intent' when it matches the brand + a relevant "
            "config/location.\n"
            "- Creative: rotate/pause clear losers; when an angle wins on lead QUALITY (site-visits, low "
            "junk), request more variants of it; where junk concentrates, test a different angle.\n"
            "- Lead quality over CTR: a cheap lead that leadzump tags junk/budget-mismatch is not a win.\n\n"
            "Produce the diagnosis. Output ONLY this JSON (no prose, no fences):\n"
            '{"narrative":"plain-language what is happening and WHY, naming the winning angle and the '
            'junk source",'
            '"ranked_actions":[{"target_id":"<from the actions above>","type":"<its type>",'
            '"priority":1,"why":"business framing of why this matters"}],'
            '"test_proposals":[{"hypothesis":"...","angle":"<axis value from a GAP>",'
            '"audience":"...","route":"A4","grounds_on":{"axis":"angle","value":"<a real gap value>"},'
            '"rationale":"..."}],'
            '"watchlist":[{"target_id":"<a thin grain>","reason":"why keep watching"}]}'
        )

    # ── parse the model payload (tolerant) ───────────────────────────────────

    @staticmethod
    def _as_list(payload: Any, key: str) -> list[Any]:
        if isinstance(payload, dict) and isinstance(payload.get(key), list):
            return payload[key]
        return []

    # ── the public entrypoint ────────────────────────────────────────────────

    async def diagnose(
        self,
        *,
        snapshot: dict[str, Any],
        action_set: dict[str, Any] | None = None,
        attribute_map: dict[str, Any] | None = None,
        vertical: str | None = None,
        auth: AuthContext | None = None,
        event_stream: AgentEventStream | None = None,
    ) -> Diagnosis:
        """Reason over J10 + J12 + J20 and emit a ``Diagnosis``.

        A5 recomputes NO numbers and applies NOTHING: ``ranked_actions`` is a
        subset of the J12 ``ActionSet`` with J12's numbers carried verbatim, thin
        grains land on the ``watchlist`` (never act-now), and ``test_proposals``
        are kept only when grounded in a real J20 attribute gap.
        """
        snapshot = snapshot or {}
        action_set = action_set or {}
        attribute_map = attribute_map or {}
        vertical = vertical or str(snapshot.get("vertical") or "") or None

        warnings: list[str] = []
        budget = {"calls": 0, "max": MAX_LLM_CALLS}

        # ---- deterministic facts (the Java numbers; A5 never recomputes) ----
        rows_by_id = self._index_rows(snapshot)
        thin = self._thin_grains(snapshot)
        signals = self._signals(snapshot, attribute_map)
        gap_keys = {(g["axis"], g["value"]) for g in signals["attributeGaps"]}

        j12_actions = [a for a in (action_set.get("actions") or []) if isinstance(a, dict)]
        # key a J12 action by (targetId, type); the model references these to add why/priority.
        j12_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for a in j12_actions:
            key = (str(a.get("targetId") or ""), str(a.get("type") or ""))
            j12_by_key.setdefault(key, a)

        # ---- ONE unified diagnose call (bounded; degrade if it fails) ----
        prompt = self._build_prompt(
            vertical=vertical or "", snapshot=snapshot, action_set=action_set,
            signals=signals, actions=j12_actions,
        )
        payload = await self._json_step(
            prompt, purpose="diagnose", budget=budget, auth=auth, event_stream=event_stream
        )
        if not isinstance(payload, dict):
            # one bounded reparse retry, then degrade to a deterministic diagnosis.
            payload = await self._json_step(
                prompt, purpose="diagnose", budget=budget, auth=auth, event_stream=event_stream
            )
        if not isinstance(payload, dict):
            payload = {}
            warnings.append("LLM produced no parseable diagnosis — degraded to deterministic narration")

        narrative = str(payload.get("narrative") or "").strip()

        # ---- ranked_actions: SUBSET of J12, numbers VERBATIM, thin grains excluded ----
        model_ranked = self._as_list(payload, "ranked_actions")
        # model's ordering hints, keyed to a J12 action.
        hint_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for it in model_ranked:
            if not isinstance(it, dict):
                continue
            key = (str(it.get("target_id") or it.get("targetId") or ""),
                   str(it.get("type") or ""))
            if key in j12_by_key:
                hint_by_key.setdefault(key, it)
                if key not in order:
                    order.append(key)
            else:
                # the model tried to surface an act-now the engine did NOT gate.
                tid = key[0]
                if tid in thin:
                    warnings.append(
                        f"dropped model act-now on thin grain '{tid}' → watchlist "
                        "(fast signal proposes, slow signal disposes)"
                    )
                elif tid:
                    warnings.append(
                        f"dropped model-invented action '{key[1]}' on '{tid}' — a genuinely-new "
                        "action must go through propose_action (the J12 gates), not the narrative"
                    )

        # append any J12 actions the model didn't rank (A5 annotates ALL gated actions).
        for key in j12_by_key:
            if key not in order:
                order.append(key)

        ranked_actions: list[AnnotatedAction] = []
        for key in order:
            action = j12_by_key[key]
            tid = str(action.get("targetId") or "")
            if tid in thin:
                # discipline (§5.3): never surface an act-now on immature signal.
                continue
            hint = hint_by_key.get(key, {})
            ranked_actions.append(
                AnnotatedAction(
                    type=str(action.get("type") or ""),
                    target_id=tid,
                    change=action.get("change") if isinstance(action.get("change"), dict) else {},
                    rationale=str(action.get("rationale") or ""),
                    why=str(hint.get("why") or hint.get("rationale") or "")[:400],
                    priority=hint.get("priority") if isinstance(hint.get("priority"), int) else 0,
                    # numbers + verdicts carried VERBATIM from J12 (A5 recomputes none):
                    expected_delta=_f(action.get("expectedDelta")),
                    confidence=_f(action.get("confidence")),
                    significance_verdict=str(action.get("significanceVerdict") or ""),
                    risk=str(action.get("risk") or ""),
                    requires_approval=bool(action.get("requiresApproval", True)),
                )
            )
        # stable order: model priority first (1..n), unranked (0) last, then by expectedDelta.
        ranked_actions.sort(
            key=lambda a: (
                a.priority if a.priority and a.priority > 0 else 10_000,
                -(a.expected_delta or 0.0),
            )
        )

        # ---- watchlist: EVERY thin grain (deterministic), + model reasons ----
        model_watch_reason: dict[str, str] = {}
        for it in self._as_list(payload, "watchlist"):
            if isinstance(it, dict):
                tid = str(it.get("target_id") or it.get("targetId") or "")
                if tid:
                    model_watch_reason[tid] = str(it.get("reason") or "")[:300]
        watchlist: list[WatchItem] = []
        for tid in sorted(thin):
            row = rows_by_id.get(tid, {})
            maturity = str(row.get("signalMaturity") or "").strip() or FAST_ONLY
            reason = model_watch_reason.get(tid) or (
                f"{maturity}: spend but not enough matured CRM outcome to trust an action yet"
            )
            watchlist.append(
                WatchItem(
                    target_id=tid,
                    grain=str(row.get("grain") or ""),
                    signal_maturity=maturity,
                    reason=reason,
                )
            )

        # ---- test_proposals: kept ONLY when grounded in a real J20 gap ----
        test_proposals: list[TestProposal] = []
        for it in self._as_list(payload, "test_proposals"):
            if not isinstance(it, dict):
                continue
            grounds = it.get("grounds_on") or it.get("groundsOn") or {}
            axis = str(grounds.get("axis") or "").strip()
            value = str(grounds.get("value") or "").strip()
            grounded = (axis, value) in gap_keys
            if not grounded:
                warnings.append(
                    f"dropped ungrounded test proposal (axis={axis!r}, value={value!r}) — "
                    "not a real gap in the J20 attribute map"
                )
                continue
            test_proposals.append(
                TestProposal(
                    hypothesis=str(it.get("hypothesis") or "")[:400],
                    angle=str(it.get("angle") or value),
                    audience=str(it.get("audience") or ""),
                    rationale=str(it.get("rationale") or "")[:400],
                    route=str(it.get("route") or "A4").upper(),
                    grounds_on={"axis": axis, "value": value},
                    grounded=True,
                )
            )

        return Diagnosis(
            narrative=narrative,
            ranked_actions=ranked_actions,
            test_proposals=test_proposals,
            watchlist=watchlist,
            signals=signals,
            llm_calls=budget["calls"],
            warnings=warnings,
        )


def get_diagnose_agent() -> DiagnoseAgent:
    """Module-level accessor for the shared DiagnoseAgent singleton."""
    return DiagnoseAgent.get_instance()
