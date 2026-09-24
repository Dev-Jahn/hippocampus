#!/usr/bin/env python3
"""hippo's subagentStatusLine (DESIGN §3.6): the agent-panel row of each hippo:lane agent shows
its codex lane's progress — `codex · <scope> · <elapsed> · <cmds> cmds · <last event>`.

Claude Code runs this every ~5s with the visible rows on stdin ({transcript_path, columns,
tasks: [{id, description, cwd, …}]}) and renders each `{"id", "content"}` line it prints in
place of that row's default. A row this script leaves out keeps Claude Code's own rendering
(measured on 2.1.281, and documented), so only hippo:lane rows are printed. It runs on every
tick of every session with the plugin enabled: stdlib only, no subprocess, small files only.
"""
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

LANE_AGENT = "hippo:lane"
# The lane agent's transcript names its lane twice: the grep result (`dispatch:<id>`) and every
# watch call (`--watch <id>`). The last mention wins — the prompt, which comes first, is main's
# text and could quote an id of some other lane.
ID_RE = re.compile(r"(?:dispatch:|--watch\s+)(d[0-9a-f]{32})\b")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")


def elapsed(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def seconds_since(start, end=None):
    try:
        t0 = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        t1 = (datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
              if end else datetime.now(timezone.utc))
    except (TypeError, ValueError):
        return 0
    return (t1 - t0).total_seconds()


def fit(text, width):
    """One line no wider than the row: wide (CJK) characters take two columns."""
    text = " ".join(text.split())
    if not width or width <= 0:
        return text
    out, used = [], 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + w > width - 1:
            return "".join(out) + "…"
        out.append(ch)
        used += w
    return text


def dispatch_id(transcript):
    try:
        found = ID_RE.findall(transcript.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return found[-1] if found else None


def alive(pid):
    """The wrapper's pid still runs — the check `hippo dispatch --watch` makes (watch_state)."""
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lane_record(cwd, did):
    """.hippo/lanes/<id>.json, walking up from the agent's cwd the way the CLI finds .hippo/."""
    for d in (cwd, *cwd.parents):
        p = d / ".hippo" / "lanes" / f"{did}.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return None  # mid-replace or unreadable: the next tick reads it
    return None


def lane_row(row, transcript):
    did = dispatch_id(transcript)
    if did is None:
        return f"codex · {row.get('description') or 'lane'} · starting"
    rec = lane_record(Path(row.get("cwd") or "."), did)
    if rec is None:
        return f"codex · {row.get('description') or 'lane'} · {did} · no lane record yet"
    head = f"codex · {rec.get('scope')}"
    cmds = f"{rec.get('cmds', 0)} cmds"
    last = rec.get("last") or "no event yet"
    took = elapsed(seconds_since(rec.get("started"), rec.get("ended") or rec.get("last_at")))
    if rec.get("status"):
        how = f"killed by {rec['signal']}" if rec.get("signal") else rec["status"]
        rc = "" if rec.get("rc") is None else f" rc={rec['rc']}"  # none: SIGKILLed before codex exited
        return f"{head} · {how}{rc} · {took} · {cmds}"
    if not alive(rec.get("pid")):  # a dead lane's timer must not keep counting
        return f"{head} · lost, its wrapper is gone · {took} · {cmds} · {last}"
    return f"{head} · {elapsed(seconds_since(rec.get('started')))} · {cmds} · {last}"


def main():
    try:
        ctx = json.load(sys.stdin)
    except ValueError:
        return
    tp = ctx.get("transcript_path") or ""
    if not tp.endswith(".jsonl"):
        return
    agents = Path(tp[: -len(".jsonl")]) / "subagents"
    for row in ctx.get("tasks") or []:
        rid = row.get("id")
        if not isinstance(rid, str) or not SAFE_ID.fullmatch(rid):
            continue
        try:
            meta = json.loads((agents / f"agent-{rid}.meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # not a subagent with a transcript: its default row stays
        if meta.get("agentType") != LANE_AGENT:
            continue
        content = fit(lane_row(row, agents / f"agent-{rid}.jsonl"), ctx.get("columns"))
        print(json.dumps({"id": rid, "content": content}, ensure_ascii=False))


if __name__ == "__main__":
    main()
