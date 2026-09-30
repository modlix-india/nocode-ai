# Lore

Curated, growing knowledge about each application we build.

## Why

An app that took three months to build carries three months of decisions,
conventions, constraints and hard-won traps. The definitions record **what** the
app is. Nothing records **why**, what was tried and abandoned, or which of the
sixteen possible ways to do something this app settled on.

So the second person to open it starts from zero, and so does the next agent
session. `cfa_app_kb` helps, but only when somebody remembers to write in it.

Lore accumulates that second layer without anyone having to remember, and
serves it back as a briefing.

## Not the pattern corpus

There are three knowledge stores in this repo and they answer different
questions. Getting them confused is easy, so:

| Store | Scope | Answers | Grows by |
|---|---|---|---|
| **pattern corpus**, `aicontext/patterns/` | cross-app, indexed by task | "how do people build a login page on this platform" | a batch rebuild against prod mongo (`modlix-mcp/scripts/corpus/builder.py`), read via `pattern_search` / `pattern_read` / `pattern_sample` |
| **cfa_app_kb**, V12 | one app | "what did we write down about this app" | the agent, on request, propose-then-confirm, six narrative sections |
| **lore**, V13 (this) | one app | "what is true about this app, and why" | continuously and automatically, from definition edits, agent turns, documents and people's notes |

The pattern corpus holds real definitions as **exemplars**. Lore holds **claims**
about one app. Neither replaces the other, and lore does not read the pattern
corpus: an exemplar from another app says nothing about what is true here.

## Shape

Two layers, kept apart on purpose.

```
  something happens                    the curator                what you read
  ─────────────────                    ───────────                ─────────────
  agent turn        ─┐
  definition edit    │
  inventory snapshot ├─►  lore_observation  ──►  LLM proposes  ──►  lore_entry
  document           │    (raw, append-only,       code disposes      (curated, typed,
  run outcome        │     deduped by                                  scored, with
  a person's note   ─┘     fingerprint)                                provenance)
```

**Observations** are cheap and forgiving. Anything that watches the app writes
them. They are never read for answers.

**Entries** are what gets read. One durable claim per row, typed by kind, with a
confidence score, its standing against contradicting entries, and links back to
the observations that produced it.

The curator is the only thing that crosses between the two, and the only place
an LLM is involved. The model returns operations; `apply_operations` validates
every one against the taxonomy and against the entries that actually exist
before anything is written. A hallucinated entry id, an invented source link, an
unknown kind or a two-word body is dropped, not applied.

## Entry kinds

| Kind | What it holds | Expires with time? |
|---|---|---|
| `purpose` | What the app or object exists to do | no |
| `decision` | A choice and its reasoning | no |
| `glossary` | A domain term as used *here* | no |
| `constraint` | A rule that must hold | no |
| `gotcha` | A trap that already cost someone time | no |
| `convention` | A pattern this app follows | no |
| `integration` | An external system and how it is reached | no |
| `howto` | A procedure specific to this app | no |
| `owner` | Who knows about what | 6-month half-life |
| `status` | What is in flight right now | 2-week half-life |

## Why almost nothing expires

The first version of this decayed every kind on a per-kind half-life, on the
theory that age is a proxy for "this might be wrong". It was the wrong model
and it was replaced.

Age is a bad proxy, and it fails in both directions. An app nobody has touched
for two years has perfectly true lore that has decayed to noise; an app under
daily churn keeps high-confidence lore that went wrong last week. Worse, decay
is **silent**: a fading entry never asks to be checked, it just stops
surfacing. That deletes the knowledge instead of correcting it, which is the
opposite of the point of lore. And the half-lives were invented numbers with
nothing behind them.

The tell was in the numbers themselves. `purpose` was set to ten years and
`decision` to five, against apps that are weeks old, so decay was already inert
for the kinds it fitted worst. The long half-lives were an admission that the
model did not fit.

What replaced it, in order of strength:

- **Supersession.** Something newer replaced the entry. The curator says so
  explicitly, and the old row moves to status `superseded`.
- **Contradiction.** Something newer disagrees and nobody has decided which
  wins, recorded as a `contradicts` link. Each unresolved contradiction halves
  confidence and both ends surface as *contested*, so a reader is told to go
  and settle it rather than being handed a quietly smaller number.
