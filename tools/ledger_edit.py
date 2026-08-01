# /// script
# requires-python = ">=3.11"
# ///
"""Rewrite a hippo ledger in place without racing the session that owns it.

    uv run --script tools/ledger_edit.py <ledger.jsonl> <transform.py> [--dry-run]

A ledger is append-only, so any retroactive fix is a whole-file rewrite — and a whole-file rewrite
against a live session is how you lose the events appended while you were thinking. Three guards,
each for a writer this cannot otherwise see:

  1. **flock on `.hippo/scribe.lock`** — the scribe takes this same lock, so while it is held the
     scribe simply skips its run. That is safe by its own design: it skips *without advancing the
     cursor*, so the next Stop covers the window. Nothing is lost by locking it out.
  2. **size re-check before the write** — the session's own `hippo log` writes through a different
     path and ignores the lock. If the file grew between the read and the write, abort; nothing is
     written and re-running picks up the new events.
  3. **atomic rename** — a torn write leaves the ledger unreadable, and it is the only copy of the
     judgments in it. A `.bak` is written first regardless.

`transform.py` is any file defining `transform(event: dict) -> dict | None`, called once per event
in order. Return the event (mutated or not) to keep it, or None to drop it. Import what you need;
it runs in this process. Keep transforms keyed by event *identity* (a dispatch id, a ref plus a
timestamp) rather than by line number, so a transform written against yesterday's snapshot still
applies correctly to a file that has grown since.

Remote ledgers: copy this file and the transform over, and run it there. Editing a fetched copy and
uploading it cannot be made safe — the upload has no idea what arrived in the meantime.
"""

import fcntl
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path


def load_transform(path):
    spec = importlib.util.spec_from_file_location("ledger_transform", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load a python module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "transform"):
        sys.exit(f"{path} defines no transform(event) function")
    return mod.transform


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    ledger = Path(argv[1])
    transform = load_transform(argv[2])
    dry = "--dry-run" in argv[3:]

    lock_path = ledger.parent / "scribe.lock"
    lock = lock_path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"ABORT: {lock_path} is held — a scribe is running. Try again in a moment.")

    size_at_read = ledger.stat().st_size
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]

    out, changed, dropped = [], 0, 0
    for e in rows:
        before = json.dumps(e, sort_keys=True, ensure_ascii=False)
        r = transform(dict(e))
        if r is None:
            dropped += 1
            continue
        if json.dumps(r, sort_keys=True, ensure_ascii=False) != before:
            changed += 1
        out.append(r)

    print(f"{len(rows)} events read — {changed} changed, {dropped} dropped, {len(out)} to write")
    if dry:
        print("dry run: nothing written")
        return 0
    if not changed and not dropped:
        print("nothing to do")
        return 0

    if ledger.stat().st_size != size_at_read:
        sys.exit(
            f"ABORT: {ledger} grew from {size_at_read} to {ledger.stat().st_size} bytes while this "
            "ran. Nothing written — re-run to pick up the new events."
        )
    shutil.copy2(ledger, str(ledger) + ".bak")
    tmp = str(ledger) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(e, ensure_ascii=False) for e in out) + "\n")
    os.replace(tmp, ledger)
    print(f"written (backup: {ledger}.bak)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
