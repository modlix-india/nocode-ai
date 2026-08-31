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

**Not yet wired:** `ingest.from_inventory` and `ingest.from_run` exist and are
tested, but nothing calls them. Inventory snapshots would answer "what is in
this app", and run outcomes would answer "what actually happens when you use
it". Both are real gaps, not deliberate omissions.

Settings:

| Setting | Default | Effect |
|---|---|---|
| `LORE_ENABLED` | `true` | Master switch |
| `LORE_OBSERVE_CHAT` | `true` | Passive turn recording. Off leaves the tools and API working |
| `LORE_OBSERVE_EDITS` | `true` | Record every successful definition write. This is the path that carries real evidence |
| `LORE_AUTOCURATE_AT` | `25` | Pending observations app-wide that trigger a pass. `0` disables |
| `LORE_AUTOCURATE_SUBJECT_AT` | `8` | ...or this many about one subject, whichever comes first |

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
- **No contradiction detection beyond what the curator notices in one batch.**
  Two entries that disagree but were curated in different passes will both
  stand. `gaps` surfaces low-confidence entries so a person can arbitrate.
- **Curation is not incremental within a batch.** One LLM call sees up to 60
  observations and up to 120 existing entries. An app with thousands of entries
  will need the context selected rather than dumped.
