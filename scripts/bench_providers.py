#!/usr/bin/env python3
"""Provider benchmark — Gemini Flash vs Claude Haiku vs GPT-4o-mini.

Runs the curated bench corpus through each candidate provider and records:
  - Turns to convergence
  - Tool-call accuracy (% succeeded without retry, % schema fetches OK)
  - Kirun DSL compile pass-rate (when the agent authored a function)
  - Per-app KB write success (propose-then-commit completes cleanly)
  - Wall-clock per conversation
  - $ per converged conversation (using each provider's published rates)

Output: scripts/bench_results/<timestamp>/{summary.md, raw.csv}.

Two modes:

  --mode live      Real LLM provider + real Modlix gateway. Default. Requires:
                     - Provider API keys (ANTHROPIC_API_KEY / OPENAI_API_KEY /
                       GOOGLE_API_KEY / DEEPSEEK_API_KEY) as env vars.
                     - --token / --token-file / MODLIX_TOKEN env: a valid
                       caller JWT against the target gateway.
                     - --gateway-url overriding settings.GATEWAY_URL if needed.
                     - A SANDBOX (CLIENT_CODE, APP_CODE) for side-effect tools.
                       Bench WILL create pages, write KB rows, etc. Never run
                       against prod.

  --mode dry-run   Real LLM provider + MockSaasClient (no gateway). Useful when
                   you want the LLM's tool-call PATTERN bench without touching
                   a real backend. Every tool returns success synthetically;
                   convergence still measures whether the LLM called the right
                   tools, but tool-call accuracy is vacuously 100%.

Bench corpus is at scripts/bench_corpus.yaml — see the file for the format.

Run:
    ./venv/bin/python scripts/bench_providers.py \\
        --providers gemini,anthropic,openai \\
        --mode dry-run

    ./venv/bin/python scripts/bench_providers.py \\
        --providers anthropic \\
        --mode live \\
        --token-file ~/.cfa-tokens/sandbox.jwt \\
        --gateway-url https://sandbox-gw.modlix.com \\
        --app-code testapp --client-code SANDBOX
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Provider keys live in .env (app/config.py reads it via pydantic's env_file).
# The key precheck below reads os.environ, so without this the bench refuses to
# run on a machine where the app itself starts fine. Existing env vars win, so
# an explicit `export DEEPSEEK_API_KEY=...` still overrides the file.
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env", override=False)
except ImportError:  # python-dotenv is in requirements; degrade instead of dying
    pass


@dataclass
class Conversation:
    """One bench conversation. Comes from bench_corpus.yaml."""
    name: str
    description: str
    messages: list[str]  # User turns, fed one at a time
    must_call_tools: list[str] = field(default_factory=list)
    # Each inner list is an equivalence group — the run satisfies the group
    # if AT LEAST ONE tool in the group was called. Use this when multiple
    # tools achieve the same outcome (e.g. `compile_kirun_text` vs
    # `save_function_from_text` for "author a Kirun function") so the oracle
    # doesn't penalize a valid alternative path. Goes alongside
    # `must_call_tools` — both must be satisfied if both are set.
    must_call_any_of_groups: list[list[str]] = field(default_factory=list)
    must_succeed_on_kirun: bool = False
    must_succeed_on_kb_write: bool = False
    # Optional setup actions run BEFORE the user-message loop. Each entry is
    # `{tool: <tool_name>, params: {...}}`. Used to reset stateful artifacts
    # (delete a page, reset a function, clear KB rows) so the bench is
    # re-runnable. Failures here are NON-FATAL: they're logged and the
    # conversation proceeds. The conv itself isn't measured on setup
    # behaviour — these calls don't show up in tool_calls.
    setup_actions: list[dict] = field(default_factory=list)


@dataclass
class BenchMetrics:
    """Per-(provider, conversation) result row."""
    provider: str
    conversation: str
    turns: int = 0              # LLM round trips (assistant messages) — the real cost driver
    user_messages: int = 0      # conversation length: how many user turns were fed
    max_tools_per_turn: int = 0  # largest parallel tool_use batch the model emitted
    single_tool_turns: int = 0   # tool-using turns that carried exactly ONE call
    tool_calls_total: int = 0
    tool_calls_succeeded: int = 0
    schema_fetches: int = 0
    schema_fetches_succeeded: int = 0
    kirun_compiles_total: int = 0
    kirun_compiles_succeeded: int = 0
    kb_writes_total: int = 0
    kb_writes_succeeded: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    wall_seconds: float = 0.0
    converged: bool = False
    failure_reason: Optional[str] = None

    def estimate_cost_usd(self, rates: dict[str, dict[str, float]]) -> float:
        r = rates.get(self.provider) or {}
        return (
            (self.input_tokens / 1_000_000) * r.get("input_per_million", 0.0)
            + (self.output_tokens / 1_000_000) * r.get("output_per_million", 0.0)
        )


# Published rates (USD per 1M tokens) at time of writing — adjust before each
# bench run. Better: pull from an env file or the provider's API.
_DEFAULT_RATES: dict[str, dict[str, float]] = {
    "gemini":     {"input_per_million": 0.075, "output_per_million": 0.30},
    "anthropic":  {"input_per_million": 1.00,  "output_per_million": 5.00},
    "openai":     {"input_per_million": 0.15,  "output_per_million": 0.60},  # gpt-4o-mini
    # DeepSeek V4 estimates (verified via dashboard 2026-06-10: a 14.3M-input
    # run cost $0.60 ≈ $0.042/M, matching the v4-flash quote). v4-pro rough
    # estimate at ~5× flash, typical Pro-tier ratio. Replace with exact rates
    # when published.
    "deepseek":   {"input_per_million": 0.40,  "output_per_million": 1.60},  # v4-pro (CFA default)
}


# Required env var per provider. Pre-flight checked before any LLM call so the
# operator gets one clear error message instead of N x 401 cascades.
_PROVIDER_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "gemini":    "GOOGLE_API_KEY",
    "deepseek":  "DEEPSEEK_API_KEY",
}


# After this many consecutive failures on the same exception class for a
# single provider, abort the rest of that provider's conversations. Catches
# auth-key and gateway-down cascades early without burning the full corpus.
_CONSECUTIVE_FAILURE_LIMIT = 2


def _check_provider_keys(providers: list[str]) -> list[tuple[str, str]]:
    """Return [(provider, missing_env_var), ...] for providers missing their key.

    Empty list means everything is set. Unknown providers (not in
    _PROVIDER_KEY_ENV) are skipped — the LLM provider factory will surface
    its own error for those.
    """
    missing: list[tuple[str, str]] = []
    for p in providers:
        env_var = _PROVIDER_KEY_ENV.get(p)
        if env_var and not os.environ.get(env_var):
            missing.append((p, env_var))
    return missing


# Tool-name sets used by the convergence oracle to classify success.
# Any tool that semantically does "Kirun compile + save" → contributes to
# kirun_compiles_total/succeeded.
_KIRUN_TOOLS: frozenset[str] = frozenset({
    "compile_kirun_text",
    "save_function_from_text",
    "save_server_function_from_text",
    "save_page_event_function_from_text",
})

# KB tool that closes the propose-then-commit cycle. Only commit counts as a
# completed write; a lone propose doesn't.
_KB_WRITE_TOOLS: frozenset[str] = frozenset({"commit_kb_update"})

# Meta-tools — schema-fetch dance.
_SCHEMA_FETCH_TOOLS: frozenset[str] = frozenset({"get_tool_schema"})


def _load_corpus(path: Path) -> list[Conversation]:
    """Load conversations from YAML or JSON. Returns empty list if missing."""
    if not path.exists():
        log.warning("Corpus file %s missing — bench will run with zero conversations.", path)
        return []
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-not-found]
        raw = yaml.safe_load(text)
    except ImportError:
        raw = json.loads(text)
    convs: list[Conversation] = []
    for entry in (raw or {}).get("conversations", []) or []:
        convs.append(Conversation(
            name=entry["name"],
            description=entry.get("description", ""),
            messages=list(entry.get("messages") or []),
            must_call_tools=list(entry.get("must_call_tools") or []),
            must_call_any_of_groups=[
                list(group) for group in (entry.get("must_call_any_of_groups") or [])
                if group
            ],
            must_succeed_on_kirun=bool(entry.get("must_succeed_on_kirun")),
            must_succeed_on_kb_write=bool(entry.get("must_succeed_on_kb_write")),
            setup_actions=[
                dict(action) for action in (entry.get("setup_actions") or [])
                if isinstance(action, dict) and action.get("tool")
            ],
        ))
    return convs


def _mark_setup_tools_fetched(actions: list[dict], context: dict) -> None:
    """Pre-mark every setup action's tool as schema-fetched so the deferred
    gate doesn't fire on internal calls."""
    fetched = context.get("fetched_schemas")
    for action in actions:
        name = action.get("tool") or ""
        if isinstance(fetched, list) and name not in fetched:
            fetched.append(name)
        elif isinstance(fetched, set):
            fetched.add(name)


