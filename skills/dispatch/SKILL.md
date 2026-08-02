---
name: dispatch
description: Operating contract for parallel delegation waves — hand several tasks to external executors (codex exec) and subagents at once while main collects, verifies and merges. Use when the user says "launch in parallel", "fleet", "wave", "split it up", or "start everything you can". Worth reading for a single delegation too, when the call pattern or worktree isolation matters.
---

# hippo: dispatch — parallel delegation waves

The successor to fleet-dispatch, revised. Every revision here comes from a dogfooding audit or a
measurement against the Opus 5 guide.

## 0. Thirty seconds before launch

1. `hippo prior show` — check which (model, effort) measured better for this kind. A prior is
   advice: main still decides the final routing from the difficulty and volume of the work at hand.
2. `hippo task` — what can start now: a lane whose task shows a `waiting on:` line still has an
   unfinished dep and belongs in a later wave.
3. `hippo directive list --active` — check what the lanes will see. Directives whose audience
   includes executors reach every lane through its own capsule (`status --inject`, re-injected
   after compaction) — **do not copy them into briefs**; one source, no drift. Hand-fold a
   constraint only where hooks cannot run and the lane might skip the bootstrap. **Grep COMMON
   against the individual brief for conflicting clauses** (contradictory clauses once produced
   a fail-closed NO-GO).
4. Asset preflight: main verifies that the files, models and data the brief names actually exist.

## 1. Role routing

| Kind of work | Assigned to |
|---|---|
| Registry, merge, gate, push, acceptance verdict | main, directly |
| An experiment that is one command (soak/bench) | main's `run_in_background` Bash |
| Implementation or investigation that edits files | an external executor lane (own worktree) |
| Boundary verification of **another executor's** output | a verification lane — budget in §4 |
| Hard design work | an independent duo, then synthesis by main |

- **Never spawn a subagent to re-verify your own work** — current models verify themselves. A
  verification lane is for the boundary of a *different* executor's output.
- Do not delegate small chores: when writing the prompt costs more than doing the work, it costs more.

## 2. Launch contract

```bash
hippo dispatch --kind kernel-impl --scope "pass2 tensorize" --task feat/x \
  -m gpt-5.6-sol -c model_reasoning_effort=high \
  -C .claude/worktrees/pass2 --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check \
  "$(cat .hippo/briefs/COMMON.md .hippo/briefs/pass2.md)"
```

Filling the three groups of arguments:

- **Labels** — `--kind` is the aggregation axis of PRIORS (`kind × exec`), so **reuse a tag rather
  than inventing one per wave**; scattered tags mean the priors never accumulate a sample
  (measured: one ledger carried 26 tags across 108 dispatches, 19 of them used exactly once).
  The vocabulary, shared with the scribe: `impl` `fix` `perf` `verify` `audit` `design`
  `research` `spike` `docs` `infra` `chore`. kind is the **category**; the subject goes in
  `--scope`, one line a human can read a month later. `--task` links a registry id.
- **Routing** — `-m <model>` and `-c model_reasoning_effort=<low|medium|high|xhigh|ultra>`; together
  with the executor these form `exec = executor/model/effort`, the second PRIORS axis. The
  executor is the agent that did the work (`codex` `claude` `fork` `subagent` `workflow`),
  never how it was launched — a codex run started in the background is still `codex`. Read
  `hippo prior` first; with no evidence yet, start from difficulty: an atomized fragment goes cheap
  (low/medium), a design or a whole-file rewrite goes high/xhigh. Do not burn the top tier on
  everything — fragmentation exists precisely so the tier can drop.
- **Sandbox** — `--dangerously-bypass-approvals-and-sandbox` (the lane runs unattended and cannot
  answer an approval prompt; the worktree is what makes that safe) and `--skip-git-repo-check`
  (the worktree is a git dir the check does not recognize). Drop both only for a read-only lane.

Mechanics:

- The wrapper records `ev:dispatch` automatically and prints the dispatch id on the first line.
- The wrapper plants `HIPPO_DISPATCH=<id>` and `HIPPO_DEPTH` in the lane's environment: every
  `hippo` write from inside arrives as `src=executor`, and the lane can report what it observed
  with `hippo log outcome --result … --note '…'` (no ref needed). That self-report is a
  **claim** — the capsule shows it as `claims …` on the in-flight line, and the verdict still
  belongs to main (§4). A lane's `hippo status --inject` carries its audience's directives plus
  its full operating tail (`report:` / `depth N:` / `discipline:` lines). With hippo installed
  and trusted in the Codex host, that capsule re-arrives automatically at start and after every
  compaction — worth it for any long lane. In Claude Code the lane inherits PATH, so bare
  `hippo` resolves; on the Codex host it does not — put the absolute `bin/hippo` path into
  COMMON.md. A lane's `directive` writes are recorded but never change the live set — a lane
  may propose, not rule.
