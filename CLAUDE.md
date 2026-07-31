# hippo

Tests: `tests/run.sh` (wraps `uv run pytest`).

Before adding any runtime surface (hook, script, CLI subcommand), check the
NOT-list in `DESIGN.md` §4 — it names what was tried and removed, and why.

Hooks are capped at 2 (`SessionStart`, `Stop`) — see `DESIGN.md` §3.4. Do not
add a third.

Clerk output contract (strict JSON, validated like `hippo log`) is defined
in `clerks/turn-scribe.md` and `clerks/distiller.md`.

Full design, ledger schema, and rationale: `DESIGN.md`.