async def _dispatch_setup_action(action: dict, by_name: dict, context: dict) -> None:
    """Run one setup action. All failure modes are non-fatal + logged."""
    name = action.get("tool") or ""
    tool = by_name.get(name)
    if not tool or not tool.execute:
        log.warning("  setup: tool %r not found — skipping", name)
        return
    params = action.get("params") or {}
    try:
        result = await tool.execute(params, context)
    except Exception as e:  # noqa: BLE001
        log.info("  setup: %s raised %s (non-fatal): %s",
                 name, type(e).__name__, e)
        return
    if getattr(result, "success", False):
        log.info("  setup: %s OK — %s", name, (getattr(result, "summary", "") or "")[:80])
    else:
        log.info("  setup: %s soft-fail (non-fatal): %s",
                 name, (getattr(result, "error", "") or "")[:80])


async def _run_setup_actions(
    actions: list[dict], agent, session,
) -> None:
    """Run pre-conversation setup actions to reset stateful artifacts.

    Each action is `{tool: <name>, params: {...}}`. Failures are logged but
    non-fatal — a "delete this page if it exists" call returning 404 is fine.
    Actions dispatch through the same tool framework the agent uses, with a
    bypass on the deferred-schema gate (no need to round-trip a synthetic
    schema response for an internal call).
    """
    if not actions:
        return
    # BaseAgent stores tools as a dict {name: ToolDefinition}; iterate values
    # rather than keys (which are strings).
    by_name = dict(agent.tools) if isinstance(agent.tools, dict) else {t.name: t for t in agent.tools}
    context = agent.build_tool_context(session)
    _mark_setup_tools_fetched(actions, context)
    for action in actions:
        await _dispatch_setup_action(action, by_name, context)


