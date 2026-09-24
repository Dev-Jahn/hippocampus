---
name: dispatch
description: Operating contract for delegation lanes — hand several tasks to external executors (codex exec) and subagents at once while main collects, verifies and merges. Use when the user says "launch in parallel", "split it up", "run a batch", or "start everything you can". Worth reading for a single delegation too, when the call pattern or worktree isolation matters.
---

# hippo: dispatch — delegation lanes

Every rule here comes from a dogfooding audit or a measurement. The flags are in `/hippo:hippo`.

## 0. Before launch

1. `hippo prior` — which exec measured better for this kind. Advice, not routing: main decides.
2. `hippo task` — a task with a `waiting on:` line belongs in a later batch.
3. Directives addressed to executors reach every lane through its capsule — **never copy them
   into briefs**. **Grep COMMON against each brief for conflicting clauses** (two contradictory
   GPU clauses once produced a fail-closed NO-GO).
4. Asset preflight: the files, models and data a brief names actually exist.

## 1. Role routing

| Work | Goes to |
|---|---|
| Registry, merge, gate, push, verdicts | main |
| A one-command experiment (soak, bench) | main's `run_in_background` |
| Implementation or investigation that edits files | a lane in its own worktree |
| Boundary verification of **another** executor's output | a verification lane (§4) |
| Hard design | an independent duo, synthesized by main |

Never spawn a lane to re-verify your own work. Do not delegate chores that cost less to do than
to brief.

## 2. Launch

```bash
hippo dispatch --kind impl --scope "pass2 tensorize" --task feat/x \
  -m gpt-6-sol -c model_reasoning_effort=high \
  -C .claude/worktrees/pass2 --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check \
  "$(cat .hippo/briefs/COMMON.md .hippo/briefs/pass2.md)"
```

- **`--kind` is the PRIORS axis — reuse a tag**: `impl fix perf verify audit design research
  spike docs infra chore` (one ledger carried 26 tags over 108 dispatches, 19 used once). The
  subject goes in `--scope`.
- **Routing**: `exec = executor/model/effort`; the executor is the agent that did the work
  (`codex claude fork subagent workflow`), never how it was launched. Codex ladder, top down:
  `gpt-6-astra` > `gpt-6-sol` > `gpt-6-luna`; slugs come from
  `~/.codex/models_cache.json`, never guessed. `ultra` is a multi-agent mode — an orchestrator
  lane's tier. An atomized fragment goes cheap; fragmentation exists so the tier can drop.
  `--fast` = codex's fast service tier, same exec axis.
- **Sandbox**: the bypass flag because the lane runs unattended (the worktree makes it safe),
  `--skip-git-repo-check` because the worktree's `.git` is a file. Drop both for read-only lanes.
- The wrapper records the launch, closes stdin, plants `HIPPO_DISPATCH`/`HIPPO_DEPTH`/
  `HIPPO_DIR`, and puts its own `bin/` first on the lane's PATH (a bare `hippo` works on both
  hosts): the lane's capsule carries its directives and report line, and its
  `log outcome` is a **claim** — the verdict is main's. Launch through `run_in_background`,
  never nohup/disown (orphans). A codex argument that collides with a wrapper flag goes after `--`.
- A subagent, fork or Workflow run — or a Codex `spawn_agent` child — needs no hippo call: the
  scribe records it and your verdict (in Claude Code its cost too, as `ag-<agentId>`).
- `--depth 1` = an orchestrator lane that may spawn; its children start at 0. Lane-origin
  launches pass a dollar breaker ($500 per parent per 24h, `dispatch: {max_wave_usd: N}` in
  `.hippo/config.yaml`); main is never gated.
- **COMMON.md carries only what nothing injects**: the seeded bootstrap and shared task
  background. Never a `bin/hippo` path — cache paths carry the plugin version and the next
  update deletes them.
- **Neutral vocabulary**: GPU memory "overlap", "contamination", "injection" tripped the
  executor's content filter ten times — write safety statements in neutral academic terms.
- To steer a lane mid-flight, kill it and resume by explicit session id (never `--last` when
  lanes run in parallel).

## 2b. Batch

For many lanes, hand the fan-out to the wrapper instead of looping launches through your turns
(a 222-lane batch cost $2; the loop driving it by hand ~$13 in context re-feeds).

