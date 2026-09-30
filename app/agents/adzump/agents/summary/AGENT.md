# Profile Writer (`SummaryAgent`)

> **Status: implemented; this doc written 2026-09-11** - describes the code as it is.

## Purpose

Turn one scraped page's text into the business's profile summary, streamed
LIVE into the craft panel's summary block while the Product Analyst's scrape
is still running. The user watches the profile being written instead of
waiting for the whole analysis to land.

## Architecture

```
agents/product/tools/scrape/profile.py
   asyncio.create_task(get_summary_agent().summarize(scraped_text, url,
                                                     ..., craft_id))
        │  (runs in parallel with the rest of the scrape pipeline)
        ▼
┌───────────────────────────────────────────────────────────────────┐
│  SummaryAgent (BaseAgent, tools=[], max_turns=1, gpt-4o)          │
│  _CraftBoundStream rewrites emit_text → emit_craft_text(craft_id) │
│  so every streamed token lands in the craft panel's summary block,│
│  never in chat.                                                   │
└───────────────────────────────────────────────────────────────────┘
```

### File layout

```
app/agents/adzump/agents/summary/
├── agent.py     SummaryAgent + get_summary_agent() + _CraftBoundStream
├── context.py   build_summary_context() - the summarization prompt
└── models.py    SummaryInput / SummaryOutput
```

## Provider configuration

| Constant | Value | Why |
|---|---|---|
| `SUMMARY_MODEL_OVERRIDE` | `openai:gpt-4o` | the helper LLM the direct call used; ProductAgent's Sonnet stays on orchestration |
| `SUMMARY_MAX_TOKENS` | `3000` | same ceiling as the old direct call |
| `SUMMARY_MAX_TURNS` | `1` | single shot, no tools |

Input is capped at 15k chars of scraped text (same cap as the old direct call).

## The streaming contract

- `_CraftBoundStream` is the whole point of this agent's stream wrapper:
  `emit_text(delta)` → `emit_craft_text(craft_id, delta)`. Thinking, tool
  events, done, error are dropped; `craft*`/`data`/`agent_*` pass through.
- `agent_finished` fires on BOTH success and error paths (v6, 2026-05-27, S1) -
  without it the Profile Writer span sat "running" 336s+ after the stream
  finished.
- On success `agent_finished.summary` is `""` **by contract**: the full text is
  already visible in the craft panel, and duplicating a 120-char preview into
  the agent row's right-meta overflowed it while adding zero information. The
  right-meta slot is only for span outcomes the user can't see elsewhere (the
  error path keeps `"ExceptionName: ..."`). The contract lives in
  `_emit_finished`'s docstring (Lance, 2026-05-27) - VisionAnalyst's
  `"logos=N creatives=N"` is the GOOD example.

## Callers

Exactly one: `agents/product/tools/scrape/profile.py::_generate_business_profile`,
usually as a parallel task started while the scrape continues (fallback: inline
on the post-scroll page when the early HTML was too thin). The returned
`SummaryOutput.text` becomes the stored profile summary. `summarize()` re-raises
on failure; the caller catches, logs `business_profile_failed`, and returns an
empty profile (the panel layout stays, no text streams in).

## Testing

No dedicated unit-test file today - the agent is a thin single-shot wrapper
and its seams (craft-bound streaming, the empty-summary contract) are exercised
live through the scrape flow. If it grows logic (e.g. structured profile
fields), give it `tests/agents/adzump/agents/summary/`.

## Design decisions

- **An agent, not a bare `openai.chat.completions.create`** - Adzump's
  convention: every LLM work unit is a BaseAgent subclass (token tracking,
  audit sub-session, observability card). The ~20ms of loop ceremony is
  noise against a ~3s response.
- **Craft-bound text routing over post-hoc rendering** - the summary is the
  first thing the user can read during a long analysis; buffering it to the
  end was the "silent spinner" experience this design kills.
