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
relative to themselves, and you can add your own alias if you want to type `hippo`.

Either way, in a project:

```
hippo init
```

That creates `.hippo/` and nothing else.

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
hippo directive add [typed flags…]        # --lifetime turn|phase|durable
                                          # --audience main|executor|all (default all)
hippo directive withdraw <directive-id>
hippo prior show
hippo prior distill [--days N]
hippo dispatch --kind K --scope S [--task T] [--depth N] [--] <codex exec args…>
                                     # codex exec wrapper: records ev:dispatch, prints its id
                                     # --depth 1 = orchestrator lane (may spawn; children start at 0)
```

Mental model: facts go in through one door (`log <event>`); bare `hippo log`
shows recent entries; `directive` and `prior` are views re-derived from the
ledger every time. Bare nouns default to a read: `task`→list, `log`→tail,
`directive`→list, `prior`→show.

Every subcommand supports `-h/--help`; errors print usage to stderr.

## Design

Full rationale, ledger schema, clerk guardrails, and the explicit NOT-list
live in [`DESIGN.md`](https://github.com/Dev-Jahn/hippocampus/blob/dev/DESIGN.md)
(development branch — not shipped with the plugin). Read it before changing behavior here —
this README only describes what the code already does.
