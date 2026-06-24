# Tests — conventions

Keep this suite **fast, deterministic, and findable**. The rules below are what
stop it rotting back into per-bug, copy-pasted files.

## 1. File tests by CODE UNIT, not by fix-campaign

Mirror `app/`. The test for `app/agents/adzump/tools/campaign_data.py` lives at
`tests/agents/adzump/tools/test_campaign_data.py` — one file per unit, found by
path.

- ✅ `test_campaign_data.py`, `test_answer_parse.py`, `test_agent_loop.py`
- ❌ `test_v3_fixes.py`, `test_v6_fixes.py`, `test_f27_*.py` (a bug number is not a code unit)

A regression for bug F27 goes in the test file for **the unit it touches**, as a
new test method, with the tag in a comment — not a new file:

```python
def test_launch_summary_does_not_pick_confirm_location(self):
    # regression: F27 (launch-step chip mis-fire)
    ...
```

The fix *story* lives in the tracker (`MOD_AI/plans/asset-upload-qa-findings.md`),
**not** restated in a multi-paragraph test docstring. One line of intent in the
test; link the tracker if more is needed.

## 2. Name tests by BEHAVIOR

A name should mean something to someone with zero memory of the fix.

- ✅ `DurationCorrectionOverwriteTests`, `test_cue_word_correction_is_traceable`
- ❌ `F9DedupTests`, `NoProgressFloorTests`, `test_l5_invariant`

## 3. Use the shared fixtures — don't re-roll scaffolding

`tests/agents/adzump/_fixtures.py` is the one place for sessions, the
`set_campaign_spec` context pair, `CampaignContext`, `RE`/`SAAS`, and `FakeStream`.

```python
from tests.agents.adzump._fixtures import make_session, spec_context, make_cctx, RE, SAAS

ctx, sc = spec_context({"duration": "30 days"}, last_user="no wait, make it 60")
r = asyncio.run(_set_campaign_spec({"duration": "60 days"}, ctx))
self.assertEqual(sc["campaign_spec"]["duration"], "60 days")
```

Do **not** hand-roll `types.SimpleNamespace()` sessions or redeclare `RE`/`SAAS`
in a test file. If you need a shape the fixtures don't cover, add it to
`_fixtures.py` so the next person reuses it.

## 4. Test below the model — deterministic only

These are unit/regression tests: **no live LLM, no network, no real services, no
sleeps, no wall-clock.** Split at the model call — test the pure helpers and the
code paths around it with hand-authored, checked-in fixtures.

Model-judged behavior (does the agent produce *good* copy / decisions) is an
**eval**, not a unit test → `evals/` (model-in-the-loop, run on demand, **not** a
merge gate). Don't jam quality judgments into `tests/`; don't assert on prompt
strings.

## 5. Test the contract, not the internals

Prefer the smallest real seam over poking privates. Avoid:

- `Agent._method(None, ...)` — the `self=None` hack to call an instance method
  statically. If a helper is genuinely pure, make it a module function.
- Asserting on exact log strings or deep private dict keys that change on benign
  refactors.

## Running

`unittest` only (pytest is **not** installed — do not add it):

```
cd nocode-ai && ./venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

Or a focused set (see CLAUDE.md for the AdPilot loop):

```
./venv/bin/python -m unittest tests.agents.adzump.tools.test_campaign_data -v
```

## Layout (target)

```
tests/
  core/                         # generic BaseAgent runtime
    test_agent_loop.py          # run-loop control flow
    test_loop_guards.py         # breaker + tool-syntax scrub (was test_stuck_loop_breaker / test_tool_syntax_scrub)
    test_builtin_tools.py
  agents/
    adzump/
      _fixtures.py              # shared scaffolding (NOT a test)
      tools/
        test_campaign_data.py   # _field_traceable, _set_campaign_spec, clear_competitor_decline …
        test_answer_parse.py    # parse_typed_answer, field_candidates
        test_suggestions.py     # present_options, _advance_chip, get_pending_suggestions
        test_competitor.py
        test_business_storage.py
      test_agent.py             # AdzumpAgent: _capture_tagged_answer, _record_prose_decline, _next_action
      agents/{vision,product}/  # sub-agents
    appbuilder/                 # currently MISSING — add coverage
evals/                          # model-judged behavior (not a merge gate)
```

Migration to this layout is **lazy and behavior-identical**: when you next touch a
unit, move its scattered fix-campaign tests into the per-unit file. No logic
changes, just relocation + dedup via `_fixtures.py`.