# ─── Observer: captures tool events + auto-approves confirmations ────────────


def _make_observer():
    """Return a BenchObserver subclass of AgentEventStream.

    Subclassed here (not module-scope) so importing this script doesn't
    require app.core.streaming to load eagerly — relevant for `--help` and
    unit tests that don't drive a full agent run.
    """
    from app.core.streaming import AgentEventStream  # local import

    class BenchObserver(AgentEventStream):
        """Captures tool calls + agent-loop errors. Auto-approves confirmations."""

        def __init__(self) -> None:
            super().__init__()
            self.tool_calls: list[tuple[str, bool]] = []  # (name, success)
            self.errors: list[str] = []
            self.cancelled = False

        async def emit_tool_result(self, tool_name, success, summary, tool_use_id=""):
            await super().emit_tool_result(tool_name, success, summary, tool_use_id)
            self.tool_calls.append((tool_name, bool(success)))

        async def emit_error(self, message):
            # The agent loop catches top-level exceptions and emits them as
            # error events rather than re-raising. Capture them here so the
            # bench can surface the REAL upstream failure (e.g. provider 401)
            # instead of the downstream "missing required tools" oracle
            # cascade that would otherwise dominate the failure_reason.
            await super().emit_error(message)
            self.errors.append(message)

        async def request_confirmation(
            self, confirmation_id, message, tool_name, display_name,
            details=None, options=None, timeout=120.0, session_id="",
        ):
            # Auto-approve every confirmation the bench encounters.
            # The legacy CRUD CONFIRMATION_TOOLS path routes through here;
            # KB propose-then-commit uses its own pending_uuid + a separate
            # user-message turn (handled at the corpus level).
            return {"approved": True, "selected": "approve"}

        async def emit_done(self, session_id="", usage=None):
            # Drain the done event but don't block on a non-existent SSE
            # consumer. The bench doesn't render events to a stream — it
            # just observes them via the captured lists.
            await super().emit_done(session_id, usage)

    return BenchObserver


# ─── Turn accounting ────────────────────────────────────────────────────────


def _assistant_turns(messages: list) -> int:
    """LLM round trips in a conversation history.

    One assistant message per LLM response, so this is the count of times the
    model was actually called — the number that multiplies by the per-turn
    prefix and that the turn limit is spent on.
    """
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant")


def _turn_batch_sizes(messages: list) -> list[int]:
    """Tool-use blocks per assistant turn, for turns that used tools.

    The length of this list is the number of tool-using turns; each value is how
    many calls the model packed into that one message. All ones means the model
    is not batching at all, and every independent call is costing a full round
    trip through the whole prefix.
    """
    sizes: list[int] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        n = sum(
            1 for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"
        )
        if n:
            sizes.append(n)
    return sizes


