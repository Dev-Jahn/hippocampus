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
   from contradictory GPU clauses). So a directive carries an age, shown never resolved, and
   directive hygiene comes before verification machinery.

## 2. The five execution layers

| Layer | What | Cost |
|---|---|---|
| deterministic script | hooks and the CLI — fast and dumb | 0 |
| **clerk** | a hook calls a cheap model (luna/sonnet class) headlessly. Work that needs judgment but not main's context | tokens only, zero main context |
| **judge** | a typed-judgment call (TypeSafe Jev) made inside a CLI path over text the code already holds — classification, selection, scoring. It returns probabilities, never prose | treated as zero, and outside both subscriptions |
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

Judge guardrails (invariant):

- **No hook of its own** — it rides a surface that already exists (the cap of two hooks is
  untouched, §3.4).
- It **never writes a verdict, an outcome, or a directive state**. Its answers are evidence that a
  code policy thresholds; the policy is what decides, and it is readable in one place.
- Every judge-backed path is a **pure addition on top of the deterministic one**. When the judge is
  unavailable the path continues exactly as before, and the gap is either recorded
  (`ev:clerk name:jev-*` with `ok:false`) or simply absent: with no key there is no judge, no row,
  no note and no changed output.
- **It is opt-in by the key alone** — no setting, no prompt, nothing a user has to decide (§3.9).
- The state is **pre-filtered by code**. Accuracy drops with irrelevant material in the state, so
  code decides what is relevant — and never shrinks relevant volume, which is the whole point of
  using this instrument rather than a small classifier.
- **Nothing numeric or temporal is asked of it** (jaggedness: no arithmetic, no counting, no dates).
- Question specs are text in `clerks/jev/*.yaml` (principle 8), with thresholds under their
  `policy` key.

## 3. Components

```
runtime (thin):   bin/hippo (shim) + cli/hippo_cli.py + 2 hooks + scripts/{clerk_run,digest_lite,dispatch}
cognition (text): clerks/{turn-scribe,distiller}.md + clerks/jev/*.yaml + skills/{hippo,checkup,dispatch}
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
home: the host hands each session a different absolute scratchpad path, so every batch retyped a
40-character prefix, and a consuming project eventually invented its own fixed path anyway
(measured). A brief belongs next to the state it describes, at a **project-relative, session-stable**
path — `.hippo/briefs/<task>.md` — reachable from the same cwd `hippo dispatch` already requires.
It is called briefs because that is what the rest of the system calls the document, including the
ledger's own `attr: brief`.
It is a convention, not a requirement: a path anywhere else still launches.
`hippo init` seeds `briefs/COMMON.md` with the lane bootstrap alone — run `status --inject`
first, re-run it after a compaction — written once and never read back: the usage contract
itself lives in the capsule's `report:` line (§6), one generated source instead of prose every
brief re-types.

### 3.2 Ledger schema (a contract — exactly this)

One line = one JSON object. Common fields: `t` (ISO8601, stamped by the writer), `ev`, and the
optional `src` (`scribe|cli|wrapper|executor`).

```jsonl
{"t":"…","ev":"dispatch","id":"d041","kind":"kernel-impl","exec":"codex/gpt-5.6-sol/high","scope":"pass2 SS-UMMA tensorize","task":"feat/x"}
{"t":"…","ev":"outcome","ref":"d041","result":"refuted","attr":"work","rework":2,"by":"verify/opus","note":"circular oracle reference"}
{"t":"…","ev":"review","id":"r007","base":"abc123f","source":"chatgpt-web","findings":4}
{"t":"…","ev":"review-status","ref":"r007","addressed":"partial","at":"def4567"}
{"t":"…","ev":"directive","id":"gpu-01","text":"use GPUs 0 and 1 only","state":"active"}
{"t":"…","ev":"directive","id":"gpu-01","state":"withdrawn"}
{"t":"…","ev":"clerk","name":"turn-scribe","ms":8100,"ok":true,"tokens":1400}
{"t":"…","ev":"usage","ref":"d041","tokens":1100000,"tin":1000000,"tcached":400000,"tout":100000,"model":"gpt-5.6-sol"}
{"t":"…","ev":"triage","ref":"d041","route":"accept-candidate","verify":false,"p":{"done":0.95,"blocked":0.02,"ask":0.03,"creep":0.04,"evidence":0.91,"risk":1.0}}
```

- `outcome.result ∈ {accepted, revised, refuted, no-go, lost}`; `attr ∈ {work, brief, harness}`
  (recommended whenever the result is not accepted — a failure count with no attribution produces a
  lying prior. Measured: the run of REFUTEDs was work, the NO-GO from contradictory GPU clauses was
  brief, and the vanished StructuredOutput was harness). A second outcome for one dispatch is
  legal from main — a deliberate re-verdict after rework is main's call — but it gets a stderr
  note, because the routing table is a *first-pass* rate and keeps reading the first verdict.
  From the scribe a second outcome is rejected outright (§3.5.6b).
- `review-status.addressed ∈ {full, partial, none}` — closed like `result`, because it is the one
  field reviews are folded on ("not fully addressed"): a free-form "fully" would read as open
  forever. `hippo log review` prints a one-line stderr reminder to record the closing
  `review-status` when the findings are dealt with — without it, the loop's second half had no
  surface that ever mentioned it.
- `src=executor` is "the agent that did the work wrote this" (§9.2, built in 1.8.0): the wrapper
  plants `HIPPO_DISPATCH=<dispatch id>` in the lane's environment, every hippo write from there
  arrives as executor, and `log outcome` defaults its `ref` to that id. A self-reported outcome
  is a **claim, not a verdict**: it joins no priors cell, pays no attribution, does not land the
  in-flight entry (which renders it as `· claims accepted`, subject visible, latest claim wins —
  §9.3), does not trip the scribe's re-judge rule, and draws no second-verdict note when main
  then judges it. The same line holds for directives: a lane's directive events are recorded
  but never fold into the live set — a lane may propose, not rule. Both are one rendering rule
  in a generated view (principle 5), not a blocked write.
- `ev:usage` (§9.6, built in 1.10.0) is what a lane actually cost — written by the wrapper at
  lane exit from what it observed (the rollout's cumulative token count with billing breakdown;
  the "tokens used" footer as total-only fallback; nothing → no event, a gap is a gap).
  `ref` joins a dispatch fail-closed. Dollars are computed at read time from the shipped
  `prices.yaml` (USD per 1M, refreshed each release, `as_of` printed by PRIORS so staleness is
  visible); a model off the sheet renders as unpriced and named, never guessed. PRIORS' routing
  table carries tokens, $ and $/accepted per cell — the question it unlocks is "the cheapest
  exec that clears the bar". A batch's total is derivable by summing children over `parent`.
- `ev:triage` (1.14.0) is the judge's reading of a finished lane (§3.6) — written by the wrapper
  (`src=wrapper`) at lane exit, from single dispatch and batch alike, and rejected from the
  scribe by the same rule as `usage`: the wrapper observed it. `route ∈ {accept-candidate,
  escalate, no-go-candidate, failed}`, `verify` is a bool, `cause ∈ {capability, spec,
  environment, transient}` or absent, and `p` is a flat map of the compact probabilities the
  route was computed from (`done`, `blocked`, `ask`, `creep`, `evidence`, and `risk` on its 0–3
  ladder — numbers only). `ref` joins a dispatch fail-closed. A judge failure writes nothing:
  the `ev:clerk name:jev-harvest ok:false` row is the gap. A route is evidence of a check rc's
  standing, never a verdict — and recording it closes a loop: the in-flight line shows the latest
  route beside the lane's claim, and PRIORS tables each triaged dispatch's route against main's
  first verdict (§3.6b), measuring the judge the way it measures executors.
- `dispatch.depth` (int, absent = 0) and `dispatch.parent` (§9.5, built in 1.9.0): depth is how
  far a lane may re-delegate — 0 is a leaf whose capsule says so, 1 may spawn children that
  start at 0. `parent` is stamped by the wrapper from `HIPPO_DISPATCH` when a launch happens
  inside a lane, which turns an unintended depth-2 into a ledger event instead of a prohibition
  nobody can check. Neither field is enforced anywhere.
- `directive.audience ∈ {main, executor, all}` (absent = all — §9.4, built in 1.8.0): *who* a
  directive binds. `status --inject` filters by reader —
  inside a lane (HIPPO_DISPATCH set) the capsule carries `executor|all`, everywhere else
  `main|all`. A re-add that omits the flag keeps the stored value, like every directive field
  (update semantics).
- `directive.state ∈ {active, withdrawn, expired}`; an active directive requires `text` only. The
  last event for an `id` is its current state (a derived view is never stored — principles 4 and
  5). A directive lives until `hippo directive withdraw` — `withdrawn` is the user changing their
  mind; `expired` survives only on old rows.
- `directive.lifetime` is **retired** (1.14.0): still an allowed key, because old rows carry it,
  and written by nothing — `directive add --lifetime` is accepted and ignored with a stderr note,
  and a scribe event that still carries one has it dropped. Measured across 28 projects on 7
  machines: of 147 `phase` directives, 63 of the 75 still live were older than 14 days (the
  phase-only aging nudge produced no withdrawals) and 34 of the 72 withdrawn went within 3 days —
  `phase` was used to mean "temporary" and then never closed; `turn` was used 55 times and 54 of
  those expired by the clock, a rule for the next answer that was already in the context. The
  concept cost a flag on every add, a table in the scribe prompt, expiry code in the scribe and a
  §6 ordering rule, and bought nothing a plain age display does not. The view keeps one old rule:
  a row whose latest active write said `turn` is **never live** — it expired at the next Stop
  under the old rule, and the view says so instead of a migration rewriting the ledger.
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
  `low|medium|high|xhigh|max|ultra|inherit`; `inherit` is for an executor that takes its setting from
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
- `.hippo/` is `$HIPPO_DIR` when that names a directory — the dispatch wrapper plants it in every
  lane's environment, so a lane reports to the ledger that launched it wherever its cwd is
  (measured: 141 of 1,167 lane outcomes, 12%, were refused for a ref the lane's own walk had
  never seen). Otherwise it is found by walking up from cwd. A `.git` *directory* is the ceiling (never adopt a
  project from beyond a real repo root); a `.git` *file* — a linked worktree — is walked through,
  so a lane calling hippo from its worktree resolves the project's real `.hippo/` (§9.1, wired
  in 1.8.0). The hooks walk conservatively for ordinary sessions (a session opened *inside* a
  worktree gets no capsule and no scribe) with one exception: under `HIPPO_DISPATCH` —
  a dispatched lane — SessionStart walks through the worktree's `.git` file too, which is what
  re-injects the capsule after the lane's own compaction (§3.4, 1.8.1).
- **Every subcommand has `-h/--help`, and errors attach the usage to stderr** (a direct fix for the
  largest source of friction in 0.x).
- The surface — the block `skills/hippo/SKILL.md` carries and the README repeats, plus the one
  internal command. A test parses the skill's block against `build_parser` both ways (every
  line parses, every command, flag and enum value is on a line), so it cannot drift again:

```
hippo init
hippo status [--inject]
hippo task add <type>/<slug> --title T [--notes N] [--deps a,b]
    [--status pending|active|done|dropped]
