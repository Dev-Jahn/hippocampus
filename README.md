<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)"
            srcset="https://raw.githubusercontent.com/Dev-Jahn/hippocampus/dev/.github/assets/logo-dark.png">
    <img src="https://raw.githubusercontent.com/Dev-Jahn/hippocampus/dev/.github/assets/logo-light.png"
         alt="hippo" width="220">
  </picture>
</p>

# hippocampus

A quiet project organ: hippocampus doesn't control agents, it gives them
perception and memory — a task registry, an append-only outcome ledger, and
background clerks that turn transcripts into evidence. Judgment stays with
the model; it just makes sure that happens on top of good evidence.
The plugin, its slash commands, and the CLI are all named `hippo`.

## Execution surface (5 layers)

| Layer | What | Cost |
|---|---|---|
| deterministic script | hooks · CLI — fast, dumb | 0 |
| **clerk** | a hook fires a cheap model headless for judgment that doesn't need main's context | tokens only |
| judge | a typed-judgment call inside a CLI path, only with `TYPESAFE_API_KEY` (below) | outside both subscriptions |
| skill | needs main's context, or main must act on the result | main context |
| main | routing, acceptance, conversation with the user | — |

## Components

- **CLI** (`bin/hippo`) — `.hippo/` project data: tasks, ledger, priors.
- **lanes** (`.hippo/lanes/`) — `hippo dispatch` keeps each codex lane's raw stderr
  (`<id>.log`), its final message (`<id>.out`) and a small record (`<id>.json`); the shell
  sees one short line per command or message instead of megabytes of codex output, and
  `hippo dispatch --watch <id>` blocks until a lane ends and prints its final lines.
- **4 hooks** (`hooks/hooks.json` on both hosts, plus Claude Code's
  `hooks/claude-hooks.json`) — `SessionStart` re-injects a ≤6-line status
  block (survives compaction); `Stop` fires the scribe clerk detached, never
  blocking; in Claude Code, `SubagentStart` hands each subagent the directives
  addressed to executors and one line saying hippo's task, directive and outcome
  writes are main's unless its brief asks for one, and `PreCompact` asks the
  compaction summary to end with `## hippo deltas` — commands for main to check
  and run afterwards.
- **clerks** (`clerks/*.md`) — headless prompts: `turn-scribe` digests a
  session into worklog + ledger events (Claude Code subagents and Workflow
  runs are recorded like codex lanes: launch, cost per model, your verdict —
  no call needed; a task, directive or verdict a subagent wrote itself shows in
  the next capsule as `worker wrote:` until you confirm or undo it), `distiller` regenerates `PRIORS.md`
  (the scribe runs it when the page is a week old and five new verdicts have
  landed; `hippo prior distill` runs it by hand).
- **agent** (`agents/lane.md`, Claude Code) — `hippo:lane` runs one codex lane so it has a
  row in the agent panel, and hands main — or the Workflow script that launched it — the
  lane's final lines when it ends. The plugin's
  `settings.json` points the panel's `subagentStatusLine` at `scripts/lane_status.py`, so that
  row reads `codex · <scope> · <elapsed> · <cmds> cmds · <last command or message>`; other
  agents' rows are left as Claude Code draws them.
- **skills** (`skills/*`) — `hippo` (the whole CLI grammar, one screen),
  `checkup` (project diagnosis, recommend-first), `dispatch` (delegation
  lanes with evidence-proportional verification).

Nothing here is enforced. `.hippo/`-less directories get silent no-ops
everywhere — zero bytes on stdout/stderr, exit 0.

## Install & init

hippo is one plugin for two hosts.

**Claude Code** — install from the marketplace:

```
/plugin marketplace add Dev-Jahn/jahns-cc-marketplace
/plugin install hippo@jahns-cc-marketplace
```

**Codex CLI** — install from the Codex marketplace, then in a project:

```
codex plugin marketplace add Dev-Jahn/jahns-codex-marketplace
codex plugin add hippo@jahns-codex-marketplace
```

Codex requires you to **review and trust hooks once** before they run: open `/hooks`
in the CLI and trust hippo's `SessionStart`/`Stop` entries. Until you do, they are
skipped silently — if the session-start capsule never appears, look there first.
Trusting them also gives lanes launched by `hippo dispatch` their capsule — at start
and again after each compaction (executor-audience directives plus the report line).
Codex also does not put a plugin's `bin/` on `PATH`; the skills resolve `bin/hippo`
relative to themselves, and you can add your own alias if you want to type `hippo`. Lanes
launched by `hippo dispatch` need neither: the wrapper puts its `bin/` on their `PATH`.

Either way, in a project:

```
hippo init
```

That creates `.hippo/` and nothing else.

### The clerk backend

The clerks resolve their backend automatically: codex (gpt-6-luna, low effort) if installed, else
headless claude (sonnet, low effort; it saves no session, so clerk runs stay out of
`claude --resume`). When a project needs a different one, set `clerk: {backend: codex|claude}` in
`.hippo/config.yaml` — it outranks the variable and the automatic choice until removed — or export
`$HIPPO_CLERK_BACKEND` / `$HIPPO_CLERK_MODEL` (pin the backend when you pin the model — a model id
for one backend is invalid on the other).

### The judge (opt-in by key)

With `TYPESAFE_API_KEY` in the environment, hippo asks TypeSafe's Jev — a judgment-only model
that returns probabilities, never prose — a few typed questions at moments where it already
holds the text: the scribe's digest (advisory hints for the clerk), the live directive set
(`directive add`, `directive list`), every dispatch's brief before it launches and
its report at exit (single and `--batch` alike), each Claude Code subagent's or
Workflow run's brief and the report that answers it, which the scribe finds at Stop, and the
open tasks beside the scribe's digest — one that reads as finished gets a `check:` line in the
next capsule until you close it or note what is left.
Answers are evidence a code policy thresholds; the judge never writes a verdict. There is no
setting and no prompt: without the key, every command behaves exactly as it always has.
Question specs are text in `clerks/jev/*.yaml`; the design is `DESIGN.md` §2 (judge), §3.5,
§3.6, §3.9.

## CLI cheat sheet

The same block as `skills/hippo/SKILL.md` (a test keeps the two, and the parser, in step):

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

Facts go in through one door (`log <event>`); `directive` and `prior` are views re-derived from
the ledger every time. Every subcommand supports `-h/--help`; errors print usage to stderr.

## Design

Full rationale, ledger schema, clerk guardrails, and the explicit NOT-list
live in [`DESIGN.md`](https://github.com/Dev-Jahn/hippocampus/blob/dev/DESIGN.md)
(development branch — not shipped with the plugin). Read it before changing behavior here —
this README only describes what the code already does.