- **A changed subject.** The object the entry describes was edited after the
  entry was last confirmed. Not wrong, but no longer evidenced: a mild haircut
  and an *unverified* mark. This is computed from the app's own `edit`
  observations, which is a real staleness signal rather than a guess.
- **Age**, for `status` and `owner` only. Their truth is defined relative to
  now, so for these age is not a proxy, it is the semantics.

Two things sit on top:

- **Corroboration** lifts confidence by `log2(source_count)`, capped at +25%, so
  ten repetitions of a wrong thing cannot outrank one confirmed fact.
- **Pinning** disables every adjustment above. A pinned entry is one a person
  vouched for, so none of these mechanisms needs to guess at what that person
  already answered; the curator may not revise, retire or supersede it, though
  the person who wrote it still can (see Pinning, below).

## Who may read and write it

Three rules, and `access.py` exists for them.

**1. Writes always land under the logged-in user's client code.** Never the app
owner's, never one supplied in a request. A CLIENTA user working on a
SYSTEM-owned app writes CLIENTA lore. An agent inherits exactly the access of
the person it is acting for.

**2. Editing lore needs edit access on the app**; reading needs read access.
Checked against the security service on every call, including the passive
turn-observer (an observation becomes an entry at the next curation pass, so it
needs the same access a hand-written entry would). Failures fail CLOSED: if
security cannot be reached, the answer is no.

**3. Reads follow the app's inheritance chain.** From
`applications/internal/appInheritance`, base client first, caller last, which is
the same call the ui service makes to resolve overrides on every other object.
CLIENTA sees SYSTEM's knowledge about the app, with CLIENTA's own on top.

### Overrides

CLIENTA cannot edit a SYSTEM row, and must not be stuck with a SYSTEM claim that
is wrong for them. So editing an inherited entry **forks** it:

```
  SYSTEM  #4  constraint  "SLA is four hours"          <- untouched, everyone else still sees it
  CLIENTA #7  constraint  "SLA is two hours"  base=#4  <- CLIENTA sees this instead
```

Retiring an inherited entry writes a **tombstone**: a retired row with
`BASE_ENTRY_ID` set, which hides the base for that client and nobody else.
Editing the same base twice edits the existing fork rather than making a second.

Resolution lives in `store.resolve_overrides`, which is pure and unit-tested. An
override written by a client ABOVE the base in the chain is ignored: inheritance
only flows downward.

The curator sees the whole chain so it does not re-derive what the owner already
recorded, but may only write its own client's rows. Inherited entries are marked
`READ-ONLY (owned by X)` in its prompt, and `apply_operations` refuses them
regardless of what the model returns.

## Subjects

An entry is about `app`, or about one object as `<type>:<name>`:

```
app                                  storage:job
page:jobsToday                       function:notifyLateJobs
```

Anything unrecognisable degrades to `app` rather than dropping the observation.

## Reading it

```
GET  /api/ai/lore/brief?app_code=X[&subject=page:jobsToday]
GET  /api/ai/lore/search?app_code=X&q=...[&kind=decision]
GET  /api/ai/lore/about?app_code=X&subject=storage:job
GET  /api/ai/lore/entries?app_code=X[&kind=&subject=&status=]
GET  /api/ai/lore/entries/{id}          # with full provenance
GET  /api/ai/lore/gaps?app_code=X       # what it does NOT know
GET  /api/ai/lore/stats?app_code=X
GET  /api/ai/lore/taxonomy              # for UI pickers
```

`brief` is the point of the service: a markdown digest ordered so that the
things constraining what you may do come before the history of how it got that
way. It is budgeted in characters and **says what it left out** rather than
presenting a trimmed list as the whole picture.

## Writing to it

There are two ways in, and the difference is the whole design.

**Someone knows something.** It goes straight in as an entry, pinned, visible
immediately. Nobody typing tribal knowledge into a box should wait on an LLM.

```
POST   /api/ai/lore/entries        {app_code, kind, title, body, subject?, tags?, confidence?, pinned?}
PATCH  /api/ai/lore/entries/{id}   edit; works on pinned entries, that is the point
DELETE /api/ai/lore/entries/{id}   retires, never hard-deletes
POST   /api/ai/lore/document       {app_code, title, content, origin?, curate?}
```