# ─── Convergence oracle ─────────────────────────────────────────────────────


def _check_any_of_groups(
    groups: list[list[str]], called_names: set[str],
) -> Optional[str]:
    """Each equivalence group must have ≥1 of its tools in called_names.

    Returns a failure reason naming every unsatisfied group so the operator
    can tell at a glance which capability the agent skipped. None when all
    groups are satisfied (or there are no groups).
    """
    unsatisfied = [g for g in groups if not (set(g) & called_names)]
    if not unsatisfied:
        return None
    return f"none-of-group called: {unsatisfied}"


def _convergence(conv: Conversation, metrics: BenchMetrics, tool_calls: list[tuple[str, bool]],
                 ) -> tuple[bool, Optional[str]]:
    """Decide whether a run satisfies the conversation's contract.

    Returns (converged, failure_reason_or_None). All metric fields below are
    derived from the same tool_calls list, so the metrics + the oracle stay
    consistent.
    """
    called_names = {name for name, _ok in tool_calls}
    missing = [t for t in conv.must_call_tools if t not in called_names]
    if missing:
        return False, f"missing required tools: {missing}"

    group_failure = _check_any_of_groups(conv.must_call_any_of_groups, called_names)
    if group_failure:
        return False, group_failure

    if conv.must_succeed_on_kirun:
        succeeded = any(
            ok for name, ok in tool_calls if name in _KIRUN_TOOLS
        )
        if not succeeded:
            return False, "must_succeed_on_kirun=true but no successful Kirun compile/save"

    if conv.must_succeed_on_kb_write:
        succeeded = any(
            ok for name, ok in tool_calls if name in _KB_WRITE_TOOLS
        )
        if not succeeded:
            return False, "must_succeed_on_kb_write=true but no successful KB commit"

    return True, None


def _classify_calls(tool_calls: list[tuple[str, bool]]) -> dict[str, int]:
    """Bucket the captured tool calls into the metric counters."""
    out = {
        "tool_calls_total": len(tool_calls),
        "tool_calls_succeeded": sum(1 for _n, ok in tool_calls if ok),
        "schema_fetches": 0,
        "schema_fetches_succeeded": 0,
        "kirun_compiles_total": 0,
        "kirun_compiles_succeeded": 0,
        "kb_writes_total": 0,
        "kb_writes_succeeded": 0,
    }
    for name, ok in tool_calls:
        if name in _SCHEMA_FETCH_TOOLS:
            out["schema_fetches"] += 1
            if ok:
                out["schema_fetches_succeeded"] += 1
        if name in _KIRUN_TOOLS:
            out["kirun_compiles_total"] += 1
            if ok:
                out["kirun_compiles_succeeded"] += 1
        if name in _KB_WRITE_TOOLS:
            out["kb_writes_total"] += 1
            if ok:
                out["kb_writes_succeeded"] += 1
    return out


# ─── Mock saas-client for dry-run mode ───────────────────────────────────────


def _install_mock_saas_client():
    """Monkey-patch _shared.get_saas_client() to return a MockSaasClient.

    Lifted from tests/conftest.py shape — every modlix tool that calls
    get_saas_client picks up the mock. The mock returns
    {success: True, data: {content: [], totalElements: 0}} for everything,
    which is enough to keep the agent loop progressing through tool calls
    without a real gateway.
    """
    from app.core.tools.base import ToolResult

    class MockSaasClient:
        def __init__(self):
            self.calls = []

        async def _record(self, method, path, headers=None, params=None, json=None):
            self.calls.append((method, path))
            return ToolResult(
                success=True,
                data={"content": [], "totalElements": 0, "id": "mock-id"},
                summary=f"(mock) {method} {path}",
            )

        async def get(self, path, headers=None, params=None):
            return await self._record("GET", path, headers, params, None)

        async def post(self, path, headers=None, json=None, params=None):
            return await self._record("POST", path, headers, params, json)

        async def put(self, path, headers=None, json=None, params=None):
            return await self._record("PUT", path, headers, params, json)

        async def patch(self, path, headers=None, json=None, params=None):
            return await self._record("PATCH", path, headers, params, json)

        async def delete(self, path, headers=None, params=None):
            return await self._record("DELETE", path, headers, params, None)

        async def close(self):
            return None

    mock = MockSaasClient()
    import app.agents.appbuilder.tools._shared as shared
    shared._client = mock  # type: ignore[attr-defined]

    def _resolver():
        return mock
    shared.get_saas_client = _resolver  # type: ignore[assignment]
    return mock


