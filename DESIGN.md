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
  the gap by inventing content. Failures land in `failures/` and checkup reports them; a scribe
  failing three runs in a row also puts one line in the capsule (§6).

Judge guardrails (invariant):

- **No hook of its own** — it rides a surface that already exists (§3.4 lists every hook, each
  with its measured reason).
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
runtime (thin):   bin/hippo (shim) + cli/hippo_cli.py + 4 hooks (2 on Codex) + scripts/{clerk_run,digest_lite,dispatch}
                  + settings.json → scripts/lane_status.py (the lane row in Claude Code's agent panel)
cognition (text): clerks/{turn-scribe,distiller}.md + clerks/jev/*.yaml + skills/{hippo,checkup,dispatch}
                  + agents/lane.md
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
  task-flags.json   # generated: open tasks a window read as ended, for the capsule (§3.5.9)
  worker-writes.json  # generated: task, directive and verdict writes a subagent made (§3.5.3d)
  failures/         # dumps of clerk output that failed validation (checkup reports them)
  briefs/           # delegation briefs (COMMON.md + one file per task) — see below
  lanes/            # per codex lane: its record, raw stderr and report (§3.6); pruned after 7 days
  config.yaml       # optional: overrides such as the clerk backend (everything works without it)
```

In a directory with no `.hippo/`, every hook and every CLI command is a **completely silent no-op**
(zero contamination of other projects).

Every file hippo rewrites (tasks, worklog, PRIORS, cursors, task flags, worker writes, lane records) is replaced whole: written to a tmp file
beside it, fsync'd, then renamed over it (`write_durable`). The ledger is only ever appended to.
Measured (b200, 2026-09-23): a node failure during an in-place worklog rewrite left steno's 412KB
worklog.md at 0 bytes on a shared filesystem that kept the truncation and lost the data.

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
{"t":"…","ev":"dispatch","id":"d041","kind":"kernel-impl","exec":"codex/gpt-6-sol/high","scope":"pass2 SS-UMMA tensorize","task":"feat/x"}
{"t":"…","ev":"outcome","ref":"d041","result":"refuted","attr":"work","rework":2,"by":"verify/opus","note":"circular oracle reference"}
{"t":"…","ev":"review","id":"r007","base":"abc123f","source":"chatgpt-web","findings":4}
{"t":"…","ev":"review-status","ref":"r007","addressed":"partial","at":"def4567"}
{"t":"…","ev":"directive","id":"gpu-01","text":"use GPUs 0 and 1 only","state":"active"}
{"t":"…","ev":"directive","id":"gpu-01","state":"withdrawn"}
{"t":"…","ev":"clerk","name":"turn-scribe","ms":8100,"ok":true,"tokens":1400}
{"t":"…","ev":"usage","ref":"d041","tokens":1100000,"tin":1000000,"tcached":400000,"tout":100000,"model":"gpt-6-sol"}
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
  visible); a model off the sheet renders as unpriced and named, never guessed. A replaced
  model keeps its row, marked `legacy: true`, while recorded usage runs on it and the vendor
  page still lists it: that row prices the history and is never routed to — the ladder, the
  tier note and the breaker's unknown-model reservation all skip it (§3.6). PRIORS' routing
  table carries tokens, $ and $/accepted per cell — the question it unlocks is "the cheapest
  exec that clears the bar". A batch's total is derivable by summing children over `parent`.
  The one other writer (1.15.0) is the scribe's own code for a native run (§3.5.3c),
  `src=scribe`: usage it summed from the agent's own transcript, which has the standing of the
  wrapper reading a rollout — code reading a record the host wrote, not a clerk paraphrasing
  one; a usage event from the clerk is still rejected (§3.5.6b). Those rows are cumulative and
  one per model (`tin` = input + cache read + cache creation, `tcached` = cache read), so PRIORS
  reads the last row per `(ref, model)` and sums the models; a dispatch is priced only when
  every model it ran on is.
- `ev:triage` (1.14.0) is the judge's reading of a finished lane (§3.6) — written by the wrapper
  (`src=wrapper`) at lane exit, from single dispatch and batch alike, and rejected from the
  clerk by the same rule as `usage`: the wrapper observed it. The scribe's code writes one too
  (`src=scribe`, 1.15.0) for a native run's answer to its brief (§3.5.3c).
  `route ∈ {accept-candidate, escalate, no-go-candidate, failed}`, `verify` is a bool,
  `cause ∈ {capability, spec, environment, transient}` or absent, and `p` is a flat map of the
  compact probabilities the route was computed from (`done`, `blocked`, `ask`, `creep`,
  `evidence`, and `risk` on its 0–3 ladder — numbers only). `trimmed` (1.15.1, absent when
  nothing was) lists the fields of the state that were shortened to fit the judge
  (`stderr_tail`, `brief`, `changes`, `report` — §3.6): the row is the only record a single
  dispatch or a native run keeps of what the judge did not see. `ref` joins a dispatch
  fail-closed. A judge failure writes nothing: the `ev:clerk name:jev-harvest ok:false` row is
  the gap. A route is evidence of a check rc's standing, never a verdict — and recording it
  closes a loop: the in-flight line shows the latest route beside the lane's claim, and PRIORS
  tables each triaged dispatch's route against main's first verdict (§3.6b), measuring the
  judge the way it measures executors.
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
  between parallel lanes, so it lists them and fails rather than guessing. An unjudged native row
  (`ag-`, §3.5.3c) competes only when no other dispatch is open: the scribe tags every subagent
  whose brief names the task, most never get a verdict (13 such rows for one task in a replayed
  mlx-vlm session), and counting them would break `task:` for good. Measured afterwards on a
  consuming project that had the no-double-record rule but not the roster: of 66 scribe-written
  dispatches, 30 restated a wrapper launch under a fresh id and 6 reused the wrapper's id exactly,
  inflating the PRIORS denominator ~1.4x — a prompt cannot enforce what its inputs do not contain.
- The **executor** is the agent that did the work (`codex`, `claude`, `fork`, `subagent`,
  `workflow` — `claude` being another Claude Code session: a peer machine, a headless run started
  by hand), not how it was launched: a codex run started in the background is still `codex`,
  and splitting it by launch mechanism scatters the sample the priors depend on. Work with no
  agent — a command main simply ran — is not a delegation and gets no dispatch event. `effort` is
  `low|medium|high|xhigh|max|ultra|inherit`; `inherit` is for an executor that takes its setting from
  the session that spawned it. **Neither vocabulary is validated on main's writes** — see §3.5.6b
  for why, and for where they are.
- `dispatch.kind: unclassified` has one writer, the scribe's code: a native run that ended with
  no row after the clerk had it listed (§3.5.3c). It is no tag — the column it makes in PRIORS
  says the kind was never read — and it keeps the run's cost and any verdict, which losing the
  row would not.
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
  worktree gets no capsule and no scribe) with two exceptions: under `HIPPO_DISPATCH` —
  a dispatched lane — SessionStart walks through the worktree's `.git` file too, which is what
  re-injects the capsule after the lane's own compaction (§3.4, 1.8.1); and SubagentStart
  always does, because Claude Code isolates a subagent in `<project>/.claude/worktrees/agent-<id>`
  (§3.4).
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
hippo directive list [--active] [--json]
hippo directive withdraw <id>
hippo prior show
hippo prior distill [--days N]
hippo dispatch --kind K --scope S [--task T] [--depth N] [--fast] [--] <codex exec args…>
hippo dispatch --batch <manifest.yaml> [--dry-run]
hippo dispatch --watch <dispatch-id> [--for SECONDS]
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

### 3.4 Hooks (four — each one here with its measured reason)

A hook runs on the host's clock, so each one needs a reason measured and written here; a hook that
fires on every tool call or prompt stays out (§4). `hooks/hooks.json` carries SessionStart and
Stop for both hosts, unchanged since 1.14 — the Codex manifest names it, and codex keys a
hook's trust by that plugin-relative path (`hippo@<marketplace>:hooks/hooks.json:session_start:0:0`
in `~/.codex/config.toml`, 0.156.1), so a moved file would come back untrusted and be skipped
silently (§3.8) for everyone who had trusted it, `codex exec` lanes included. SubagentStart and
PreCompact live in `hooks/claude-hooks.json`, which only the Claude Code manifest names; Claude
Code loads the default `hooks/hooks.json` and adds a manifest-named file (2.1.281, measured:
each event fired once), while codex reads a manifest-named path *in place of* the default
(0.156.1's `load_plugin_hooks`). That keeps the two new hooks off codex until they are measured
there (0.156.1's hook config lists SubagentStart but not PreCompact, and a moved contract has
already broken the capsule once, below). The scripts share `hooks/lib.sh` — the clerk gate, the
stdin reader, the project walk, the CLI call and the JSON envelope — and tell the CLI which
moment they inject for through `HIPPO_INJECT`, an internal env var rather than a flag: a
SessionStart source, `subagent` or `precompact` (`hippo status --inject` reads it); stdin's
`transcript_path`, `prompt_id` and `trigger` ride along in `HIPPO_TRANSCRIPT`, `HIPPO_PROMPT` and
`HIPPO_TRIGGER`, which is how a compaction is told to be an agent's own (PreCompact, below).

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
  constraints evaporate. On source=compact main's capsule ends with the `compact:` line (§6),
  which points at the summary's `## hippo deltas` (PreCompact, below), and its in-flight line
  names main's native runs still out (§6). An agent's own compaction fires this hook as main's
  too; it gets the SubagentStart slice instead (below).
- **SubagentStart** (Claude Code; matcher `*`): `hooks/subagent_start.sh` → silent exit 0 with no
  `.hippo/`, for agent type `fork` (it already carries main's context, capsule included) and for
  `hippo:lane` (it only watches a codex lane, whose capsule comes from codex's own SessionStart);
  otherwise the live directives addressed to executors under the header `[hippo] directives N
  live — this project's recorded rules`, then one line whether or not a directive is live —
  `· hippo task, directive and outcome writes are main's — run one only when your brief asks for
  it, otherwise put what should change in your report` — in the same envelope with
  `hookEventName: "SubagentStart"`. The header named the lines "the user's standing rules"
  through 1.15.1; every Workflow agent gets this slice (42 of 42 in this repo on 2.1.282), and its
  harness tells it the relayed request is the only user voice, so the header now names a
  record, not a voice. The writes line exists because a native worker's shell carries exactly
  main's environment — every agent kind, Workflow agents included (18 env dumps on 2.1.282:
  no variable marks a subagent or carries its id, `AI_AGENT` reads `…_agent` in main's shell
  too) — so its `hippo task done` lands as main's, and a mlx-vlm Workflow agent did close a
  task on the real project that way, with nothing recording it was not main (§3.5.3d records it
  now). It is text: measured live (2026-09-26, sonnet), worded without "only when your brief
  asks", a background Agent and a Workflow agent both declined the write their brief asked for
  and reported it instead — and the real mlx-vlm close was one main's workflow script asked
  for; worded as above, both ran it. Its walk crosses a linked
  worktree's `.git` file (§3.3): an `isolation: "worktree"` agent starts in
  `<project>/.claude/worktrees/agent-<id>` (measured, 2.1.281), and those are the agents that
  edit files — the conservative stop left exactly them without "never push". The reason: SessionStart
  never fires when a native subagent starts (neither subagent transcript in the session measured
  below carries one; it does fire after the subagent's own compaction, PreCompact below), so
  until this hook a user's standing rule reached a Claude Code worker only if main retyped it
  into the brief — the gap the audience axis (§9.4) closed for lanes. The slice
  is directives and that one line only: no `report:` line (a native worker runs without
  `HIPPO_DISPATCH`, so its `log outcome` would land as src=cli — main's verdict on its own
  work — while the scribe records the run and main's verdict itself, §3.5.3c), no depth line
  (`HIPPO_DEPTH` indexes wrapper lanes, §9.5; a subagent's nesting is main's call in its brief).
  Measured on 2.1.281 (2026-09-24): it fires for general-purpose, Explore, fork, claude, custom
  and workflow-subagent agents, foreground and background, the matcher being the agent type;
  stdin carries `session_id`, `transcript_path` (the parent's), `cwd`, `prompt_id`, `agent_id`,
  `agent_type` and `hook_event_name` — no brief text; only the JSON envelope is injected (plain
  text is dropped), and it lands in the subagent's own transcript as a `hook_additional_context`
  attachment, never in the parent's; a failing hook never blocks the subagent, but a running one
  holds its first request — 113ms in a headless session (the host's own `durationMs`), 94ms
  median over 15 direct runs, 73ms of it the one CLI call. In that session a general-purpose
  subagent quoted back exactly the executor slice, and a fork quoted main's capsule — its own
  context, with nothing injected. A fork gets no writes line either: it is main's context
  copied, `cli:` line included, and a write it makes is recorded like any worker's (§3.5.3d).
- **PreCompact** (Claude Code): `hooks/pre_compact.sh` → silent exit 0 with no `.hippo/`, and
  for an agent's own compaction (below); otherwise plain text with exit 0, which the host
  appends to the compaction instructions: end the summary with `## hippo deltas`, one line per
  change the conversation made that hippo's lists do not show yet, each the exact command that
  records it (`hippo task done <id> --note '…'`, `hippo task set <id> notes '…'`, `hippo directive
  withdraw <id>` only if the user said so, `hippo directive add --id <id> --text '…'` when the
  user changed one, `edit <file>: '<old>' → '<new>'` for a memory or doc line now false), or
  `none`; then the open tasks (most recently updated first: id — title — the first 80 chars of
  notes) and main's live directives (id — the first 80 chars), 3,000 chars at most with the cut
  items counted. The reason: a compaction is the one moment the summarizer still sees everything
  the conversation changed, and the capsule that follows it re-injects hippo's *records* — the
  very state that went stale when a task shipped or a ruling moved without a call. Measured on
  2.1.281 (2026-09-24, manual and auto): the summary followed the appended request — a
  `## hippo deltas` section with the right statuses, updated again on a second compaction — but
  its formatting drifted (bullets added, a `task ` prefix dropped), so every line is a command
  main reads, checks and runs or skips (§6's `compact:` line), never a shape a parser depends
  on. With this hook (haiku, manual `/compact`) the summary carried `## hippo deltas:` over the
  three exact commands the conversation called for — `task done`, `task set … notes`, `directive
  withdraw` — main, resumed, ran all three, and the next compaction's section read `none`. hippo
  applies none of them itself: a stale-state flag is shown, never applied (§6 — a clerk once
  withdrew a live hold). SessionStart(compact) runs after the summary lands in main's
  context, which is what lets the capsule point at it. PreCompact can fire with no compaction
  following ("no assistant messages in summarize set, bailing"), which costs nothing — the text
  only asks. A manual `/compact` writes the text into the transcript main reads next (an auto
  one does not), hence main's audience only (§9.4). An agent's own compaction fires this hook,
  SessionStart(compact) and PostCompact as main's: main's `session_id`, `transcript_path` and
  `prompt_id`, no `agent_id` or `agent_type` (the bundled code builds this input without the
  agent's context), the same `CLAUDE_*` env names — measured on 2.1.282 for a foreground, a
  background and a Workflow agent, each paging a file past a small auto-compaction window
  (`CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000` + `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=25`). The prompt
  id is the host's one per process: every line an agent writes, its compaction's included,
  carries the prompt main is at (a Workflow agent compacting during main's notification turn
  carried that turn's id). Unguarded, such summaries ended in `## hippo deltas` and
  SessionStart(compact) put main's capsule, `compact:` line included, into the agent — a worker
  whose summary proposed `hippo task done` would have run it as main's verdict (src=cli). 1.15.1
  caught only a foreground agent main was blocked on; with `CLAUDE_CODE_FORK_SUBAGENT=1` (this
  user's setting) every Agent call runs in the background, and a Workflow's agents always do, so
  that check never matched.

  So the CLI reads main's side, from the hook's `transcript_path`, `prompt_id` and `trigger`
  (`HIPPO_TRANSCRIPT`, `HIPPO_PROMPT`, `HIPPO_TRIGGER`), 0.25s on — past the host's 100ms
  flush. A compaction starts right before a request, so right after a user line is appended (a
  prompt, a tool's result, a notification, a host message: a channel doorbell arrives as a meta
  line and starts a turn), stamped 25–51ms before the hook's process starts (37 of 38
  measured), and only once every call of the response before it is answered — the host sends
  no request while one is out; and nothing the compaction writes lands before SessionStart and
  PostCompact have returned — its boundary is stamped before them and written after (none of 27
  SessionStart reads found it, at the hook's start or 0.3s on). So at both hooks the compacting
  side is still at that *compaction point*: its last line a user line a model turn follows,
  with no call of its latest response out. That response is every line with its message id —
  a response's lines interleave with its tools' results (485 of 6,390 responses on this
  machine) — and a new prompt after it drops a call a dead process left unanswered. Then:

  - PreCompact with `trigger: manual` is main's — only the user types /compact, into main;
  - a `prompt_id` not yet in main's transcript is a prompt main just took (a manual /compact
    takes one of its own, measured): main's;
  - main at that prompt and at a compaction point could be the one compacting: main's,
    whatever the agents show — main losing its request is the worse error;
  - otherwise main is idle, writing its reply, waiting on a call of its latest response (a tool
    it runs; a foreground agent, alone or beside calls already back), sitting after lines no
    model turn follows — a local command's (`<command-name>`, `<local-command-stdout>`:
    /compact, /context), bash mode's (`<bash-input>`, `<bash-stdout>`: a `!` command, which
    takes a prompt id of its own) or an Esc's (`[Request interrupted by user…]`, at the prompt
    it interrupted); over this machine's transcripts no model turn followed any of 131, 2 and 18
    such lines (the host's own `No response requested.` once did) — or already past the hook's
    prompt (compacting, it could not have moved on), and the compaction is an agent's: one at a
    compaction point, anywhere under `<session>/subagents/` (Workflow agents sit in
    `workflows/<runId>/`), the latest if several.
    PreCompact then says nothing, and SessionStart(compact) hands the agent its SubagentStart
    slice (a fork too: the capsule it carried from main is what its compaction summarized away;
    `hippo:lane` nothing, as at its start).

  Measured live (2.1.282, 2026-09-26, sonnet; each hook's owner read afterwards from where its
  user line and boundary landed): fork mode on, a background Agent and a Workflow agent
  compacting while main sat idle — 8 PreCompacts silent, 4 SessionStarts the slice, main's own
  compaction in its notification turn asked and got its capsule, and no agent summary carried
  the section; a manual /compact while a background agent read — main's request and capsule,
  the agent's later compactions silent with the slice; main compacting mid-turn, once with a
  Workflow of its own still out — 3 of 3 requests, each summary with the section; fork mode off,
  a foreground agent compacting while main waited — every PreCompact silent, 3 of 3 slices; main
  compacting right after a foreground agent returned — 2 of 2 requests. The 1.15.1 case is
  inside the rule: main blocked on a foreground agent has that call out, whether its transcript
  ends in the call or in the result of another call of the same response. The first cut of
  this rule read main's last line alone, and that second shape broke it — measured, fork mode
  off: main made two foreground Agent calls in one message, and for the 100s between the first
  one's result and the second's its transcript ended in that result at its current prompt, so
  the second agent's compaction took main's request and capsule. With the call read as out,
  that snapshot names the agent, and live (the same two calls, the second agent paging a file)
  its 4 PreCompacts were silent and its 3 SessionStarts the slice while main's transcript ended
  in the first one's result.

  Still main's, so wrong: an agent compacting while main, at the same prompt, waits on its own
  reply — main is at a compaction point either way (measured: 3 requests and one capsule into
  an agent while main wrote a long reply; each summary's section read `none`) — and one
  compacting while main's own manual compaction runs, whose prompt is not on disk until it ends
  (2 measured).
  Timing could split the first at PreCompact (main's own line is under a second old there), but
  a threshold would rest on hook latency this project does not control, and SessionStart could
  not follow: there both sides' lines are as old as the compaction. The other way round, a
  user line of main's that misses the 0.25s read makes main's compaction an agent's if one is
  at a compaction point then: 1.15.1's measurement bounds that write (main's result was on disk
  by +50ms in 4 probes) except for a result written on a resume (a killed call's "interrupted"
  result was not on disk 247ms after its stamp). A finished agent stays where it ended for
  good, so the ends that are no compaction point matter: a Workflow agent ends in its
  StructuredOutput call's result (178 of 178 on this machine, no line after any), one stopped
  by Esc in the interrupt line. Read as a bare user line, 197 of 457 agent transcripts on this
  machine were candidates for good (17 of the 87 sessions with agents had one, and there main's
  side alone decided); read as compaction points, 9 are (8 sessions), 7 of them agents killed
  mid-call, their last line that call's result (`Exit code 137`). Scoping the two texts
  instead — the request telling a conversation that began with another agent's brief to write
  `none`, the `compact:` line saying it is main's — was weighed and left out: the summarizer
  would be judging whether its own conversation began with a brief, and a user's first prompt
  can read as one, which costs main its request, the worse error; what an agent does with
  either line was not measured.
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
3c. **Native runs** (1.15.0, in every mode; the key only adds the triage). Most delegation now
   goes through the host's own subagents, which the wrapper cannot wrap — 30 days on this Mac,
   49 `hippo log dispatch` calls, nearly all beside an Agent call — so main spent turns logging
   what the clerk then recorded under an id of its own, and no such run's cost reached PRIORS. A
   Claude Code session keeps every end of one on disk, and the scribe reads them itself.
   **Index** (code, before the prefilter, so a completion in a skipped window is not lost): one
   streaming pass over main's transcript up to the window's end — a completion may belong to a
   launch long before the cursor — finds each Agent/Task call the host confirmed
   (`toolUseResult.status: async_launched` and its `agentId`; a foreground call's `completed`
   carries its report inline), each Workflow launch (`runId`, `taskId`, `workflowName`, the
   script) and every `<task-notification>`, on a user line or a `queued_command` attachment
   (the `queue-operation` lines around it are bookkeeping, Bash background tasks notify in the
   same shape under ids no launch has, and the task-id always names the agent: the
   `<tool-use-id>` names the call it answers — the launch or a SendMessage, on 2.1.237 and
   2.1.280 alike — and is missing when the agent resumed on its own background work, or (once,
   on 2.1.237) on the end of an agent a SendMessage reached while it still ran). An agent's
   own Agent calls live in its transcript, and its `subagents/agent-<id>.meta.json` names the
   `parentAgentId`: nested runs are indexed through their parent and carry
   `parent: ag-<parentAgentId>`. A Workflow's own agents are not runs — the run is one — and
   a `hippo:lane` agent, which only babysits a codex lane the wrapper records, is skipped:
   beside main as a run, and inside a Workflow as one of its agents — the run's usage, exec and
   changes leave it out (measured live: a relay's 17,304 of a run's 41,740 tokens, and its `low`
   effort tied the run's `high` for exec), and a Workflow that ended with nothing else gets no
   row. **Ends**: a run ends at a notification — or, where none comes, at main's TaskStop
   naming its task id, and a Workflow at its run file saying `killed` or `failed`: the host
   writes `<session>/workflows/<runId>.json` at a launch's end with its `taskId`, `status` and
   end time, and a stopped or failed Workflow never notifies (measured, mlx-vlm: 4 runs stopped
   by TaskStop, all `killed`, none notified, one after 2.59M tokens no usage row carried). A run
   file naming another task is an earlier launch's — a resume keeps the runId.
   **Rows**: a run's id is `ag-<agentId>` (`ag-<runId>` for a Workflow), which main sees at
   launch. The kind is the one slot a reading of the brief has to fill, and the clerk reads
   the turn anyway: the payload lists each run with no row yet, launched within 24h, as
   `ag-<id> · <executor> · <description> · brief: <first 300 chars>`,
   the clerk answers `{"ev":"dispatch","id":"ag-…","kind":…}` for every line, in every window,
   whether or not the digest is about that run (its prompt says so in those words), and code
   keeps only the kind — one of the dispatch skill's tags, else the event is dumped and the run
   stays listed — and fills the rest from what the run left behind: `exec` = `fork` (meta
   `isFork`), `workflow` or `subagent` / the agent's own `message.model` with the context tag
   and snapshot date stripped, which is the prices.yaml key (a Workflow run's most-used across
   its agents; while no line exists, what the host resolved — an Agent launch's
   `resolvedModel`, a Workflow run file's `defaultModel`) / the `effort` its assistant lines
   record (`inherit` when none does — haiku's don't); `scope` = the call's description or the
   workflow's name; `task` = the one tasks.yaml id the brief — or a Workflow's `args`, the
   value its script reads — names as a whole token; `parent` as above. A Workflow's row waits
   for the run's end, quietly, and the run stays listed until then: its exec is its agents'
   majority, and a row written at the first Stop read whatever lines existed a second after
   the launch — measured live, `workflow/claude-sonnet-5/low` where the whole run read `…/high`,
   and, before any agent's first reply, no model at all, which dumped a failure for a run that
   was only starting. A listed run that has **ended** with no row after the clerk — it skipped
   the line, its kind was refused, or no clerk answered the window — gets its row from code,
   under the kind `unclassified`, so every run that ends within its listing has exactly one
   row. Measured (2026-09-25): the cheap clerk recorded a listed run only when the window's
   digest was about it; hippo's first 1.15 windows listed 4 finished Workflow runs, the clerk
   answered `events: []`, and they aged out of the list with 284M tokens recorded nowhere —
   open-webui lost 6 Workflow and 5 Agent runs the same way. Code has no honest reading of a
   brief, and a guessed tag would sit in a real PRIORS column: `unclassified` is a column that
   says the kind was never read, while the cost and any verdict are kept. An outcome naming
   such a run in the same output gets the row first and lands on it. Main's own `log dispatch`
   for a run is its record — a `src=cli` dispatch whose id is the run's bare runId or agentId,
   the one the launch printed (measured, mlx-vlm: 15 of 17 Workflow runs had one beside the
   scribe's `ag-wf_…` twin, main's verdicts on one row and the cost on the other, so no
   Workflow's cost reached PRIORS), or within the same 24h whose scope equals the description
   (case and spacing aside) — and the scope match is exact on purpose, which is its limit: a
   scope main paraphrased is not recognized and the run gets a second row, where a similarity
   guess would cost runs their rows (6b measured those). Main does paraphrase: all 4 of its
   `log dispatch` rows for Agent runs in this repo's ledger did ("Jev judge layer + scribe
   gate (wave 1)" for "Jev wave 1: client + scribe gate"), which is why the skills now say no
   call is needed. When main's row is a run's record, a clerk outcome naming the run's own
   `ag-` id lands on that row: the skills teach main that id, so main judges the run by it, and
   an outcome checked against the ledger alone would be dumped in exactly the twin case this
   closes (replayed: main's bare `wf_…` row, "ag-wf_… is accepted" in the turn, and the
   verdict reached only failures/). `hippo log outcome --ref ag-…` for a run with no row yet
   says so, and that the verdict is read off the turn — only for a ref that can be a run id
   (`ag-a…`, `ag-wf_…`): a Workflow launch prints its Task ID first, and a ref built from that
   is told it names no run rather than "no call needed", which would drop the verdict; a ref
   whose bare id main logged itself is pointed at that row. **Usage** (code): at every end — a
   notification, or a stop with none — and for a run whose row landed after its end, the
   run's own transcript(s) are summed per model — one API message once, though it spans
   several lines as it streams (49 of 59 messages in one agent); nothing before the first user
   line, because a fork's transcript opens with main's own launching message copied in, main's
   message id and usage; no `<synthetic>` line. The notification's `subagent_tokens` is the
   final context size, not spend, and is never read. A row equal to the last one for
   `(ref, model)` is not written again, so re-reading a window writes nothing twice, and a
   resumed agent gets a fresh cumulative row at its next completion.
   **Triage** (judge on only): a run's answer to its brief is read exactly as a wrapper lane at
   exit (§3.6), `src=scribe`, once per dispatch, at most 8 calls a window, and only in the
   window where that answer became complete — a resumed agent's later report is never triage
   material, even when the first reading failed, and a run stopped with no notification has no
   answer to read. The answer is the run's notifications up to the first after its first that
   names a call other than its launch — that one answers a SendMessage. It is complete at its
   first final notification, and every one up to it is
   read, in order: an interim report before it can be the whole report, the final one after it
   only a line saying its watcher expired. An interim one — the agent stopped with background
   work of its own still running, and says so — completes it only where the host's final one
   cannot be waited for: once the agent has answered a SendMessage (main moved on from the
   interim report), or once the agent's own transcript shows it idle since its last report
   with all the work it had started ended — each piece by its own notification or a TaskStop,
   and a Monitor with neither at its deadline, the `timeoutMs` its launch result states whatever
   the call asked for — 0, none, for a `persistent` one, which hosts 2.1.246–258 ran and which
   ended with a notification each time. A Monitor's events carry no status, its expiry included,
   and one after the report only moves that end later, to its own time: an expiry that wakes the
   idle agent just after the deadline leaves the run unsettled before it (an agent gets its
   expiry from 0.40s before the deadline to 0.27s after). Never earlier: the host can hold a
   notice for an idle agent and write it into the agent's transcript, under its own earlier
   time, only once the agent is resumed (mlx-vlm has an expiry written almost 9 hours after its
   time), and an event read as the end then would settle the run back in a window that had found
   it still watching — a run never triaged. That moment is fixed in the agent's transcript, so
   the window that finds it is the only one; the interim reports, in order, are the report.
   Measured (mlx-vlm, 2026-09-23): 3 of 14 agent runs sent only interim notifications. In each
   the work left was a `tail -f` Monitor that expired 5–11 minutes after the agent's last
   report; the host queued the expiry in main's transcript and never delivered it to the idle
   agent, so it never resumed and no final notification came in the two days the session ran on
   — and each interim report was the agent's whole report. A fourth's only final notification
   answered its second SendMessage; its answer to the brief is its first, interim report. What
   stays open is a background command whose end never reaches the agent's transcript — that run
   is read when it ends, or never — or reaches it only once the agent is resumed, whose earlier
   time settles the run back in a window that found it running (the host writes a task's end
   late too: 10 recorded, all to an agent mid-turn, none yet to an idle one), and a Stop in the
   fraction of a second between a Monitor's deadline and an expiry that reaches the agent after
   it, which reads the run as settled.
   brief = the call's prompt (a Workflow's script); report = the answer's `<result>`s (a
   Workflow's whole result from `workflows/<runId>.json` — the notification's copy is cut at
   ~8k — handed to the judge as the JSON structure it is, not as a string of it whose every
   quote is escaped, and fitted by its structure like any over-budget report, §3.6: measured,
   2 of 23 results were over the budget, 226,811 and 129,195 characters of research and
   design output; with every finding and field kept, 305 of 657 strings cut to 215
   characters and 4 of 174 to 3,833, both fit); rc 0 only on `completed`; changes are read
   agent by agent (an Agent run's one agent, each of a Workflow's but its hippo:lane agents):
   git facts from the agent's worktree when it ran isolated and a base is honest (a HEAD that
   never moved: the working tree; one that moved and has not reached main's branch: its
   merge-base with main's HEAD), a Workflow agent's `worktreePath` being in its own meta.json
   under `subagents/workflows/<runId>/`; else the files it edited with its edit tools plus its
   `edited_text_file` attachments, those in the project only, headed as what they are (a
   shell edit of a file never read is not visible) — never main's tree, which also holds
   main's work. Measured on wf_cf850115-50b: 5 of the 12 paths its agents edited were scratch
   files under the session's scratchpad, its fix agent's commit in its worktree — three files,
   no edit-tool call — was in no list, and the heading called the lot "files this agent
   edited"; read against the real root, its list is the 7 project files and wf_a1d55d12-410's
   44 paths are 9 (both worktrees since removed, so the lists stand in for git). The route
   never reaches the clerk.
   Nothing here raises out of the scribe: a bug is dumped to `failures/*-native-*` and the clerk
   runs as always. Replayed Stop by Stop over four real sessions on this Mac (2026-09-24, mock
   clerk and judge, scratch ledgers): 55 runs — 26 subagents, 3 forks, 3 nested forks, 23
   Workflow runs — got 55 rows; the 51 that had notified got usage (53 per-model totals over
   seven models), and 46 were triaged — the other five being 2 Workflow results over the judge's
   budget and the 3 interim-only runs, all five triaged since 1.15.1. Replayed again over the
   same windows (2026-09-25), the mlx-vlm session went from 26 to 30 of 32 runs triaged and
   the hippo one from 9 to 10 of 12, the rest not yet notified; over both sessions as they
   stood a day later, 60 of 65 and 12 of 13 — every run that notified (the 5 left had been
   killed, and hippo's last was still running) — and none twice. The clerk had recorded two
   of the same Workflow runs as `workflow/subagent/inherit` and `workflow/unknown/inherit`;
   code read `workflow/claude-opus-5-5/xhigh` for both. The index costs 0.05s over a 24MB
   transcript, and 0.10s over 39MB, 0.03s of it reading the agents whose answer is interim.
   Replayed from the Stop where 1.15 went live in each of three projects (2026-09-26, main's
   own rows injected at their times, runs launched from 24h before it; the mock clerk answering
   only the runs a window launched or notified, which is what the cheap clerk was seen doing),
   the release against this fix: hippo 9 → 12 of 13 runs with a row (the 13th still running),
   the 4 that had aged out now `unclassified`, recorded tokens 251M → 536M; open-webui 0 → 13
   of 13 (production had 2), 309M tokens; mlx-vlm 15 twins → 0, ended runs with usage 16 → 17
   of 17 (the killed one's 2.59M), main's 15 verdicts on the row that carries the cost 0 → 15,
   Workflow cells in PRIORS with tokens 0 → 9 of 14. With a clerk that answers nothing, every
   ended run still gets its row. And the real sonnet clerk, on hippo's window where main
   merged two Workflow runs' work to `dev` and closed their task, recorded both verdicts in 1
   of 5 runs with the release's prompt and roster, and in 5 of 5 with this one's.
   **Codex is unchanged**: a `spawn_agent`'s brief is encrypted in the rollout
   (`gAAAAB…`), so the clerk keeps recording those children from the digest.
3d. **Worker writes** (every mode, no judge; after 3c's usage and triage, on every path). A
   native worker's shell carries exactly main's environment (§3.4), so a `hippo task done` it
   runs lands as main's, and nothing at write time can tell — a per-call hook is the only other
   place that could, and §4 keeps those out. The scribe reads it afterwards, from the runs 3c
   indexed: for each run the window touched or ended (a TaskStop, or a run file saying `killed`
   or `failed`, ends one with no notification, 3c; past 24h that window is the last to read it)
   and each still running (launched within 24h), the run's own transcript(s) — an agent's, a
   nested agent's, every agent of a Workflow (`subagents/workflows/<runId>/agent-*.jsonl`) but a
   `hippo:lane` relay, which is not read beside main either (3c skips it both ways; it runs
   `hippo dispatch` and `--watch`, neither a write counted here) — and never main's, so main's
   own calls never count; nothing before the first user line (a fork's opens with main's
   launching message).
   Each Bash command is walked the way step 9's replay walked main's own calls: quotes, operators,
   redirections and heredoc bodies (a brief that quotes a command is text), `H=hippo` and
   `${v%%pat}` expanded, a `for … in …; do … done` run per item, `h() { hippo … "$@"; }` taken as
   an alias; each hippo command in it is read by hippo's own parser. The set is the writes that
   are main's (§9.2: a worker may not say a task is done or its work accepted, and may not
   rule): `task add|set|done|drop`, `directive add|withdraw` and `log outcome`, `log raw` of
   either kind included; a dispatch, a review or a read is an observation a worker may make.
   A call counts only where **this project's state shows it landed** — a task whose `updated`
   stamp falls between the call (to the second, as the CLI stamps) and its result, or the
   notification that a call sent to the background ended, and whose fields hold every value the
   call set (a `done`'s or `drop`'s status, the field a `set` wrote as tasks.yaml stores it, an
   added task's title, status, notes and deps), or a `src=cli` ledger row stamped there whose
   kind, id and values do (a directive's state and text, a verdict's ref and result). The state
   proves the call's success and its project at once. Resolving the project from the cwd and
   the command's `cd`s cannot follow a `cd` into a variable set elsewhere, `HIPPO_DIR` or a
   script, and reading success from the output misses output a command discarded (`2>&1 | tail
   -2` after a `directive add` can keep its notes and drop its record); a call against a scratch
   copy, a worktree outside the repo or another project, or one that failed, leaves nothing here
   to match, and one from a worktree of this repo does. The values are part of the match
   because the thing alone is not: a `task set <id> notes` matched on its task claimed main's
   close of that task in the same window, and a background call's window runs as long as the
   command does. `log outcome --from-batch` is not counted: its verdicts are named in a journal
   whose path is relative to a shell the scribe never sees, so nothing on the line says which
   rows it wrote, and matched on its kind alone it claimed any verdict main wrote in the window
   (both shapes probed in review, 2026-09-26). None of the worker writes measured below was one.
   The record is `worker-writes.json` — `{"task:<id>" | "directive:<id>" | "outcome:<dispatch
   id>": {t, run, op}}`, `t` the state's own stamp, the latest write per key winning — which
   §6's `worker:` line reads. A write shows until main writes to the same thing after `t`: a
   task until its `updated` passes `t` (every task write moves it) or it leaves tasks.yaml, a
   directive or a verdict until a later event on it from the CLI or the scribe. The file is
   written under the scribe's lock, merged and pruned of what no longer shows, like
   task-flags.json (step 9), and it is no ledger event (§3.2 stays exactly as it is): the fact
   is re-derivable from the transcripts while they exist, and the file holds only what still
   asks for main. A worker's verdict remains the first one PRIORS reads (§3.2); main's own
   verdict after it clears the line and stays in the ledger as the correction, and a wrong
   first-pass entry is a `tools/ledger_edit.py` fix — no worker verdict has been measured.
   Measured (2026-09-26): replayed Stop by Stop over mlx-vlm session 01994ca9 (475 windows, 54
   Workflow runs) against a scratch copy of its ledger and tasks (tasks.yaml plus the archive
   the done ones moved to), the one real write — `wf_a868d1a2-52f`'s agent ran `hippo task done
   ops/artifact-pruning-20260924` at 20:20:16Z, as main's workflow script asked it to — was
   recorded at the first Stop after it, 20:27:54Z, and nothing else was. Over this repo's own
   session (7dd428b0, 52 windows) it parsed 46 worker writes — 33 directive, 12 task, 1
   outcome — every one against a scratch project, and recorded none; open-webui's (847fa716, 67
   windows) had none. The step took 0.023s a window at the median and 0.085s at most across the
   three, reading up to 38 transcripts (24MB) in one window. Re-run once the match took every
   value a call set, the three replays found the same.
   Live (2.1.282, sonnet, mock clerk): a background Agent's `task done` and a Workflow agent's
   `directive add`, each asked by main's brief, were recorded under their runs; main, resumed,
   read the line, confirmed both with writes of its own, and the next scribe emptied the file.
   Not seen: a write made from a script, through `xargs` or `bash -c`, or through an alias from
   the user's shell profile. A write main makes inside a worker call's own window to the same
   thing with the same values — the same status on the task, the same verdict on the dispatch —
   reads as the worker's; it costs a line.
4. Resolve the backend: `config.yaml > $HIPPO_CLERK_BACKEND > automatic (codex/gpt-6-luna/low when
   codex exists, otherwise claude -p sonnet at low effort) > mock` (for tests). That pair is hippo's
   one cheap tier (the lane agent, §3.6, is its sonnet-low half); hippo runs no haiku (the A/B note
   in clerk_run.sh). 120s timeout. `$HIPPO_CLERK_MODEL`
   overrides the model on whichever backend is resolved; it is one variable for both, so pin the
   backend when you set it — a model id for one backend is invalid on the other.
5. Prompt = `clerks/turn-scribe.md` + the live directive roster + the recent dispatch roster + the
   native runs to record, when any (3c) + the digest. Both rosters exist for one reason: an id
   the clerk coins for a subject that already has one forks it instead of updating it, and the
   digest cannot be relied on to contain the existing id. The dispatch roster is also the set an
   outcome may legally `ref`, with the listed native ids the same output records. It is the last
   12 dispatches plus every row of this session's native runs launched within 24h that has no
   verdict yet, each of those with its task and a run's worktrees (`· task …`, `· worktrees
   wf_<run>-N`): main judges a native run by what the digest shows of it — its branch, its
   task, its description — rarely by its id. Measured (2026-09-25): 0 of 21 `ag-wf_` rows ever
   got a verdict, so no Workflow's cost reached PRIORS, though main had merged the work of every
   finished one in hippo; and 17 rows landing in one mlx-vlm window left 5 of them off the last
   12. The prompt says the same in rule 2 (the A/B in 3c). Expected output = strict JSON:
   `{"worklog": "…", "events": [ …ledger events without t… ]}`.
6. Validation, **per event**: check each event by the same rules as `hippo log` (per-ev key
   whitelist — unknown keys rejected; `t` and `src` are always stamped by the writer; exec shape
   and ref existence as in §3.2). A rejected event is dumped to `failures/` and skipped; the
   turn's other events and its worklog line are still recorded. One hallucinated ref must not
   erase a good worklog — that is what "the dump is the record" means. A failure *of the clerk
   call itself* (no JSON, wrong envelope, nonzero rc) is still all-or-nothing and records
   `ev:clerk ok:false`. Either way the ledger is never contaminated and **the cursor advances**
   (never re-bill the same input forever). **Never fill a gap by inventing content.**
6b. **Five extra rules, on the clerk's output only.** A scribe `usage` is rejected — the
   wrapper observed the cost and was there when the lane ran. A scribe `dispatch` is rejected when its
   executor is `codex`, or when either closed slot of `exec` is outside its vocabulary
   (`codex|claude|fork|subagent|workflow` / `low|medium|high|xhigh|max|ultra|inherit`). A scribe
   `outcome` is rejected when its `ref` already has a verdict: "at most one outcome per
   dispatch" was in the prompt and the roster marked the judged ids, and the clerk re-judged
   anyway — measured on this repo's own ledger, one dispatch judged `revised` was re-judged
   `accepted` by two later scribe runs, and a verdict main had recorded through the CLI was
   restated by the scribe 26 seconds later. A prompt cannot hold a rule its writer does not
   check. Main's writes are not checked either way and should not be (a deliberate re-verdict
   is main's call; it gets a stderr note, §3.2). And in a window where 3c indexed native runs,
   a `fork`, `subagent` or `workflow` dispatch under an id 3c did not list is a restatement and
   is rejected — on Claude Code the Agent and Workflow tools are the only way one starts, and 3c
   read them. The costly way to get this wrong is to coin an id and put main's verdict on it, so
   the rejection does not cost the verdict: when the restatement says which run it meant — its
   scope is one run's description (this window's runs first, then every indexed one), or it
   names none and the window touched one run that this output did not record under its own id
   — that run is recorded with the restatement's kind if it had no row, and an outcome naming
   the restated id lands on the run's. The exception in that last clause is the restatement of
   an *older* run in a window that touched one new run: moving its verdict onto the new run
   would put a wrong verdict in a cell and block the right one, so it stays in the dump. For the
   same reason an outcome naming a listed run whose dispatch this output refused or skipped is
   dumped, saying so: the next clerk never sees that verdict.

   The line is *who observed the value*, not who is trusted. The launcher builds `exec` from its
   own argv and a handed vocabulary holds — measured, 0 malformed in 110, and `kind` has held the
   same way with no validation at all. The scribe infers `exec` from a transcript, and that is
   where a vocabulary stops working: 12 malformed in 45, every one scribe-written
   (`background/CPU/sol-high`, `unknown/GPT-5.6/unknown`, `bash/unknown/unknown`).

   A codex launch belongs to the wrapper, which was present when it happened. The scribe writing
   one yields either a duplicate — every confirmed pair measured was scribe-vs-launcher, 27–167s
   apart — or a record of a launch that bypassed the wrapper, which is a gap better seen as a gap
   than filled with an inferred row that then dilutes the priors. What only the scribe can see, and
   must keep recording, is what the wrapper cannot cover: `fork`, `subagent`, `workflow`, and
   `claude` for another Claude Code session (measured: one `fork` arm of a design duo existed in
   no other record).

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
9. **Open tasks that look ended** (judge on only). A task main finished and never closed stays
   open in tasks.yaml, and every capsule after it re-injects the task as open work — the stale
   record PreCompact's request catches only at a compaction (§3.4). So after step 8, on the
   clerk's success and failure paths alike, the scribe asks `clerks/jev/task-end.yaml` over the
   window's digest — the one the gate read — beside every open task (`pending|active`; id,
   title and notes as tasks.yaml holds them), one `done_{i}` each: does the digest show that
   task's work finished or abandoned? It is a request of its own, never merged into the gate's,
   whose recall was measured on a digest-only state (3b). An answer at or over `recheck_at` 0.5
   is asked again with that one task alone beside the digest; at or over `flag_at` 0.7 on the
   recheck, the task is flagged. Every request is metered (`ev:clerk name:jev-task-end`,
   `src:scribe`); a failed one writes that row `ok:false`, puts its reason on stderr
   (`jev-task-end: …`) and flags nothing. A flag is `{t, p}` under the task's id in
   `task-flags.json`, `t` being when the scribe read the tasks, and it is shown, never applied:
   the capsule's `check:` line asks main (§6), and no status changes. It is not a ledger event —
   §3.2 stays exactly as it is — because what shows is derived from the file and tasks.yaml at
   every read: a flag goes quiet by itself once main writes to the task after it (its `updated`
   stamp passes `t`) or the task is no longer open. The file is written here only, under the lock
   the scribe holds for its whole run (step 1): read, merged with this window's flags, pruned of
   every flag that no longer shows, and replaced whole — two sessions' scribes keep each
   other's flags.

   It runs last because it reads nothing the scribe wrote (the clerk never writes tasks.yaml)
   and nothing waits on it: a slow or failed judge holds up no clerk and no distiller, and a
   flag lands at most one Stop later than it could. Its windows are the ones the replay below
   asked — every one past the prefilter, whatever the gate answered (the gate decides nothing,
   3b), and none with no task open. **Codex runs it the same**: the question reads only the
   digest, and digest_lite reduces a rollout to the line vocabulary the gate already reads
   there. The replay's windows were all Claude Code's; one Codex turn, smoked live (btop's first
   hippo turn, which squash-merged three tasks), flagged all three (0.96, 0.91, 0.97).

   Measured (2026-09-25): a replay of every Stop in 6 sessions over 5 projects on this Mac —
   592 windows, 96.3% of the task judgments from one mlx-vlm session, each window's rosters
   rebuilt as they stood then — asked 5,635 task judgments in 715 requests, median 672 ms each.
   Of the 22 flags that fell outside the window where main closed the item, independent
   adjudication labelled 15 right and 7 wrong; 6 of the wrong ones were task flags, all one
   shape — one stage merged, the task goes on. That is 6 in 5,635, 0.106% false task flags per
   judgment: at some 2,000 judgments a day (20 open tasks, ~100 Stops) about 2 wrong flags
   against 5–15 real closures. Wrong flags would outnumber right ones only from 0.25–0.75%.
   65 of 69 task closures were flagged at or before main closed them and 16 at least one window
   early, but only 5 of those 16 had a session start in between, where the line shows (4 right,
   1 wrong): the line earns its place on the task main does not close in the turn that finished
   it, and a wrong one costs a line and a note. The bars jitter — an identical state re-asked
   moved a stage-1 answer by up to 0.09 and a recheck by up to 0.03, and 4 task rechecks sat in
   [0.68, 0.72] — so a flag at the bar may come and go between runs, and they are not retuned on
   this data. The shipped step, run live over the replay's 20-window pilot (the 18 windows with a
   task open, 28 requests, one window through the whole `hippo scribe`, gate included), raised the
   same 7 task flags plus one in that band: a recheck the replay read at 0.68 read 0.70, and 0.66
   when asked once more — main closed that task in that very window. Its 109 stage-1 answers moved
   from the replay's by 0.01 on average and 0.10 at most, none across `recheck_at` — the replay's
   state also carried the live directives, the one input that differs. Asking about those was the
   replay's other half — per live directive, did a user line drop, reverse or complete it — and it
   caught 3 of 15 withdrawals, so it was not built.

### 3.6 The dispatch wrapper (`hippo dispatch`)

A codex exec wrapper: the point that already knows the model and effort from its own argv is
exactly the point to collect them automatically (principle 6). It takes the `--kind`, `--scope` and
`--task` labels, records `ev:dispatch`, prints the dispatch id on stdout's first line, and then runs
`codex exec … < /dev/null`, passing its stdout through unmodified. A `--fast` flag prepends
`-c service_tier="fast"` to the codex arguments — a per-launch latency choice carried in argv like
the rest of codex's grammar, invisible to the exec axis. It also plants
`HIPPO_DISPATCH=<id>`, `HIPPO_DEPTH` and `HIPPO_DIR`
in the child's environment, and puts its own `bin/` first on the child's `PATH` — the whole of the
executor data plane's wiring (§9.1, §9.2, §9.5). The `PATH` entry exists because codex adds no
plugin `bin/` (§8), and the dispatch skill had told main to pin the absolute `bin/hippo` in
COMMON.md: a versioned cache path that the next plugin update deleted (1.14.2, found in steno). A
launch made from inside a lane records that lane as `parent`. Since 1.10.0 the wrapper is a
pass-through rather than an exec: it stays alive to *read* (never rewrite) the stream — the
banner's session id and model, the "tokens used" footer — and at lane exit records `ev:usage`
from the rollout or the footer (§9.6). Those markers ride codex's *stderr* (measured, 0.144.6;
stdout carries only the agent's own output) — so stderr is the stream it reads, and stdout
passes through untouched. 1.10.0–1.11.0 read stdout instead, saw no session id, and
silently recorded nothing — the stub test had encoded the wrong stream; fixed in 1.11.1. It does not record an
outcome — the acceptance judgment belongs to main (through the CLI directly) or to the scribe
(by inference); what the lane itself records under that id is a claim, never the verdict.

**The lane's own record** (1.15.0). In Claude Code a background `hippo dispatch` showed as a
shell row labelled with its raw command, and nobody could see what the lane was doing: codex's
raw stderr runs to megabytes (one lane: 56k lines, 2.3MB), so 18 of 18 real background launches
redirected it (`> log 2>&1; tail`), and the shell's details view shows the last 10 lines of the
last 8KB. The wrapper now keeps what those redirects improvised. codex's raw stderr goes whole
to `.hippo/lanes/<id>.log` — still read on the way for the banner, the footer and triage's
400-line tail — and stdout, passed through byte for byte, is kept as `<id>.out`: the agent's
final message, which is the lane's report. The shell's stderr gets a compact stream instead:
one line per new codex command (`lane <scope> · <elapsed> · exec: <command, one line, ≤100
chars>`, recognized only in codex's own `<shell> -lc '…' in <cwd>` shape — 1,657 of 1,657 in
sixteen real logs) and per agent message (`… · said: <first sentence, ≤120 chars>`), at most
one line per 3s with the newest event winning, then the final line (`exited rc=0 · 14 cmds ·
187,135 tokens · raw log <path>`) and the triage line. No compact line may read as a prompt:
Claude Code wakes main when a background shell has not grown for 45s and its last line matches
one of seven patterns in its binary — `(y/n)`, `[y/n]`, `(yes/no)`, a `Do you|Would you|Shall
I|Are you sure|Ready to …?` question, `Press any key|Enter` (no word boundary: `express any key`
matches), `Continue?`, `Overwrite?` — so the three characters those need — `?`, the space after
`press`, the slash of `y/n` — are swapped for look-alikes. `.hippo/lanes/<id>.json` holds only
what the rollout cannot give back cheaply — `id scope exec pid pgid started codex_session cmds
last last_at log report` (paths absolute: the record is read from any cwd), then `status` (`exited|killed`), `rc`, `signal`, `triage` and, written
last, `ended` — rewritten whole at the stream's cadence, and a new lane start prunes lane files
older than 7 days (no schedule, §4). Batch lanes run through the same machinery: `<id>.err`
keeps its shape as the raw log, the compact lines join the batch's stderr, and each lane gets
its record under its dispatch id. Without `.hippo/` there is no record, and the raw log goes to
a temp file the final line names.

**The kill trap.** codex runs in a session of its own, so a signal reaches it only through the
wrapper: SIGTERM, SIGHUP and SIGINT are forwarded to codex's process group (SIGKILL after 5s if
it lingers, and whatever of the group outlives codex is killed with it), the record says
`killed` and the signal within 0.5s — not once codex has exited: a stub codex taking 3s to exit
was SIGKILLed with its wrapper before either was on record — and gets its rc when codex exits,
`ev:usage` comes from the rollout as on any exit, triage runs when the judge is on, and the
wrapper exits 128+signal (a batch stops launching, signals every running lane and exits the
same way; rerunning it resumes). The handlers hold only while codex can run: a signal during
the launch notes, or during a batch's harvest, takes its default action and stops the wrapper. Before 1.15.0 a killed lane recorded
no rc, no usage and no triage — and Claude Code does kill background shells: under critical
memory pressure once main has been idle 30 minutes with no agent running, and with a stopped
agent's shells. Its kill is SIGTERM to the whole process tree, then SIGKILL 1.5s later
(2.1.281), so the order is status and rc first, then usage, then the judge — the part that may
not fit in 1.5s is the part that may go missing.

**Watching a lane.** `hippo dispatch --watch <id> [--for SECONDS]` blocks until the lane's
record says it ended or SECONDS pass (default 540, under the Bash tool's 600s ceiling). Still
running, it prints one line — `lane <id> running · <elapsed> · <cmds> cmds · last: <event>` —
and exits 3; ended, it prints the final lines — `lane <id> exited rc=0 after 5m12s · 14 cmds ·
<scope>`, the triage line when there is one, `report: <path>`, `raw log: <path>` — and exits 0;
no record is 2, outside a project too (like the launch, this surface is never silent: a
watcher told nothing would read it as an end). It reads `.hippo/lanes/` and writes nothing.
A record without `ended` whose
wrapper pid is gone has ended if it holds a status (the wrapper died before codex exited, or
while the judge read) and is `lost` if not. Once the lane ended it waits up to 2s for the wrapper to exit, so the shell
that ran the lane finishes inside the call rather than after it; `--for 0` is a one-shot read.
It exists for the lane agent below, which must keep its turn open while the lane runs — and a
blocking foreground call, never a sleep, is what keeps it open.

**The lane agent** (`agents/lane.md`, Claude Code). A codex lane launched as `Agent(subagent_type:
"hippo:lane", description: "<scope>", prompt: "<the hippo dispatch command>")` gets a row in the
agent panel — Enter opens its transcript, x stops it, and the kill trap records the stop. The agent
is sonnet at low effort with Bash alone (hippo's cheap Claude tier; it runs no haiku — §3.5.4): it
runs the command verbatim in the background, reads the id off its output's `dispatch:<id>` line,
loops `--watch` in the foreground, and replies with the final lines, which reach main as the one
completion notification. Measured before building (spike, 2026-09-24): an agent that ends its turn
while its background job runs shows `completed` on its row, and main then gets an interim
notification plus an extra wake-up turn when the job ends; an agent whose turn stays open on
blocking calls causes neither. Measured end to end (2.1.281, a stub codex behind a real hippo:lane,
2026-09-24): main got one notification per lane — the lane shell's own completion reached the agent
inside its last watch call, not after its turn — and x on the row was recorded `killed by SIGTERM`,
triage included, within a second. The relay's cost, re-measured at sonnet/low (headless 2.1.281, a
stub codex, 2026-09-24): 4 calls, 10.4k cache-read + 4.5k cache-write + 0.6k output tokens for a
24s lane, with exactly the three Bash calls its body names; each further 9-minute watch window adds
one short call. (The first build ran haiku: 17.1k + 7.1k + 1.5k on a 1m27s lane, and it did not
keep to the body's calls.) It is a relay, not the delegate surface §4 retired: it routes nothing
and binds no role — main still chose the exec in the command it hands over — and it never edits
that command, stops the lane or does its work, so no judgment of its own reaches the record. The
plain Bash form stays for the Codex host, which has no agent panel, and for a lane launched from
inside a lane.

**With no record to watch** — `--watch` exits 2 because the project has no `.hippo/`, or the
output never shows a `dispatch:` id — the agent waits on the command itself, in one foreground
call: `f='<output file>'; SECONDS=0; while lsof -w -t "$f" >/dev/null; r=$?; [ $r = 0 ]; do [
$SECONDS -lt 540 ] || exit 3; sleep 5; done; [ $r = 1 ] || { echo "lane: cannot tell whether the
command still runs (lsof exited $r)" >&2; exit 4; }; tail -n 20 "$f"`. The background shell and
the wrapper hold that file open until they exit — codex writes into the wrapper's pipes, and
Claude Code itself does not hold it (measured with `lsof`, 2.1.282) — so the loop ends with the
command; still held after 540s it exits 3 like the watch, and the agent runs it again. `lsof`
exits 0 while a process holds the file and 1 once none does; any other code means it did not run
— 127 where it is not installed, as on a minimal Linux image — and the wait exits 4 with that
line, which the agent replies with, rather than print the file's last lines as if the command
had ended. The first form took every non-zero `lsof` exit for "nothing holds it": with `lsof`
off `PATH` and a writer holding the file for 8s, it printed the launch line and exited 0 at
once, and the agent would have replied and ended its turn on a running lane (measured under zsh,
2026-09-26). This form, live (2.1.282, sonnet, a stub codex, no `.hippo/`, `/usr/sbin` left off
the Bash tool's `PATH`, 2026-09-26), as a plain Agent and inside a Workflow: the wait exited 4
and each reply — main's result, the Workflow's — was that one line (the agent left out the
shell's `command not found` line above it); inside the Workflow the lane was SIGTERMed 7s in, as
the agent's turn ended. Main is told; the lane is not saved. The wait is measured on macOS
(`lsof` 4.91) only. It replaced "end your turn and reply when the completion notice arrives",
which a Workflow breaks: a Workflow agent whose turn ends is done, and its background command is
killed with it (measured, 2.1.282: the lane SIGTERMed 6s into an 8s run, the run returning only
the two launch lines). Re-measured (2.1.282, sonnet, a stub codex, 2026-09-26), as a plain Agent
and inside a Workflow, a 20s and a 570–600s lane each: with `.hippo/` the watch exited 0 for the
short lane and 3 at 540s then 0 for the long one; without it the watch exited 2, the wait held
until the lane's shell was gone — the long one's exiting 3 once at 541s — and every lane ended
`exited rc=0`. Each of the eight replies was byte-equal to its last call's output (the wait's is
the wrapper's lines and the host's `[exited with code 0]`), each Workflow's result held them as
given, and main got one notification per plain agent. A Workflow script launches the agent as
`agent("<command>", {agentType: 'hippo:lane'})`; with no schema it returns those lines as a
string. That run is a Workflow run of its own to the scribe (§3.5.3c): 38,801 sonnet tokens for
the two relays above, which is why the dispatch skill sends a fan-out with nothing after it to
`--batch`. Headless `-p` waits for a background Workflow only ten minutes past main's last turn,
then kills it (`print wind-down … at the wait ceiling`; the lane is recorded `killed by
SIGTERM`), which is why the Workflow's long lane was 570s.

**The row** (`settings.json` → `subagentStatusLine` → `scripts/lane_status.py`). Claude Code
runs a plugin's subagentStatusLine about every 5s with the visible agent rows on stdin. For each
row whose `subagents/agent-<id>.meta.json` says `hippo:lane`, the script takes the lane's
dispatch id from that agent's transcript (the last `dispatch:<id>` or `--watch <id>` in it —
the prompt, which comes first, is main's text and may quote other ids), reads
`.hippo/lanes/<id>.json` walking up from the row's cwd, and prints `{"id": …, "content": "codex
· <scope> · <elapsed> · <cmds> cmds · <last event>"}`, cut to the row's width; an ended lane
shows its status, rc and duration instead, and one whose wrapper is gone with no status shows
`lost` — never a timer still counting. Every other row is left out, which keeps Claude
Code's own rendering (measured, 2.1.281: `◯ general-purpose  <description> … 15s · ↓ 20.3k
tokens`; documented: an omitted id keeps the default, an empty content hides the row) — a
printed row replaces the whole default, type, description and stats alike. Stdlib only and no
subprocess, because it runs on every tick of every session with the plugin on: 25ms median
(29ms max) for a tick with a lane row and three others, 4ms where no lane ever started. How
the plugin reaches its own script: Claude Code 2.1.281 neither substitutes
`${CLAUDE_PLUGIN_ROOT}` in a plugin's settings.json nor exports it to that command (measured:
it expanded to nothing), the documented placeholder list covers skill and agent content, hook
and monitor commands and MCP/LSP servers only, the plugin `bin/` is on the Bash tool's PATH
but not this command's, and the command runs in the project directory. So the command is a
short `sh` walk up from there to `.hippo/lanes/.statusline`, a pointer holding the script's
absolute path that every lane start rewrites (best effort: lanes starting in one instant share
its tmp file, and a lost rename must not cost a lane its record) — the wrapper is the one
process that knows where the plugin lives — and a project that never launched a lane pays one
`sh` per tick and no python. The pointer is data that names code the command runs as the user,
on every tick of any session with the plugin on, so the walk stops where `find_hippo` does (a
`.git` directory, `$HOME`: an ancestor's `.hippo/` may be another user's — a world-writable
`/tmp` is one), and it runs only an absolute `…/scripts/lane_status.py` the user owns, named by
a pointer the user owns. After a plugin update the pointer names a deleted cache path until the next lane
start, and until then every row keeps its default. The plugin's value is a default: a user's
own `subagentStatusLine` replaces it (and would have to render hippo:lane rows itself).

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
launched on gpt-6-luna/medium`), and a brief that may contradict a live directive the lane's
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
batch's already-made routing and expires with the batch. Each entry launches through `codex
exec`, stamped with `HIPPO_DISPATCH`/`HIPPO_DEPTH` and recorded as `ev:dispatch` + `ev:usage`
exactly like a single dispatch — the §3.2 schema is unchanged, and usage rides the same stderr
banner and footer this section already reads. The manifest keeps its `executor` key with
`codex` the one valid value; the `claude -p` adapter beside it was retired in 1.15.0 (§4), and a
manifest that still names it fails with one line saying what replaced it.

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
resolved and checked at validation) is the child's working directory and its check's; a lane
may carry `-C` in `args` instead — triage reads whichever directory the lane worked in. The
flags that used to choose all of this measured zero calls across 28 projects and were retired
(§4): a feature that needs a flag is a feature main does not use.
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
lane's summary of itself is at the end) and the record names what was cut — the journal line
and the `ev:triage` row's `trimmed` — no silent shortening, and no answer invented for a judge
that failed: `route: null` and an `ev:clerk name:jev-harvest ok:false` row are the record. A
structured report (a Workflow's JSON result, §3.5.3c) is never cut through the middle: every
key and item stays, and every string in it is cut to the one longest length that fits,
keeping its head, where a finding or a field states its point. Below `TRIAGE_LEAF_MIN` (120
characters, about a sentence per item) it no longer says what the result said, so such a
report is left whole, the judge refuses it as over budget, and the caller prints that reason
on stderr (single dispatch and the scribe alike).

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
the model that tier resolves to on the gpt rows of `prices.yaml` (the lowest *price level* is
`cheap`, the highest is `top`, the second highest is `mid` — levels, not rows, because two
generations of one model can share a price and would otherwise make `mid` a twin of `top`;
within a level the sheet's first row wins; read at call time so a price refresh moves the
ladder), the effort, and then at most one step of adjustment from the ledger's own cells — a
tier this kind keeps failing at goes up one, and the cheapest tier whose record clears the bar
takes the work. A probe on real
briefs (three algorithm briefs from a consuming project against one cross-cutting design brief,
20 questions, 0.7s) separated them cleanly: scope 0.9 vs 3.0, design judgment 0.0 vs 2.8, spec
gaps 0.1–0.3 vs 2.0, a named check 0.7–0.8 vs 0.1. The output is a table, the notes an entry
earned and `<manifest>.plan.jsonl`, one record per entry so a batch's routing decision can be
joined to its outcomes later. The notes: no `check` where the brief names nothing runnable; a
`kind` outside the vocabulary PRIORS aggregates on; a routed model two tiers from the demand
(`reads top-tier (…) — routed to gpt-6-luna/medium`); and a brief that may contradict a
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
A scribe triage (§3.5.3c) counts wherever it lands: it is written at Stop, usually after a
verdict main typed that turn, and neither main nor the clerk ever sees it — an independent
reading, which is what the table measures. A ledger with no triage row gets no section. The
open items leave out native runs (`ag-` ids) with no verdict and give their count on one line:
hippo records every Agent and Workflow run, so an unjudged quick look-up is not a forgotten
lane, and a list of them would bury the lanes that are.

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
  It names the id a native run is recorded under — `ag-<agentId>`, or `ag-<runId>` for a
  Workflow, whose launch result prints a `Task ID` first and the `Run ID: wf_…` four lines on; a
  ref built from the Task ID names nothing hippo will ever record — and asks main to name exactly
  one task id in a brief or script (the scribe links a run only when its brief names one task id;
  a second, anywhere in it, links none — §3.5.3c) and `ag-wf_…` when it states a Workflow's
  verdict. Measured 2026-09-26: in this repo's ledger 0 of 4 Workflow rows had a verdict — one's
  task done and its branch merged — and 1 of 4 a task, against 2 of 3 subagent rows with a
  verdict; mlx-vlm linked 1 of 17.
- **`hippo:checkup`** (~5KB) — a `/doctor`-style project diagnosis. It reads the ledger, PRIORS,
  failures, cursor gaps, recent transcripts and CLAUDE.md/memory, then reports waste patterns
  (retry loops, limit stalls, orphan dispatches), directive hygiene (stale or contradictory
  directives versus the documents, from the notes `directive list` prints) and clerk health (gaps,
  failures, overhead, and why PRIORS is stale when auto-distill has not fired). Proposals are
  recommend-first, at most two AskUserQuestion rounds, with reversibility stated. Nothing is
  applied automatically.
- **`hippo:dispatch`** (~10KB, from 16KB — a third of it described batch flags retired in 1.14.0)
  — the revised "fleet-dispatch", now "delegation lanes": the measured lessons only, the flags
  left to `hippo:hippo`. The key revisions (all grounded in the audit and the guide): a verifier
  **reports everything and main filters** (with a literal-minded model, a severity ceiling
  genuinely hides findings); the verification budget is **proportional to the refutation rate
  in PRIORS** rather than a fixed ritual; no re-verifying one's own work (boundary verification
  only); safety statements about GPUs and memory use neutral vocabulary (10 measured
  content-filter false positives); grep the shared and individual brief clauses for
  contradictions before composing them; symbol coupling is checked across lanes; and the gate
  check and the push must always be separate calls. Measured use: 7 invocations plus 37 file
  reads across 28 projects, against 2 for checkup. Its routing table names the shape beside
  the role — one scoped worker to an Agent, a fan-out or a panel read as one result to a
  Workflow (whose `isolation: 'worktree'` replaces the manual worktree step), what PRIORS or a
  directive sends to codex to a codex lane or `--batch` — and leaves the executor to that
  evidence: until 2026-09-26 no passage compared the three, while a live directive in this repo
  sent implementation to Claude subagents. The lane agent's Workflow form (§3.6) sits beside
  its Agent form and `--batch`, which fans out with no relay agent at all.

### 3.8 Hosts (Claude Code · Codex CLI)

The same repo is a plugin for both hosts. Three of the four layers (CLI, clerk, skill) were always
host-agnostic and only the hooks were host-bound — and that wall came down when codex grew a hook
engine (measured on 0.144.6).

| | Claude Code | Codex CLI |
|---|---|---|
| Manifest | `.claude-plugin/plugin.json` | `.codex-plugin/plugin.json` (names the `skills` and `hooks` paths) |
| Hooks | `hooks/hooks.json` (the default) plus `hooks/claude-hooks.json`, named by the manifest — all four (§3.4) | `hooks/hooks.json`, named by the manifest — SessionStart and Stop, **the same file**; event keys (PascalCase), matcher, stdin payload fields and the SessionStart `hookSpecificOutput.additionalContext` envelope are all identical (codex ≥0.146 rejects bare text, §3.4) |
| Plugin `bin/` | added to PATH automatically | **not added** → a skill resolves `bin/hippo` relative to its own SKILL.md; a dispatched lane gets it from the wrapper (§3.6) |
| Transcript | Claude JSONL | codex rollout JSONL — `digest_lite.py` detects the format from the first lines and reduces both to the same line vocabulary |
| Agent panel | `agents/lane.md` (`hippo:lane`) + `settings.json` `subagentStatusLine` (§3.6) | none — a lane launches through plain Bash, and `hippo dispatch --watch` works the same |

Constraints specific to codex (0.144.6):

- **Hooks are skipped silently until they are trusted.** Review and trust them once through
  `/hooks`, or bypass with `--dangerously-bypass-hook-trust`. If the capsule never appears after
  installing, look here first. The trust is keyed by the hooks file's path in the plugin
  (0.156.1), which is why codex's file never moves (§3.4).
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
- **The specs**, and where each is asked: `scribe-gate` (the scribe's gate, §3.5.3b),
  `task-end` (open tasks a window reads as ended, §3.5.9), `directive` (conflict and audience
  notes, §6), `brief-check` and `route` (before a launch, §3.6), `harvest` (a lane's triage at
  exit, §3.6, and a native run's, §3.5.3c), `failure-cluster` and `verify` (the harvest, §3.6).
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
| PreToolUse/PostToolUse/UserPromptSubmit hooks | Latency on every call, plus hooks measured to produce no output. The four in §3.4 fire once per session, subagent, compaction or turn, each for a measured reason |
| OPERATING CONTRACT-style resident injection | 8–14KB re-injected, measured. The only resident thing is the one block in §6 |
| a hand-written PROGRESS.md | It goes stale. Replaced by worklog (generated) + ledger (facts) + PRIORS (distilled) |
| typed refusal gates, frozen sidecars, remote verify | Record, never enforce (principle 3) |
| installing a cron job automatically | A user who wants one sets it up. The plugin does not own a schedule — auto-distill rides the Stop-driven scribe instead: due-when rule, no schedule (§3.5.8) |
| routing.yaml / depth-tier model config | Retired 1.11.0 before being built: prices are `prices.yaml` facts, tier-worth is PRIORS `$/accepted`, the decision between them is main's — frozen config is the stale-instruction shape (§1 principle 9). The runaway worry it addressed is handled by the fan-out circuit breaker (§3.6) instead. The shape it was retired in favour of is the batch plan (§3.6, `--batch --dry-run`, and ahead of every judged launch): the same question answered per batch, computed fresh from the price sheet and the ledger, printed as a suggestion — never a file that outlives the batch |
| batch mode flags (`--harvest`, `--plan`, `--resume`, `--fresh`, `--concurrency`, `--causes`) | Retired 1.14.0. Measured across 28 projects (2026-09-23): zero calls to `--harvest`, `--resume`, `--fresh`, `--concurrency` and `--causes`, six to `--plan` in one project, against 11 `--batch` calls in two — while the flag-free single form carried 1,470 of 1,945 dispatch rows. A feature that needs a flag is a feature main does not use: the journal now decides launch / resume / harvest, the harvest ends every run, `--dry-run` is the plan, and concurrency is the manifest key (§3.6) |
| directive lifetimes (`turn\|phase\|durable`) | Retired 1.14.0 (§3.2). Measured across 28 projects: 63 of 75 live `phase` directives were past 14 days and the aging nudge produced no withdrawals, 34 of 72 withdrawn went within 3 days, and 54 of 55 `turn` directives expired by the clock. One lifetime — until withdrawn — plus an age shown on every line (§6) |
| generic bulk ledger ingest (`log --file`, a bulk endpoint) | It would enlarge the mutation grammar toward the retired ingest family above — facts enter through one door. The accepted shape is the journal-scoped `log outcome --from-batch` (§3.6), which narrows what a row may say instead of widening it |
| `claude -p` dispatch lanes | Retired 1.15.0: ~28k tokens of fixed cache creation per call; 3 wrapper rows ever; native Agent/fork/Workflow do the same work and hippo records them (§3.5.3c). The clerk's own `claude -p` backend (§3.5.4) stays |

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

After a compaction (SessionStart source=compact) main's capsule closes on one more line —
``· compact: if the summary above has a `## hippo deltas` section, run those commands first — they
are proposals; skip any that are wrong`` — because the summary lands in main's context before the
capsule does (measured), and PreCompact asked it for those commands (§3.4). "Has", not "ends
with": the host appends its own paragraphs after the summary. The condition is there for Codex,
which fires SessionStart(compact) but gets no PreCompact. A native subagent gets none of this
block: SubagentStart hands it the `[hippo] directives N live — this project's recorded rules`
header and its executor-audience directive lines, then the line that hippo's task, directive
and outcome writes are main's unless its brief asks for one, nothing else, and so does the
SessionStart(compact) of its own compaction, which the host fires as main's (§3.4 — except
while main, at the same prompt, waits on its own reply).

The `cli:` line is main's only (a lane has its `report:` line instead): the command grammar,
because the capsule is what re-arrives after a compaction and that is exactly when the grammar
was being re-read — measured, 405 `--help` calls across 19 projects, and 59 of Codex's 96 (61%)
came within 30 tool calls of a compaction.

A `check:` line (main's only) names the open tasks whose flag still shows (§3.5.9) —
`· check: feat/x, fix/y look finished or abandoned — close them, or note what is left` — at
every session start, compaction included, in tasks.yaml order, and is absent when none does. It
is the judge's reading and worded as one, never a status: main closes the task (`task
done|drop`) or writes what is left into its notes, and either write moves the task's `updated`
stamp past the flag, which is what hides it — nothing is deleted by hand and nothing is applied
(staleness is shown, never resolved, as the third rule below says of directives). Neither a
lane's capsule nor a subagent's slice carries it: closing a task is main's call, and a worker's
surface is two commands (§9.7). Without the key the scribe never writes the file, so a project
that never had one never sees the line.

A `worker:` line (main's only) names the task, directive and verdict writes a subagent made
that main has not written to since (§3.5.3d), oldest first, each with the run that made it —
`· worker wrote: task done ops/artifact-pruning-20260924 (ag-wf_a868d1a2-52f) — not yours yet:
confirm it with a write of your own, or undo it` — five at most, the rest counted with the
file's name; absent when none shows. A native worker's shell is main's, so each of those writes
landed as main's (src=cli), and the line is where that stops being silent: main re-runs or
touches it to make it its own, or undoes it, and either write hides it, like the `check:`
line. Measured live (2.1.282): main, resumed with two such writes in the line — both ones its
own brief had asked for — confirmed both and the line went; replayed, the only real one in 475
mlx-vlm windows is the example above. It works without the key.

A `scribe:` line (main's only) appears when the latest three or more `ev:clerk name:turn-scribe`
rows all failed — the streak and the newest scribe dump's first line, which carries the backend's
own first words —
`· scribe: the last 7 runs failed — clerk rc=1: codex: ERROR: unexpected status 401 Unauthorized… (.hippo/failures/)`.
Measured on b200 (2026-09-24): the scribe failed 7 runs in a row on an expired codex login and
nothing said so, and each dump read `clerk rc=1` over an empty stderr — `clerk_run.sh` threw the
backend's stderr away. It now keeps it in a temp file and, on a non-zero exit, prints the last
error lines, the cause first — only lines shaped like the backend's own errors (`ERROR:`, a
timestamped tracing line), because codex echoes its whole prompt, digest included, to stderr and
an unanchored search promoted a transcript line to "the cause"; a timeout or a signal is named
from the exit code. One failure is noise (a timeout, a flaky network); three in a row is an organ
that stopped working.

`in flight` is delegations launched and not yet judged, within 24h, **counting only what a
launcher wrote** (`src` `wrapper`/`cli`). A lane's self-report does not land an entry — it rides
it, subject visible (`pass2 tensorize (0h42m · claims accepted)`): an executor-sourced statement
never renders as a flat fact (§9.3), and only main's verdict clears the line. It is the one part of "where was I" that is a fact rather
than a plan, and it is the reason it belongs here instead of in a hand-kept file: measured on a
consuming project, this query returned exactly the three lanes that project was listing by hand,
while the same query over every writer returned 16 — scribe-inferred rows swamp it. With nothing
flying the line is absent; a dispatch older than a day is not in flight but forgotten, and
`prior distill` already reports those as open items.

After a compaction the line also names main's native runs still out (§3.5.3c) —
`build:parser (workflow · 0h12m)` — read from main's transcript, not the ledger: launched in the
background, its answer to the brief not complete yet, and not ended by the scribe's end rule
(§3.5.3c): no TaskStop of main's naming it, and for a Workflow no run file saying `killed` or
`failed` for this launch. The summary can drop a launch, and then main no longer knows a result
is owed (measured: main compacting with a Workflow of its own still out got `wf-sleeper
(workflow · 0h00m)`). "Not
complete" is the rule the scribe reads an answer by (`native_answer`): no notification, or only
interim ones — the agent stopped with background work of its own still running, which promises
a final one — while that work runs and main has not messaged the agent since. Two runs on this
machine sent an interim report that held no result ("Command is running in the background…")
and their real one 122s and 124s later, so counting any notification as back would drop such a
run exactly while its result is owed; counting only a final one would list forever the 5 of 9
interim-first runs here that never got one — each one's work has since ended with the agent
idle, and none is listed. A run main stopped never notifies (measured, mlx-vlm: 4 of 4
Workflow runs, their run files `killed`, none notified across two later restarts), so its
TaskStop is what ends it — or, for a Workflow stopped or failed with no TaskStop of main's, its
run file; a run whose process ended is notified `stopped` when the session resumes (measured,
2.1.282, a process ended by SIGTERM). Only after a compaction: at startup
nothing is out yet, and a resume notifies the old process's runs itself. A `hippo:lane` relay
is left out: its lane is the ledger's own entry. So is a run main logged itself, under its bare
id or with its description as the scope: the scribe takes that row as the run's record
(§3.5.3c), and the line lists the row already — probed, the run showed twice, under two names
and two ages. And so, for this line only, is a Workflow whose run file says `completed` for this
launch: one that finished while main compacted has its notification queued until the compaction
ends, while its result is in that file already (measured live, 2.1.282: the run file written
0.2s into a 15s compaction, the notification delivered 0.5s after the capsule, which had listed
the run); the scribe still ends the run at that notification.

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
  `directive add` and at every human-facing `directive list` the judge (§3.9) reads the whole
  live set and notes probable conflicts and audience mismatches — by itself, never behind a
  flag (the flagged form measured 8 calls in one project of 28). A note is the whole of it: the
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
  1.8.1). Running the Stop hook per lane multiplies clerk cost by the batch width. It *does* get
  the capsule — SessionStart's side of the same gate — because a lane that compacts loses its
  brief's constraints exactly the way main used to (§3.4). If a depth-1 orchestrator's reasoning
  is worth keeping, the distillation belongs in the dispatch wrapper at lane exit — not in
  another hook.
- **A discarded lane's events survive in the ledger while its code does not.** This is a feature —
  "this approach was tried and failed" is recorded nowhere today — but it requires the dispatch to
  carry an ending (merged / discarded / killed), or a derived view will present abandoned work as
  done.
- **Concurrent writers.** Short appends are atomic; whole-file rewrites are not. `tools/ledger_edit.py`
  guards on the scribe lock plus a size re-check, and the size re-check is the one that still holds
  when the writers are executors rather than the scribe.
- **The surface handed to an executor should be two commands** — `hippo status --inject` and
  `hippo log outcome`. Not `task`, not `directive add`, not `prior`. A small surface is a small
  policy; most of §9.2 is unnecessary if there is nothing to misuse. A native worker (Agent,
  fork, Workflow) has no `HIPPO_DISPATCH` to turn its writes into claims, and no signal in its
  environment stands in for one (§3.4), so there the same line is text in its SubagentStart
  slice and a record after the fact (§3.5.3d): what it writes anyway shows in main's capsule
  until main makes it its own or undoes it.

### 9.8 Order

§9.2–9.4 are the data plane and stand alone; §9.5–9.6 are the control plane on top of it and are
half-blind without it (an orchestrated batch with no shared memory starves its own children). Build
the data plane first. Its minimum is two things — the `executor` src value and the audience axis —
which is small enough that it may not need a major version at all.