`/document` is for knowledge that already exists somewhere: a README, a spec, a
handover note, a thread somebody pasted into a file. It splits on headings,
ingests each section with provenance, and by default curates immediately so you
see entries rather than a queue.

**Something happened.** It goes in as evidence and the curator decides what it
means.

```
POST /api/ai/lore/observe          {app_code, kind, source, subject, body, meta}
POST /api/ai/lore/note             {app_code, text, subject, author}
POST /api/ai/lore/curate           {app_code, batch_size, wait}
POST /api/ai/lore/backfill/app-kb  {app_code}
```

### Pinning

**Pinning costs more than it looks.** `Entry.standing` returns `None` for a
pinned entry, so `retrieval._mark` never renders one as `contested` or
`unverified`, and `effective_confidence` returns early — which means a
contradiction against a pinned entry is *recorded and then rendered invisible*,
and `gaps` can never surface it either, because a pinned entry never becomes
low-confidence. When `apply_operations` tries to supersede a pinned entry it
falls back to writing a `contradicts` link, and that link has no visible effect.

So a pinned entry does not merely resist correction; it resists being flagged as
suspect. The only routes back are a human `PATCH` or `lore_correct`.

The test is therefore **not** "is this important" but **"could an edit to this
app falsify it?"** Pin `purpose`, and pin the constraints that are true by
construction. Do not pin a behavioural constraint scoped to a page or a flow,
even though it is the same kind — those are the claims an edit is most likely to
falsify, and the stale-subject mark is the signal you would be switching off.
The seeded files pin 3-6 rows out of 15-22. A second reason to be sparing:
`_rank` sorts on `pinned` as a hard first key, so pinning everything makes the
flag stop discriminating and is indistinguishable from pinning nothing, except
that the curator is now locked out.

Seeded rows carry `SEED_SOURCE`, which gives a reader the provenance benefit of
a pin — a person wrote this down deliberately — without the immunity cost.



Pinning protects an entry from **the curator**, not from people. A person who
wrote something down may edit or retire it freely; `revise_entry` and
`set_entry_status` take `force=True` on the human paths and the curator never
passes it. Making someone unpin before editing their own note would be a
two-step dance for nothing.

Admin, behind `X-Admin-Token`:

```
POST /api/ai/lore/admin/sweep?min_pending=5&max_apps=10
GET  /api/ai/lore/admin/apps
```

`client_code` always comes from the token, never from the request. There is no
cross-tenant read.

## How it reaches the model

Tools are a pull: the agent has to decide to look. That is fine for a specific
question and useless for the thing lore is for, which is stopping an agent from
confidently redoing something this app decided against in March. An agent that
does not know to ask will not ask.

So lore is also **pushed**, at two scales (`context.py`):

**Big picture**, the app's briefing, folded into the system prompt once per
request by both appbuilder agents. Purpose, the rules, the conventions, recent
decisions and why. An overriding client is told which lines are the owner's and
that their own writes will not change what the owner sees.

**Small picture**, what is known about the object now in focus, injected as a
per-turn reminder. Focus is derived from tool inputs (`page_name`,
`storage_name`, …) in `BaseAgent._run_tool_block`, and each subject is pushed
once per session: repeating it every turn would spend the whole reminder budget
restating what was said three turns ago.

Both are budgeted, both check read access, and both return `""` on any failure.

## Agent tools

Six verbs, shared by appbuilder v3 and v4:

| Tool | Use |
|---|---|
| `lore_brief` | Once at the start of a task on an app you have not touched this session |
| `lore_search` | Before deciding anything that might already have been decided |
| `lore_about` | Before editing an object you did not create |
| `lore_add` | The user **stated** a fact. Record it as a typed, pinned entry, now |
| `lore_note` | You **saw** something worth keeping. Let the curator work out what it means |
| `lore_correct` | The user says a recorded entry is wrong |

`lore_add` versus `lore_note` is the same split as the two write paths above.
"Technicians must never see pricing" is a rule the user stated: there is nothing
to infer, so it becomes an entry immediately. "The user seemed annoyed that the
export takes 40 seconds" is evidence: it might become a `gotcha`, or it might
become nothing, and that is the curator's call once it can weigh it against
everything else it knows.

