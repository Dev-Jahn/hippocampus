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
  * whether one launch was recorded twice — the launcher writing its own dispatch and the scribe
    inferring the same one from the transcript. Both records validate; the pair inflates the
    PRIORS denominator and leaves a permanently unjudged twin.

Pass the pre-migration `.bak` alongside the live file to see a fix as a before/after.
"""

import collections
import datetime
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

    dups = duplicate_launches(rows)
    if dups:
        print(f"  {len(dups)} launch(es) recorded twice — the scribe restating a launcher record "
              f"within {DUP_WINDOW_S}s:")
        for gap, s, w, note in dups:
            print(f"     +{gap:3d}s  scribe {s.get('id')}  ~  {w.get('src')} {w.get('id')[:18]}")
            print(f"            S: {str(s.get('scope'))[:66]}")
            print(f"            L: {str(w.get('scope'))[:66]}")
            for n in note:
                print(f"            ! {n}")

    live = [d for d in _directives(rows) if d.get("state") == "active"]
    if live:
        sizes = [(d["id"], len(" ".join(str(d.get("text", "")).split()))) for d in live]
        over = [f"{i} ({n})" for i, n in sorted(sizes, key=lambda x: -x[1])
                if n > hippo_cli.DIRECTIVE_TEXT_NUDGE]
        print(f"  directives: {len(live)} live, {sum(n for _, n in sizes)} chars resident"
              + (f" — oversized: {', '.join(over)}" if over else ""))
    return total


# A duplicate is minted while the launch is still going by — measured, every confirmed pair on a
# real ledger sat between 27 and 167 seconds apart. Scope text is NOT the discriminator: the
# launcher's scope carries argv detail the scribe's paraphrase drops, so a difflib ratio on a
# confirmed pair ran as low as 0.46 while an unrelated pair reached 0.35. Time separates them
# cleanly where text does not.
DUP_WINDOW_S = 180


def duplicate_launches(rows):
    """Scribe dispatches that restate a launch the launcher already recorded.

    Reported, never resolved: two checks have to stay with a reader. A *different executor family*
    beside a launch is normally a parallel arm rather than a copy — `hippo dispatch` wraps codex
    exec only, so a fork or subagent arm has no launcher record by construction (measured: one
    `fork/...` dispatch 63s after its codex sibling was a two-arm design duo, and its twin's own
    outcome said so). And a twin still awaiting an outcome may simply be in flight."""
    def when(e):
        try:
            return datetime.datetime.strptime(e.get("t", ""), "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None

    disp = [e for e in rows if e.get("ev") == "dispatch" and when(e)]
    judged = {e.get("ref") for e in rows if e.get("ev") == "outcome"}
    launcher = [e for e in disp if e.get("src") in ("wrapper", "cli")]
    fam = lambda e: str(e.get("exec", "")).split("/")[0]
    out = []
    for s in (e for e in disp if e.get("src") == "scribe"):
        near = sorted(((abs((when(w) - when(s)).total_seconds()), w) for w in launcher),
                      key=lambda x: x[0])
        near = [(g, w) for g, w in near if g <= DUP_WINDOW_S]
        if not near:
            continue
        gap, w = near[0]
        note = []
        if len(near) > 1:
            note.append(f"{len(near)} launcher records inside the window — a parallel burst, so "
                        "confirm which one this restates")
        if fam(s) != fam(w):
            note.append(f"different executor ({fam(s)} vs {fam(w)}) — likely a parallel arm, not "
                        "a copy")
        if s.get("id") in judged:
            note.append("the scribe copy carries the verdict — repoint it, do not just drop")
        if w.get("id") not in judged:
            note.append("the twin has no outcome yet — it may still be in flight")
        out.append((int(gap), s, w, note))
    return sorted(out, key=lambda x: x[0])


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