- `--depth 1` marks an **orchestrator lane**: one task the size of a session, allowed to
  dispatch children of its own — this is what the `ultra` tier and the shared ledger exist for.
  Its children start at depth 0 and their capsules say not to re-delegate. Indexed, never
  enforced: a child launched anyway is recorded with its `parent`, so an unintended depth-2 is
  an event you can see in the ledger, not a rule nobody can check. One blast-radius guard
  applies to lane-origin launches only, denominated in dollars, never lanes: the wave's cost
  per parent per 24h (measured usage where a child finished, a nominal sheet-price reservation
  where it has not) warns past half the budget and refuses past it — $500 by default, so a
  thousand luna-class children clear it while ~45 sol-class ones trip it. Size it per project
  via `.hippo/config.yaml` `dispatch: {max_wave_usd: N}`. Main's own launches are never gated.
- **COMMON.md carries only what nothing injects**: the seeded bootstrap clause (keep it when
  editing), the absolute `bin/hippo` path on the Codex host, and genuinely wave-common task
  background. Not directives and not lane discipline — the capsule carries both. Individual
  briefs are task-specific content only (§3).
- In Claude Code `hippo` is on PATH. **In Codex the plugin's `bin/` is not on PATH** — resolve
  `../../bin/hippo` relative to this SKILL.md into an absolute path and call that.
- **cwd must be somewhere `.hippo/` is findable upward.** The repo root with `-C <worktree>`
  remains the tidy shape, but a lane's worktree cwd resolves too — the walk goes through a
  worktree's `.git` file to the project's real `.hippo/` (1.8.0).
- The wrapper has `codex exec … < /dev/null` built in (an unclosed stdin hangs). Launch the
  command itself through the harness's `run_in_background` — no nohup/disown or other detach
  outside the harness (that produced orphans).
- When a codex argument collides with a wrapper flag (`--kind`, `--scope`), put it after `--`.
- Keep briefs in `.hippo/briefs/` (COMMON.md concatenated with the individual brief). It is a
  project-relative, session-stable path reachable from the cwd this command already requires —
  no absolute scratchpad prefix to retype or mistype. Launch several lanes in parallel from a
  single message.
- **Neutral vocabulary**: phrasing about GPU memory overlap, contamination or injection tripped the
  executor's content filter as a cybersecurity false positive ten times — write safety statements
  in neutral academic terms.
- To steer a lane mid-flight, kill it and resume with an explicit session id (never `--last` when
  lanes run in parallel).

**Batch form** (`--batch`, 1.12.0) — for a large uniform wave, hand the whole fan-out to the
wrapper instead of looping launches through your own turns (measured: a 222-lane fleet cost $2,
the launch/harvest loop driving it ~$13 in context re-feeds):

```bash
hippo dispatch --batch wave.yaml [--concurrency N] [--resume | --fresh] [--dry-run]
```

```yaml
concurrency: 8
defaults:
  kind: impl
  executor: codex            # or claude
  model: gpt-5.6-luna
  effort: medium
  args: ["--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"]
  briefs: [.hippo/briefs/COMMON.md]
  check: "tests/run.sh {test}"
entries:
  - scope: "algo: fenwick tree"
    brief: .hippo/briefs/fenwick.md
    vars: {test: tests/test_fenwick.py}
```

- The manifest is a per-wave brief, written fresh each wave — never standing routing config. The
  thirty seconds of §0 still come first; the manifest records the routing you decided, it does
  not decide it.
- **One batch call per stage; the journal is the hand-off; main stays between stages.** A wave
  with dependencies is stages: batch stage 1, read its journal and outputs, judge, then batch
  stage 2. Do not encode a DAG into one manifest — sequencing is main's job.
- **Mass-identical check failures are one defect, not N.** When a large fraction of a wave fails
  its check the same way, diagnose the common cause before writing a repair manifest — the batch
  moved judgment out of the launch loop, so nothing inside it will do this for you (measured: 130
  identical import failures were one missing `pytest.ini`, paid as 130 repair lanes).
- Per-entry prompt = `defaults.briefs` contents + entry `brief` + inline `prompt`, with `{var}`
  substitution in the prompt and the check. Outputs land in `<manifest-stem>.out/` per entry;
  one summary JSON line arrives on stdout at the end.
- Editing entries isolate via per-entry `args` carrying `-C .claude/worktrees/<id>` (worktrees
  created by main **before** the batch call, §5); a read-only wave may drop all three arguments.
