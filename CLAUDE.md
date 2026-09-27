# hippo

Tests: `tests/run.sh` (wraps `uv run pytest`).

Before adding any runtime surface (hook, script, CLI subcommand), check the
NOT-list in `DESIGN.md` §4 — it names what was tried and removed, and why.

`hooks/hooks.json` never moves — codex keys hook trust by that path. A new hook
needs a measured reason in `DESIGN.md` §3.4 first; per-call hooks stay out (§4).

Clerk output contract (strict JSON, validated like `hippo log`) is defined
in `clerks/turn-scribe.md` and `clerks/distiller.md`.

Jev (`clerks/jev/*.yaml`, `DESIGN.md` §3.9) is opt-in by `TYPESAFE_API_KEY` alone —
never add a config key, flag or prompt for it; without the key hippo behaves exactly as
before. Tests run with `HIPPO_JEV_BACKEND=off` by default (conftest) and must never
reach the network.

Full design, ledger schema, and rationale: `DESIGN.md`.