hippo task set <id> title|status|notes|deps <value>      # positional: no --flags
hippo task done <id> [--note N]
hippo task list [--status s1,s2] [--all] [--json]
hippo task show <id> [--json]
hippo task drop <id>
hippo log dispatch --id D --kind K --exec executor/model/effort --scope S
    [--task T] [--depth N] [--parent D]
hippo log outcome --ref <dispatch-id>|task:<task-id> --result accepted|revised|refuted|no-go|lost
    [--attr work|brief|harness] [--rework N] [--by executor/model] [--note N]
hippo log outcome --from-batch <journal> [--dry-run] < verdicts.jsonl
hippo log review --id R --base <sha> --source S --findings N
hippo log review-status --ref R --addressed full|partial|none [--at <sha>]
hippo log raw '<json>'
hippo log tail [-n N] [--ev TYPE]
hippo directive add --text T [--id kebab-id] [--audience main|executor|all]
    [--state active|withdrawn|expired]
hippo directive list [--active] [--json] [--hygiene]
hippo directive withdraw <id>
hippo prior show
hippo prior distill [--days N]
hippo dispatch --kind K --scope S [--task T] [--depth N] [--fast] [--] <codex exec args…>
hippo dispatch --batch <manifest.yaml> [--dry-run]
# a bare noun reads: task → list, log → tail, directive → list, prior → show
hippo scribe --transcript P --session S   # internal: the Stop hook calls it detached
```

- **Bare-noun default**: omitting the subcommand of `task|log|directive|prior` runs `list|tail|list|show`
  respectively (`hippo task -h` still prints task's own help).
- The mental model (also stated in one line by the top-level `--help`): facts go in through one
  door, `log <event>`; bare `hippo log` reads recent records; `directive` and `prior` are derived
  views recomputed from the ledger every time.
- Task states: `pending|active|done|dropped`. tasks.yaml may be edited by hand and the CLI always
  re-parses it. No acrobatics such as comment preservation (a lesson from 0.x).
- `deps` answers one question — *what can I start now* — and `task list` is where it answers it:
  a task with an unfinished dep gets a `waiting on:` line under it, nothing otherwise. A dep naming
  no task is printed with a `?`, because that is the same silent shape as a dangling `ref`.
  Status is never derived from it: a manual `blocked`-like state would mean "waiting on something
  outside the registry" and that is a different fact, so the two are kept apart (principle 4 —
  derive the mechanical part, store only what cannot be derived). Until 1.7.2 nothing read this
  field at all, and a consuming project used it in 4 of 98 tasks while keeping its real ordering in
  a hand-written document — the rational response to a field that does nothing, not evidence that
  ordering was unwanted.
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
  no `.hippo/`; otherwise `hippo status --inject` (the §6 format), wrapped as
  `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "…"}}`. Both
  hosts read that shape; codex 0.146 *requires* it (0.144 took the bare text, so the capsule
  silently stopped arriving on a host upgrade — the failure mode a shared contract has: it moves
  under you). One envelope for both hosts rather than a branch on which one is running: the
  branch would be a guess that rots the moment either contract moves. Re-injection after a compact is
  what makes it a **context keeper**: live directives survive compaction (the fix for the loss
  measured across 86 compactions). Under `HIPPO_DISPATCH` the walk crosses a worktree's `.git`
  file (§3.3), so a dispatched lane whose host fires hooks gets the same treatment — its
  audience slice plus the `report:` line, at start and after every compaction. The lane-side
  gap this closes is the same measured loss, unhandled: a long lane compacts and its brief's
  constraints evaporate.
- **Stop**: `hooks/stop.sh` — parse `transcript_path`, `session_id` and `cwd` from the stdin JSON;
  silent exit 0 with no `.hippo/`; otherwise `setsid hippo scribe … >/dev/null 2>&1 &` and then
  **exit 0 immediately** (<100ms). Under `HIPPO_DISPATCH` it exits at once instead: the executor
  gets no scribe (§9.7) — a per-lane Stop would multiply clerk cost by the batch width, and a
  scribe over a lane's transcript would mint src=scribe rows (verdicts) out of a worker's
  self-narrative.

### 3.5 The scribe pipeline (inside `hippo scribe`)

1. Non-blocking flock on `.hippo/scribe.lock` — if it is held, just exit (the cursor covers the gap
   on the next run automatically).
2. Load this session's cursor from `cursors.json` → compress only the lines after it with
   `digest_lite.py` (a light port of the digest logic proven on the 479MB audit).
3. **Deterministic prefilter**: if the digest has no TOOL or USER line, update the cursor and exit
   (zero model calls).
3b. **The judge gate** (§3.9). The prefilter answers *did anything happen*; this answers *is any of
   it the scribe's business*, which is a judgment and therefore not a regex. `clerks/jev/scribe-gate.yaml`
   asks five yes/no questions over the digest alone — a standing user instruction, a verdict on
   delegated work, a pasted external review with a sha, a worker launched, substantive work
   finished or failed. The call is self-metered like any clerk
   (`ev:clerk name:jev-gate ok ms tokens`, `src:scribe`), whether it succeeded or not.
   The gate **never decides whether the clerk runs**. The first draft skipped the clerk when
   every answer was low; measured before shipping (2026-09-23) on 90 windows of a consuming
   project's transcripts, cut just before each scribe-written event, a 0.2 floor cleared for
   18/19 directives, 16/19 outcomes, 9/9 dispatches and 8/12 reviews — so a skip rule would have
   dropped about one real event in seven — while of 31 windows where the clerk recorded nothing,
   exactly one had every answer under 0.2: one clerk call in thirty saved. Almost every window
   that passes the prefilter holds substantive work, and the worklog line is wanted for it. So the
   probabilities ride into the clerk's payload as a `# gate hints` section between the dispatch
   roster and the digest, marked advisory, with the digest still the only evidence (the clerk
   prompt says so in its own words), and the token saving of this layer is nil by design — its
   value is the hints and the metered signal checkup can read. A failed judge inserts no section, so the clerk sees exactly
   what it sees today, and the reason lands on stderr. With the backend off the gate does not
   exist: no row, no section, no change in behavior. Measured live on the suite's fake transcript
   (2026-09-23): 570–640ms and 873 tokens for all five questions in one request.
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
6b. **Four extra rules, on the clerk's output only.** A scribe `usage` is rejected — the
   wrapper observed the cost and was there when the lane ran. A scribe `dispatch` is rejected when its
   executor is `codex`, or when either closed slot of `exec` is outside its vocabulary
   (`codex|claude|fork|subagent|workflow` / `low|medium|high|xhigh|max|ultra|inherit`). A scribe
   `outcome` is rejected when its `ref` already has a verdict: "at most one outcome per
   dispatch" was in the prompt and the roster marked the judged ids, and the clerk re-judged
   anyway — measured on this repo's own ledger, one dispatch judged `revised` was re-judged
   `accepted` by two later scribe runs, and a verdict main had recorded through the CLI was
   restated by the scribe 26 seconds later. A prompt cannot hold a rule its writer does not
   check. Main's writes are not checked either way and should not be (a deliberate re-verdict
   is main's call; it gets a stderr note, §3.2).

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
8. **Auto-distill** (1.14.0), after step 7 or after a failed clerk call alike: the distiller runs
   exactly as `hippo prior distill` does when PRIORS is **due** — `PRIORS.md` is missing or older
   than `DISTILL_STALE_DAYS = 7`, **and** at least `DISTILL_MIN_NEW = 5` verdicts (outcomes that
   are not executor claims — the set PRIORS reads) landed after the last `ev:clerk
   name:distiller` row, or ever when there is none. Both halves are read from the ledger, never
   a counter file, and the distiller row is the self-metering. The count restarts after a
   *failed* distiller row too, for the reason the cursor advances in step 6: a dead clerk must
   not be re-billed at every Stop. The scribe already holds the lock and runs detached, so the
   distiller's 300s blocks nothing. Measured across 28 projects (2026-09-23): `prior distill`
   had run 18 times ever, in 10 projects, 5 of them typed by hand, and PRIORS — the routing
   evidence the dispatch skill tells main to read — was stale in every one. A schedule was ruled
   out (§4); the Stop that already drives the scribe is the clock.

