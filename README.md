# hippo

A quiet project organ: hippo doesn't control agents, it gives them
perception and memory — a task registry, an append-only outcome ledger, and
background clerks that turn transcripts into evidence. Judgment stays with
the model; hippo just makes sure it happens on top of good evidence.

## Execution surface (4 layers)

| Layer | What | Cost |
|---|---|---|
| deterministic script | hooks · CLI — fast, dumb | 0 |
| **clerk** | hook/cron fires a cheap model headless for judgment that doesn't need main's context | tokens only |
| skill | needs main's context, or main must act on the result | main context |
| main | routing, acceptance, conversation with the user | — |

## Components

- **CLI** (`bin/hippo`) — `.hippo/` project data: tasks, ledger, priors.
- **2 hooks** (`hooks/hooks.json`) — `SessionStart` re-injects a ≤6-line
  status block (survives compaction); `Stop` fires the scribe clerk detached,
  never blocking.
- **clerks** (`clerks/*.md`) — headless prompts: `turn-scribe` digests a
  session into worklog + ledger events, `distiller` regenerates `PRIORS.md`.
- **skills** (`skills/*`) — `checkup` (project diagnosis, recommend-first),
  `dispatch` (delegation with evidence-proportional verification).

Nothing here is enforced. `.hippo/`-less directories get silent no-ops
everywhere — zero bytes on stdout/stderr, exit 0.

## Install & init

Install via the Claude Code plugin marketplace, then in a project:

```
hippo init
```

This creates `.hippo/` and nothing else.

## CLI cheat sheet

```
hippo status [--inject]
hippo task add <id> --title T [--status pending] [--deps a,b]
hippo task set <id> <field> <value>
hippo task done <id> [--note N]
hippo task list [--status s1,s2] [--all] [--json]
hippo task show <id> | task drop <id>
hippo log <ev> [typed flags…]        # dispatch|outcome|review|review-status
hippo log raw '<json>'
hippo log tail [-n N] [--ev TYPE]
hippo directive list [--active] [--json]
hippo directive add [typed flags…]
hippo directive retract <directive-id>
hippo prior show
hippo prior distill [--days N]
```

Mental model: facts go in through one door (`log <event>`); bare `hippo log`
shows recent entries; `directive` and `prior` are views re-derived from the
ledger every time. Bare nouns default to a read: `task`→list, `log`→tail,
`directive`→list, `prior`→show.

Every subcommand supports `-h/--help`; errors print usage to stderr.

## Design

Full rationale, ledger schema, clerk guardrails, and the explicit NOT-list
live in [`DESIGN.md`](https://github.com/Dev-Jahn/hippo/blob/dev/DESIGN.md)
(development branch — not shipped with the plugin). Read it before changing behavior here —
this README only describes what the code already does.