# ─── Live-mode credentials resolution ────────────────────────────────────────


def _resolve_jwt(args) -> Optional[str]:
    """Resolve the caller JWT for live mode. Order: --token-file → --token → env."""
    if getattr(args, "token_file", None):
        path = Path(args.token_file).expanduser()
        if not path.exists():
            log.error("Token file %s does not exist", path)
            return None
        return path.read_text(encoding="utf-8").strip()
    if getattr(args, "token", None):
        log.warning("--token passed on the CLI; prefer --token-file to keep secrets out of shell history.")
        return args.token
    return os.environ.get("MODLIX_TOKEN") or None


# ─── The runner ─────────────────────────────────────────────────────────────


def _hydrate_session_auth(session, args, auth_context_cls) -> Optional[str]:
    """Attach an AuthContext + session-context fields. Returns an error string
    when live-mode is missing a JWT, else None."""
    if args.mode == "live":
        token = _resolve_jwt(args)
        if not token:
            return (
                "live mode requires a JWT. Pass --token-file or --token, or set "
                "MODLIX_TOKEN in the environment."
            )
        session.auth = auth_context_cls(
            token=token, client_code=args.client_code, client_id=0,
            user_id=0, app_code=args.app_code, access_app_code="appbuilder",
            forwarded_host=args.gateway_host or "localhost", forwarded_port="80",
        )
        session.context["app_code"] = args.app_code
        session.context["client_code"] = args.client_code
    else:
        # dry-run: synthetic auth so the agent loop has something to read.
        session.auth = auth_context_cls(
            token="bench-dry-run-stub-token",  # NOSONAR — synthetic stub, not a real secret
            client_code=args.client_code or "SANDBOX",
            client_id=0, user_id=0, app_code=args.app_code or "testapp",
            access_app_code="appbuilder", forwarded_host="dry-run.local",
            forwarded_port="80",
        )
        session.context["app_code"] = session.auth.app_code
        session.context["client_code"] = session.auth.client_code
    session.context["headers"] = session.auth.to_headers()
    return None


async def _run_one(
    provider_name: str,
    conv: Conversation,
    args,
) -> BenchMetrics:
    """Drive one conversation through one provider and capture metrics."""
    metrics = BenchMetrics(provider=provider_name, conversation=conv.name)

    # Lazy imports — keep --help cheap.
    from app.agents.appbuilder.context import build_appbuilder_context
    from app.agents.appbuilder.agent import AppBuilderAgent
    from app.agents.appbuilder.tools.registry import ALL_TOOLS
    from app.core.session import BaseSession, AuthContext

    if args.mode == "dry-run":
        _install_mock_saas_client()

    # Initialize MySQL pool if configured — KB tools (propose_kb_update,
    # commit_kb_update, kb_app_get, etc.) need the pool. The FastAPI app
    # normally calls init_db_pool() in its startup hook, but the bench
    # bypasses FastAPI; without this call, KB conversations fail with
    # "Database pool not initialized". Safe to call multiple times (idempotent).
    try:
        from app.db.connection import init_db_pool
        await init_db_pool()
    except Exception as e:  # noqa: BLE001
        log.debug("DB pool init skipped (%s: %s)", type(e).__name__, e)

    # Build agent + session
    ctx = build_appbuilder_context()
    await ctx.load()
    agent = AppBuilderAgent(
        context_builder=ctx,
        tools=ALL_TOOLS,
        catalog=None,
        api_catalog=None,
        provider=provider_name,
    )

    session = BaseSession(agent_name=agent.name)
    session.session_id = f"bench-{provider_name}-{conv.name}-{int(time.time())}"

    auth_err = _hydrate_session_auth(session, args, AuthContext)
    if auth_err:
        metrics.failure_reason = auth_err
        return metrics

    observer_cls = _make_observer()
    observer = observer_cls()

    # Reset stateful artifacts before the conv runs (e.g. delete a page we
    # plan to create, decompile-then-replace a function we plan to add steps
    # to). Setup actions don't count toward tool_calls so the oracle only
    # measures the agent's user-facing behaviour.
    if args.mode == "live":
        await _run_setup_actions(conv.setup_actions, agent, session)

    # Drive each user message through the agent
    for i, msg in enumerate(conv.messages):
        log.info("  turn %d/%d: %s", i + 1, len(conv.messages), msg[:80])
        try:
            await agent.run(user_message=msg, session=session, event_stream=observer)
            metrics.user_messages += 1
        except Exception as e:  # noqa: BLE001
            metrics.failure_reason = f"turn {i + 1} raised {type(e).__name__}: {e}"
            break

    # Real turn accounting. `metrics.turns` used to be incremented once per USER
    # message, which reported 61 turns for a run that made 175 LLM round trips and
    # hid the fact that every single batch was one call wide. The agent appends
    # exactly one assistant message per LLM response, and the tool_use blocks in
    # it ARE the parallel batch, so both numbers come straight from the history
    # with no agent-side instrumentation.
    _batches = _turn_batch_sizes(session.get_messages())
    metrics.turns = _assistant_turns(session.get_messages())
    metrics.max_tools_per_turn = max(_batches) if _batches else 0
    metrics.single_tool_turns = sum(1 for b in _batches if b == 1)

    # Capture token usage
    usage = session.total_usage or {}
    metrics.input_tokens = int(usage.get("input_tokens", 0))
    metrics.output_tokens = int(usage.get("output_tokens", 0))

    # Classify tool calls + apply convergence oracle
    classified = _classify_calls(observer.tool_calls)
    for k, v in classified.items():
        setattr(metrics, k, v)

    converged, oracle_reason = _convergence(conv, metrics, observer.tool_calls)
    metrics.converged = converged
    if not converged:
        metrics.failure_reason = _resolve_failure_reason(
            metrics.failure_reason, observer.errors, oracle_reason,
        )

    return metrics


