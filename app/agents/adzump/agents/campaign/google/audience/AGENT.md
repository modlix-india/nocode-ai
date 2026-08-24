# Audience Agent

Chooses **who a campaign reaches**, then answers for that choice and changes it by
conversation. Where the keyword agent decides *what someone typed*, this one decides *who the
person is* — and they are not interchangeable: a Demand Gen campaign has no keywords at all,
so this agent is the only thing between a business brief and a targeted ad.

**Channel-neutral by design.** A Google `Audience` resource serves Demand Gen, Performance Max
and App campaigns, so the channel arrives as an argument and only decides which segments are
allowed to serve. Nothing here is named for one channel.

**Vocabulary — four levels that get conflated:**

| Term | Means | Where it lives |
|---|---|---|
| **segment** | one catalogue entry — an in-market category, life event, detailed demographic, custom segment or user list | `AudienceSignal.ref` |
| **dimension** | a group of segments, or one demographic filter | `Audience.dimensions[]` |
| **audience** | the single resource holding every dimension, attached to the ad group | `AudienceTargetingResult` |
| **signal** | our word for one chosen segment *plus why it was chosen* | `AudienceSignal` |

```mermaid
flowchart LR
    subgraph AUD["Audience — ONE resource on the ad group"]
      direction TB
      subgraph D1["dimension: segments"]
        S1[in-market] -.OR.- S2[life event] -.OR.- S3[custom segment]
      end
      D2["dimension: age"]
      D3["dimension: income"]
      X["exclusion — user lists ONLY"]
    end
    D1 === |AND| D2 === |AND| D3
```

Segments **OR** within a dimension; dimensions **AND** across. That one sentence decides
whether a campaign reaches many people or almost nobody, and it is the fact most often got
backwards — splitting segments across dimensions *narrows* to an intersection.

---

## 1. The core idea

Google's catalogue is roughly a thousand entries, and a name alone is ambiguous.
"Construction" sits under a product category — people **buying** construction services — and
again under Employment, people who **work** in construction. Opposite audiences. Anything that
matches on names will confidently choose the wrong one.

So the agent does not pick from a list of names. It **loads the real catalogue**, reads each
entry's position in the tree, chooses with the ancestry in front of it, and records the
reason. Afterwards that record is what lets it answer *"why this one?"* without guessing, and
edit the set without rebuilding it.

Three structural facts shape every decision:

- **Broad across kinds, sharp within one.** Segments in a dimension are OR'd, so a second
  label for the same people widens the audience without adding anyone — but purchase intent,
  the life event that triggers it, and who the buyer is are *different* people, and naming
  only one of them leaves most of the market unreached. Optimized targeting expands from the
  selection as "an informed starting point", so near-synonyms get amplified loosely while a
  missing angle is simply never reached.
- **Exclusions are almost impossible.** `ExclusionSegment` has exactly one variant,
  `user_list`. "Exclude people interested in rentals" cannot be expressed, so the agent has to
  say so rather than quietly dropping it.
- **Some things are unreachable.** `custom_affinity`, `custom_intent` and `combined_audience`
  exist on `AdGroupCriterion` but have no `AudienceSegment` equivalent — in grouped mode they
  do not exist at all.

> **One line for a reviewer:** it reasons over the real catalogue with the tree in view and
> records why it chose what it chose — so the agent that built the audience is the one that
> defends it.

---

## 2. Two modes, one class

Two configured singletons, so neither mode sees the other's instructions or tools.

| | **build** (`suggest`) | **manage** (`handle`) |
|---|---|---|
| runs | once per campaign, headless | per user message, panel already on screen |
| prompt | `BASE` | `BASE_MANAGE` |
| prose | swallowed — the panel is the output | forwarded — the prose *is* the answer |
| tools | fetch · search · submit_segments · submit_demographics | search · lookup_segment · edit_audience · draft/submit custom segment |

The submit tools are deliberately absent from manage mode: they replace a set wholesale, which
would fabricate rationales for segments the model never re-derived and clobber a panel click
made in parallel. Manage edits go through `edit_audience`.

---

## 3. End to end

```mermaid
sequenceDiagram
    actor User
    participant Main as Main Agent (adzump)
    participant CC as prepare_campaign_review
    participant CA as CampaignAgent
    participant AT as audience_targeting (tool)
    participant AA as AudienceAgent
    participant Panel as Review Panel (craft)

    User->>Main: confirms the campaign summary
    Main->>CC: prepare_campaign_review()
    CC->>CA: create(spec, product_data, craft_id)
    CA->>AT: audience_targeting()
    Note over AT: gates on channel, resolves the COUNTRY first —<br/>availability is country-scoped, so it decides what can be offered
    AT->>AA: suggest(business, account, channel, country)
    AA-->>AT: AudienceTargetingResult (signals + demographics + groups)
    AT->>Panel: campaign_build.demand_gen.audience → panel
    CC-->>Main: elicited=multi → loop pauses, review open

    Note over User,Panel: TWO edit paths<br/>• panel click → audience_update, 0 LLM<br/>• words → manage_audience → AudienceAgent.handle()
    User->>Main: "why did you pick X?" / "add something for young families"
    Main->>AA: manage_audience(user_message)  — router only, never answers itself
    AA-->>User: prose reply, panel re-emitted in place
    User->>Main: "launch" → publish reads the edited audience
```

