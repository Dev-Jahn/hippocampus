# hippocampus 1.0 — design document (founding)

> 2026-07-31. A reset derived from a full audit of a month of research-cc dogfooding
> (37 sessions, 806 delegations, 479MB), the Opus 5 prompting guide, and an analysis of the
> `/doctor` skill. The previous generation (the 0.x round/review/delegate/brief machinery)
> retired to the `legacy` branch.

## 0. One sentence

**hippocampus is not a process that controls an agent; it is a background organ that gives the
agent perception (observation) and memory (organization). Judgment and invention belong to the
model, and this organ's job is to quietly make sure that judgment happens on top of good evidence.**

> Naming: the project is **hippocampus**. The plugin registration name, the slash prefix, the CLI
> and the state directory are all the short `hippo` (`/hippo:checkup`, `hippo status`, `.hippo/`,
> `HIPPO_*`).

## 1. Principles (every one derived from measured dogfooding)

1. **Brevity** — the real cost of a discipline is not obeying it but *thinking about* it. When an
   agent's attention leaks into the tooling, the project slows down.
   (Measured: of 1,081 calls to the previous generation's CLI, 7.9% were error responses and
   syntax-retry loops; 259 runs of the boundary hook produced no output at all; 8–14KB contract
   documents were re-injected.)
2. **Transparency** — however complex the inside is, it must not leak onto the surface. The only
   test for exposing something: *"does knowing this change the agent's next action?"* If not, silence.
3. **Record, never enforce** — in a world where git guarantees integrity, provenance is one factual
   line in the ledger, not a gate. When a check is genuinely needed, it lives *inside* the service
   rather than as a gate.
4. **The derivability test** — never store what can be re-derived later (the raw telemetry is
   already in the transcript). What we store is only the *judgment*, which exists in that moment
   and then evaporates.
5. **Interpretation is generated, not stored** — a hand-maintained interpretation document goes
   stale (PROGRESS.md proved it). The ledger accumulates facts only; interpretive surfaces such as
   PRIORS are regenerated every time.
6. **Judgment in main, evidence in hippo** — a model adapts on its own when the evidence is in
   front of it, but it cannot remember to collect that evidence. Routing, acceptance and policy
   evolution belong to main; the measurement, accumulation and distillation behind them belong to hippo.
7. **The only good constraint is a reversibility constraint** — instead of constraints that forbid
   actions, invest in making every action undoable (worktree isolation was the single
   best-performing pattern).
8. **Complexity lives in text, not in code** — logic that needs judgment goes into a clerk or skill
   prompt. Less runtime code means fewer corner cases. (`/doctor` — a single 43KB prompt — is the
   existence proof.)
9. **The more obedient the model, the more dangerous a stale instruction is** — a current model does
   not ignore a stale directive, it executes it faithfully (demonstrated by the fail-closed incident
   from contradictory GPU clauses). So a directive carries a lifetime (scope) as a first-class
   concept, and directive hygiene comes before verification machinery.

## 2. The four execution layers

| Layer | What | Cost |
|---|---|---|
| deterministic script | hooks and the CLI — fast and dumb | 0 |
| **clerk** | a hook or cron calls a cheap model (luna/sonnet class) headlessly. Work that needs judgment but not main's context | tokens only, zero main context |
| skill | work that needs main's context, or where main must act on the result | main's context |
| main | routing, acceptance, talking to the user | — |

Clerk guardrails (invariant):

- A hook must launch a clerk **detached** (never block Stop for even a second). Before launching, a
  deterministic prefilter skips the model call entirely for trivial turns.
- **No recursion** (a clerk never spawns a clerk), no writes (beyond its designated output), and a
  timeout is mandatory.
- The transcript is **untrusted input**: a clerk's output goes only into the ledger and generated
  files; it never edits configuration or CLAUDE.md directly (it may propose, no more).
- **Self-metering**: running a clerk is itself recorded in the ledger (`ev:clerk`).
- **A variant of "no silent death"**: the system survives a dead clerk by design, but it never fills
  the gap by inventing content. Failures land in `failures/` and checkup reports them.

## 3. Components

```
runtime (thin):   bin/hippo (shim) + cli/hippo_cli.py + 2 hooks + scripts/{clerk_run,digest_lite,dispatch}
cognition (text): clerks/{turn-scribe,distiller}.md + skills/{hippo,checkup,dispatch}
resident (small): the capsule injected at SessionStart (§6 below)
enforcement:      none
```

### 3.1 Project data (`.hippo/`, per project)