Both `lore_add` and `lore_correct` pin what they write, because a person saying
"it works like this" is the best signal lore can get and the curator should not
be able to quietly undo it.

## Automatic accumulation

Two hooks, both in `BaseAgent`, both entirely best-effort: every failure is
swallowed at debug level, because lore must never affect a user's turn.

**Edits — `_observe_edit_to_lore`, on every tool call.** This is the path that
carries real evidence. A chat turn is the agent narrating what it believes it
did; an edit is what the platform actually accepted, and it names the object it
happened to. `watch.classify` decides whether a call was a definition write and
what its subject is, at the single point where every tool executes, so no tool
has to remember to report itself. It is conservative by design: a tool it
cannot place produces nothing, because a missed edit costs one observation
while a wrong one puts a false claim into an app's permanent knowledge. Around
90 of the CFA's tools currently classify as writes.

**Turns — `_observe_to_lore`, at the end of each turn.** The user's instruction
and the agent's summary, as two observations.

The proportion matters. A CFA build is roughly five turns of a hundred-odd tool
calls: turns alone yielded about ten observations for an entire application,
each a 60KB transcript with no single subject. Edits yield hundreds, each small
and each attached to one page, storage or function.

**Curation fires** on either of two triggers: `LORE_AUTOCURATE_AT` pending
observations app-wide, or `LORE_AUTOCURATE_SUBJECT_AT` about a single subject.
The second exists because app-wide volume is the wrong unit on its own. Thirty
edits scattered across thirty objects have taught less than eight against one
page, and only the second yields an entry worth reading. `curate` refuses to
run concurrently with itself for the same app, so a busy build cannot stack
passes.

**Inventory — `_observe_inventory_to_lore`, once per session.** Hung off the
AppBuilder agent's preflight grounding, which already fetches the app definition
and page names once per session, so it costs no extra call. The value is not the
list — the definitions hold that — but that the curator can tell a claim about
`page:foo` from a claim about something that no longer exists. Unchanged
snapshots collapse by fingerprint, so a quiet app accumulates nothing.

**Failures — `_observe_failure_to_lore`, on a failed tool call.** Restricted to
the eight tools that *execute* rather than edit (`execute_function`,
`validate_page`, `query_storage_rows`, `drive_page` and so on), and only on
failure. A repeated identical failure collapses into `SEEN_COUNT`, which is the
shape of a real gotcha. Successes are deliberately not recorded: a function that
ran fine is not knowledge, and the volume would swamp the batch quotas.

**Batch composition.** `curator.select_batch` is pure and applies a per-kind
quota — `manual` and `run` effectively unlimited, `edit` 40, `doc` 20, `chat` 8 —
and drops chat rows that carry no declarative marker. It runs over rows
*already in the table*, not only new writes, which is what makes an existing
backlog of narration harmless without deleting anything.

Settings:

| Setting | Default | Effect |
|---|---|---|
| `LORE_ENABLED` | `true` | Master switch |
| `LORE_OBSERVE_CHAT` | `true` | Passive turn recording. Off leaves the tools and API working |
| `LORE_OBSERVE_EDITS` | `true` | Record every successful definition write. This is the path that carries real evidence |
| `LORE_AUTOCURATE_AT` | `25` | Pending observations app-wide that trigger a pass. `0` disables |
| `LORE_AUTOCURATE_SUBJECT_AT` | `8` | ...or this many about one subject, whichever comes first |
| `LORE_OBSERVE_AGENT_NARRATION` | `false` | Record the agent's own summary. Off because the first 192 chat observations produced zero entries |
| `LORE_OBSERVE_INVENTORY` | `true` | One object-inventory snapshot per session |
| `LORE_OBSERVE_RUNS` | `true` | Record failures of tools that execute rather than edit |
| `LORE_CURATOR_TIER` | `balanced` | Curation's own tier. Was hardcoded `fast`, which is what produced zero entries |
| `LORE_CURATOR_MAX_TOKENS` | `16000` | Output budget. Must leave room for a reasoning model to think *and* emit |
| `LORE_CURATOR_TIMEOUT_SECONDS` | `240` | Bounds one model call. The provider clients set no timeout of their own |
| `LORE_MAX_CURATION_ATTEMPTS` | `3` | Drop an observation the model has declined this many times |
| `LORE_KEEP_RAW_RESPONSE` | `false` | Store the redacted model response on the run row, for a debugging window |
| `LORE_BIG_PICTURE_BUDGET` | `3800` | Briefing size in the system prompt. 2600 rendered only 10-12 entries |
| `LORE_ADVISE_BEFORE_EDITS` | `true` | Append a subject's constraints and traps to the first write to it, in the same turn |