```yaml
concurrency: 8
defaults: {kind: impl, executor: codex, model: gpt-6-luna, effort: medium,
           briefs: [.hippo/briefs/COMMON.md],
           args: ["--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"]}
entries:
  - {scope: "algo: fenwick tree", brief: .hippo/briefs/fenwick.md, check: "tests/run.sh {test}",
     vars: {test: tests/test_fenwick.py}}
```

- **The journal decides**: no journal → everything launches; some entries unfinished → it
  resumes by itself (lanes whose triage named `capability` or `spec` are skipped — they need a
  new brief); all done → nothing launches. Every run ends with the harvest table. Delete the
  journal to start over.
- **`--dry-run` is the plan**: difficulty per brief, the suggested exec, notes. An entry with
  no `model` launches on the suggestion when `TYPESAFE_API_KEY` is set; without it, `model` is
  required. The manifest is per-batch data, never standing config.
- Editing entries get worktrees created by main first: `-C` in `args`, or
  `cwd: .claude/worktrees/<id>`.
- **One batch per stage; main stays between stages.** Do not encode a DAG into one manifest.
- **Mass-identical failures are one defect**: 130 identical check failures were one missing
  `pytest.ini`, paid as 130 repair lanes. Diagnose the cluster, repair it with one brief.
- **Read the harvest table before any lane.** Escalations first, then no-gos. `accept-candidate`
  is not acceptance — it has the standing of a passing check; `verify` says which lanes deserve
  a verification lane. `check` is evidence, never a verdict.
- Serialize verdicts once judged: `hippo log outcome --from-batch <journal> < verdicts.jsonl`
  (`--dry-run` first). Edit the harvest's `verdicts.jsonl` down to the lanes you inspected and
  accepted — never map `rc` or a claim to acceptance. A relaunch mints a new dispatch id.

## 3. Brief contract

(1) A read-first list naming **only files in the worker's tree** (a gitignored document was
once a dead reference). (2) Scope and non-scope. (3) Acceptance pre-registered, RED first, **as
a property, not an implementation instruction** ("fix it like this" made two lanes diverge
over four rounds). (4) Where the report goes, plus a stdout summary. (5) Merge cautions — split
hot files into sections in advance. (6) A verification lane reproduces **in a tempdir only**
("read-only" as an instruction did not prevent contamination).

Early no-go, background-not-sleep, no re-delegation and the report command ride the capsule —
not the brief. Do not pin a base SHA; "your worktree's starting HEAD is the base".

## 4. Verification budget

- Proportional to each exec's refutation rate in PRIORS: a verifier per lane where it is high,
  a spot-check where it is low. No evidence yet → verify the first batch.
- **No severity ceiling in a verifier's brief** — a literal model obeys and reports less. Write
  "report every finding; filtering happens on the collection side" (the harvest ranks them).
- Record `--result refuted --attr work|brief|harness` honestly — a wrong attribution makes the
  priors lie. Acceptance after repair is `revised --rework <n>`.

## 5. Isolation, collection, merge

- Every editing lane gets `.claude/worktrees/<name>` on its own branch, made **before** launch
  and removed **after** the merge (`git worktree add .claude/worktrees/<name> -b task/<name>`).
  Copy untracked build artifacts in when the lane needs them.
- A killed lane's worktree is inspected (`git log`, `status`) and pushed if worth keeping before
  removal — untracked artifacts were lost once.
- **Disjoint files are not disjoint lanes**: grep the symbols a lane deletes or renames for
  consumers in the other lanes and in dev (symbol coupling once broke all of dev).
- Collect in notification order: report → `git log <base>..HEAD` → `git merge --squash` →
  **re-run the targeted gate** → push.
- **Gate and push are separate calls** — `tail …; git push` pushed a failing state twice.
- While an authoritative measurement runs, main commits nothing to tracked files.
- "Still running" is measured, not inferred: the PID in the process table (`nvidia-smi
  --query-compute-apps` for GPU work), never a worktree commit.

## 6. Shared GPUs

Go through a flock runner at a fixed project path (never a session scratchpad — per-session
copies broke mutual exclusion). No hand-set `CUDA_VISIBLE_DEVICES`; CPU work first, GPU
verification batched. An idle GPU gets assigned by main.