```
.hippo/
  tasks.yaml        # work registry (YAML a human can read and fix)
  ledger.jsonl      # append-only event ledger
  worklog.md        # generated: the human-facing work log the scribe accumulates (date sections)
  PRIORS.md         # generated: the distilled surface the distiller regenerates
  cursors.json      # the scribe's per-session transcript cursors
  failures/         # dumps of clerk output that failed validation (checkup reports them)
  briefs/           # delegation briefs (COMMON.md + one file per task) — see below
  config.yaml       # optional: overrides such as the clerk backend (everything works without it)
```

In a directory with no `.hippo/`, every hook and every CLI command is a **completely silent no-op**
(zero contamination of other projects).

`briefs/` is the one directory hippo does not read. It exists because delegation briefs had no
home: the host hands each session a different absolute scratchpad path, so every wave retyped a
40-character prefix, and a consuming project eventually invented its own fixed path anyway
(measured). A brief belongs next to the state it describes, at a **project-relative, session-stable**
path — `.hippo/briefs/<task>.md` — reachable from the same cwd `hippo dispatch` already requires.
It is called briefs because that is what the rest of the system calls the document, including the
ledger's own `attr: brief`.
It is a convention, not a requirement: a path anywhere else still launches.

### 3.2 Ledger schema (a contract — exactly this)

One line = one JSON object. Common fields: `t` (ISO8601, stamped by the writer), `ev`, and the
optional `src` (`scribe|cli|wrapper`).

```jsonl
{"t":"…","ev":"dispatch","id":"d041","kind":"kernel-impl","exec":"codex/gpt-5.6-sol/high","scope":"pass2 SS-UMMA tensorize","task":"feat/x"}
{"t":"…","ev":"outcome","ref":"d041","result":"refuted","attr":"work","rework":2,"by":"verify/opus","note":"circular oracle reference"}
{"t":"…","ev":"review","id":"r007","base":"abc123f","source":"chatgpt-web","findings":4}
{"t":"…","ev":"review-status","ref":"r007","addressed":"partial","at":"def4567"}
{"t":"…","ev":"directive","id":"gpu-01","text":"use GPUs 0 and 1 only","lifetime":"phase","state":"active"}
{"t":"…","ev":"directive","id":"gpu-01","state":"withdrawn"}
{"t":"…","ev":"clerk","name":"turn-scribe","ms":8100,"ok":true,"tokens":1400}
```

- `outcome.result ∈ {accepted, revised, refuted, no-go, lost}`; `attr ∈ {work, brief, harness}`
  (recommended whenever the result is not accepted — a failure count with no attribution produces a
  lying prior. Measured: the run of REFUTEDs was work, the NO-GO from contradictory GPU clauses was
  brief, and the vanished StructuredOutput was harness).
- `directive.lifetime ∈ {turn, phase, durable}`, `state ∈ {active, withdrawn, expired}`. The last
  event for an `id` is its current state (a derived view is never stored — principles 4 and 5).
  It is `lifetime` rather than `scope` because `dispatch.scope` already means *what a delegation
  covers* — one key with two unrelated meanings is how a schema teaches the wrong thing.
  `withdrawn` is the user changing their mind; `expired` is the clock running out, which today
  only `turn` does. A `turn` directive is live until the **first Stop that begins after it was
  recorded**, and the scribe expires it there before writing the current turn's events — so the
  ones it is about to record get their turn, and main's mid-turn ones get the rest of theirs.
- `directive.id` is lowercase kebab ascii (`[a-z0-9]` joined by `-`) — fail-closed. It is the only
  handle for *superseding* a directive, so it has to be typeable from memory in a project whose
  prose is in any language. For the same reason the scribe is handed the live ids alongside the
  digest (§3.5.5): a clerk that invents a fresh id for an existing subject does not update it, it
  silently forks it. `hippo directive add` derives the id from `--text` and **refuses** when that
  leaves nothing (text with no ascii letters) rather than falling back to a meaningless `directive-<hash>`.
  Derived ids carry a 4-char hash of the full text, so they never collide across different text —
  which makes them content fingerprints, not subject handles. To supersede, pass `--id` yourself.
