# Bench day, 2026-09-02 — where things stand

Branch `feature/slm-compiler`. **Nothing is committed.** 1150 tests pass.

## What was found

1. **Stream assembly dropped every parallel tool call but one.** `app/core/agent.py`
   held a single `current_tool` slot. The OpenAI-compatible path (DeepSeek,
   MiniMax) emits every `tool_use_start` first, then arguments and ends keyed by
   id — so a 3-call batch kept ONE call and gave it ANOTHER call's arguments,
   silently. Anthropic was immune (it adopts final blocks). Fixed as
   `_ToolBlockAssembler`, 10 tests in
   `tests/core/test_parallel_tool_stream_assembly.py`.

   The model was never the problem: `scripts/probe_parallel_tool_calls.py` gets 2
   parallel calls on toy tools and 3 on the real 171-tool payload. The chitfund
   audit blamed model behaviour and verified both ends of the pipe, not the
   middle.

2. **Result: -32% turns** on the 13 comparable conversations (175 → 118.7, mean
   of THREE clean full-corpus runs), **-33% on `real-world-taskmate`** (72 → 48),
   the corpus's closest analogue to a one-shot app build. Eight of the twelve
   light conversations gave an identical turn count all three times.

   Full 17-conversation corpus, per run: turns [221, 236, 258] (mean 238, sd 19),
   calls [272, 291, 309], converged [11, 11, 10], calls/turn 1.22, widest batch 9.

   **Unexplained:** the totals rise monotonically across the three sequential
   runs (221 → 236 → 258), driven mostly by `shopkeep` (47 → 55 → 72). That is a
   trend, not obviously noise. Checking whether dry-run leaks real apps into the
   gateway was inconclusive (the local JWT is expired, everything 403s). Resolve
   this before quoting the corpus total as a stable baseline; the -32% on the
   13-conversation subset is the more defensible figure.

   Wall clock: 99% of it is LLM streaming (183s wall vs 181s LLM over 40 turns).
   Light conversations run ~4.5s/turn, the heavy five 13-19s/turn because context
   growth lengthens prefill every turn. The heavy five are 83% of the ~55min
   runtime, so iterate with `--only` on the light twelve.

3. **Three defects in the bench itself**, all fixed: `turns` counted user
   messages not LLM round trips (a 175-turn run read as 61); the circuit breaker
   treated the oracle's own "missing required tools" verdict as an
   infrastructure cascade and aborted after 2, so the four heaviest
   conversations had never run in ANY bench; and the script could not read
   `.env`.

4. **HOT_TOOLS earns its keep — a negative result.** `CFA_HOT_TOOLS=off` cost
   turns (129 → 140) and one conversation's convergence. The gate docstring's
   claim that the set is "mostly redundant" is wrong. Default stays `full`.
   Measured delta of the full set over the same tools stripped: 15,031 tokens
   per turn (the code comment says "3-5K"), 13% of DeepSeek's 112K window.

5. **Corpus analysis reorders the SLM plan** (`scripts/analyze_definition_corpus.py`):

   | Axis | Volume | Distinct vocabulary | For 80% |
   |---|---:|---:|---:|
   | Structure | 82,704 components | 2,857 signatures | **81** |
   | Styling | 393,459 style leaves | 18,969 (prop,value) | **1,861** |
   | Logic | 4,075 Kirun steps | 244 step functions | **10** |

   Held-out structural coverage: 94.7% mean, 98.2% median. `UIEngine.SetStore`
   alone is 49% of all steps; median function is 3 steps.

   Read: structure and logic are TEMPLATE problems, not learning problems.
   Styling is the only axis with real novelty. **Recommendation: do not build
   the compiler SLM.** Build the template library; aim any learned component at
   styling.

## Same-document write guard (added after the benches)

Fixing the assembler un-masked a hazard the bug had been hiding. `asyncio.gather`
(`app/core/agent.py`) dispatches a batch concurrently with serialisation only for
elicitation, and `_load_save` (`tools/modlix/pages.py`) is fetch → mutate → PUT
with no version check. While every batch collapsed to one call this was
unreachable; now two writes to one page in a batch would both read the same
version and the later save would silently discard the earlier edit.