### 3.6 The dispatch wrapper (`hippo dispatch`)

A codex exec wrapper: the point that already knows the model and effort from its own argv is
exactly the point to collect them automatically (principle 6). It takes the `--kind`, `--scope` and
`--task` labels, records `ev:dispatch`, prints the dispatch id on stdout's first line, and then runs
`codex exec … < /dev/null`, forwarding every line unmodified. A `--fast` flag prepends
`-c service_tier="fast"` to the codex arguments — a per-launch latency choice carried in argv like
the rest of codex's grammar, invisible to the exec axis. It also plants
`HIPPO_DISPATCH=<id>`, `HIPPO_DEPTH` and `HIPPO_DIR`
in the child's environment — the whole of the executor data plane's wiring (§9.1, §9.2, §9.5). A
launch made from inside a lane records that lane as `parent`. Since 1.10.0 the wrapper is a
pass-through rather than an exec: it stays alive to *read* (never rewrite) the stream — the
banner's session id and model, the "tokens used" footer — and at lane exit records `ev:usage`
from the rollout or the footer (§9.6). Those markers ride codex's *stderr* (measured, 0.144.6;
stdout carries only the agent's own output) — so stderr is the one piped-and-forwarded stream,
and stdout passes through untouched. 1.10.0–1.11.0 read stdout instead, saw no session id, and
silently recorded nothing — the stub test had encoded the wrong stream; fixed in 1.11.1. It does not record an
outcome — the acceptance judgment belongs to main (through the CLI directly) or to the scribe
(by inference); what the lane itself records under that id is a claim, never the verdict.

