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
resident (small): one block injected at SessionStart (§6 below)
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
  prompts/          # delegation briefs (COMMON.md + one file per task) — see below
  config.yaml       # optional: overrides such as the clerk backend (everything works without it)
```

In a directory with no `.hippo/`, every hook and every CLI command is a **completely silent no-op**
(zero contamination of other projects).

`prompts/` is the one directory hippo does not read. It exists because delegation briefs had no
home: the host hands each session a different absolute scratchpad path, so every wave retyped a
40-character prefix, and a consuming project eventually invented its own fixed path anyway
(measured). A brief belongs next to the state it describes, at a **project-relative, session-stable**
path — `.hippo/prompts/<task>.md` — reachable from the same cwd `hippo dispatch` already requires.
It is a convention, not a requirement: a path anywhere else still launches.

### 3.2 Ledger schema (a contract — exactly this)

One line = one JSON object. Common fields: `t` (ISO8601, stamped by the writer), `ev`, and the
optional `src` (`scribe|cli|wrapper`).

```jsonl
{"t":"…","ev":"dispatch","id":"d041","kind":"kernel-impl","exec":"codex/gpt-5.6-sol/high","scope":"pass2 SS-UMMA tensorize","task":"feat/x"}
{"t":"…","ev":"outcome","ref":"d041","result":"refuted","attr":"work","rework":2,"by":"verify/opus","note":"circular oracle reference"}
{"t":"…","ev":"review","id":"r007","base":"abc123f","source":"chatgpt-web","findings":4}
{"t":"…","ev":"review-status","ref":"r007","addressed":"partial","at":"def4567"}
{"t":"…","ev":"directive","id":"gpu-01","text":"use GPUs 0 and 1 only","scope":"phase","state":"active"}
{"t":"…","ev":"directive","id":"gpu-01","state":"retracted"}
{"t":"…","ev":"clerk","name":"turn-scribe","ms":8100,"ok":true,"tokens":1400}
```

- `outcome.result ∈ {accepted, revised, refuted, no-go, lost}`; `attr ∈ {work, brief, harness}`
  (recommended whenever the result is not accepted — a failure count with no attribution produces a
  lying prior. Measured: the run of REFUTEDs was work, the NO-GO from contradictory GPU clauses was
  brief, and the vanished StructuredOutput was harness).
- `directive.scope ∈ {turn, phase, durable}`, `state ∈ {active, retracted, expired}`. The last event
  for an `id` is its current state (a derived view is never stored — principles 4 and 5).
- `dispatch.exec` is exactly `vehicle/model/effort`, no whitespace, and `outcome.ref` /
  `review-status.ref` must name an event that exists in this ledger — both fail-closed. These two
  fields are the axes PRIORS aggregates on, so a free-form value is not a small mess, it is a
  column of one. Measured on a real ledger: 26 kinds across 108 dispatches with 19 used exactly
  once, 24 spellings of exec for 4 vehicles (including the wrapper's own path and the literal
  placeholder), and **54% of outcomes joining to no dispatch at all** — half from a caller passing
  a task id, half from ids the scribe invented in the right shape. All of it looked like data.
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
hippo directive retract <directive-id>
hippo prior show
hippo prior distill [--days N]              # run the distiller clerk → regenerate PRIORS.md
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
2. Load this session's cursor from `cursors.json` → compress only the lines after it with
   `digest_lite.py` (a light port of the digest logic proven on the 479MB audit).
3. **Deterministic prefilter**: if the digest has no TOOL or USER line, update the cursor and exit
   (zero model calls).
4. Resolve the backend: `config.yaml > $HIPPO_CLERK_BACKEND > automatic (codex/gpt-5.6-luna/low when
   codex exists, otherwise claude -p sonnet) > mock` (for tests). 120s timeout.
5. Prompt = `clerks/turn-scribe.md` + the digest. Expected output = strict JSON:
   `{"worklog": "…", "events": [ …ledger events without t… ]}`.
6. Validation, **per event**: check each event by the same rules as `hippo log` (per-ev key
   whitelist — unknown keys rejected; `t` and `src` are always stamped by the writer; exec shape
   and ref existence as in §3.2). A rejected event is dumped to `failures/` and skipped; the
   turn's other events and its worklog line are still recorded. One hallucinated ref must not
   erase a good worklog — that is what "the dump is the record" means. A failure *of the clerk
   call itself* (no JSON, wrong envelope, nonzero rc) is still all-or-nothing and records
   `ev:clerk ok:false`. Either way the ledger is never contaminated and **the cursor advances**
   (never re-bill the same input forever). **Never fill a gap by inventing content.**
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

## 6. The resident surface (in full — anything larger is a regression)

```
[hippo] tasks 3 open · directives 2 live · priors 07-31 · worklog 07-31
· live(durable): keep review replies in context, never save them to a file
· live(phase): use GPUs 0 and 1 only
· last: merged the v2 Pareto duo, full gate green (1421)
```

Two rules govern the directive block:

- **durable is never folded.** A user ruling with no lifetime that is invisible at session start is
  effectively not there. Durable directives come first and all of them appear (up to 200 chars per
  line). No number of phase/turn directives may push one out.
- The cap is a **character budget** (1600), not a line count — there is no reason a long directive
  and a short one should cost the same. When something is folded, do not stop at `+N more`: print
  the command that shows the full text (`hippo directive`) alongside it. The point of folding is to
  name an action, not a count.

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
