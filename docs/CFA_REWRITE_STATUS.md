# CFA Rewrite — Status & Retirement Notes

Tracks the multi-session implementation of the customer-facing AppBuilder
agent rewrite (plan at `~/.claude/plans/fuzzy-frolicking-badger.md`).

## Status

| Phase | Description | Status |
|---|---|---|
| 1.1 | Port pure-Python helpers (_kirun_dsl, _kirun_layout, _conventions) | ✅ Done |
| 1.2 | Port _page_ops.py for new tools | ✅ Done |
| 1.3 | Catalog get_catalog/set_catalog singleton + lifespan wiring | ✅ Done |
| 1.4a | Port infra.py (env, cache, logs) — pattern proof | ✅ Done |
| 1.4b | Port components (3) + pages/composition (26) + kirun core (22) | ✅ Done |
| 1.4c | page_event_functions + step ops + DSL variants (10), schemas+storages+storage_data (16), visuals+image_ops+browser-sessions (24), security+transports (23), app_admin = apps+themes+styles+uri_paths (22), messaging = notifications+connections+templates+events (28), runtime = personalization (3). html_compiler intentionally skipped — granular pages/components tools cover greenfield authoring. | ✅ Done |
| 1.5 | Registry aggregation across all tool surfaces | ✅ Done |
| 2 | Auth bridge (caller JWT + app_user resolution) | ✅ Done |
| 3 | Deferred tool surface (search_tools + get_tool_schema) | ✅ Done |
| 3b | Agent-loop synthetic-schema injection on first-call | ✅ Done |
| 4 | Code workspace + code-reading tools | ✅ Done |
| 5 | Platform docs tools + aicontext layout + content migrated (29 refs + 159 patterns + 1309 sample files: 436 JSON + 629 DSL + 244 tree.txt) | ✅ Done |
| 6 | Per-app KB MySQL schema + tools + seed migration | ✅ Done |
| 7 | Cross-env promotion endpoints + script | ✅ Done |
| 8 | Gemini provider + bench scaffold | ✅ Done (bench corpus pending) |
| 9 | Retire modlix-mcp / modlix-apps | ⏳ This doc + execute checklist |

**Tool surface now: 210 tools** across 6 surfaces (LEGACY=10, MODLIX=182, META=2, WORKSPACE=5, KB_APP=6, PLATFORM_DOCS=5). MODLIX breakdown: infra=5, components=3, pages=26, kirun=22, kirun_events=10, schemas=16, visuals=12, visuals_browser=4, image_ops=8, security=23, app_admin=22, messaging=28, runtime=3. PLATFORM_DOCS now includes `pattern_sample(task_name, file_name)` for fetching the page-JSON / Kirun-DSL / component-tree examples that back each pattern recipe. Zero name collisions across the whole surface.

## What's actually shippable today (after `pip install -r requirements.txt`)

- `POST /api/ai/appbuilder/chat` accepts the existing `ChatRequest` plus a
  new optional `app_user: {token? | username+password?}` field.
- `session.get_app_user_token()` resolves credentials lazily for
  screenshot/drive tools (Phase 1.4b ports those).
- 209 tools registered: 10 legacy CRUD + 182 modlix port + 2 meta
  (search/get_schema) + 5 code-workspace + 6 KB + 4 platform-doc.
- Three providers wired in `get_llm_provider`: anthropic (default),
  openai, deepseek, **gemini** (new). Add `GEMINI_API_KEY` to flip default.
- New admin endpoints under `/api/ai/admin/app-kb/{export,import}` for
  dev → stage → prod promotion. Guarded by `ADMIN_TOKEN`.
- New MySQL table: run `migrations/V12__CFA_App_KB.sql` against the
  ai database.
- Two scripts:
  - `scripts/migrate_modlix_apps.py` — one-off seed migration.
  - `scripts/promote_app_kb.py` — recurring dev → stage → prod promotion.
  - `scripts/bench_providers.py` — provider bench harness skeleton.

## Pre-flight before any deploy

1. **Install new deps**:
   ```
   pip install -r requirements.txt
   ```
   Adds `kirun-py>=0.1`, `google-generativeai>=0.8.0`.

2. **Run the migration**:
   ```
   # Flyway picks up V12__CFA_App_KB.sql automatically; manual: psql/mysql
   # client and execute the file content.
   ```

3. **Set required env**:
   ```
   ADMIN_TOKEN=<random-32-char-secret-per-env>
   GEMINI_API_KEY=<key>          # only when flipping default
   CFA_WORKSPACE_DIR=/var/cfa/workspace  # default; override if needed
   ```

4. **Provision the workspace volume**:
   ```bash
   mkdir -p /var/cfa/workspace
   cd /var/cfa/workspace
   git clone --depth=1 https://github.com/modlix-india/nocode-saas
   git clone --depth=1 https://github.com/modlix-india/nocode-ui
   git clone --depth=1 https://github.com/modlix-india/nocode-kirun
   ```
   For local dev the code_workspace tools fall back to siblings of nocode-ai
   automatically; no provisioning needed.

