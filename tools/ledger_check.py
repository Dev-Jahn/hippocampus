# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Check a hippo ledger against the contract this working copy implements.

    uv run --script tools/ledger_check.py <ledger.jsonl> [more.jsonl ...]

Read-only. Two things a bare `hippo log tail` cannot tell you:

  * whether every event would still be *accepted* today — the schema moves (scope -> lifetime,
    retracted -> withdrawn, the exec and kind vocabularies), and a ledger written under an older
    contract keeps its old spelling forever. Events are checked with the very validator the CLI
    uses, so this answers "would hippo write this today", not "does it look about right".
  * whether `ref` actually joins. The validator alone cannot see this: it checks one event, and a
    dangling ref is only visible against the whole file. An outcome whose ref names nothing is
    invisible in every aggregate that groups by dispatch — it does not error, it just silently
    fails to count.

Pass the pre-migration `.bak` alongside the live file to see a fix as a before/after.
"""

import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))
import hippo_cli  # noqa: E402

# t and src are stamped by the writer, so a recorded event legitimately carries them while
# validate_event (which sees events on their way in) rejects them. Strip before checking.
WRITER_STAMPED = ("t", "src")


def load(path):
    rows, malformed = [], 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            malformed += 1
    return rows, malformed


def check(path):
    rows, malformed = load(path)
    print(f"\n=== {path}")
    print(f"{len(rows)} events" + (f", {malformed} malformed lines" if malformed else ""))
    print("  by ev:", dict(collections.Counter(e.get("ev") for e in rows).most_common()))

    dispatch_ids = {e["id"] for e in rows if e.get("ev") == "dispatch" and e.get("id")}
    review_ids = {e["id"] for e in rows if e.get("ev") == "review" and e.get("id")}

    failures = collections.Counter()
    for e in rows:
        err = hippo_cli.validate_event({k: v for k, v in e.items() if k not in WRITER_STAMPED})
        if not err:
            ev = e.get("ev")
            want = dispatch_ids if ev == "outcome" else review_ids if ev == "review-status" else None
            if want is not None and e.get("ref") not in want:
                err = f"ev={ev}: ref does not name an event in this ledger"
        if err:
            failures[err[:100]] += 1

    total = sum(failures.values())
    print(f"  {total} of {len(rows)} events would be rejected by this working copy's contract")
    for reason, n in failures.most_common():
        print(f"     {n:4d}  {reason}")

    # The two PRIORS axes, printed whether or not they are currently valid: a fragmented axis is
    # not a contract violation, it is just an axis that will never accumulate a sample.
    disp = [e for e in rows if e.get("ev") == "dispatch"]
    if disp:
        kinds = collections.Counter(e.get("kind") for e in disp)
        execs = collections.Counter(e.get("exec") for e in disp)
        singles = sum(1 for _, n in kinds.items() if n == 1)
        print(f"  kind: {len(kinds)} distinct over {len(disp)} dispatches ({singles} used once)")
        print("     ", dict(kinds.most_common(8)))
        print(f"  exec: {len(execs)} distinct")
        print("     ", dict(execs.most_common(6)))

    live = [d for d in _directives(rows) if d.get("state") == "active"]
    if live:
        sizes = [(d["id"], len(" ".join(str(d.get("text", "")).split()))) for d in live]
        over = [f"{i} ({n})" for i, n in sorted(sizes, key=lambda x: -x[1])
                if n > hippo_cli.DIRECTIVE_TEXT_NUDGE]
        print(f"  directives: {len(live)} live, {sum(n for _, n in sizes)} chars resident"
              + (f" — oversized: {', '.join(over)}" if over else ""))
    return total


def _directives(rows):
    """Same last-event-wins fold the CLI does, over a plain list of rows."""
    cur = {}
    for e in rows:
        if e.get("ev") == "directive" and e.get("id"):
            cur.setdefault(e["id"], {"id": e["id"]}).update(
                {k: v for k, v in e.items() if k not in ("ev", "src")}
            )
    return list(cur.values())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(1 if sum(check(p) for p in sys.argv[1:]) else 0)