# Oracle verdicts — the bench's own judgement that a run did not do the work.
# These are measurements, never cascades, so they must not trip the breaker.
# Matched as prefixes of the classified head so a reworded verdict keeps working.
_ORACLE_VERDICT_PREFIXES: tuple = (
    "missing required tools",
    "none-of-group called",
    "must_succeed_on_kirun",
    "must_succeed_on_kb_write",
)


def _failure_class(reason: Optional[str]) -> Optional[str]:
    """Extract the leading exception class (or marker) from a failure_reason.

    Examples:
      'Agent error: AuthenticationError: 401 ...'  → 'AuthenticationError'
      'AuthenticationError: 401 invalid x-api-key' → 'AuthenticationError'
      'ConnectError: gateway down'                  → 'ConnectError'
      'missing required tools: [...]'              → 'missing required tools'
      None                                          → None

    The circuit breaker treats two consecutive runs with the same class as
    a cascade — likely a single upstream cause hitting every conversation
    (auth wall, gateway down, quota exceeded). Aborting the rest of the
    provider's runs saves time + noise.

    An ORACLE verdict is not a cascade and returns None. "missing required
    tools" means the agent ran and did not do the work, which is a result, not
    an infrastructure fault — and it is exactly the result a bench exists to
    record. Counting it tripped the breaker after two ordinary non-convergences
    and skipped the last four conversations (shopkeep + the three clone runs) on
    every run ever recorded, which are the heaviest in the corpus and the
    closest in shape to the one-shot app build the whole exercise is about.
    """
    if not reason:
        return None
    # Strip the "Agent error: " prefix the agent loop prepends on top-level errors.
    body = reason.removeprefix("Agent error: ")
    head = body.split(":", 1)[0].strip()
    if head.startswith(_ORACLE_VERDICT_PREFIXES):
        return None
    return head or None


async def _execute_one(provider_name: str, conv: Conversation, args) -> BenchMetrics:
    """Run one conversation, catching uncaught exceptions into the metrics row."""
    t0 = time.monotonic()
    try:
        m = await _run_one(provider_name, conv, args)
    except Exception as e:  # noqa: BLE001
        m = BenchMetrics(provider=provider_name, conversation=conv.name)
        m.failure_reason = f"{type(e).__name__}: {e}"
    m.wall_seconds = time.monotonic() - t0
    return m


def _update_circuit_breaker(
    m: BenchMetrics, consecutive: int, cascade_class: Optional[str],
) -> tuple[int, Optional[str], bool]:
    """Return updated (consecutive, cascade_class, should_abort) after one run."""
    if m.converged:
        return 0, None, False
    cls = _failure_class(m.failure_reason)
    if cls and cls == cascade_class:
        consecutive += 1
    else:
        consecutive = 1
        cascade_class = cls
    should_abort = bool(cls) and consecutive >= _CONSECUTIVE_FAILURE_LIMIT
    return consecutive, cascade_class, should_abort