`BaseAgent.write_conflict_key` / `_batch_write_collision` now fall back to serial
dispatch for exactly those batches. Core holds the mechanism and returns None by
default; `AppBuilderAgent._RMW_TOOLS` (49 read-modify-write tools) holds the
knowledge, so core carries no table of another layer's tools. The key is
`family:identity:app_code`, so:

- two writes to `home` → serialised
- writes to `home` and `login` → still parallel (the persona encourages this)
- `home` in two different apps → still parallel
- six reads plus one write → still fully parallel
- `update_page(name=home)` vs `update_theme(name=home)` → different documents
- an unreadable target → family wildcard, serialised rather than raced
- creates are deliberately unguarded: two creates of one name is a loud backend
  conflict, not a silent lost edit

14 tests in `tests/core/test_same_document_write_guard.py`, including drift tests
that fail if the table names an unregistered tool or if a known page-mutating
tool is missing from it.

Confidence: the decision logic is unit tested and the serial fallback is the same
path every single-tool turn already takes. NOT yet observed firing on a real
batch — across ~100 observed batches the model never produced a colliding pair,
which is the persona rule working. A runtime check on `end-to-end-new-page`
confirmed the win survives: `[create_page, create_storage,
save_function_from_text]` ran parallel (three different documents) with no
serialisation.

## Open, not done

- **Browser-session leak.** The reaper in `visuals_browser.py:51` is in-process
  and lazy (runs on the next `drive_page`). A process that exits orphans its
  chromium children. Three sequential runs accumulated enough orphans to poison
  run 3 (shopkeep 5 turns instead of ~50, clone-linear 0 turns) and hold the
  stdout pipe open. UNVERIFIED in production — only observed locally.
- **Convergence cannot be read as quality in dry-run.** MockSaasClient has no
  login page and no Sign In button, so every conversation that EDITS
  pre-existing state is structurally unwinnable, and those are exactly the six
  that fail. `page-event-onclick` run 3 is the proof: 8 calls, all reads, and the
  agent's own chain of thought reads "The login page doesn't exist in testapp" →
  "There is no `testapp` app registered" → "maybe this is a dry-run
  environment". It correctly refused to patch a button that does not exist; the
  oracle failed it for not calling `patch_component_props`. The bench docstring's
  claim that "convergence still measures whether the LLM called the right tools"
  in dry-run is wrong for state-dependent conversations.
  Measuring convergence needs `--mode live` against a SEEDED sandbox. Not started.
- **The oracle also encodes the pre-batch-tools workflow.** It demands
  `patch_component_styles` where `add_components` now takes `style_properties`
  inline, and demands `bulk_patch_component_props` for `bulk-style-update`'s
  "change every Button's backgroundColor" — a STYLE, for which
  `bulk_patch_component_styles` is the right tool. Same class of staleness as
  chitfund audit 3.3. Needs an outcome-based oracle, not a tool-identity one.
- `bulk_patch_component_props` still takes one patch per filter, not
  heterogeneous `patches=[{component_key, properties}]` (chitfund audit 3.9).
- Run 3 was discarded once (browser orphans held the pipe open), then rerun
  cleanly after switching the launch to write straight to a file instead of
  piping through grep, plus a chromium reap either side. All three runs here are
  complete 17/17.
- All measurement was `--mode dry-run`: turn shape is real, tool outcomes mocked.

## Logs

- `00-baseline-prefix.log` — the original 13-conv baseline (broken turn metric;
  real turns reconstructed from the log: 147 calls / 175 turns / 100% single-call)
- `01-arms-hot-tools.log` — HOT_TOOLS full vs off, both with the stream fix
- `02-full17-three-runs.log` — runs 1-2 complete; run 3 contaminated, discarded
- `03-full17-run3-rerun.log` — clean run 3, FULL output (not grep-filtered)
- CSVs: `scripts/bench_results/20260902-160128`, `-165432`, `-175108`