Lore needs the AI tracking database (`AI_TRACKING_ENABLED`). Without it every
write silently no-ops rather than raising.

## Relationship to `cfa_app_kb`

| | `cfa_app_kb` (V12) | lore (V13) |
|---|---|---|
| Written by | the agent, on request, propose-then-confirm | anything that watches the app |
| Shape | six narrative sections per app | one claim per row, typed |
| Queried by | section | question |
| Ages | not at all | only `status` and `owner`; the rest hold until contradicted |
| Provenance | a commit message | links to the observations behind it |

They are complementary. Lore reads app_kb as a source (`ingest.from_app_kb`,
also exposed as `POST /backfill/app-kb`) and **never writes to it**.

## Layout

| Module | Contains |
|---|---|
| `models.py` | Taxonomy, hashing, effective confidence. Pure, no I/O, fully unit tested |
| `access.py` | Who may read/write, and the client inheritance chain |
| `store.py` | Data access and override resolution. No policy |
| `context.py` | Pushing lore into the model: big picture, small picture, focus tracking |
| `ingest.py` | Source adapters. Best-effort, never raise into a caller |
| `curator.py` | Observations to entries: the LLM pass and its guard rails |
| `retrieval.py` | Search, briefings, per-object knowledge, gap analysis |
| `tools.py` | The five agent verbs |
| `router.py` | HTTP surface |

Schema: `migrations/V13__Lore.sql` + `V14__Lore_Overrides.sql`. Tests:
`tests/test_lore.py` (110, no database and no LLM required).

## Why it produced nothing for its first three weeks

Worth recording in full, because the shape of the failure is more instructive
than the fix.

Measured on 2026-09-04: **267 observations, zero entries.** Seven curation runs,
each considering 25-44 observations, each recording `ENTRIES_ADDED=0` with
`ERROR` null. Four things were wrong at once, and the schema could not tell them
apart.

**The cause was `max_tokens=4000`.** Curation ran on `model_tier="fast"`, which
under DeepSeek resolves to a V4 reasoning model. On the real curation prompt that
model spends roughly 20,000 characters thinking before it emits anything, blows
through a 4,000-token output budget, and returns `finish_reason: "length"` with
`content` of `None`. `parse_operations("")` short-circuits to `[]` **before** its
own `logger.warning`, so the failure emitted nothing at all. The same prompt at
16,000 tokens needs 5,289 completion tokens and yields seven operations.

Three things made that invisible for three weeks:

1. **The run row could not express it.** `apply_operations` counted `rejected`
   and `contradicted`; `close_run` dropped both. A run where the model returned
   nothing and a run where every operation was refused wrote identical rows.
   `V15__Lore_Curation_Diagnostics.sql` adds `OBS_RENDERED`, `OPS_RETURNED`,
   `ENTRIES_REJECTED`, `RESPONSE_CHARS`, `REASONING_CHARS`, `STOP_REASON`,
   `MODEL` and `ATTEMPTS`. `REASONING_CHARS` beside a zero `RESPONSE_CHARS` is
   the fingerprint of this exact failure.
2. **Every pass burned its own evidence.** `curate` marked the whole batch
   curated regardless of outcome — the comment said an observation that produced
   no entry "has still been considered", which was true of the ones the model
   saw and false of the rest. The render budget of 24,000 characters against a
   chat-heavy batch meant roughly 60% of a batch never reached the model and was
   consumed anyway. 165 observations were spent for nothing. Now only the
   rendered ids are marked, a parse failure marks nothing, and
   `CURATION_ATTEMPTS` (not `CURATED_AT`) breaks the loop for a row the model
   keeps declining.