async def _run_provider(
    provider_name: str, corpus: list[Conversation], args,
) -> list[BenchMetrics]:
    """Run a whole provider's pass with a same-class-failure circuit breaker.

    If `_CONSECUTIVE_FAILURE_LIMIT` consecutive conversations fail with the
    same exception class, abort the rest of the provider's conversations
    and synthesize "skipped" rows for them. Catches auth + gateway cascades
    early without burning the full corpus.
    """
    rows: list[BenchMetrics] = []
    consecutive = 0
    cascade_class: Optional[str] = None
    aborted = False

    for idx, conv in enumerate(corpus):
        if aborted:
            skipped = BenchMetrics(provider=provider_name, conversation=conv.name)
            skipped.failure_reason = (
                f"skipped: previous {_CONSECUTIVE_FAILURE_LIMIT} consecutive runs "
                f"failed with {cascade_class!r}"
            )
            rows.append(skipped)
            continue

        log.info("Running %s × %s ...", provider_name, conv.name)
        m = await _execute_one(provider_name, conv, args)
        rows.append(m)
        log.info(
            "  %s × %s: converged=%s, %d tool calls in %d turns "
            "(max %d/turn, %d single-call), %.1fs",
            provider_name, conv.name, m.converged, m.tool_calls_total, m.turns,
            m.max_tools_per_turn, m.single_tool_turns, m.wall_seconds,
        )

        consecutive, cascade_class, should_abort = _update_circuit_breaker(
            m, consecutive, cascade_class,
        )
        if should_abort:
            remaining = len(corpus) - idx - 1
            if remaining > 0:
                log.error(
                    "Aborting %s: %d consecutive runs failed with %r. "
                    "Skipping %d remaining conversation(s).",
                    provider_name, consecutive, cascade_class, remaining,
                )
                aborted = True

    return rows


def _resolve_failure_reason(
    existing: Optional[str], observer_errors: list[str], oracle_reason: Optional[str],
) -> Optional[str]:
    """Pick the most informative failure_reason for a non-converged run.

    Precedence: existing (e.g. from a raised exception during the turn
    loop) > observer-captured emit_error > oracle's "missing required
    tools" reason. The agent loop catches top-level exceptions and emits
    them through emit_error rather than re-raising, so observer_errors is
    where Anthropic 401s / Gemini quota errors / gateway timeouts surface
    — far more useful than the oracle's downstream "no tool was called"
    cascade.
    """
    if existing:
        return existing
    if observer_errors:
        return observer_errors[0]
    return oracle_reason


def _validate_args(args, providers: list[str]) -> Optional[str]:
    """Pre-flight checks on the CLI args. Returns an error message or None."""
    if args.mode == "live":
        if not args.app_code or not args.client_code:
            return "live mode requires --app-code and --client-code"
        if "prod" in (args.client_code or "").lower() or "prod" in (args.gateway_url or "").lower():
            return (
                "Refusing to run live mode against what looks like prod "
                f"(client_code={args.client_code!r}, gateway={args.gateway_url!r}). "
                "If this is intentional, use a sandbox; never bench against prod."
            )
    # Provider-key precheck — applies to BOTH modes (dry-run still calls the real LLM).
    missing = _check_provider_keys(providers)
    if missing:
        lines = ["The following provider API keys are not set in the environment:"]
        for p, env_var in missing:
            lines.append(f"  - {p}: needs ${env_var}")
        lines.append("")
        lines.append("Set them and re-run, or pass --providers without the unconfigured ones.")
        return "\n".join(lines)
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--providers", default="anthropic",
                       help="Comma-separated providers to bench (default: anthropic)")
    parser.add_argument("--corpus", default="scripts/bench_corpus.yaml",
                       help="Path to the conversation corpus")
    parser.add_argument("--out-dir", default="scripts/bench_results",
                       help="Where to write summary.md + raw.csv")
    parser.add_argument("--mode", choices=["live", "dry-run"], default="live",
                       help="live: real LLM + real gateway. dry-run: real LLM + MockSaasClient.")
    parser.add_argument("--token", default=None,
                       help="Caller JWT (live mode). Prefer --token-file.")
    parser.add_argument("--token-file", default=None,
                       help="File containing the caller JWT (live mode).")
    parser.add_argument("--gateway-url", default=None,
                       help="Override settings.GATEWAY_URL (live mode).")
    parser.add_argument("--gateway-host", default=None,
                       help="X-Forwarded-Host value to send (live mode).")
    parser.add_argument("--app-code", default=None,
                       help="Target app_code for tool calls.")
    parser.add_argument("--client-code", default=None,
                       help="Target client_code for tool calls.")
    parser.add_argument("--only", default=None,
                       help="Comma-separated conversation names to run (default: all).")
    args = parser.parse_args()

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]

    err = _validate_args(args, providers)
    if err:
        log.error(err)
        return 2

    if args.gateway_url:
        # Override settings.GATEWAY_URL before any tool imports the client.
        from app.config import settings  # type: ignore[attr-defined]
        settings.GATEWAY_URL = args.gateway_url

    corpus = _load_corpus(Path(args.corpus))
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        corpus = [c for c in corpus if c.name in wanted]
    if not corpus:
        print(
            f"No conversations to run (corpus={args.corpus}, --only={args.only}). "
            "Curate scripts/bench_corpus.yaml first."
        )
        return 1

    out_dir = Path(args.out_dir) / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("Bench start: mode=%s, providers=%s, corpus=%d conv(s)", args.mode, providers, len(corpus))

    all_rows: list[BenchMetrics] = []
    for provider_name in providers:
        all_rows.extend(await _run_provider(provider_name, corpus, args))

    # Write CSV
    csv_path = out_dir / "raw.csv"
    headers = [f.name for f in dataclasses.fields(BenchMetrics)]
    lines = [",".join(headers)]
    for r in all_rows:
        lines.append(",".join(_csv_cell(getattr(r, h, "")) for h in headers))
    csv_path.write_text("\n".join(lines), encoding="utf-8")

    # Write a one-page Markdown summary grouped by provider.
    summary = out_dir / "summary.md"
    summary.write_text(_render_summary(args, providers, corpus, all_rows), encoding="utf-8")

    print(f"\nWrote {csv_path}")
    print(f"Wrote {summary}")
    return 0


