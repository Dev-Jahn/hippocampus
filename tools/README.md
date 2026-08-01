# tools/ — development only

Not shipped. `release.sh` projects an **allowlist** onto `main`, and this directory is not on it,
so nothing here reaches an installed plugin. Same arrangement as `tests/` and `DESIGN.md`.

These exist because the contract moves. Every schema change (`scope` → `lifetime`, `retracted` →
`withdrawn`, the `exec` and `kind` vocabularies) leaves consuming projects holding a ledger written
under the old rules, and a ledger is append-only — the fix is always a whole-file rewrite of
somebody's live state.

| tool | what it answers |
|---|---|
| `ledger_check.py <ledger…>` | would every event still be accepted today, and does `ref` actually join? Read-only. Takes several files, so a `.bak` next to the live one reads as before/after. |
| `ledger_edit.py <ledger> <transform.py>` | rewrite in place without racing the session that owns the file. |

Both run standalone: `uv run --script tools/ledger_check.py …`.

`ledger_check.py` imports the working copy's own `cli/hippo_cli.py`, so it reports what *this*
checkout would accept — check out the version a project is running to audit against that version.

A `ref` that names nothing is the failure worth knowing about. It is not an error anywhere: the
event validates, it writes, it sits in the file, and it is quietly missing from every aggregate
that groups by dispatch. Nothing tells you except this.

## Finding a launch that was recorded twice

The launcher writes `ev:dispatch` from its own argv; a scribe running an older contract could also
infer the same launch from the transcript and write a second record. Both validate. The pair
inflates the PRIORS denominator and leaves a twin that can never be judged.

**Match on Δt, not on scope text.** The launcher's scope carries the argv detail the scribe's
paraphrase drops, so string similarity is structurally low even when the subject is identical —
measured on a real ledger, confirmed pairs scored as low as 0.46 while unrelated pairs reached
0.35. There is no threshold that separates them. Time does: every confirmed pair sat 27–167s
apart, because a duplicate is minted while the launch is still going by.

`ledger_check.py` reports candidates and refuses to resolve them, because two things it cannot
settle change the answer:

- **A different executor family beside a launch is usually a parallel arm, not a copy.** `hippo
  dispatch` wraps codex exec only, so a `fork` or `subagent` arm has no launcher record by
  construction. One `fork/...` dispatch 63s after its codex sibling was the second half of a
  design duo — its twin's own outcome said "duo 착지: codex(…)". Dropping it would have erased a
  real delegation.
- **Parallel waves put several launches inside the window.** With a burst, more than one launcher
  record is within Δt of the same scribe record and only one is the twin. The report prints both
  scopes side by side so this takes a glance to settle: on a real ledger, 4 of 5 surviving
  candidates turned out to be unrelated launches that merely started at the same moment.

## Writing a transform

```python
# fix_exec.py — one function, called once per event, in order.
MAP = {"d041": "codex/gpt-5.6-sol/high"}

def transform(e):
    if e.get("ev") == "dispatch" and e["id"] in MAP:
        e["exec"] = MAP[e["id"]]
    return e            # None drops the event
```

```bash
uv run --script tools/ledger_edit.py path/to/.hippo/ledger.jsonl fix_exec.py --dry-run
uv run --script tools/ledger_edit.py path/to/.hippo/ledger.jsonl fix_exec.py
```

Key the transform on event **identity** — a dispatch id, or a ref plus a timestamp — never on line
number. A mapping built from a snapshot then stays correct against a file that has grown since,
which is what makes it safe to work out the hard part offline and apply later.

## Editing a ledger a session is still writing to

Run the tool **on the machine that owns the file**. Editing a fetched copy and uploading it cannot
be made safe: the upload cannot know what arrived in the meantime.

`ledger_edit.py` takes the scribe's own `scribe.lock`, so the scribe skips its run while the tool
holds it — and it skips without advancing its cursor, so the next Stop covers the gap. It then
re-checks the file size before writing, because a session's own `hippo log` writes through a
different path and ignores that lock. Measured, 2026-08-01: a ledger grew by 12 events between two
applications minutes apart, all preserved.