Why it is a CLI subcommand: a plugin puts only `bin/` on PATH, and `${CLAUDE_PLUGIN_ROOT}` is empty
in an ordinary Bash call. Leaving it in `scripts/` means every consuming project grows its own shim
with a hard-coded cache path (measured). `scripts/dispatch.sh` remains only as a compatibility
forwarder for those shims.

**The fan-out circuit breaker** (1.11.0) is the one check that lives inside this service
(principle 3 allows exactly that), and it is denominated in **dollars, never in lanes** — a
thousand luna-class children are a legitimate batch, the fiftieth sol-class one is the measured
disaster shape (the 336k-token re-delegation spiral, and its §9.5 sequel: an expensive model
launching hundreds of expensive children). Lane-origin launches only (`parent` present): the
children's cost is summed per parent per 24h — **measured usage where a child has finished, a
nominal reservation where it has not** (1 Mtok in + 0.2 Mtok out at the child's model's sheet
price; a burst launches everything before anything finishes, so a measured-only breaker would
see $0 exactly when it matters). Past half the budget the wrapper warns on stderr with the
arithmetic; past the budget ($500 by default — `config.yaml` `dispatch.max_wave_usd`) it
refuses the launch and tells the lane to report no-go instead. An unknown model reserves at
the sheet's top tier — a typo must not dodge the breaker. The nominal figures are the guard's
arithmetic, not data: nothing of them reaches the ledger. **Main is never gated** — a batch of
any size launched from the session is main's judgment, and gating it would be the enforcement
this design rejects. A lane that bypasses the wrapper still succeeds: this stops accidents,
not adversaries, and every measured failure was an accident.

This surface is the one exception to the silent no-op rule: with no `.hippo/` it warns and
**launches anyway**. Its real job is running codex, and swallowing the launch because the record
failed would make it a trap rather than a wrapper. The remaining arguments — including everything
after `--` — are passed through without interpretation; that is codex's grammar, not this CLI's.

**The judge on a single launch** (1.14.0). Measured across 28 projects, 1,470 of 1,945 dispatch
rows came through the single form — and it got nothing from the judge, which only batch asked.
With the key it now gets what a batch lane gets, with no flag. Before the launch the wrapper
reads the prompt where codex's grammar puts it — the last argument, when that is not
flag-shaped and is longer than 40 characters; otherwise it asks nothing at launch — and prints
at most two kinds of stderr note: a routed model two tiers from what the brief's difficulty
demands (`dispatch: note — this brief reads top-tier (scope 1.0, novelty 2.8, spec 2.1) —
launched on gpt-5.6-luna/medium`), and a brief that may contradict a live directive the lane's
capsule will carry (`dispatch: note — brief may conflict with directive <id> (0.83): …`). Notes,
never gates: the launch goes ahead. At exit the lane is triaged over the same state a batch
lane gets — its final message as `report`, captured through codex's `--output-last-message`
into a temp file of the wrapper's own (prepended like `--fast`, so it lands ahead of any
subcommand; a caller's own `-o`/`--output-last-message` is read instead and left in place),
the stderr tail the wrapper already forwarded, `check_output: null`, and the changes in `-C
<dir>` or the cwd — and one stderr line reports it: `dispatch: triage escalate (done .41 ·
blocked .03 · ask .62 · creep .05 · verify yes)`. The same function triages a batch lane, and
the route lands as `ev:triage` (§3.2). stdout is untouched throughout. Without the key none of
this exists — no capture flag in codex's argv, no request, no row, no note.

**Batch** (1.12.0). One launch per call was the right shape until the batches were not: a
measured 222-lane "fleet" cost $2 to run, while the orchestrator model driving its launch/harvest
loop cost ~$13 in turn-loop context re-feeds — the ceremony around the lanes cost six times the
lanes. `hippo dispatch --batch <manifest.yaml>` moves exactly that ceremony into the wrapper —
fan-out, concurrency, id capture, parent stamping, usage collection, breaker checks, journaling,
resume — and leaves the model what needs a model: selection (writing the manifest) and judgment
(verdicts). The manifest is **per-batch data, authored fresh like a brief, never standing
config** — the routing.yaml retired to §4 would have frozen a judgment; a manifest records one
batch's already-made routing and expires with the batch. Each entry launches through one of two
adapters, `codex exec` or `claude -p --output-format json`, both stamped with
`HIPPO_DISPATCH`/`HIPPO_DEPTH` and recorded as `ev:dispatch` + `ev:usage` exactly like a single
dispatch — the §3.2 schema is unchanged. Codex usage rides the same stderr banner and footer this
section already reads; claude's single stdout JSON object supplies the token fields (input +
cache read + cache creation as tin, cache read as tcached, output as tout), and its
`total_cost_usd` is deliberately **not** recorded — $ derives from `prices.yaml`, so PRIORS
prices every executor through one formula instead of trusting each executor's own bill.

A journal beside the manifest records every launch and exit, and since 1.14.0 the journal — not
a flag — decides what a run does. **No journal**: every entry launches. **Unfinished entries**:
the run resumes by itself, and says so first (`resuming <manifest>: N to relaunch, M skipped`).
Entries whose last exit (and check) passed stay done; the rest relaunch unless their latest
triage named a cause a relaunch cannot clear — `capability` or `spec` needs a different brief,
then a new entry — and those are skipped with a journal `skip` record and one stderr line naming
them and their cause. No triage, or a triage that named no cause, relaunches: a filter that
cannot read the cause must not be the reason a lane is dropped. **Every entry done**: nothing
launches. Every run then ends with the harvest (below); to start over, delete the journal. A
relaunch mints a **new** dispatch id, because two launches are two facts and the ledger never
rewrites one. `concurrency` is a manifest key. An entry's `cwd` (default: the batch's own, and
resolved and checked at validation) is the child's working directory for both adapters and for
its check; a claude lane's worktree is `cwd: .claude/worktrees/<name>`, since claude takes no
`-C`, while a codex lane may carry `-C` in `args` as before — triage reads whichever directory
the lane worked in. The flags that used to choose all of this measured zero calls across 28
projects and were retired (§4): a feature that needs a flag is a feature main does not use.
An entry's optional
`check` command runs after the child exits and its rc lands in the journal — **evidence, never a
verdict**: batch writes no `ev:outcome`, because a passing check is not acceptance and the
judgment belongs to main at any scale (principle 3 does not dilute with volume). The circuit
breaker gates batch launches through the same arithmetic as single ones — factored into one
verdict function both paths call, consulted before every launch — and, as ever, main is never
gated.