- `check` is evidence, not a verdict: its rc lands in the journal and batch never writes an
  outcome. Verdicts still follow §4, per lane.
- `--resume` skips entries whose last exit (and check) passed and relaunches the rest. A
  relaunch mints a **new** dispatch id — two launches are two facts; record the verdict against
  the id that produced the accepted work.

## 3. Brief contract

(1) Background — a "read before starting" list, since the executor has zero context; name only
**files that actually exist in the worker's tree** (naming a gitignored document once produced a
dead reference nobody could open). (2) Scope and non-scope. (3) Verification demands: RED first,
and pre-register acceptance before starting — **as a property, not as an implementation
instruction** ("fix it like this" clauses made two lanes diverge over four rounds; restating them
as properties converged). (4) Where the report is saved plus a stdout summary. (5) Merge cautions
(split hot files into sections in the brief, in advance). (6) A verification lane reproduces
dynamically **in a tempdir only** — the real repo state and documents are untouchable
("read-only" as an instruction alone did not prevent contamination).

Deliberately absent from briefs since 1.9.0: early no-go, background instead of foreground
sleep, the re-delegation rule (now indexed by `--depth`, §2), and permission to record
observations — all of it rides the lane's capsule, generated, one source. A brief is
task-specific content only. (The 336k-token re-delegation spiral that motivated the old blanket
clause is why depth 0 lanes still receive it — from the capsule, not from your prose.)

## 4. Verification budget (proportional to evidence, not a fixed ritual)

- Allocate in proportion to each executor's refutation rate in PRIORS: one verification per lane
  where the rate is high, a spot-check or nothing where it is low and acceptance is stable. With no
  evidence yet, verify the first wave and adjust from the data.
- **Never put a severity ceiling or a "be conservative" instruction in a verifier's brief** — a
  literal-minded model obeys and genuinely reports less. Write "report every finding; filtering
  happens on the collection side".
- Record the outcome from the verifier's verdict:
  `hippo log outcome --ref <id> --result refuted --attr work --note "..."` — or
  `--ref task:<task-id>` when only one dispatch for that task is still awaiting a verdict.
  Attribute honestly — output problem = work, brief defect = brief, infrastructure loss = harness.
  A wrong attribution makes the priors lie. When acceptance came only after repair, record
  `--result revised --rework <round-trips>` — the rework sum is a column PRIORS reports.

## 5. Isolation, collection, merge

- Every editing lane gets `{PROJECT_ROOT}/.claude/worktrees/<name>` and its own branch, created by
  main **before** launching and removed by main **after** the merge:

  ```bash
  git worktree add .claude/worktrees/<name> -b task/<wave>-<name> HEAD   # before
  git worktree remove .claude/worktrees/<name> && git branch -d task/<wave>-<name>   # after
  ```

  `.claude/` is gitignored, so the worktrees never dirty the main tree and one directory holds them
  all. Untracked build artifacts (compiled extensions and friends) do not come along with a
  checkout — copy them in, preserving relative paths, when the lane needs them. Do not pin a base
  SHA in the brief; say "the starting HEAD of your worktree is the base".
- A killed lane's worktree is **not** reset or cleaned before inspection: check `git log`/`status`,
  push a branch if anything is worth keeping, and only then remove it (untracked artifacts were
  lost this way once).
- File ownership alone is not enough to call parallel lanes disjoint — **extract the symbols a lane
  deletes or renames and grep for consumers in the other lanes and in dev** (symbol coupling once
  broke all of dev).
- Collection, in notification order: tail the output, read the report, review
  `git log --oneline <base>..HEAD` → `git merge --squash` from the repo root → **re-run the targeted
  gate after the merge** → push when green.
- **Never put the gate check and the push in one call** — chaining `tail …; git push` pushed a
  failing state twice. Check the gate's rc, *then* push in a separate call.
- While an authoritative measurement (a performance table, say) is running, main does not commit to
  tracked files (that discarded a measurement).
- Before reporting "still running", measure it: confirm the PID in the process table (plus
  `nvidia-smi --query-compute-apps` for GPU work). Never infer it from the existence of a worktree
  commit (that produced two false reports the user had to correct).

## 6. Shared GPUs (where applicable)

Go through a flock runner (a `gpu_run.sh`-style script at a fixed project path — never in a session
scratchpad: per-session copies broke mutual exclusion). No setting CUDA_VISIBLE_DEVICES by hand;
CPU work first, GPU verification batched. When a GPU sits idle, main assigns it proactively (twice
the user had to point out lanes piling onto one device).