Both sessions are throwaway. What survives is what the run hands back.

---

## 4. Inside the build run

```mermaid
flowchart TD
    A([suggest]) --> F

    subgraph Ctx["injected once (build_dynamic_context)"]
      direction LR
      C1[BUSINESS brief] --- C2[COUNTRY]
    end

    F[["fetch_audience_segments<br/>3 taxonomies → filter to what can SERVE<br/>ancestry resolved against ALL fetched"]] --> SEL
    SEL["SELECT phase<br/>read the tree, not the names"] -->|nothing fits| SAY[/"say the catalogue has no segment<br/>— never substitute a near match"/]
    SEL --> T2[["submit_segments<br/>every ref re-checked against what was fetched"]]
    T2 --> DEM["DEMOGRAPHICS phase<br/>narrow ONLY where the product excludes people"]
    DEM --> T3[["submit_demographics"]]
    T3 --> DONE([done — no phase re-injected])

    SEARCH[["search_audience_segments<br/>narrow the tree by a phrase"]] -.-> SEL
```

### 4.1 Phases come from what the run produced

There is no step counter. `current_phase(state)` reads state: no segments means SELECT,
segments but no demographics means DEMOGRAPHICS, both means **done** — and a finished phase
stops being injected.

That matters. Re-asking a finished step invites a compliant model to answer it twice, and a
second `submit_demographics` with no arguments would overwrite the narrowing it just recorded.
Deriving the phase also means a retry or a resumed session lands in the same place, rather
than depending on a counter that may not have survived.

### 4.2 What the run refuses to do

- **Invent an id.** Every submitted ref is checked against what was fetched. An invented one
  reaches nobody, or the wrong people, and nothing downstream would report it.
- **Pad to a number.** There is a soft floor, but a thin catalogue is a real answer for a niche
  business. A shortfall is reported, never filled with near-misses.
- **Substitute a loose match.** If the catalogue has nothing, it says so and stops there.