The verdicts return through one call once judged: `hippo log outcome --from-batch <journal>`
reads verdict rows as JSON-lines on stdin and resolves each `(entry, attempt)` through the
journal's latest exited attempt to its dispatch id — **serialization after verification, never
verification** (a 222-lane batch's verdicts cost 222 scalar calls before this). A row lands only on a
dispatch whose executor claim still awaits a verdict, carries its own `note`, and the whole
input validates before the first append; everything exceptional — an earlier attempt, a
deliberate re-verdict, a lane that never claimed — keeps the scalar command, where the
exception stays visible.

Measured against its predecessor on the same 200-algorithm "fleet": the orchestrator's cost fell
$13.07 → $9.23 (turn-loop input nearly halved, 19.6M → 10.9M tokens) and wall time 42 → 28
minutes. The run also measured the shape's one hazard: with judgment moved out of the launch
loop, 130 *identical* check failures (one missing `pytest.ini` — a defect the loop-driving
orchestrator of round one had diagnosed once and fixed globally) were paid as 130 per-entry
repair lanes. Batch replaces the loop's ceremony, not its judgment — mass-identical failures
still deserve a diagnosis before a repair manifest (the dispatch skill says so now).

**Harvest triage.** What remained expensive after batch was the *reading*: the $9.23 above is
almost entirely main opening 222 lane reports to reach 222 verdicts, and the 130 identical
failures were paid as 130 repair lanes because nothing clustered them first. The judge (§3.9)
moves the reading, and only the reading: at every lane's exit the wrapper hands it that lane
whole — the full brief, the full report, the stderr tail with codex's banner filtered out, the
check output, and `git status --short` + `diff --stat` of the directory the lane worked in —
and asks eight literal questions about it. Code, not the model, turns the answers into a
**route** (`failed` from the exit codes alone, then `no-go-candidate`, `accept-candidate`,
else `escalate`) and a `verify` hint, against thresholds that sit in `clerks/jev/harvest.yaml`
next to the questions they belong to. A route is evidence of exactly the standing a check rc
has — `accept-candidate` is not acceptance, and batch still writes no `ev:outcome`. The record
lands in the journal as a `triage` line with every probability and in the ledger as
`ev:triage` (§3.2), and the progress line gains `triage=<route>`. Over budget, the state is
trimmed in one fixed order (stderr, brief, changes, then the report from its *head*, since a
lane's summary of itself is at the end) and the record names what was cut — no silent
shortening, and no answer invented for a judge that failed: `route: null` and an `ev:clerk
name:jev-harvest ok:false` row are the record.

**The harvest.** Every run ends with it, on stdout above the summary line (which stays last).
Each exited entry is read — the triage its latest attempt already carries, or a fresh one where
it has none (the judge was off then, or failed on an earlier run; a lane is never read twice in
one run, and never re-read once it has a route) — the failures are clustered, and one table
prints: id, rc, check, the lane's own claim, route, verify, the numbers that produced the
route, and the path to the file that holds the diagnosis, sorted so what needs main's eyes
comes first. Clustering is greedy and one-pass: each failure is asked once against the
representatives found so far — "would one fix clear both?" — and joins the first above
`same_cause_at`, else opens its own cluster. It is a high threshold on purpose, since a wrong
merge hides a defect behind another's diagnosis while a wrong split costs a second read. The
footer names each cluster and then hands main the two next commands: the same `hippo dispatch
--batch <manifest>` when some failures carry a cause a relaunch could clear (`transient`,
`environment`) — rerunning it *is* the resume — and the `log outcome --from-batch` line for
`<manifest>.verdicts.jsonl`, which the harvest rewrites with one row per `accept-candidate` and
none for a failure — a failure needs a diagnosis, not a verdict. The note on each row says main
confirmed it, because main is expected to read the table and pipe the file only if that is
true. A lane whose `kind` is `verify` is read once more: its report is split into findings in
code (a finding is a bullet, a numbered item or a heading, plus the lines under it), each one is
scored for severity and for whether it is a defect at all rather than a preference or a
question, and the top five ride under that lane's row worst-first — the verifier is told to
report everything and let the collection side filter (dispatch skill §4), and that filtering
was a main turn per verifier. All of the judged part exists only where the judge does: with no
`TYPESAFE_API_KEY` there is no triage record, no ranking, no route column and no verdicts file,
and the table prints with its judged columns as `-` and one stderr line saying so.

**The plan.** `--batch <manifest> --dry-run` launches nothing and prints the plan; with the
judge on, the same pass runs ahead of every launch over the entries about to launch, printed to
stderr before the batch starts. §9.6 turned routing into "what is the cheapest exec that clears
the bar", and PRIORS answers half of that — what a `kind × exec` has cost and returned. The half
no ledger can know before a launch is how hard *this* brief is, which main has been guessing off
the priors page. So the judge is asked five literal questions about each entry's brief — scope,
novelty, how completely the goal is specified, whether a machine could confirm completion, and
which kind of work it is — and code does everything after that: the tier the difficulty demands,
the model that tier resolves to on `prices.yaml` (the lowest *price level* is `cheap`, the
highest is `top`, the second highest is `mid` — levels, not rows, because two generations of
one model share a price and would otherwise make `mid` a twin of `top`; within a level the
sheet's first row wins; read at call time so a price refresh moves the ladder), the effort, and
then at most one step of adjustment from the ledger's own cells — a tier this kind keeps failing
at goes up one, and the cheapest tier whose record clears the bar takes the work. A probe on real
briefs (three algorithm briefs from a consuming project against one cross-cutting design brief,
20 questions, 0.7s) separated them cleanly: scope 0.9 vs 3.0, design judgment 0.0 vs 2.8, spec
gaps 0.1–0.3 vs 2.0, a named check 0.7–0.8 vs 0.1. The output is a table, the notes an entry
earned and `<manifest>.plan.jsonl`, one record per entry so a batch's routing decision can be
joined to its outcomes later. The notes: no `check` where the brief names nothing runnable; a
`kind` outside the vocabulary PRIORS aggregates on; a routed model two tiers from the demand
(`reads top-tier (…) — routed to gpt-5.6-luna/medium`); and a brief that may contradict a
directive its lane will carry — the same pass asks `clerks/jev/brief-check.yaml`, one question
per live directive with audience `executor|all`, and notes those at or over 0.7 (metered as
`jev-brief`; the measured incident is a fail-closed NO-GO from contradictory clauses).
**Auto-routing**: an entry that leaves `model` unset launches on the suggestion, model and
effort both; one the judge could not route stops the run before anything launches, and with the
judge off it fails validation exactly as before (`model is required`). An entry that names its
own model is still read, and only ever gets a note. **The manifest is not modified** — a
suggestion that rewrote the file would be the frozen config of §4 with an extra step; this one
is computed fresh per run and expires with it. With no key `--dry-run` still prints the ladder,
what each entry already routes to and the priors evidence for it, with the difficulty columns
as `-`.

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

The same page measures the judge (1.14.0): a `triage agreement` section tables each triaged
dispatch's route — the latest `ev:triage` recorded before main's first verdict, which is what
main had in front of it — against that verdict, route × result, with one line under it for
`accept-candidate` precision (accepted or revised, of the accept-candidates that got a verdict).
A ledger with no triage row gets no section.

The page no longer waits for a manual run: the scribe regenerates it when it is due (§3.5.8), and
`hippo prior distill` stays for the run nobody wants to wait a week for.

Cells under n=4 get no rate but are still named with their n: a percentage over n=1 reads as
evidence and is not one, while dropping it silently leaves the reader unable to tell a suppressed
cell from an absent one. The raw ledger is deliberately not sent — everything the page needs is on
the sheet, and shipping 300 JSONL lines only offers something to recompute from, badly.

### 3.7 Skills (three)

> Naming: the plugin name is `hippo`, so the slash prefixes are `/hippo:hippo`, `/hippo:checkup`
> and `/hippo:dispatch`; the CLI command (`hippo`), `.hippo/` and the `HIPPO_*` environment
> variables use the same name.

- **`hippo:hippo`** (~3KB) — the main nudge skill. Its description is the fixed line
  "This is your hippocampus. Always use it." — that single line sits in every session's skill list
  and is the only thing that invites use (principle 2: a short description rather than a resident
  injection). The body is the document models actually read — measured 2026-09-23, Codex lanes
  read it 856 times while `--help` was called 405 times in 19 projects — so it carries the whole
  grammar in one block (every command, flag and enum value, held to `build_parser` by a test),
  when to reach for each, and what runs by itself. One screen; anything larger is a regression.
- **`hippo:checkup`** (~5KB) — a `/doctor`-style project diagnosis. It reads the ledger, PRIORS,
  failures, cursor gaps, recent transcripts and CLAUDE.md/memory, then reports waste patterns
  (retry loops, limit stalls, orphan dispatches), directive hygiene (stale or contradictory
  directives versus the documents, with `directive list --hygiene`) and clerk health (gaps,
  failures, overhead, and why PRIORS is stale when auto-distill has not fired). Proposals are
  recommend-first, at most two AskUserQuestion rounds, with reversibility stated. Nothing is
  applied automatically.
- **`hippo:dispatch`** (~9KB, from 16KB — a third of it described batch flags retired in 1.14.0)
  — the revised "fleet-dispatch", now "delegation lanes": the measured lessons only, the flags
  left to `hippo:hippo`. The key revisions (all grounded in the audit and the guide): a verifier
  **reports everything and main filters** (with a literal-minded model, a severity ceiling
  genuinely hides findings); the verification budget is **proportional to the refutation rate
  in PRIORS** rather than a fixed ritual; no re-verifying one's own work (boundary verification
  only); safety statements about GPUs and memory use neutral vocabulary (10 measured
  content-filter false positives); grep the shared and individual brief clauses for
  contradictions before composing them; symbol coupling is checked across lanes; and the gate
  check and the push must always be separate calls. Measured use: 7 invocations plus 37 file
  reads across 28 projects, against 2 for checkup.

### 3.8 Hosts (Claude Code · Codex CLI)

The same repo is a plugin for both hosts. Three of the four layers (CLI, clerk, skill) were always
host-agnostic and only the hooks were host-bound — and that wall came down when codex grew a hook
engine (measured on 0.144.6).

| | Claude Code | Codex CLI |
|---|---|---|
| Manifest | `.claude-plugin/plugin.json` | `.codex-plugin/plugin.json` (names the `skills` and `hooks` paths) |
| Hooks | `hooks/hooks.json` | **the same file** — event keys (PascalCase), matcher, stdin payload fields and the SessionStart `hookSpecificOutput.additionalContext` envelope are all identical (codex ≥0.146 rejects bare text, §3.4) |
| Plugin `bin/` | added to PATH automatically | **not added** → a skill resolves `bin/hippo` relative to its own SKILL.md |
| Transcript | Claude JSONL | codex rollout JSONL — `digest_lite.py` detects the format from the first lines and reduces both to the same line vocabulary |

Constraints specific to codex (0.144.6):

- **Hooks are skipped silently until they are trusted.** Review and trust them once through
  `/hooks`, or bypass with `--dangerously-bypass-hook-trust`. If the capsule never appears after
  installing, look here first.
- Installing and trusting hippo in the Codex host is also what gives **dispatched lanes** their
  capsule (start + post-compaction, §3.4): `codex exec` fires plugin hooks — the reason
  `clerk_run.sh` must pass `--disable hooks` — so a lane launched by `hippo dispatch` carries
  the gate's env either way. Smoke-test the exec-mode compact event once per codex upgrade;
  0.144.6 is the measured baseline.
- The project `.codex/` layer loads **only in a trusted project** (plugin hooks are unaffected).
- `"async": true` parses but is **skipped** — a Stop hook earns its non-blocking behavior by
  detaching itself (our `stop.sh` already does, with setsid and all three streams closed).
- The `version` in the two manifests must match (a test enforces it).

### 3.9 The judge (Jev)

TypeSafe's Jev is a judgment-only model: it takes a `state` (a string or a JSON value) and a map of
typed questions, and returns probabilities — it cannot write prose, which is exactly why it is safe
to point at an untrusted transcript. Three question types: `noul` (one yes/no →
`{"noul": 0.87}`), `choice` (pick one of the named options → the option, a confidence and the
distribution) and `score` (an ordered ladder of levels → a number, a confidence and a legend).
`POST https://api.typesafe.ai/v1/systemone`, `Authorization: Bearer $TYPESAFE_API_KEY`, body
`{"model": …, "state": …, "questions": {…}}`; the reply carries `answers` and `usage`.
Measured on this machine (2026-09-23): 0.7–1.5s per request, a 20-question probe and a
77-question probe each answered in one round trip (the 77 in 0.9s). The cost is a third budget —
it is charged to neither the Claude nor the Codex subscription — and is treated as zero here.

**Large context is the point.** What separates this from a BERT-class classifier is that it takes a
large, messy state — a whole digest, a whole lane report, a brief beside the live directive set —
and returns a calibrated judgment over it. Code pre-filters *irrelevant* material and never shrinks
relevant volume. The hard limit is the model's context (32k tokens for the state plus the longest
question, 64k for the request), and the client owns it: `JEV_STATE_BUDGET_CHARS = 110_000`
(≈28k tokens at 4 chars/token, leaving the questions their room). Over budget, `judge` returns a
failure reading `state exceeds jev budget (<n> chars)` and the caller continues as it would on any
other failure — it never truncates, because a shortened state answers a different question. A 422
from the API is the same path.

The known weaknesses (docs.typesafe.ai/model-jaggedness/jev-1.13) shape every use: it reads
literally, does no arithmetic and no counting, loses accuracy when the state carries irrelevant
material, can be moved by adversarial text inside the state, and is weaker on CJK than on English.
So: one narrow judgment per question, criteria that agree with their instruction (a `true` that
describes "no" confuses it), state pre-filtered by code, and every threshold evaluated in code.

- **There is no setting, by design.** The backend is `live` when `TYPESAFE_API_KEY` is set and
  non-empty and `off` otherwise — a machine with the key gets the judge, a machine without it gets
  exactly the plugin as it was, byte for byte, and nothing ever asks the user. `$HIPPO_JEV_BACKEND`
  (`live|mock|off`) exists for developers and for the test suite, not as a user-facing switch, and
  there is deliberately no `config.yaml` key: the clerk has one because a user chooses between two
  real backends there, and here there is nothing to choose. `$HIPPO_JEV_MODEL` overrides the model
  (default `jev-latest`). 20s per request, one retry after 2s on 429/529, nothing else retried.
- **Specs are text** — `clerks/jev/<name>.yaml`, a `questions` map plus an optional `policy` map
  (principle 8: the judgment lives in prose a person tunes, the thresholds in a key code reads).
  `jev_questions(name, **vars)` renders `{var}` placeholders in the question id and in
  `instructions`; `criteria` are copied verbatim, and an unknown `{placeholder}` is left exactly as
  written, so a spec that names a variable its caller did not pass still loads. A brace that is not
  a `{name}` at all is a malformed spec and dies at load, where a test catches it — the same
  treatment a broken clerk prompt gets. A caller fans out over n items by
  calling it once per item and merging the maps — n narrow questions in one request, not one
  question about n things.
- **`judge(hp, name, state, questions) → (answers, meta)`** never raises for a backend or network
  problem. `meta` is `{ok, reason, ms, tokens, model}`; `tokens` is the reply's input+output usage.
  A missing answer for a requested id is a failure like any other. A missing or malformed spec file
  still dies — that is missing infrastructure, like a missing clerk prompt.
- **The mock backend** is how the tests never touch the network: `$HIPPO_JEV_MOCK_OUTPUT` names a
  JSON file `{"answers": {<id>: <answer>}, "default": {"noul": …, "choice": …, "score": …}}` — by
  id first, then by question type, and no default for that type is a failure naming the id.
  `$HIPPO_JEV_MOCK_CAPTURE` receives the request body that would have gone out, so a test asserts
  on what was actually asked (the same contract `HIPPO_MOCK_CAPTURE` has for the clerk). The test
  suite runs with `HIPPO_JEV_BACKEND=off` by default.

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
| installing a cron job automatically | A user who wants one sets it up. The plugin does not own a schedule — auto-distill rides the Stop-driven scribe instead: due-when rule, no schedule (§3.5.8) |
| routing.yaml / depth-tier model config | Retired 1.11.0 before being built: prices are `prices.yaml` facts, tier-worth is PRIORS `$/accepted`, the decision between them is main's — frozen config is the stale-instruction shape (§1 principle 9). The runaway worry it addressed is handled by the fan-out circuit breaker (§3.6) instead. The shape it was retired in favour of is the batch plan (§3.6, `--batch --dry-run`, and ahead of every judged launch): the same question answered per batch, computed fresh from the price sheet and the ledger, printed as a suggestion — never a file that outlives the batch |
| batch mode flags (`--harvest`, `--plan`, `--resume`, `--fresh`, `--concurrency`, `--causes`) | Retired 1.14.0. Measured across 28 projects (2026-09-23): zero calls to `--harvest`, `--resume`, `--fresh`, `--concurrency` and `--causes`, six to `--plan` in one project, against 11 `--batch` calls in two — while the flag-free single form carried 1,470 of 1,945 dispatch rows. A feature that needs a flag is a feature main does not use: the journal now decides launch / resume / harvest, the harvest ends every run, `--dry-run` is the plan, and concurrency is the manifest key (§3.6) |
| directive lifetimes (`turn\|phase\|durable`) | Retired 1.14.0 (§3.2). Measured across 28 projects: 63 of 75 live `phase` directives were past 14 days and the aging nudge produced no withdrawals, 34 of 72 withdrawn went within 3 days, and 54 of 55 `turn` directives expired by the clock. One lifetime — until withdrawn — plus an age shown on every line (§6) |
| generic bulk ledger ingest (`log --file`, a bulk endpoint) | It would enlarge the mutation grammar toward the retired ingest family above — facts enter through one door. The accepted shape is the journal-scoped `log outcome --from-batch` (§3.6), which narrows what a row may say instead of widening it |

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
· live(23d): keep review replies in context, never save them to a file
· live: use GPUs 0 and 1 only
· in flight: NVFP4 factor-rebasing 6-part (0h42m), r2 UNCERTAIN 4건 (0h12m)
· last: merged the v2 Pareto duo, full gate green (1421)
· cli: task add|set|done|list · log dispatch|outcome|review|review-status · directive add|withdraw · prior · dispatch [--batch] — /hippo:hippo has the flags
```

The `cli:` line is main's only (a lane has its `report:` line instead): the command grammar,
because the capsule is what re-arrives after a compaction and that is exactly when the grammar
was being re-read — measured, 405 `--help` calls across 19 projects, and 59 of Codex's 96 (61%)
came within 30 tool calls of a compaction.

`in flight` is delegations launched and not yet judged, within 24h, **counting only what a
launcher wrote** (`src` `wrapper`/`cli`). A lane's self-report does not land an entry — it rides
it, subject visible (`pass2 tensorize (0h42m · claims accepted)`): an executor-sourced statement
never renders as a flat fact (§9.3), and only main's verdict clears the line. It is the one part of "where was I" that is a fact rather
than a plan, and it is the reason it belongs here instead of in a hand-kept file: measured on a
consuming project, this query returned exactly the three lanes that project was listing by hand,
while the same query over every writer returned 16 — scribe-inferred rows swamp it. With nothing
flying the line is absent; a dispatch older than a day is not in flight but forgotten, and
`prior distill` already reports those as open items.

Everything else such a file carries has a home already: the current phase is a directive,
what shipped is the worklog, ordering is `task deps`, and a merge hazard belongs in the brief for
the lane that will cause it (dispatch skill §5). A re-entry document is what appears when those
surfaces go unused — not a gap in this design.

When the reader is a dispatched lane (`HIPPO_DISPATCH` set), a three-line operating tail is
appended —

```
· report: hippo log outcome --result … --note '…' — no --ref needed; recorded as your claim, main judges
· depth 0: do not re-delegate — implement it yourself; no codex exec, no subagent, no workflow
· discipline: report no-go early when the premise does not hold; long runs go to background — never poll with a foreground sleep
```

— the lane's whole operating contract, generated where the lane reads it (principle 5) instead
of hand-copied into every brief. The depth line follows `HIPPO_DEPTH` (§9.5): at depth ≥ 1 it
grants dispatching children instead, and notes they start at depth 0.

Four rules govern the directive block:

- **Nothing is folded away.** Every active directive addressed to the reader is injected, in
  full, in ledger order (audience §9.4: a lane's capsule carries `executor|all`, main's carries
  `main|all`). A user
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
- **Staleness is shown, never resolved.** A directive lives until it is withdrawn, so its age is
  the one thing about it that changes: every live line carries its age from 14 days
  (`live(23d): …`), and the volume notes name every directive 30 days or older with the one
  question that matters — still true? The lifetimes this replaced (§4) sorted directives by how
  long they *should* hold, and the measurement says nobody closed them: 63 of 75 live `phase`
  directives were past 14 days, and the phase-only nudge produced no withdrawals. Age applies to
  every line because a `durable` ruling goes stale too — it just takes longer to notice. Nothing
  is withdrawn automatically, and
  the scribe may not infer a withdrawal from anything but the user saying so — measured
  (2026-08-02), a clerk once withdrew a live hold because an assistant report mentioned its
  keyword. Automation that decides is the failure mode; visibility is the fix, and the verdict
  stays with main and the user.
- **Content is judged, never enforced.** The three rules above count characters and days; what
  the directives *say* went unread, and an obedient model is most dangerous where two live
  clauses contradict each other (measured: a fail-closed NO-GO out of two GPU clauses). At
  `directive add` and at `directive list --hygiene` the judge (§3.9) reads the whole live set and
  notes probable conflicts and audience mismatches. A note is the whole of it: the
  stored value never changes, nothing is refused, and with no key there is no judge and no note.
  The threshold is deliberately conservative and the reading is two-stage — a probe over this
  repo's live set (8 directives + 3 planted, 77 questions, 0.9s) ranked the two planted conflicts
  first (0.83, 0.82) and the planted non-conflict under 0.25, but scored two *unrelated* pairs at
  0.66-0.74 with the whole set in view, so a pair that stage 1 flags is asked again alone
  (`recheck_at` 0.5) and only reported when it survives (`report_at` 0.7).

## 7. Testing policy

Tests are a means: CLI round trips (add/set/list with multi-filter/done), log validation (valid and
malformed, fail-closed), the directive lifecycle, status --inject (present and absent, silent), the
whole scribe pipeline (mock backend: cursor advance, ledger append, worklog append, lock contention,
malformed JSON isolated into failures), and digest_lite basics. Around twenty of them. `uv run pytest`.

## 8. Salvage record

- The audit's digest logic (digest.py, proven on 479MB) → `scripts/digest_lite.py`
- The task registry concept (1,081 voluntary uses even after the plugin was switched off = revealed
  preference) → a thin rewrite
- The body of the "fleet-dispatch" skill → the revised `skills/dispatch`
- Everything else from 0.x → retired to the `legacy` branch. Audit report:
  `~/workspace/b200-2-research-cc-audit/`

## 9. The shared brain (§9.2–9.4 shipped in 1.8.0, §9.5 depth in 1.9.0, §9.6 cost in 1.10.0; routing.yaml retired to §4 — nothing in this section remains unbuilt)

Today a delegated executor starts blind. Everything it needs — the live directives, the task it
serves, what has already been tried — is re-typed by hand into a brief, which is why `COMMON.md`
exists and why briefs keep growing. The abstract *intent* behind an instruction does not survive
that transcription at all; only the instruction does.

The proposal is to let executors read and write the same `.hippo/` main does. Hierarchy is kept —
main still decides — but the memory is one memory.

### 9.1 The plumbing already exists

`.hippo/` is found by walking **up** from cwd (§3.3), and an editing lane's worktree is
`{repo}/.claude/worktrees/<name>` — inside the repo. One thing did need wiring (found while
building 1.8.0): the walk used to stop at the worktree's own `.git` *file*, exactly the boundary
this section assumed it crossed. It now walks through a `.git` file and stops only at a `.git`
directory — a real repo root. A lane whose worktree sits *outside* the repo never reaches it, so
the wrapper also plants `HIPPO_DIR` and every resolution — the CLI's and both hooks' — takes it
before walking (1.14.0; 12% of lane outcomes had been refused for it). Everything else was
policy, which is why it was worth writing down before building.

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

Time was the only directive axis (a `lifetime`, retired in 1.14.0 for an age — §3.2). The missing
one is **audience**, and it is invisible until directives
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
existing volume nudges (§6) already surface. The audience note of §6's fourth rule is how that
wide default gets narrowed in practice: nobody types `--audience` while writing a rule, so the
judge reads the text afterwards and says which axis value it sounds like.

### 9.5 Depth, so the spiral is visible instead of forbidden

Some work is one task and simultaneously the size of a whole session. Such a dispatch should be
allowed to orchestrate: main becomes the orchestrator of orchestrators.

This directly inverts the dispatch skill's "no re-delegation" clause, which exists because of a
measured loss (one lane spiralled through 336k tokens and produced zero commits). The clause should
not be deleted — it should be indexed:

- `--depth 0` (default) — unchanged. The **capsule** carries the no-re-delegation clause
  automatically (built in 1.9.0 — the clause moved out of briefs entirely).
- `--depth 1` — may spawn. Its children are depth 0 and receive that clause.

Recording depth makes an unintended depth 2 an event in the ledger rather than a prohibition nobody
can check (built: the wrapper stamps `parent` from `HIPPO_DISPATCH` on any launch made inside a
lane). An earlier draft of this section wanted a `.hippo/routing.yaml` for depth-tier model
routing; it is **retired, not built** (see §4). §9.6 absorbed both halves: prices are facts in
`prices.yaml`, and which tier earns its price is PRIORS' `$/accepted` — freezing the judgment
between them into config is the stale-instruction shape this design keeps declining. What
survives of the worry is not routing but blast radius, and that is the fan-out circuit breaker
(§3.6).

### 9.6 PRIORS has no cost axis, and that is the actual blocker

PRIORS aggregates quality — refutation and acceptance rates — over `kind × exec`. That was the
right question while every dispatch cost roughly the same. It stops being the right question in two
ways at once: a depth-1 dispatch is a *batch*, not a lane, and filing it beside a single cheap
dispatch under the same `kind` makes the prior lie; and once a cheap tier is genuinely cheap, the
question changes from

> which exec performs best → **what is the cheapest exec that clears the bar**

which the schema before 1.10.0 could not answer at all. (One premise here was optimism, like
§9.1's: the wrapper *exec'd* codex and read no stream — becoming a pass-through observer was
the wiring this section needed, built with `ev:usage` in 1.10.0.) `codex exec` reports its
usage, so recording it at lane exit is collection at the point that
already knows (principle 6). Cost per *accepted* outcome is then derivable, and §9.5's routing stops
being a guess. It also composes with §9.2 for free: children writing to the same ledger under a
parent's dispatch id means a batch's cost sums itself.

### 9.7 Consequences to settle before building

- **The executor gets no scribe** (enforced by the Stop hook's `HIPPO_DISPATCH` gate since
  1.8.1). Running the Stop hook per lane multiplies clerk cost by the batch width, and the hook
  cap is two (§3.4). It *does* get the capsule — SessionStart's side of the same gate — because
  a lane that compacts loses its brief's constraints exactly the way main used to (§3.4). If a
  depth-1 orchestrator's reasoning is worth keeping, the
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
half-blind without it (an orchestrated batch with no shared memory starves its own children). Build
the data plane first. Its minimum is two things — the `executor` src value and the audience axis —
which is small enough that it may not need a major version at all.
