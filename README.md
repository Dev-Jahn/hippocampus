# waystone

A quiet project organ: waystone doesn't control agents, it gives them
perception and memory — a task registry, an append-only outcome ledger, and
background clerks that turn transcripts into evidence. Judgment stays with
the model; waystone just makes sure it happens on top of good evidence.

## Execution surface (4 layers)

| Layer | What | Cost |
|---|---|---|
| deterministic script | hooks · CLI — fast, dumb | 0 |
| **clerk** | hook/cron fires a cheap model headless for judgment that doesn't need main's context | tokens only |
| skill | needs main's context, or main must act on the result | main context |
| main | routing, acceptance, conversation with the user | — |

## Components

- **CLI** (`bin/waystone`) — `.waystone/` project data: tasks, ledger, priors.
- **2 hooks** (`hooks/hooks.json`) — `SessionStart` re-injects a ≤6-line
  status block (survives compaction); `Stop` fires the scribe clerk detached,
  never blocking.
- **clerks** (`clerks/*.md`) — headless prompts: `turn-scribe` digests a
  session into worklog + ledger events, `distiller` regenerates `PRIORS.md`.
- **skills** (`skills/*`) — `checkup` (project diagnosis, recommend-first),
  `dispatch` (delegation with evidence-proportional verification).

Nothing here is enforced. `.waystone/`-less directories get silent no-ops
everywhere — zero bytes on stdout/stderr, exit 0.

## Install & init

Install via the Claude Code plugin marketplace, then in a project:

```
waystone init
```

This creates `.waystone/` and nothing else.

## CLI cheat sheet

```
waystone status [--inject]
waystone task add <id> --title T [--status pending] [--deps a,b]
waystone task set <id> <field> <value>
waystone task done <id> [--note N]
waystone task list [--status s1,s2] [--all] [--json]
waystone task show <id> | task drop <id>
waystone log <ev> [typed flags…]        # dispatch|outcome|review|review-status
waystone log raw '<json>'
waystone log tail [-n N] [--ev TYPE]
waystone directive list [--active] [--json]
waystone directive add [typed flags…]
waystone directive retract <directive-id>
waystone prior show
waystone prior distill [--days N]
```

Mental model: facts go in through one door (`log <event>`); bare `waystone log`
shows recent entries; `directive` and `prior` are views re-derived from the
ledger every time. Bare nouns default to a read: `task`→list, `log`→tail,
`directive`→list, `prior`→show.

Every subcommand supports `-h/--help`; errors print usage to stderr.

## Design

Full rationale, ledger schema, clerk guardrails, and the explicit NOT-list
live in [`DESIGN.md`](https://github.com/Dev-Jahn/waystone/blob/rebirth/DESIGN.md)
(development branch — not shipped with the plugin). Read it before changing behavior here —
this README only describes what the code already does.
