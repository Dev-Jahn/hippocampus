---
name: hippo
description: This is your hippocampus. Always use it.
---

# hippo — the project's hippocampus

hippo does not control you; it holds the memory — what you delegated, what was accepted or
refuted, which of the user's instructions are still alive. The judgment is always yours.

## Grammar

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
```

## When

- Work will outlive this turn → `task add`; `task done` when it ships.
- A delegation gets a verdict → `log outcome` (`--ref task:<id>` finds its open dispatch).
- The user rules → `directive add`; re-add the same `--id` to change it, `withdraw` when done,
  `--audience main` when a lane never needs it. A rule for the next answer only is not one.
- An external review arrives → `log review`; its findings dealt with → `log review-status`.
- Before routing a delegation → `prior`.
- Codex lanes → `dispatch`; many at once → `--batch` (`/hippo:dispatch` has the contract);
  `--watch <id>` waits for one to end and prints its report path.
- A subagent or Workflow run (in Codex, a `spawn_agent` child): no call — hippo records the
  launch and your verdict from the transcript at the end of the turn (in Claude Code its cost
  too, under the id `ag-<agentId>`, or a Workflow's `ag-<runId>` once it ends — its Run ID,
  not its Task ID).
- Lost your place → `status`.

## What runs by itself

- The scribe, at every Stop: turns the transcript into ledger events and a worklog line, records
  each Claude Code subagent and Workflow run with its cost, and regenerates PRIORS when it is a
  week old and five new verdicts have landed.
- The capsule, at session start and after every compaction: tasks, live directives, in flight.
  In Claude Code each subagent gets the directives addressed to executors — no need to copy them
  into its brief — and a compaction summary ends with `## hippo deltas`, commands to check and run.
- The judge, only when `TYPESAFE_API_KEY` is set: gate hints for the scribe, notes on directives,
  routing and triage on dispatch and on a subagent's report, and a capsule `check:` line for an
  open task a turn seems to have finished. Without the key nothing changes.
- The dispatch wrapper records the launch, the lane's usage and its triage.
- Recording through the CLI only raises certainty; nothing breaks if you skip a record.

In Claude Code, and in any lane `hippo dispatch` launched, `hippo` is on PATH. **In a Codex main
session it is not** — resolve `../../bin/hippo` relative to this SKILL.md into an absolute path.
