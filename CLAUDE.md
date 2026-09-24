# hippo

Tests: `tests/run.sh` (wraps `uv run pytest`).

Before adding any runtime surface (hook, script, CLI subcommand), check the
NOT-list in `DESIGN.md` §4 — it names what was tried and removed, and why.

Hooks: `SessionStart` and `Stop` on both hosts (`hooks/hooks.json` — codex keys
hook trust by that path, so it never moves), plus `SubagentStart` and
`PreCompact` on Claude Code only (`hooks/claude-hooks.json`, named by the Claude
manifest) — see `DESIGN.md` §3.4. A new hook needs a measured reason written
there first; per-call hooks stay out (§4).

Clerk output contract (strict JSON, validated like `hippo log`) is defined
in `clerks/turn-scribe.md` and `clerks/distiller.md`.

Jev question specs live in `clerks/jev/*.yaml` (`DESIGN.md` §3.9); tests run with
`HIPPO_JEV_BACKEND=off` by default (conftest) and must never reach the network.

Full design, ledger schema, and rationale: `DESIGN.md`.