3. **The input was mostly not knowledge.** 192 of 267 observations were `chat`,
   and nearly all were either the agent describing itself ("I'm an expert
   application builder…") or a build instruction that the resulting `edit`
   observation already recorded far better. `models.looks_durable` now filters
   those at ingest *and* inside `select_batch`, so the existing backlog is
   harmless without deleting anything. `LORE_OBSERVE_AGENT_NARRATION` is off by
   default.

Two smaller defects found on the way, both worth knowing:

- **`watch.py` was recording false claims.** `remove_component_styles` starts
  with `remove_`, so it classified as `delete`, and the subject of the
  observation is the page — producing *"appbuilder deleted page `ContactCFA`"*
  for a call that removed ten style leaves from one component.
  `action_on_subject` now distinguishes the verb that is true of the *tool* from
  the verb that is true of the *subject*; `action_for` is unchanged, because its
  answer was never the wrong one.
- **`SUBJECT_TYPES` was declared and never enforced.** A curation pass invented
  `form:`, `contracts:`, `preview:` and `contactform:` subjects, none of which
  any read path can reach: `lore_about` and the per-turn push key off the
  subject a *tool call* produces, and those only ever use the real types.
  `normalise_subject` now enforces the list, and the curator prompt names it.

And one robustness hole that only showed itself under repair: **the provider call
had no timeout.** A curation pass was observed stuck for 67 minutes on zero CPU,
holding its run row open. Curation is a detached background task, so an unbounded
call means one hung connection stops curating that app forever.
`LORE_CURATOR_TIMEOUT_SECONDS` bounds it.

## Seeds and transport

Lore for the flagship apps is **hand-authored and committed**, not extracted by a
model, under `app/services/lore/seeds/<app>.yaml`. Four apps are seeded:
`appbuilder`, `leadzump`, `marketingai`, `sitezump`. `cxapp` deliberately is not:
its only documentation is a v2 specification for an app code that does not
exist, and seeding a plan as a fact into a live app is worse than leaving it
empty.

A seed file **is** a transport document — same format, same parser, same
validation gate — so seeding an app is just an import, and the seeds are
exercised by every import test rather than living in a code path nothing else
uses. They sit inside `app/` because the Dockerfile ships only `app/`,
`scripts/` and `migrations/`.

`app/services/lore/transport.py` has four functions: `parse` (pure), `export`,
`plan` (reads only, decides everything) and `apply`. The `plan`/`apply` split is
what lets a screen show someone what an upload would do before it writes.

**The per-client delta resolution is not implemented there.** It already existed:
`store.edit_in_scope` forks-or-revises depending on whether the caller owns the
row, `store.retire_in_scope` writes a tombstone versus a retirement, and
`store.resolve_overrides` walks the chain base-first. The importer's only job is
to match a document row to a database row and decide which of those to call, and
it must never write merge SQL of its own.

Identity is `SEED_KEY`, not `BODY_HASH` (V16). `BODY_HASH` is the dedupe key and
changes whenever the wording does, so matching on it would import an edited entry
as a new row and leave the stale one standing beside it. The fork and tombstone
paths both carry the key across, or a re-import would fail to match an override
and create a second fork beside the first.

Four refusals, each protecting something specific:

- **`resolved: true` is refused.** A flattened export imported into a client turns
  every inherited row into an owned copy and breaks the override model.
- **`mode="replace"` is not implemented.** One import could wipe a client's
  curated knowledge and the only undo is `lore_entry_history`.
- **`supersedes` links are not portable.** They carry a local `SUPERSEDED_BY`
  pointer, so importing one asserts history that did not happen here.
- **`id`, `version`, `source_count`, `sources` and every timestamp are never
  imported.** Observation ids are local to an instance; honouring them would
  attach an entry to an unrelated observation, which is the provenance corruption
  `_clean_sources` exists to prevent.

One rule that is easy to get wrong: **an inherited row that is identical to the
document is skipped, not forked.** Forking an identical body gives that client a
private copy of something it already inherits, and the owner's later corrections
stop reaching it — which is how importing one shared seed into every client
quietly destroys the inheritance it was meant to use.

Two surfaces: `GET /api/ai/lore/export` and `POST /api/ai/lore/import`
(`dry_run` defaults to true), both on the caller's JWT behind `_write_scope`
rather than on the admin token, because a browser cannot hold `ADMIN_TOKEN`
safely. The CLI is `scripts/lore_transport.py`, which reuses
`access.resolve_scope` — it works without a token because the security service's
`applications/internal/**` routes are permitAll — and skips only
`require_write()`, with `--client` mandatory as the compensating control.

### Authoring a seed

Seven rules, from what went wrong while writing the first four:

1. **No definition restatement.** No page ids, component counts, root component
   keys or step counts. The per-page "Key facts" tables in the source docs are
   almost entirely that. It is already in the definitions, stale on the next
   edit, and it crowds out what cannot be derived. `dealProfile.md` would have
   yielded forty such rows; it yielded three real ones.
2. **No plans.** Anything phrased as should, will, propose, gap, roadmap or TBD
   is not a fact. `ROADMAP.md` and `IDE_PLAN.md` are excluded outright.
3. **Verify every non-`app` subject against the live definitions.** This caught
   a real error: `appbuilder`'s own `OVERVIEW.md` says `defaultPage: homeTwo`,
   and there is no `homeTwo` page — the live value is `builderLanding`. It also
   turned up that `properties.forbiddenPage` names a page that does not exist,
   which became an entry.
4. **No platform generalities.** "Modlix pages have 16 breakpoints" is not an
   entry; "this app only styles three of them, by convention" is.
5. **No secret values.** One entry records that a hardcoded shared secret exists
   in a page definition and where, because that is the durable fact someone
   needs. The value is not in the file. A seed is read by every agent working on
   the app; it is not a vault.
6. **A `decision` must contain the why.** Without it, it is a `convention`.
7. **`owner` = 0.** Nothing in the source documentation supports an owner claim,
   and `owner` is one of the two kinds that ages, so an invented one decays into
   a puzzle.

Roughly 60 app-level entries per app is the target: `BRIEF_CAPS` sums to 63, so
that is the most a full-app briefing will ever render. Do **not** write one entry
per page — `leadzump` documents 86 pages, and subject-scoped entries surface
through `lore_about` and the per-turn push exactly when an agent touches that
object, which is why that count can safely exceed 63.

`tests/test_lore_seeds.py` gates the files on merge. The subject check is the one
that earns its keep: `normalise_subject` degrades an unrecognised subject to
`app` silently, so a typo would file an entry where nothing will ever look for
it.

## Things deliberately not done yet

- **No embeddings.** Retrieval is MySQL fulltext with a LIKE fallback for short
  tokens. Good enough at the scale of one app's knowledge; revisit if briefings
  start missing obvious matches.
- **No cross-client reconciliation.** When an overriding client's entry
  contradicts the owner's, both stand and `gaps` surfaces it. Nothing tells the
  owner that three of their clients have all overridden the same rule, which is
  exactly the signal that the base is wrong.
- **No cross-app lore.** Every read is scoped to one (client, app). Platform-
  wide patterns belong in the platform KB, not here. `known_apps()` exists for
  admin listing only.
- **`review` has no producer.** It stays in `OBSERVATION_KINDS` as a reserved
  slot. Nothing in the system captures "the user said that was wrong", and
  adding an adapter with no caller is how `from_inventory` and `from_run` came
  to be written and never called for three weeks. Wire it when a thumbs-down
  on a turn exists.
- **`draft` has no writer.** It stays in `ENTRY_STATUSES`, is accepted by
  `add_entry`, and is excluded from every read path — which makes it the right
  shape for a review queue if one is ever needed. The hand-authored seeds did
  not need one: a person wrote them and a person read the diff.
- **No screen.** The lore service, its HTTP surface and its transport all run,
  and the agent uses them. There is no Lore pane in the AppBuilder workspace
  and no knowledge line on the object editors. The designed screen is kept as
  the spec at `modlix-apps/appbuilder_SYSTEM/mockup/index.html`; note that its
  per-kind "half-life" labels are the one thing in it that is wrong, since that
  model was abandoned (see above).
- **No contradiction detection beyond what the curator notices in one batch.**
  Two entries that disagree but were curated in different passes will both
  stand. `gaps` surfaces low-confidence entries so a person can arbitrate.
- **Curation is not incremental within a batch.** One LLM call sees up to 60
  observations and up to 120 existing entries. An app with thousands of entries
  will need the context selected rather than dumped.