def _csv_cell(v: Any) -> str:
    """Escape a value for CSV (quote if contains comma/quote/newline)."""
    s = "" if v is None else str(v)
    if any(ch in s for ch in (",", '"', "\n")):
        return '"' + s.replace('"', '""') + '"'
    return s


def _render_provider_block(name: str, rows: list[BenchMetrics]) -> list[str]:
    """Build the per-provider Markdown lines. Pure function — easy to test."""
    converged = sum(1 for r in rows if r.converged)
    total_cost = sum(r.estimate_cost_usd(_DEFAULT_RATES) for r in rows)
    total_secs = sum(r.wall_seconds for r in rows)
    total_in = sum(r.input_tokens for r in rows)
    total_out = sum(r.output_tokens for r in rows)
    kirun_t = sum(r.kirun_compiles_total for r in rows)
    kirun_ok = sum(r.kirun_compiles_succeeded for r in rows)
    kb_t = sum(r.kb_writes_total for r in rows)
    kb_ok = sum(r.kb_writes_succeeded for r in rows)
    total_turns = sum(r.turns for r in rows)
    total_calls = sum(r.tool_calls_total for r in rows)
    single = sum(r.single_tool_turns for r in rows)
    widest = max((r.max_tools_per_turn for r in rows), default=0)
    # `single` vs total turns is the headline: when every tool-using turn carries
    # exactly one call, the model is not batching at all and each independent
    # call pays a full round trip through the entire prefix.
    lines = [
        f"## {name}",
        f"- converged: {converged}/{len(rows)}",
        f"- LLM turns: {total_turns} for {total_calls} tool calls "
        f"({total_calls / total_turns:.2f} calls/turn)" if total_turns else "- LLM turns: 0",
        f"- parallel batching: {single} single-call turns, widest batch {widest}",
        f"- total wall: {total_secs:.1f}s",
        f"- tokens: {total_in:,} in + {total_out:,} out",
        f"- Kirun compile pass-rate: {kirun_ok}/{kirun_t}" if kirun_t else "- Kirun: no compile attempts",
        f"- KB write pass-rate: {kb_ok}/{kb_t}" if kb_t else "- KB: no write attempts",
        f"- estimated $: ${total_cost:.4f}",
    ]
    non_conv = [r for r in rows if not r.converged]
    if non_conv:
        lines.append("- non-converged:")
        for r in non_conv:
            lines.append(f"  - {r.conversation}: {r.failure_reason or '(no reason)'}")
    lines.append("")
    return lines


def _render_summary(args, providers: list[str], corpus: list[Conversation],
                    all_rows: list[BenchMetrics]) -> str:
    """Build the full summary.md content."""
    md = ["# Provider bench results", "",
          f"Mode: {args.mode}",
          f"Corpus: {len(corpus)} conversations × {len(providers)} providers",
          ""]
    for provider_name in providers:
        rows = [r for r in all_rows if r.provider == provider_name]
        if not rows:
            continue
        md.extend(_render_provider_block(provider_name, rows))
    return "\n".join(md)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