5. **Seed per-app KB from modlix-apps (one-time)**:
   ```
   ./venv/bin/python scripts/migrate_modlix_apps.py \
     --path /Users/kirangrandhi/kiran/fincity/modlix-apps \
     --updated-by 1 \
     --message "Seed migration from modlix-apps @ <date>"
   ```
   Dry-run with `--dry-run` first.

## Phase 1.4 — modlix-mcp port complete

All ~190 modlix-mcp source tools that are in scope have been ported into
`nocode-ai/app/agents/appbuilder/tools/modlix/` as native `ToolDefinition`
modules. Final layout:

| Module | Source files | Tools shipped |
|---|---|---|
| `infra.py` | environment + cache + logs | 5 |
| `components.py` | components.py | 3 |
| `pages.py` | pages + composition + composition_v2 | 26 |
| `kirun.py` | functions + server_functions + function_steps + function_execute + kirun_dsl_tools + kirun_primitives | 22 |
| `kirun_events.py` | page_event_functions + DSL page-event variants | 10 |
| `schemas.py` | schemas + storages + storage_data | 16 |
| `visuals.py` | screenshot + preview + image_gen + files (uploads/transforms/secured) | 12 |
| `visuals_browser.py` | drive (Playwright BrowserSession registry) | 4 |
| `image_ops.py` | image_ops (Pillow-based) | 8 |
| `security.py` | security + transports | 23 |
| `app_admin.py` | apps + themes + styles + uri_paths | 22 |
| `messaging.py` | notifications + connections + templates + events (defs + actions) | 28 |
| `runtime.py` | personalization (READ-only) | 3 |
| **Total MODLIX_TOOLS** | | **182** |

Intentionally not ported:
- `login`, `_apply_developer_login`, `try_fresh_login_from_settings` — auth
  comes from caller JWT via `context["headers"]`, not from `.env`.
- `html_compiler_tools.py` (4 tools) — the agent has granular page/
  component tools that cover greenfield authoring. The HTML compiler
  earns its keep mostly on landing-page-style work and produces lossy
  structure that still needs binding + event wiring afterward. If a bench
  run shows the agent struggling on greenfield page authoring, revisit.

Each tool follows the locked-in `infra.py` pattern:
- `ToolDefinition` with an `async _execute_<name>(params, context)` closure.
- `context["headers"]` for the caller JWT, `get_saas_client()` for HTTP,
  and helpers in `tools/modlix/_page_ops.py`, `_kirun_dsl.py`,
  `_kirun_layout.py`, `_conventions.py`.
- `truncate()` hard caps stripped — the agent loop handles output sizing.

## Phase 3b — agent-loop synthetic schema injection

Currently the meta-tools (`search_tools`, `get_tool_schema`) work, but the
agent loop doesn't auto-inject a tool's schema when the LLM calls it
without having fetched it. That requires a careful edit to
`app/core/agent.py`'s dispatch path: detect "tool name not in
session.context['fetched_schemas']", respond with a synthetic schema
message, and let the LLM retry. Out of scope for this round because the
hot dispatch path is sensitive.

## Phase 9 — retirement checklist

Once Phases 1.4b + 3b are done and at least 2 customer flows are verified
end-to-end on the new surface:

### modlix-mcp retirement

- [ ] Strip remaining `modlix-*-local` entries from `~/.claude.json` mcpServers.
  Already mostly stripped per memory `project-modlix-mcp`.
- [ ] `git tag v-final-iteration` on the modlix-mcp repo before archiving.
- [ ] Archive the repo (Settings → Danger zone → Archive). Code stays
  read-only on GitHub for history.
- [ ] Update CLAUDE.md and any memory entries that reference modlix-mcp paths
  to point at the new nocode-ai locations.
- [ ] Optional: a redirect note in `modlix-mcp/README.md` pointing readers
  to nocode-ai.

### modlix-apps retirement

- [ ] Verify the seed migration is complete and lossless:
  `SELECT client_code, app_code, COUNT(*) FROM cfa_app_kb GROUP BY 1, 2`
  shows expected row counts for every per-app folder that existed.
- [ ] Cross-check unhandled files reported by migrate_modlix_apps.py —
  manually copy any that should be in the KB via propose_kb_update.
- [ ] `git tag v-final-modlix-apps` then archive the repo.
- [ ] Update CLAUDE.md memories.

## Open questions for the team

- Bench corpus: who curates the 10-15 conversations for
  `scripts/bench_providers.py`? The script is shell-only; corpus is editorial.
- Vision quality on Gemini Flash for screenshot critique: needs real
  testing before the default flip. Bench should include at least 3
  conversations with screenshots.
- Pattern library migration: the ~130 by-task READMEs from
  `modlix-mcp/agent_context/corpus/by-task/` need to land at
  `nocode-ai/app/agents/appbuilder/aicontext/patterns/` (or
  `aicontext/corpus/by-task/` — the tools accept either layout).