- `dispatch.exec` is exactly `executor/model/effort`, no whitespace, and `outcome.ref` /
  `review-status.ref` must name an event that exists in this ledger — both fail-closed. These two
  fields are the axes PRIORS aggregates on, so a free-form value is not a small mess, it is a
  column of one. Measured on a real ledger: 26 kinds across 108 dispatches with 19 used exactly
  once, and 24 spellings of exec for 3 real executors — of 11 distinct first slots, 8 were
  category errors, mostly a *launch mechanism* (`background`, `bash`, the wrapper's own path)
  where the agent belonged. And **54% of outcomes joined to no dispatch at all** — half from a
  caller passing a task id, half from ids the scribe invented in the right shape. All of it
  looked like data. Both halves are answered without loosening the join: the scribe is handed the
  recent dispatch ids (§3.5.5) instead of being asked to find them in a digest, and a caller may
  write `--ref task:<task-id>`, which resolves at write time to that task's dispatch still awaiting
  an outcome and **stores the dispatch id**. Two open dispatches for one task is a real ambiguity
  between parallel lanes, so it lists them and fails rather than guessing. Measured afterwards on a
  consuming project that had the no-double-record rule but not the roster: of 66 scribe-written
  dispatches, 30 restated a wrapper launch under a fresh id and 6 reused the wrapper's id exactly,
  inflating the PRIORS denominator ~1.4x — a prompt cannot enforce what its inputs do not contain.
- The **executor** is the agent that did the work (`codex`, `claude`, `fork`, `subagent`,
  `workflow`), not how it was launched: a codex run started in the background is still `codex`,
  and splitting it by launch mechanism scatters the sample the priors depend on. Work with no
  agent — a command main simply ran — is not a delegation and gets no dispatch event. `effort` is
  `low|medium|high|xhigh|ultra|inherit`; `inherit` is for an executor that takes its setting from
  the session that spawned it. **Neither vocabulary is validated on main's writes** — see §3.5.6b
  for why, and for where they are.
- `review.base` is the reviewed commit (`^[0-9a-f]{7,40}$`) — **this is the whole of SHA pinning**
  (principle 3). Without a known sha, do not record the review event at all.
- Validation: `hippo log` checks the required fields per ev, fail-closed. An unknown `ev` is rejected.
- Elapsed time is not a field: derive it from the dispatch/outcome timestamps (principle 4).
- Scale: a month of research-cc = 806 delegations → roughly 1,600 lines ≈ 300KB. grep is plenty.

### 3.3 CLI (`bin/hippo` → `cli/hippo_cli.py`)

- Implementation: a single Python file (PEP 723 inline metadata, deps: PyYAML); `bin/hippo` is a
  `uv run --script` shim (falling back to python3 when uv is absent, with a clear error on failure).
- **Every subcommand has `-h/--help`, and errors attach the usage to stderr** (a direct fix for the
  largest source of friction in 0.x).
- The surface:

```
hippo init                                  # creates .hippo/ and nothing else
hippo status [--inject]                     # one-block summary; --inject is for the hook (silent no-op rule)
hippo task add <id> --title T [--status pending] [--notes N] [--deps a,b]
hippo task set <id> <field> <value>
hippo task done <id> [--note N]
hippo task list [--status s1,s2] [--all] [--json]   # comma multi-filter (fixes a 0.x request)
hippo task show <id> [--json] | task drop <id>
hippo log <ev> [typed flags…]               # dispatch|outcome|review|review-status
hippo log raw '<json>'                      # validate, then append
hippo log tail [-n N] [--ev TYPE]           # read recent records
hippo directive list [--active] [--json]
hippo directive add [typed flags…]          # auto-id derived from text when --id is omitted
hippo directive withdraw <directive-id>
hippo prior show
hippo prior distill [--days N]              # compute the scorecard, then have the distiller
                                            #   clerk write PRIORS.md around it
hippo dispatch --kind K --scope S [--task T] [--] <codex exec args…>   # §3.6
hippo scribe --transcript P --session S     # internal surface the Stop hook calls detached
```

- **Bare-noun default**: omitting the subcommand of `task|log|directive|prior` runs `list|tail|list|show`
  respectively (`hippo task -h` still prints task's own help).
- The mental model (also stated in one line by the top-level `--help`): facts go in through one
  door, `log <event>`; bare `hippo log` reads recent records; `directive` and `prior` are derived
  views recomputed from the ledger every time.
- Task states: `pending|active|done|dropped`. tasks.yaml may be edited by hand and the CLI always
  re-parses it. No acrobatics such as comment preservation (a lesson from 0.x).
- **Identifiers** (conventions, not enforced — recording beats refusing):
  a task id reads `<type>/<kebab-slug>` (`feat/stream-carry`, `fix/cursor-gap`) so it is
  self-explanatory in a commit, a branch name and a ledger line at once. A task type is *not* a
  dispatch `kind`: a task is a unit of work, a kind is what one delegation did, and a single task
  normally produces dispatches of several kinds (`impl`, then `verify`). A dispatch id only has
  to be unique and greppable — `hippo dispatch` mints one, the scribe writes a short one, and a
  hand-written one just needs to be something an `outcome` can name later.

### 3.4 Hooks (exactly two — adding a third is forbidden)

`hooks/hooks.json`:

- **SessionStart** (startup, resume, clear, compact): `hooks/session_start.sh` → silent exit 0 with
  no `.hippo/`; otherwise `hippo status --inject` (the §6 format). Re-injection after a compact is
  what makes it a **context keeper**: live directives survive compaction (the fix for the loss
  measured across 86 compactions).
- **Stop**: `hooks/stop.sh` — parse `transcript_path`, `session_id` and `cwd` from the stdin JSON;
  silent exit 0 with no `.hippo/`; otherwise `setsid hippo scribe … >/dev/null 2>&1 &` and then
  **exit 0 immediately** (<100ms).

### 3.5 The scribe pipeline (inside `hippo scribe`)

1. Non-blocking flock on `.hippo/scribe.lock` — if it is held, just exit (the cursor covers the gap
   on the next run automatically).
1b. Expire every live `turn` directive — before the prefilter, because a turn ended whether or not
   this window was worth a model call, and before the clerk writes, because that is what gives the
   turn directives it is about to record their one turn.
2. Load this session's cursor from `cursors.json` → compress only the lines after it with
   `digest_lite.py` (a light port of the digest logic proven on the 479MB audit).
3. **Deterministic prefilter**: if the digest has no TOOL or USER line, update the cursor and exit
   (zero model calls).
4. Resolve the backend: `config.yaml > $HIPPO_CLERK_BACKEND > automatic (codex/gpt-5.6-luna/low when
   codex exists, otherwise claude -p sonnet) > mock` (for tests). 120s timeout. `$HIPPO_CLERK_MODEL`
   overrides the model on whichever backend is resolved; it is one variable for both, so pin the
   backend when you set it — a model id for one backend is invalid on the other.
5. Prompt = `clerks/turn-scribe.md` + the live directive roster + the recent dispatch roster + the
   digest. Both rosters exist for one reason: an id the clerk coins for a subject that already has
   one forks it instead of updating it, and the digest cannot be relied on to contain the existing
   id. The dispatch roster is also the set an outcome may legally `ref`. Expected output =
   strict JSON:
   `{"worklog": "…", "events": [ …ledger events without t… ]}`.
6. Validation, **per event**: check each event by the same rules as `hippo log` (per-ev key
   whitelist — unknown keys rejected; `t` and `src` are always stamped by the writer; exec shape
   and ref existence as in §3.2). A rejected event is dumped to `failures/` and skipped; the
   turn's other events and its worklog line are still recorded. One hallucinated ref must not
   erase a good worklog — that is what "the dump is the record" means. A failure *of the clerk
   call itself* (no JSON, wrong envelope, nonzero rc) is still all-or-nothing and records
   `ev:clerk ok:false`. Either way the ledger is never contaminated and **the cursor advances**
   (never re-bill the same input forever). **Never fill a gap by inventing content.**
6b. **Two extra rules, on the clerk's output only.** A scribe `dispatch` is rejected when its
   executor is `codex`, or when either closed slot of `exec` is outside its vocabulary
   (`codex|claude|fork|subagent|workflow` / `low|medium|high|xhigh|ultra|inherit`). Main's writes
   are not checked this way and should not be.

   The line is *who observed the value*, not who is trusted. The launcher builds `exec` from its
   own argv and a handed vocabulary holds — measured, 0 malformed in 110, and `kind` has held the
   same way with no validation at all. The scribe infers `exec` from a transcript, and that is
   where a vocabulary stops working: 12 malformed in 45, every one scribe-written
   (`background/CPU/sol-high`, `unknown/GPT-5.6/unknown`, `bash/unknown/unknown`).

   A codex launch belongs to the wrapper, which was present when it happened. The scribe writing
   one yields either a duplicate — every confirmed pair measured was scribe-vs-launcher, 27–167s
   apart — or a record of a launch that bypassed the wrapper, which is a gap better seen as a gap
   than filled with an inferred row that then dilutes the priors. What only the scribe can see, and
   must keep recording, is what the wrapper cannot cover: `fork`, `subagent`, `workflow`, `claude`
   (measured: one `fork` arm of a design duo existed in no other record).

   This is not the enforcement principle 3 refuses. The clerk is a component hippo spawns with its
   tools disabled and whose every event it already parses and may reject; it is not a party whose
   work is being constrained, and §9.2's argument against access lists is about writers hippo does
   *not* control. Two earlier attempts to fix this in the prompt alone both failed, because both
   asked the clerk to **compare** — to spot the launcher's trace in a digest, then to match a
   paraphrased scope against a verbose one (similarity of confirmed pairs ran as low as 0.46 while
   unrelated pairs reached 0.35, so no threshold exists). This rule asks it only to classify the
   single event in front of it.
7. On success → append the events (`src:scribe`), append one line to today's date section of
   worklog.md, update the cursor, and append the `ev:clerk` self-metering event.

### 3.6 The dispatch wrapper (`hippo dispatch`)

A codex exec wrapper: the point that already knows the model and effort from its own argv is
exactly the point to collect them automatically (principle 6). It takes the `--kind`, `--scope` and
`--task` labels, records `ev:dispatch`, prints the dispatch id on stdout's first line, and then runs
`codex exec … < /dev/null` unchanged. It does not record an outcome — the acceptance judgment
belongs to main (through the CLI directly) or to the scribe (by inference).

Why it is a CLI subcommand: a plugin puts only `bin/` on PATH, and `${CLAUDE_PLUGIN_ROOT}` is empty
in an ordinary Bash call. Leaving it in `scripts/` means every consuming project grows its own shim
with a hard-coded cache path (measured). `scripts/dispatch.sh` remains only as a compatibility
forwarder for those shims.

This surface is the one exception to the silent no-op rule: with no `.hippo/` it warns and
**launches anyway**. Its real job is running codex, and swallowing the launch because the record
failed would make it a trap rather than a wrapper. The remaining arguments — including everything
after `--` — are passed through without interpretation; that is codex's grammar, not this CLI's.

### 3.6b The distiller split — the clerk writes the page, the code does the sums

`hippo prior distill` computes the whole scorecard itself — the dispatch ⋈ outcome join, the
first-pass rate per `kind × exec`, the refuted+revised share per exec, attribution, unjoined
outcomes, stale dispatches, open reviews, clerk overhead — and hands the clerk a fact sheet
instead of the ledger. The clerk writes every sentence of PRIORS.md; it writes no number that is
not already on the sheet.

This is not a style preference. Measured on a consuming project (293 events): **all seven cells
the clerk produced disagreed with the ledger**, in both directions, and against the formula the
clerk's own prompt states. Two of the errors changed the advice — `no-go` outcomes counted as
failures invented a worst-performing cell (`perf × xhigh` reported 5/8, actually 2/2, below the
sample threshold entirely), and two verify cells read 100% while each hid a refutation, which is
precisely the number the verification-budget advice is derived from. The join is deterministic, so
a model was the wrong instrument (principles 4 and 6), and PRIORS is read as evidence — a page
whose digits cannot be trusted is worse than no page.

Cells under n=4 get no rate but are still named with their n: a percentage over n=1 reads as
evidence and is not one, while dropping it silently leaves the reader unable to tell a suppressed
cell from an absent one. The raw ledger is deliberately not sent — everything the page needs is on
the sheet, and shipping 300 JSONL lines only offers something to recompute from, badly.

### 3.7 Skills (three)

> Naming: the plugin name is `hippo`, so the slash prefixes are `/hippo:hippo`, `/hippo:checkup`
> and `/hippo:dispatch`; the CLI command (`hippo`), `.hippo/` and the `HIPPO_*` environment
> variables use the same name.

- **`hippo:hippo`** — the main nudge skill. Its description is the fixed line
  "This is your hippocampus. Always use it." — that single line sits in every session's skill list
  and is the only thing that invites use (principle 2: a short description rather than a resident
  injection). The body holds nothing but a brief CLI guide (the grammar in one line, when to reach
  for what, and that nothing is enforced); anything larger than that is a regression.
- **`hippo:checkup`** — a `/doctor`-style project diagnosis. It reads the ledger, PRIORS, failures,
  cursor gaps, recent transcripts and CLAUDE.md/memory, then reports waste patterns (retry loops,
  limit stalls, orphan dispatches), directive hygiene (stale or contradictory directives versus the
  documents) and clerk health (gaps, failures, overhead). Proposals are recommend-first, at most two
  AskUserQuestion rounds, with reversibility stated. Nothing is applied automatically.
- **`hippo:dispatch`** — the revised fleet-dispatch. The key revisions (all grounded in the audit and
  the guide): a verifier **reports everything and main filters** (with a literal-minded model, a
  severity ceiling genuinely hides findings); the verification budget is **proportional to the
  refutation rate in PRIORS** rather than a fixed ritual; no re-verifying one's own work (boundary
  verification only); safety statements about GPUs and memory use neutral vocabulary (10 measured
  content-filter false positives); long runs go to background plus Monitor (no foreground sleep
  polling); grep the shared and individual brief clauses for contradictions before composing them;
  and the gate check and the push must always be separate calls.

### 3.8 Hosts (Claude Code · Codex CLI)

The same repo is a plugin for both hosts. Three of the four layers (CLI, clerk, skill) were always
host-agnostic and only the hooks were host-bound — and that wall came down when codex grew a hook
engine (measured on 0.144.6).

| | Claude Code | Codex CLI |
|---|---|---|
| Manifest | `.claude-plugin/plugin.json` | `.codex-plugin/plugin.json` (names the `skills` and `hooks` paths) |
| Hooks | `hooks/hooks.json` | **the same file** — event keys (PascalCase), matcher, stdin payload fields and SessionStart stdout injection are all identical |
| Plugin `bin/` | added to PATH automatically | **not added** → a skill resolves `bin/hippo` relative to its own SKILL.md |
| Transcript | Claude JSONL | codex rollout JSONL — `digest_lite.py` detects the format from the first lines and reduces both to the same line vocabulary |

Constraints specific to codex (0.144.6):

- **Hooks are skipped silently until they are trusted.** Review and trust them once through
  `/hooks`, or bypass with `--dangerously-bypass-hook-trust`. If the capsule never appears after
  installing, look here first.
- The project `.codex/` layer loads **only in a trusted project** (plugin hooks are unaffected).
- `"async": true` parses but is **skipped** — a Stop hook earns its non-blocking behavior by
  detaching itself (our `stop.sh` already does, with setsid and all three streams closed).
- The `version` in the two manifests must match (a test enforces it).

## 4. What does not exist (the NOT-list — reintroducing any of it requires revising this document)

| Absent | Why (measured) |
|---|---|
| round / round close | No clear scope criterion, plus waiting on review = development stops. The user had already dismantled it with continuous dispatch |
| review packet, ingest, receipt, attestation | Replies stay raw in the chat (a review saved to a file dies in attention — demonstrated across 6 rounds of whack-a-mole). One field, `ev:review.base`, is enough pinning |
| a delegate surface, role-binding config | Routing comes from main's judgment plus the evidence in PRIORS. Freezing it in config is the source of stale-instruction incidents |
| typed brief facts, assurance DAG | Intent belongs in short documents and conversation. Drift is handled by making it visible, not by control |
| PreToolUse/PostToolUse/UserPromptSubmit hooks | Latency on every call, plus hooks measured to produce no output. Two hooks is the ceiling |
| OPERATING CONTRACT-style resident injection | 8–14KB re-injected, measured. The only resident thing is the one block in §6 |
| a hand-written PROGRESS.md | It goes stale. Replaced by worklog (generated) + ledger (facts) + PRIORS (distilled) |
| typed refusal gates, frozen sidecars, remote verify | Record, never enforce (principle 3) |
| installing a cron job automatically | A user who wants one sets it up. The plugin does not own a schedule |

## 5. After the MVP (recorded only; not being built now)

- staleness resolver: a stale review reply → a delta digest against the current HEAD (a clerk). The
  base-SHA existence check lives *inside* that service.
- watchman: detect dispatches with no outcome and limit stalls → notify over telegram (mostly
  deterministic).
- Fold a read-oriented exposure of the distilled result (the habit of running `prior show` right
  before delegating) into the dispatch skill.
- Let delegated executors read and write the same `.hippo/` main does, so a brief carries the work
  and not the whole context — drafted in full in §9.

## 6. The capsule (in full — anything larger is a regression)

```
[hippo] tasks 3 open · directives 2 live · priors 07-31 · worklog 07-31
· live(durable): keep review replies in context, never save them to a file
· live(phase): use GPUs 0 and 1 only
· in flight: NVFP4 factor-rebasing 6-part (0h42m), r2 UNCERTAIN 4건 (0h12m)
· last: merged the v2 Pareto duo, full gate green (1421)
```

`in flight` is delegations launched and not yet judged, within 24h, **counting only what a
launcher wrote** (`src` `wrapper`/`cli`). It is the one part of "where was I" that is a fact rather
than a plan, and it is the reason it belongs here instead of in a hand-kept file: measured on a
consuming project, this query returned exactly the three lanes that project was listing by hand,
while the same query over every writer returned 16 — scribe-inferred rows swamp it. With nothing
flying the line is absent; a dispatch older than a day is not in flight but forgotten, and
`prior distill` already reports those as open items.

Everything else such a file carries has a home already: the current phase is a `phase` directive,
what shipped is the worklog, ordering is `task deps`, and a merge hazard belongs in the brief for
the lane that will cause it (dispatch skill §5). A re-entry document is what appears when those
surfaces go unused — not a gap in this design.

Two rules govern the directive block:

- **Nothing is folded away.** Every active directive is injected, in full, durable first. A user
  ruling that is invisible at session start is effectively not there, and a cap does not fix that
  problem — it makes it quiet. Newlines are collapsed (a multi-line value would break the
  one-per-line shape); the text itself is never cut.
- **Volume is a warning, never a limit** (principle 3). The write always goes through; what follows
  it, on stderr, is what the live set now costs: any directive over 200 chars, named by id and
  size → compress it and re-add under the same `--id`; 8 or more live, or 1600 characters in total
  → compress, or withdraw the stale ones.
  The notes describe the **whole live set, not the text just written**, and both `directive add`
  and `directive list` emit them. Warning only at write time is the failure this fixes: the
  expensive directives are usually the ones already resident, so the one moment they were
  mentionable had already passed and every session went on paying in silence.

## 7. Testing policy

Tests are a means: CLI round trips (add/set/list with multi-filter/done), log validation (valid and
malformed, fail-closed), the directive lifecycle, status --inject (present and absent, silent), the
whole scribe pipeline (mock backend: cursor advance, ledger append, worklog append, lock contention,
malformed JSON isolated into failures), and digest_lite basics. Around twenty of them. `uv run pytest`.

## 8. Salvage record

- The audit's digest logic (digest.py, proven on 479MB) → `scripts/digest_lite.py`
- The task registry concept (1,081 voluntary uses even after the plugin was switched off = revealed
  preference) → a thin rewrite
- The body of the fleet-dispatch skill → the revised `skills/dispatch`
- Everything else from 0.x → retired to the `legacy` branch. Audit report:
  `~/workspace/b200-2-research-cc-audit/`

## 9. The shared brain (draft for 2.0 — nothing here is built)

Today a delegated executor starts blind. Everything it needs — the live directives, the task it
serves, what has already been tried — is re-typed by hand into a brief, which is why `COMMON.md`
exists and why briefs keep growing. The abstract *intent* behind an instruction does not survive
that transcription at all; only the instruction does.

The proposal is to let executors read and write the same `.hippo/` main does. Hierarchy is kept —
main still decides — but the memory is one memory.

### 9.1 The plumbing already exists

`.hippo/` is found by walking **up** from cwd (§3.3), and an editing lane's worktree is
`{repo}/.claude/worktrees/<name>` — inside the repo. So an executor invoking `hippo` from its
worktree already resolves the project's real `.hippo/`. Nothing needs to be wired. What is missing
is entirely policy, which is the reason this is worth writing down before building it.

### 9.2 Observation and verdict, not read and write

An access-control list is the wrong instrument, and would be the first thing in this project to
break principle 3. It also would not work: an executor has a shell, and `>> .hippo/ledger.jsonl`
costs it nothing. Pretending to prevent what cannot be prevented is the failure this repo keeps
declining.

The distinction that does hold is not read/write but **what kind of statement is being made**:

| An executor may record | Why |
|---|---|
| what it observed — what it did, what broke, that the premise did not reproduce | it is the only witness; today that reaches main only through a report file |
| — but not that its own work is accepted, or that a task is done | it cannot be the judge of its own output (constitution: main owns acceptance) |

This is a statement about honesty, not permission. An executor saying "it works" *is* a claim, not
a fact, and hippo only has to render it as one.

Mechanism: `src` gains a fourth value, `executor` — "the agent that did the work wrote this",
using §3.2's existing term rather than coining one. One environment variable carries it, set by the
dispatch wrapper into the child's environment:

```
HIPPO_DISPATCH=d041     # ⇒ src=executor, and `ref` defaults to d041
```

A self-reported outcome (`src=executor`, `ref` = the writer's own dispatch) is therefore
distinguishable forever from main's judgment on the same dispatch (`src=cli`), and derived views
fold only the latter into acceptance. Two keys, no enforcement. An executor that forges `src=cli`
succeeds — and has now lied in an append-only file, which is a far better place to be than a
blocked write.

### 9.3 The real risk is belief propagation, not writes

Executor A records "implemented"; thirty minutes later executor B is injected with the shared
capsule and reads it. **An unverified claim has become the network's shared fact**, and no
verification gate sits between them. Today this cannot happen because lanes cannot talk to each
other; the isolation that costs so much is also carrying a safety property nobody wrote down.

So the injected capsule must never render an executor-sourced statement as a flat assertion. The
subject has to survive: `· d041 claims: pass2 tensorize lands`. Principle 5 is what makes this
cheap — the view is generated on every read, so one rendering rule changes what the whole network
believes.

### 9.4 Directives need an audience axis

`lifetime` is a **time** axis. The missing one is **audience**, and it is invisible until directives
start reaching executors. Of this repo's own live set: "answer in Korean" governs how main speaks
to the user and is noise or worse to an executor; "every file in the repo is written in English"
is something an executor must know and is today hand-copied into COMMON.md; "bump patch only"
concerns a release an executor never performs.

Injecting all of it into six parallel executors multiplies principle 9 rather than repeating it:
an executor is **more** obedient than main — a cheap model, no context, and no channel to say "this
constraint does not fit what I am looking at". A directive like `no-premature-surrender` (324 chars,
measured on a consuming project) handed to a literal-minded worker is a token fire.

`directive --audience main|executor|all`, defaulting to `all`. A narrow default fails by silently
hiding a constraint from the worker that needed it; a wide default fails by noise, which the
existing volume nudges (§6) already surface.

### 9.5 Depth, so the spiral is visible instead of forbidden

Some work is one task and simultaneously the size of a whole session. Such a dispatch should be
allowed to orchestrate: main becomes the orchestrator of orchestrators.

This directly inverts the dispatch skill's "no re-delegation" clause, which exists because of a
measured loss (one lane spiralled through 336k tokens and produced zero commits). The clause should
not be deleted — it should be indexed:

- `--depth 0` (default) — unchanged. The brief carries the no-re-delegation clause automatically.
- `--depth 1` — may spawn. Its children are depth 0 and receive that clause.

Recording depth makes an unintended depth 2 an event in the ledger rather than a prohibition nobody
can check. Model routing for such a dispatch (the reason to pin a strong model at the top and a
cheap one underneath) belongs in a visible `.hippo/routing.yaml`, not compiled into the CLI: it is a
claim about *prices*, and prices go stale between releases.

### 9.6 PRIORS has no cost axis, and that is the actual blocker

PRIORS aggregates quality — refutation and acceptance rates — over `kind × exec`. That was the
right question while every dispatch cost roughly the same. It stops being the right question in two
ways at once: a depth-1 dispatch is a *fleet*, not a lane, and filing it beside a single cheap
dispatch under the same `kind` makes the prior lie; and once a cheap tier is genuinely cheap, the
question changes from

> which exec performs best → **what is the cheapest exec that clears the bar**

which the current schema cannot answer at all. `codex exec` reports its usage and the wrapper
already reads that stream, so recording tokens on the outcome is collection at the point that
already knows (principle 6). Cost per *accepted* outcome is then derivable, and §9.5's routing stops
being a guess. It also composes with §9.2 for free: children writing to the same ledger under a
parent's dispatch id means a fleet's cost sums itself.

### 9.7 Consequences to settle before building

- **The executor gets no scribe.** Running the Stop hook per lane multiplies clerk cost by the wave
  width, and the hook cap is two (§3.4). If a depth-1 orchestrator's reasoning is worth keeping, the
  distillation belongs in the dispatch wrapper at lane exit — not in a third hook.
- **A discarded lane's events survive in the ledger while its code does not.** This is a feature —
  "this approach was tried and failed" is recorded nowhere today — but it requires the dispatch to
  carry an ending (merged / discarded / killed), or a derived view will present abandoned work as
  done.
- **Concurrent writers.** Short appends are atomic; whole-file rewrites are not. `tools/ledger_edit.py`
  guards on the scribe lock plus a size re-check, and the size re-check is the one that still holds
  when the writers are executors rather than the scribe.
- **The surface handed to an executor should be two commands** — `hippo status --inject` and
  `hippo log outcome`. Not `task`, not `directive add`, not `prior`. A small surface is a small
  policy; most of §9.2 is unnecessary if there is nothing to misuse.

### 9.8 Order

§9.2–9.4 are the data plane and stand alone; §9.5–9.6 are the control plane on top of it and are
half-blind without it (an orchestrated fleet with no shared memory starves its own children). Build
the data plane first. Its minimum is two things — the `executor` src value and the audience axis —
which is small enough that it may not need a major version at all.
