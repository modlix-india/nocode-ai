#!/usr/bin/env python3
"""Probe whether a provider will emit MORE THAN ONE tool_call in one message.

Why this exists: a bench run of 13 conversations produced 147 tool calls in 175
LLM turns — every single batch exactly one call wide. That is either the model
declining to batch or the provider not supporting it, and the two have entirely
different fixes (prompt wording vs. a request parameter or a provider swap).
Guessing costs a wasted prompt-engineering cycle, so ask the API.

The probe hands the model two obviously independent tools and an instruction
that needs both, then reports how many tool_calls came back in the first
assistant message. It also retries with `parallel_tool_calls=True` explicitly,
since OpenAI-compatible endpoints disagree on the default.

Usage:
    ./venv/bin/python scripts/probe_parallel_tool_calls.py            # all configured
    ./venv/bin/python scripts/probe_parallel_tool_calls.py --providers deepseek,openai
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env", override=False)
except ImportError:
    pass

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather for one city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_population",
            "description": "Current population of one city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
]

_PROMPT = (
    "Get the weather in Paris AND the population of Paris. "
    "These are independent lookups: issue both tool calls now, in this one "
    "message, rather than waiting for the first result."
)

# (settings key for api_key, settings key for base_url, settings key for model)
_PROVIDERS = {
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL_BALANCED"),
    "minimax": ("MINIMAX_API_KEY", "MINIMAX_BASE_URL", "MINIMAX_MODEL_BALANCED"),
    "openai": ("OPENAI_API_KEY", None, "OPENAI_MODEL_BALANCED"),
}


def _probe(name: str, explicit_flag: bool) -> str:
    from openai import OpenAI
    from app.config import settings

    key_attr, base_attr, model_attr = _PROVIDERS[name]
    api_key = getattr(settings, key_attr, None)
    if not api_key:
        return f"skipped (no ${key_attr})"
    kwargs = {"api_key": api_key}
    if base_attr:
        base = getattr(settings, base_attr, None)
        if base:
            kwargs["base_url"] = base
    client = OpenAI(**kwargs)
    model = getattr(settings, model_attr, None) or "gpt-4o-mini"

    req = {
        "model": model,
        "messages": [{"role": "user", "content": _PROMPT}],
        "tools": _TOOLS,
    }
    if explicit_flag:
        req["parallel_tool_calls"] = True
    try:
        resp = client.chat.completions.create(**req)
    except Exception as e:  # noqa: BLE001
        return f"ERROR {type(e).__name__}: {str(e)[:160]}"
    calls = resp.choices[0].message.tool_calls or []
    names = ", ".join(c.function.name for c in calls)
    return f"{len(calls)} tool_call(s) [{names}]  model={model}"


def _probe_real_payload(name: str) -> str:
    """Same question, asked behind the AppBuilder's actual tools[] payload.

    Two toy tools is not the situation the agent is in. This sends the real
    advertised surface (171 tools, the hot ones carrying full schemas) plus the
    real system prompt, so a "yes" on the toy probe and a "no" here localises
    the suppression to payload scale rather than the provider's capability.
    """
    from openai import OpenAI
    from app.config import settings
    from app.agents.appbuilder.agent import AppBuilderAgent
    from app.agents.appbuilder.context import build_appbuilder_context
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    key_attr, base_attr, model_attr = _PROVIDERS[name]
    api_key = getattr(settings, key_attr, None)
    if not api_key:
        return f"skipped (no ${key_attr})"
    kwargs = {"api_key": api_key}
    if base_attr and getattr(settings, base_attr, None):
        kwargs["base_url"] = getattr(settings, base_attr)
    client = OpenAI(**kwargs)
    model = getattr(settings, model_attr, None) or "gpt-4o-mini"

    ctx = build_appbuilder_context()
    agent = AppBuilderAgent(context_builder=ctx, tools=ALL_TOOLS, provider=name)
    advertised = [
        t for t in agent._anthropic_tools
        if t.get("name") not in agent._deferred_tool_names and not t.get("__builtin__")
    ]
    oai_tools = [
        {"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
        }}
        for t in advertised
    ]
    ask = (
        "In app `testapp`: list the themes, list the pages, and get the app record. "
        "These three reads are independent of each other. Issue all three tool "
        "calls now in this one message."
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": ask}],
            tools=oai_tools,
        )
    except Exception as e:  # noqa: BLE001
        return f"ERROR {type(e).__name__}: {str(e)[:160]}"
    calls = resp.choices[0].message.tool_calls or []
    names = ", ".join(c.function.name for c in calls)
    return f"{len(calls)} tool_call(s) [{names}]  ({len(oai_tools)} tools advertised)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--providers", default=",".join(_PROVIDERS),
                    help="Comma-separated providers to probe")
    ap.add_argument("--real-payload", action="store_true",
                    help="Also probe behind the AppBuilder's real tools[] payload")
    args = ap.parse_args()

    for name in [p.strip() for p in args.providers.split(",") if p.strip()]:
        if name not in _PROVIDERS:
            print(f"{name:10s} unknown provider")
            continue
        print(f"{name:10s} 2 toy tools    : {_probe(name, False)}")
        print(f"{name:10s} + explicit flag: {_probe(name, True)}")
        if args.real_payload:
            print(f"{name:10s} real payload   : {_probe_real_payload(name)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
