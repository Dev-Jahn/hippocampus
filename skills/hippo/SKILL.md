---
name: hippo
description: This is your hippocampus. Always use it.
---

# hippo — the project's hippocampus

hippo does not control you. It just holds the memory for you: what you delegated, what was
accepted or refuted, and which of the user's instructions are still alive. The judgment is
always yours.

## How to call it

In Claude Code, `hippo` is on PATH. **In Codex it is not** — resolve `../../bin/hippo` relative
to this SKILL.md into an absolute path and call that.

## The grammar, in one line

Facts go in through one door, `hippo log <event>`; the nouns are windows that read them back.
Call a noun on its own and you get its default view (`hippo task` = the list, `hippo log` = recent
records, `hippo directive` = the live instructions, `hippo prior` = the routing priors).

## When to reach for what

- While delegating: `hippo log dispatch --id <new id> --kind <tag> --exec <vehicle/model/effort> --scope "<one line>"`
  (a codex exec launched as `hippo dispatch --kind … --scope … -- <codex args>` records itself)
- When a delegation gets a verdict: `hippo log outcome --ref <id> --result accepted|revised|refuted|no-go|lost --attr work|brief|harness`
- When the user gives a standing instruction: `hippo directive add --text "…" --scope turn|phase|durable`,
  and when it is over, `hippo directive retract <id>`
- When an external review reply arrives: `hippo log review --id <new id> --base <sha> --source <where> --findings <n>`
- Before deciding delegation routing: `hippo prior` — which model and effort measured better
- To see where things stand: `hippo status`

Delegation briefs live in `.hippo/prompts/` — a project-relative path that survives sessions,
so nothing has to retype an absolute scratchpad prefix. `/hippo:dispatch` has the full contract.

## What you don't have to do

Every turn, a background clerk reads the transcript and infers most of the events above on its
own. Recording through the CLI only raises the certainty. Nothing breaks if you skip a record —
this organ does not enforce.