The build run **cannot** offer a custom segment — it does not hold those tools ([§2](#2-two-modes-one-class)).
A thin catalogue ends the build honestly; the segment is drafted afterwards, in manage
([§6.2](#62-custom-segments)), once the user asks. So every custom segment starts as a
sentence the user typed at a panel that already exists.

---

## 5. The record — how "why?" is answerable

Each signal carries its `rationale` and its `path`, which answer the two questions users
actually ask:

- *"Why is this here?"* → the rationale, in the agent's own words at the time.
- *"Who does this actually reach?"* → the ancestry, which is what disambiguates the name.

`lookup_segment` answers from that record, and says plainly when there is none rather than
inventing a past reason — "we did not pick it" is usually "it was not the sharpest fit", not a
judgement worth reconstructing.

---

## 6. The manage flow

The main agent is a **pure router**: it forwards the verbatim message and does not answer
audience questions or classify the request. The agent that recorded the reasons is the one
that has them.

Each turn builds a fresh session holding its **own copy** of the audience, seeded with the
catalogue and a short window of prior exchanges, and hands the edited audience back when the
turn ends — including when the turn later fails, since edits already applied are real and the
user has already seen them in the panel.

```mermaid
sequenceDiagram
    actor User
    participant Main as Main Agent (router)
    participant H as AudienceAgent.handle()
    participant S as throwaway session
    participant Parent as main session

    User->>Main: "why is X here?" / "drop the finance ones"
    Main->>H: manage_audience(verbatim message)
    Parent->>S: OWN COPY of the audience + catalogue + last 4 exchanges
    Note over S: a shared reference would carry exactly ONE edit —<br/>the envelope's writer copies, so later edits would land on a copy
    H->>S: lookup / search / edit_audience
    S-->>Parent: hand back on the way out (even if the turn failed)
    H-->>User: prose reply + panel re-emitted in place
```

The copy matters: the build envelope's writer copies on write, so a shared reference would
carry the first edit and then silently stop propagating.

### 6.1 Catalogue first; emptiness is the signal

```mermaid
flowchart TD
    U[/"user asks for an audience"/] --> S[["search_audience_segments"]]
    S -->|hits| ADD[["edit_audience — add"]]
    S -->|nothing| OFFER["offer a custom segment<br/>never substitute a loose match"]
    U -->|asks for search behaviour outright| OFFER
    OFFER --> D[["draft_custom_segment<br/>READ-ONLY — creates nothing"]]
    D --> ASK{{"show the terms and ASK"}}
    ASK -->|yes| SUB[["submit_custom_segment<br/>refuses without a draft"]]
    ASK -->|no| STOP([nothing created])
    SUB --> TARGET([created and targeted])
```

The agent never classifies "is this a custom-segment request" — the catalogue answers or it
does not, and *that* decides. Which is true to the mechanism: custom segments exist precisely
because the taxonomy does not cover everything.

### 6.2 Custom segments

A custom segment reaches people by **what they search**. Drafting and approving are split
across *different turns* so the user's confirmation sits between them **structurally**, not by
good behaviour — the model cannot add a segment on the turn it was merely asked about one. The
draft is spent on use, so a second "yes" cannot silently duplicate.

A draft lives until it is **used or replaced**. Expiry is keyed on the id stamped at draft
time (`DRAFT_ID_KEY`), so drafting again hands the user's next answer the newer list, while a
draft they never acted on is dropped after one turn.

A run that ends by asking the user to approve terms declares it with
`ToolResult.data["elicited"]`, and names `user_message` as the field the reply fills — the
core contract for a conditionally-eliciting tool (`app/core/tools/base.py`). Two things
follow. Core closes the turn, so the question reaching the user is the only one on screen.
And the orchestrator's next step becomes *hand the reply back to `manage_audience`* rather
than the next campaign step: this agent asked, holds the drafted terms the answer refers to,
and is the only thing that can act on a yes.

Spoken edits take **lists**: `edit_custom_segment` applies one action to every value in the
call, looks all volumes up together, and redraws the panel once.

**Only manage mode can do this.** The build agent holds no custom-segment tools, so a
segment always begins with something the user typed at a panel that already exists.

**Approving still creates nothing at Google.** `submit_custom_segment` records a **blueprint**
in session context under a `pending:customAudience:<label>` ref; the resource is created at
**launch**, by `publish.py`. Approval means "put this in the campaign", not "write to my
account" — and until launch the panel keeps every term, URL and app editable. Creating on
approval would mean the first thing in the account is a segment they were still editing.

```mermaid
sequenceDiagram
    actor User
    participant H1 as handle() — turn 1
    participant Parent as main session
    participant H2 as handle() — turn 2
    participant Panel as craft panel
    participant PUB as publish.py
    participant G as Google

    User->>H1: "target people searching for home loans"
    H1->>H1: draft_custom_segment(themes)<br/>autosuggest → Planner → volume
    H1-->>User: "here are the terms — add it?"
    H1->>Parent: carry the DRAFT (session is discarded)
    Note over Parent: without this the terms die with the session<br/>and turn 2 would create something else

    User->>H2: "yes"
    Parent->>H2: reseed the draft
    H2->>H2: submit_custom_segment — REFUSES without a draft
    H2->>Parent: blueprint + pending: ref, panel re-emitted
    Note over Parent: nothing exists at Google yet

    User->>Panel: edit terms / URLs / apps
    Panel->>Parent: edits the blueprint in place

    User->>PUB: launch
    PUB->>G: create CustomAudience (members = blueprint)
    G-->>PUB: resource name
    PUB->>Parent: pending: ref swapped for the real one
```

Because the ref is what the emitter targets, publish **refuses** to build a payload that
still contains a `pending:` one — a campaign pointing at a segment that was never created
would be accepted by Google as a bad ad group, not rejected as a bad ref.

The panel shows a segment and can **untarget** it, but its `add` searches Google's
catalogue — where a custom segment by definition is not. Building one needs two steps over
real terms and an explicit yes, which a single click cannot express. Untargeting a *pending*
one discards the blueprint; untargeting a *created* one is not deletion — the resource stays
in the account, and the next campaign that wants the same name gets ` (2)` appended rather
than reusing it (`resolve_name`).

Terms come from the **agent's own phrasings** — the user's words first, then how real people
would type the same intent — expanded through autosuggest and then the Keyword Planner, which
expands again and returns volume. Zero-volume terms are dropped: a term nobody searches
reaches nobody.

Google's published limits are enforced **here, not by the API**. `validateOnly` on
`customAudiences:mutate` accepts an over-length keyword and even zero members, so a
"validated" payload can still target something we did not mean.

**A dry run cannot cover this path.** `ADZUMP_PUBLISH_DRY_RUN` drops the pending segment
before validating, since a `pending:` ref is not a thing Google can check — and if it was the
only audience, publish refuses rather than validating a campaign with the audience removed.
Exercising it means a real launch, and the segment that creates is permanent.

### 6.3 Deliberately not built yet

These are **not** API limits — Google supports all three. They are scope calls, recorded here
so the next change starts from what is true rather than from what the code implies.

| Deferred | What Google actually allows | Why it is out for now |
|---|---|---|
| **Editing a created segment's members** | `CustomAudience.members` is mutable: *"If members are presented in UPDATE operation, existing members will be overridden"* (v24 proto). A `customAudiences:mutate` update op with an `updateMask` on `members` replaces the whole list. | Editing is a **pre-launch** flow today. The panel drops its editor once the segment is created, and `apply_member_edit` refuses a non-`pending:` ref. Wiring the update path means a new adapter method, read-modify-write of the full member list, and deciding whether an edit should silently reshape a segment other live campaigns may already target. |
| **Reusing an existing custom segment** | `list_enabled` already reads the account's segments; any of them can be targeted by resource name. | It is only used to avoid a name collision. Offering "you already have one for this" needs a match the user can trust — name similarity is not it, and the members that would decide it are not returned by the list call. |
| **Lookalike segments** | `LookalikeUserList`, seeded from a user list. | Needs a seed list of **1000+** members that the advertiser must already own; nothing in this flow can produce one, so it would be an option that fails for most accounts. |

The pre-launch editor is the reason creation is deferred to launch. If a reviewer asks why
approval does not create the resource, the two facts are linked: post-creation edits are not
built, so the editable window has to sit before creation.

---

## 7. Invariants

These hold whichever path a change arrives by — panel click, spoken edit, or the build itself.

| Invariant | Why |
|---|---|
| One mutation path (`apply_edit`) | a spoken edit cannot break something a click could not |
| Dimension groups partition the positives | a ref in no group is silently untargeted; in two it ANDs and narrows to an intersection |
| Only user lists can be excluded | anything else is not expressible, and accepting it would drop it at emit |
| The last positive cannot be removed | grouped mode has no untargeted fallback — an ad group with no segment cannot run. The emitter refuses the same state, so demographics alone can never stand in for a segment |
| Undetermined is decided per dimension | each `*Dimension` message declares its own `include_undetermined`; one shared flag would tie income — where undetermined is most users outside the few countries Google reports it in — to whatever was decided for gender |
| Income bands are one unbroken span | the API takes any set, but Google's picker is a from/to pair, so a gap in the middle is a campaign the advertiser could never verify in their own account |
| An opaque ref is re-resolved, never trusted | a ref carries no label, kind or ancestry, and a stored snapshot cannot re-check that it still serves |

---

## 8. What the user sees

The panel groups signals by **kind**, because "buying this" and "into this" reach different
people and the user needs that distinction to choose. Each row carries its ancestry as a
breadcrumb, and exclusions get their own section since only user lists can be there at all.

| | panel | chat |
|---|---|---|
| catalogue segment — add | yes, via search | yes |
| catalogue segment — remove | yes | yes |
| **custom segment — create** | **no** (§6.2) | yes |
| custom segment — untarget | yes | yes |
| custom segment — edit its terms/URLs/apps | yes, **until launch creates it** | yes |
| demographics | yes | yes |
| "why is this here?" | — | yes |

The agent is shown the catalogue as an indented **tree** — it is navigating a thousand entries
and structure helps it. The panel gets a **flat list**, because a handful of chosen segments
spread across branches is a list, not a tree.

---

## 9. File map

| File | Holds |
|---|---|
| `agent.py` | the two singletons, `suggest`, `handle`, the phase hook |
| `context.py` | base prompts and the phase machine |
| `tools.py` | the build tools |
| `manage_tools.py` | lookup and edit |
| `custom_segment.py` | draft and submit |
| `catalogue.py` | the targetable catalogue — one loader for every caller |
| `models.py` | signals, demographics, the result, and the limits the API will not enforce |
| `constants.py` | Google's published limits, then our own guards — kept apart on purpose |

Outside this package: `adapters/google/audience_taxonomy.py` (fetch + cache),
`adapters/google/custom_audience.py`, `tools/google/audience_targeting.py` (the build tool),
`tools/google/audience_update.py` (the shared mutation), `tools/audience_management.py` (the
router), `craft.py` (the panel).

---

## 10. Tuning

`constants.py` separates **Google's limits**, which are fixed, from **our guards**, which are
chosen and expected to move with campaign results. `MAX_SIGNALS_PER_KIND` is a ceiling, not a
target — and because optimized targeting expands past whatever is selected, **the agent should
never be judged by how many segments it returns.**

A reasoning model at the balanced tier: choosing an audience is judgement, not lookup. Turns
are bounded at eight — load, select, narrow is three calls, and the rest is room to search and
revise.
