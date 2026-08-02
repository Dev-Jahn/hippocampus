# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""hippo CLI — ledger/task/directive recording and the clerk pipeline (DESIGN.md §3.2, §3.3, §3.5)."""

import argparse
import collections
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CLERKS = ROOT / "clerks"
SCRIPTS = ROOT / "scripts"
TASK_STATUSES = ("pending", "active", "done", "dropped")
OPEN_STATUSES = ("pending", "active")
SCRIBE_TIMEOUT = 120  # DESIGN §3.5.4
DISTILL_TIMEOUT = 300
REAP_GRACE = 15  # clerk_run.sh owns the real deadline; we outlive it to read its rc
LOCK_WAIT = 3.0  # brief blocking retry so the tail of the last turn is not lost
SUBSTANTIVE = re.compile(r"^(?:\[\d+\]\s*)?(TOOL|USER)\b")
WORKLOG_ENTRY = re.compile(r"^- (\d\d:\d\d) (.*)$")

# --- ledger schema (DESIGN §3.2 — exactly this) ------------------------------

REQUIRED = {
    "dispatch": ("id", "kind", "exec", "scope"),
    "outcome": ("ref", "result"),
    "review": ("id", "base", "source", "findings"),
    "review-status": ("ref", "addressed"),
    "directive": ("id", "state"),
    "clerk": ("name", "ok"),
    # usage: what a lane actually cost (§9.6) — written by the wrapper at lane exit, from the
    # banner + rollout / footer it observed. tokens is the total; tin/tcached/tout carry the
    # billing breakdown when the rollout gave one.
    "usage": ("ref", "tokens"),
}
# Per-ev key whitelist — anything else is rejected. A field that is not in the schema
# (§3.2) would silently poison the derived aggregates (the distiller).
ALLOWED = {
    # depth: how far this lane may re-delegate (§9.5 — 0 = leaf, told not to spawn; the clause
    # is indexed, not deleted). parent: the dispatch id of the lane that launched this one —
    # an unintended depth-2 becomes an event in the ledger, not a prohibition nobody can check.
    "dispatch": ("id", "kind", "exec", "scope", "task", "depth", "parent"),
    "outcome": ("ref", "result", "attr", "rework", "by", "note"),
    "review": ("id", "base", "source", "findings"),
    "review-status": ("ref", "addressed", "at"),
    "directive": ("id", "text", "lifetime", "state", "audience"),
    "clerk": ("name", "ok", "ms", "tokens"),
    "usage": ("ref", "tokens", "model", "tin", "tcached", "tout"),
}
# Fields only the writer stamps. Rejected if a caller (clerk output, log raw, environment)
# supplies them: the scribe reads an untrusted transcript, so it must not be able to forge
# the timestamp or the source.
WRITER_ONLY = ("t", "src")
# executor = "the agent that did the work wrote this" (§9.2). A self-reported outcome is a
# claim, never a verdict: everything that folds outcomes into acceptance skips this src.
SRC_VALUES = ("scribe", "cli", "wrapper", "executor")  # DESIGN §3.2
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# exec is the second axis PRIORS aggregates on, so its shape is a contract, not a hint:
# exactly executor/model/effort, no whitespace. Measured on a real ledger, a free-form field
# produced 24 spellings for 3 real executors. Of the 11 distinct first slots, 8 were category
# errors — mostly a launch mechanism ("background", "bash", the wrapper's own path) rather than
# the agent that did the work, which is what the old name "vehicle" invited.
EXEC_RE = re.compile(r"^[^/\s]+/[^/\s]+/[^/\s]+$")
# The placeholder words themselves showed up as values ("vehicle/gpt-5.6-sol/high"): the
# shape was right, so only naming them catches it.
EXEC_PLACEHOLDERS = {"executor", "vehicle", "model", "effort"}
# The two closed slots of exec. They are checked only on the scribe's own output (§3.5.6b), never
# on what main writes: a vocabulary holds where the writer *has* the value and fails where it has
# to infer one. Measured on a consuming project — the wrapper, which reads its own argv, produced
# 0 malformed exec in 110 dispatches; the scribe, reading a transcript, produced 12 in 45.
EXECUTORS = {"codex", "claude", "fork", "subagent", "workflow"}
EFFORTS = {"low", "medium", "high", "xhigh", "ultra", "inherit"}
ENUMS = {
    ("outcome", "result"): {"accepted", "revised", "refuted", "no-go", "lost"},
    ("outcome", "attr"): {"work", "brief", "harness"},
    ("directive", "lifetime"): {"turn", "phase", "durable"},
    ("directive", "state"): {"active", "withdrawn", "expired"},
    # audience is the second directive axis (§9.4): lifetime is *when* it holds, audience is
    # *who* it binds. Absent = all — a narrow default would silently hide a constraint from
    # the worker that needed it.
    ("directive", "audience"): {"main", "executor", "all"},
    # addressed is the one field reviews are folded on ("not fully addressed" in the fact
    # sheet), so it is closed like result: a free-form "fully" would read as open forever.
    ("review-status", "addressed"): {"full", "partial", "none"},
}
# A directive id is a handle the scribe and the user both have to type from memory, so it is
# kebab ASCII or nothing. An id derived from non-ASCII text collapses to the empty string, and
# an id that is empty (or spelled differently every time) cannot supersede anything.
DIRECTIVE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
INT_FIELDS = ("findings", "rework", "ms", "tokens", "depth", "tin", "tcached", "tout")


def validate_event(e):
    """Reason string on failure, None when the event is valid."""
    if not isinstance(e, dict):
        return "an event must be a JSON object"
    ev = e.get("ev")
    if ev not in REQUIRED:
        return f"unknown ev: {ev!r} (allowed: {', '.join(sorted(REQUIRED))})"
    for f in WRITER_ONLY:
        if f in e:
            return f"ev={ev}: {f} is stamped by the writer — a caller cannot set it"
    unknown = sorted(k for k in e if k != "ev" and k not in ALLOWED[ev])
    if unknown:
        return (
            f"ev={ev}: key not allowed: {', '.join(unknown)} "
            f"(allowed: {', '.join(ALLOWED[ev])})"
        )
    for f in REQUIRED[ev]:
        if e.get(f) is None or (isinstance(e[f], str) and not e[f].strip()):
            return f"ev={ev}: required field missing: {f}"
    if ev == "directive":
        if not DIRECTIVE_ID_RE.match(str(e["id"])):
            return (
                f"ev=directive: id must be lowercase kebab ascii ([a-z0-9] joined by '-'): "
                f"{e['id']!r}"
            )
        if e["state"] == "active":
            for f in ("text", "lifetime"):
                if not e.get(f):
                    return f"ev=directive state=active: required field missing: {f}"
    for (evn, field), allowed in ENUMS.items():
        if ev == evn and field in e and e[field] not in allowed:
            return f"ev={ev}: {field}={e[field]!r} — allowed: {', '.join(sorted(allowed))}"
    for f in INT_FIELDS:
        if f in e and (isinstance(e[f], bool) or not isinstance(e[f], int)):
            return f"ev={ev}: {f} must be an integer"
    if ev == "clerk" and not isinstance(e["ok"], bool):
        return "ev=clerk: ok must be true or false"
    if ev == "dispatch":
        ex = str(e["exec"])
        if not EXEC_RE.match(ex) or EXEC_PLACEHOLDERS & set(ex.split("/")):
            return (
                f"ev=dispatch: exec must be executor/model/effort with no spaces: {ex!r} "
                "(executor is the agent that did the work — codex|claude|fork|subagent|workflow "
                "— not how it was launched)"
            )
    # review.base is the whole of SHA pinning (§3.2) — refuse placeholders like "unknown".
    if ev == "review" and not SHA_RE.match(str(e["base"])):
        return f"ev=review: base must be a 7-40 char hex sha: {e['base']!r}"
    return None


# --- basics -------------------------------------------------------------------


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def one_line(text, limit=None):
    """Fold a string onto one line, and cap it when a limit is given (§6).

    A directive's text is written by the scribe from an untrusted transcript, so the newlines
    always have to go — a multi-line value would break the one-directive-per-line shape of the
    resident surface. Length is a different matter: truncating a directive silently drops the
    tail the author cared about, so directives are folded but never cut (principle 3, record
    rather than enforce). `hippo directive add` warns about length instead."""
    s = " ".join(str(text or "").split())
    return s if limit is None or len(s) <= limit else s[: limit - 1] + "…"


ACTIVE_PARSER = None  # usage to attach to errors (m7 — main() fills it in after parsing)


def die(msg, code=1):
    print(msg, file=sys.stderr)
    if ACTIVE_PARSER is not None:
        print(ACTIVE_PARSER.format_usage().rstrip(), file=sys.stderr)
    sys.exit(code)


def find_hippo():
    """Walk up from cwd looking for .hippo/ (the git root and $HOME are the ceiling).

    A `.git` *directory* is a real repository root: stop there, never adopt a project from
    beyond it. A `.git` *file* marks a linked worktree, and by convention lanes live inside
    the repo (`.claude/worktrees/<name>`) — walk through it, so an executor calling hippo
    from its worktree resolves the project's real .hippo/ (§9.1)."""
    d = Path.cwd().resolve()
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        home = None
    for p in [d, *d.parents]:
        if (p / ".hippo").is_dir():
            return p / ".hippo"
        if (p / ".git").is_dir():
            break
        if p == home:  # never go above $HOME — we could adopt someone else's project
            break
    return None


def resolve_src(src=None):
    """src ∈ scribe|cli|wrapper|executor (DESIGN §3.2, §9.2). The built-in surfaces pass it
    programmatically (`hippo dispatch` stamps wrapper itself). HIPPO_DISPATCH — planted by the
    wrapper into the lane's environment — makes every write from that lane src=executor: the
    agent that did the work wrote this. HIPPO_SRC remains for an *external* launch wrapper
    declaring itself one — and either env value dies loudly if it is off the whitelist. An
    executor that forges its way around this succeeds, and has now lied in an append-only
    file — a better place for it than a blocked write (§9.2)."""
    v = (src
         or ("executor" if os.environ.get("HIPPO_DISPATCH") else None)
         or os.environ.get("HIPPO_SRC") or "cli")
    if v not in SRC_VALUES:
        die(f"src must be one of {'|'.join(SRC_VALUES)}: {v!r} (check HIPPO_SRC)")
    return v


def append_event(hp, e, src=None):
    # t/src are always stamped by the writer: anything a caller sent is dropped here
    # (validate_event refuses it upstream anyway) and replaced with the verified value.
    body = {k: v for k, v in e.items() if k not in WRITER_ONLY}
    rec = {"t": now_iso(), **body, "src": resolve_src(src)}
    with (hp / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read_ledger(hp):
    p = hp / "ledger.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def judged_refs(rows):
    """Dispatch ids that carry a *verdict*. An executor's self-report (src=executor) is a
    claim, not a verdict (§9.2) — it never counts as judged anywhere that word matters: the
    in-flight line, the scribe's re-judge rule, the task: ref resolution, the priors join."""
    return {e.get("ref") for e in rows
            if e.get("ev") == "outcome" and e.get("src") != "executor"}


def executor_claims(rows):
    """ref → the lane's latest word for it (§9.3: rendered as a claim, subject visible, never
    as a flat fact). Last claim wins: a lane revising itself (accepted → no-go) should be shown
    saying what it currently says."""
    out = {}
    for e in rows:
        if e.get("ev") == "outcome" and e.get("src") == "executor":
            out[e.get("ref")] = e.get("result")
    return out


def directives(hp):
    """id → current state (the last event for an id is the truth — no derived file).

    Directive events written by a lane (src=executor) are recorded but never fold: a directive
    is a standing rule for every reader, and a lane adding or withdrawing one for the whole
    network is exactly the belief propagation §9.3 exists to prevent. The attempt stays in the
    ledger — visible to checkup and grep — it just does not become what the capsule believes.
    Same shape as outcomes: an executor may record, and the derived views decide what a
    recording means."""
    cur = {}
    for e in read_ledger(hp):
        if e.get("ev") != "directive" or not e.get("id"):
            continue
        if e.get("src") == "executor":
            continue
        d = cur.setdefault(e["id"], {"id": e["id"]})
        d.update({k: v for k, v in e.items() if k not in ("ev", "src")})
    return cur


def tasks_load(hp):
    p = hp / "tasks.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None
    data = data or {}
    if not isinstance(data, dict) or not isinstance(data.get("tasks", []), list):
        die(f"malformed tasks.yaml: expected {{tasks: [...]}} ({p})")
    data.setdefault("tasks", [])
    return data


def tasks_save(hp, data):
    p = hp / "tasks.yaml"
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def find_task(data, tid):
    for t in data["tasks"]:
        if t.get("id") == tid:
            return t
    return None


def config(hp):
    p = hp / "config.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def clerk_env(hp, timeout):
    env = dict(os.environ)
    env["HIPPO_CLERK_TIMEOUT"] = str(timeout)
    backend = (config(hp).get("clerk") or {}).get("backend")
    if backend:  # config.yaml > $HIPPO_CLERK_BACKEND (DESIGN §3.5.4)
        env["HIPPO_CLERK_BACKEND"] = str(backend)
    return env


def run_clerk(hp, prompt_path, input_text, timeout):
    """Call clerk_run.sh → (stdout, stderr, rc, ms, tokens).
    Missing infrastructure is always a hard error. tokens is a chars/4 estimate (m2)."""
    script = SCRIPTS / "clerk_run.sh"
    if not script.exists():
        die(f"no clerk runner: {script}")
    if not prompt_path.exists():
        die(f"no clerk prompt: {prompt_path}")
    prompt_text = prompt_path.read_text(encoding="utf-8")
    # Scratch files live outside .hippo/ (system temp dir) — the contents of .hippo/
    # must stay exactly what §3.1 documents.
    fd, tmp_name = tempfile.mkstemp(prefix="hippo-clerk-", suffix=".txt")
    tmp = Path(tmp_name)
    os.close(fd)
    tmp.write_text(input_text, encoding="utf-8")
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [str(script), str(prompt_path), str(tmp)],
            capture_output=True,
            text=True,
            timeout=timeout + REAP_GRACE,
            env=clerk_env(hp, timeout),
        )
        out, err, rc = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        out, err, rc = "", f"timeout {timeout}s", -1
    finally:
        tmp.unlink(missing_ok=True)
    tokens = (len(prompt_text) + len(input_text) + len(out)) // 4
    return out, err, rc, int((time.monotonic() - t0) * 1000), tokens


def dump_failure(hp, kind, text):
    d = hp / "failures"
    d.mkdir(exist_ok=True)
    # A second-resolution timestamp alone lets two failures in the same second overwrite each other.
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    p = d / f"{stamp}-{kind}-{os.getpid()}-{uuid.uuid4().hex[:6]}.txt"
    p.write_text(text, encoding="utf-8")
    return p


def extract_json(text):
    """Lenient JSON object extraction: tolerates surrounding noise and code fences."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


# --- commands -----------------------------------------------------------------


def cmd_init(_args):
    hp = Path.cwd() / ".hippo"
    if hp.is_dir():
        print(f"already exists: {hp}")
        return
    hp.mkdir(parents=True)
    (hp / "failures").mkdir()
    # briefs/ is never read by hippo — created so the brief convention (§3.1) is discoverable
    # instead of every wave reinventing an absolute scratchpad path. The COMMON.md seed is
    # written once and never read back: it bootstraps every lane to the capsule, which is
    # where the usage contract actually lives (single source, generated).
    (hp / "briefs").mkdir()
    (hp / "briefs" / "COMMON.md").write_text(
        "# COMMON — shared brief clauses (hand-maintained; hippo never reads this file)\n"
        "\n"
        "Lane bootstrap: run `hippo status --inject` before starting. Its `live(…)` lines are\n"
        "standing constraints, and its `report:` line tells you how to record your outcome.\n"
        "Re-run it after any context compaction — constraints do not survive compaction on\n"
        "their own.\n",
        encoding="utf-8",
    )
    (hp / "ledger.jsonl").touch()
    tasks_save(hp, {"tasks": []})
    print(f"created: {hp}")
    print(
        "next: `hippo directive add --text \"…\" --lifetime durable` for a standing instruction, "
        "`hippo task add <type>/<slug> --title …` for work, `hippo status` to see both.",
    )
    print(
        ".hippo/ is this project's memory (ledger, tasks, worklog) — most projects gitignore "
        "it; commit it only if collaborators should share one memory.",
    )


# Nudge thresholds for the directive block (DESIGN §6). None of these is enforced: every active
# directive is injected in full, whatever the totals say. They exist so the surfaces that touch
# directives can name the cost while the author can still do something about it — silently folding
# a ruling away is the failure mode principle 9 is about, and a cap is just a quieter way to lose it.
DIRECTIVE_TEXT_NUDGE = 200  # one directive this long is asking to be compressed
DIRECTIVE_COUNT_NUDGE = 8  # this many live at once is asking for a hygiene pass
DIRECTIVE_TOTAL_NUDGE = 1600  # total characters resident in every session from here on
# Staleness is shown, never resolved (a scribe once withdrew a live hold because a report
# mentioned its keyword — automation that decides is the failure, visibility is the fix).
# Only `phase` ages: durable is indefinite by definition and turn expires by itself.
PHASE_AGE_SHOW_D = 7  # a phase directive this old carries its age in the capsule line
PHASE_STALE_NUDGE_D = 14  # this old, the volume notes ask whether the phase is over


def directive_volume_notes(hp):
    """Notes about what the live directive set currently costs, or [] when it costs little.

    Measured against the set as it stands, not against the one directive being written: the
    expensive ones are usually the ones already resident, and warning only at write time leaves
    them permanently unmentioned — every session pays and nobody is ever told."""
    live = [d for d in directives(hp).values() if d.get("state") == "active"]
    sized = [(d["id"], len(one_line(d.get("text", "")))) for d in live]
    total = sum(n for _, n in sized)
    long = sorted([(i, n) for i, n in sized if n > DIRECTIVE_TEXT_NUDGE], key=lambda x: -x[1])
    notes = []
    if long:
        listed = ", ".join(f"{i} ({n})" for i, n in long)
        notes.append(
            f"note: {len(long)} directive(s) over {DIRECTIVE_TEXT_NUDGE} chars — {listed}. "
            "Compress and re-add under the same --id."
        )
    if len(live) >= DIRECTIVE_COUNT_NUDGE or total >= DIRECTIVE_TOTAL_NUDGE:
        notes.append(
            f"note: {len(live)} live directives, {total} chars — all of it rides every session "
            "from here on (each reader gets its audience's slice). Compress them, or withdraw "
            "the stale ones (`hippo directive withdraw <id>`)."
        )
    now = datetime.now(timezone.utc)
    stale = sorted(
        ((d["id"], (now - t).days) for d in live
         if d.get("lifetime") == "phase" and (t := event_time(d))
         and (now - t).days >= PHASE_STALE_NUDGE_D),
        key=lambda x: -x[1],
    )
    if stale:
        listed = ", ".join(f"{i} ({n}d)" for i, n in stale)
        notes.append(
            f"note: phase directive(s) {PHASE_STALE_NUDGE_D}d or older — {listed}. If that "
            "phase is over, withdraw them (`hippo directive withdraw <id>`); if it is not, "
            "they are still doing their job."
        )
    return notes


IN_FLIGHT_WINDOW_H = 24


def in_flight(hp):
    """Delegations launched but not yet judged — the one part of "where was I" that is a fact.

    Only launcher-written dispatches count (`wrapper`/`cli`): those are the ones somebody chose to
    launch and will come back to. Scribe-inferred rows would swamp it — measured on a consuming
    project, the same query over all writers returned 16 where three lanes were actually flying,
    and restricting it to the launcher returned exactly those three, matching by hand the list the
    project was maintaining in a file.

    A dispatch older than a day is not in flight, it is forgotten; `prior distill` already reports
    those as open items, which is the right place for them.

    A lane's self-reported outcome does not land it: the claim rides along, subject visible
    (§9.3), and the entry leaves this line only when main's verdict arrives."""
    rows = read_ledger(hp)
    judged = judged_refs(rows)
    claims = executor_claims(rows)
    now = datetime.now(timezone.utc)
    out = []
    for e in rows:
        if e.get("ev") != "dispatch" or e.get("src") not in ("wrapper", "cli"):
            continue
        if e.get("id") in judged:
            continue
        t = event_time(e)
        if not t or (now - t) > timedelta(hours=IN_FLIGHT_WINDOW_H):
            continue
        mins = int((now - t).total_seconds()) // 60
        age = f"{mins // 60}h{mins % 60:02d}m"
        claim = claims.get(e.get("id"))
        tail = f"{age} · claims {claim}" if claim else age
        out.append(f"{one_line(e.get('scope', ''), 44)} ({tail})")
    return out


def status_lines(hp):
    """DESIGN §6 resident surface (header + live directives + in flight + last).

    Audience (§9.4): inside a dispatched lane (HIPPO_DISPATCH set) the capsule carries the
    directives addressed to executors; everywhere else, the ones addressed to main. `all`
    (and absent, its default) reaches both."""
    data = tasks_load(hp)
    n_open = sum(1 for t in data["tasks"] if t.get("status") in OPEN_STATUSES)
    reader = "executor" if os.environ.get("HIPPO_DISPATCH") else "main"
    live = [d for d in directives(hp).values() if d.get("state") == "active"
            and (d.get("audience") or "all") in (reader, "all")]

    def stamp(name):
        p = hp / name
        return (
            datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d")
            if p.exists()
            else "—"
        )

    lines = [
        (
            f"[hippo] tasks {n_open} open · directives {len(live)} live "
            f"· priors {stamp('PRIORS.md')} · worklog {stamp('worklog.md')}"
        )
    ]
    # durable first — a standing ruling should be read before the situational ones. Every active
    # directive appears in full: a directive that is invisible at session start is effectively not
    # there (principle 9, read backwards), and that is as true of the ninth one as of the first.
    # Volume is handled by warning the author at `directive add` time, not by dropping text here.
    ordered = [d for d in live if d.get("lifetime") == "durable"] + [
        d for d in live if d.get("lifetime") != "durable"
    ]
    now = datetime.now(timezone.utc)
    for d in ordered:
        label = d.get("lifetime") or "?"
        # A phase has an end, so its age is information; showing it is the whole staleness
        # mechanism (nothing expires by itself — the verdict stays with main and the user).
        if label == "phase":
            t = event_time(d)
            if t and (now - t).days >= PHASE_AGE_SHOW_D:
                label = f"phase·{(now - t).days}d"
        lines.append(f"· live({label}): {one_line(d.get('text', ''))}")
    # Nothing flying → no line. The capsule only spends a line on a question that has an answer.
    flying = in_flight(hp)
    if flying:
        lines.append(f"· in flight: {', '.join(flying)}")
    p = hp / "worklog.md"
    if p.exists():
        # Within the latest date section, only look at the "- HH:MM …" entries the scribe wrote.
        # (Nested bullets or free-form human bullets must not be picked up as `last`.)
        wl = p.read_text(encoding="utf-8").splitlines()
        starts = [i for i, ln in enumerate(wl) if ln.startswith("## ")]
        section = wl[starts[-1] + 1 :] if starts else wl
        entries = [m.group(2).strip() for m in map(WORKLOG_ENTRY.match, section) if m]
        if entries:
            lines.append(f"· last: {one_line(entries[-1], 120)}")
    if reader == "executor":
        # The lane's whole operating contract, generated where the lane actually reads
        # (principle 5): briefs no longer hand-copy any of it, and it survives the lane's own
        # compaction wherever the SessionStart gate re-injects this capsule. The re-delegation
        # clause is indexed by depth (§9.5) — never enforced, the wrapper records what happens.
        try:
            depth = int(os.environ.get("HIPPO_DEPTH", "0"))
        except ValueError:
            depth = 0
        lines.append(
            "· report: hippo log outcome --result accepted|revised|refuted|no-go|lost "
            "--note '…' — no --ref needed; recorded as your claim, main judges"
        )
        if depth <= 0:
            lines.append(
                "· depth 0: do not re-delegate — implement it yourself; "
                "no codex exec, no subagent, no workflow"
            )
        else:
            lines.append(
                f"· depth {depth}: you may dispatch children (`hippo dispatch …`) — "
                "each child starts at depth 0 and is told not to re-delegate"
            )
        lines.append(
            "· discipline: report no-go early when the premise does not hold; "
            "long runs go to background — never poll with a foreground sleep"
        )
    return lines


def cmd_status(args):
    hp = args.hp
    print("\n".join(status_lines(hp)))
    if args.inject:
        return
    open_tasks = [
        t for t in tasks_load(hp)["tasks"] if t.get("status") in OPEN_STATUSES
    ]
    if open_tasks:
        print("\ntasks:")
        for t in open_tasks:
            print(f"  {t['id']}  [{t.get('status', '?')}]  {t.get('title', '')}")


def cmd_task_add(args):
    data = tasks_load(args.hp)
    if find_task(data, args.id):
        die(f"task id already exists: {args.id}")
    t = {
        "id": args.id,
        "title": args.title,
        "status": args.status,
        "notes": [args.notes] if args.notes else [],
        "deps": [d.strip() for d in args.deps.split(",") if d.strip()]
        if args.deps
        else [],
        "updated": now_iso(),
    }
    if t["status"] not in TASK_STATUSES:
        die(f"status must be one of {'|'.join(TASK_STATUSES)}: {t['status']}")
    data["tasks"].append(t)
    tasks_save(args.hp, data)
    print(f"added {args.id}")


def cmd_task_set(args):
    data = tasks_load(args.hp)
    t = find_task(data, args.id)
    if not t:
        die(f"no such task id: {args.id}")
    field, value = args.field, args.value
    if field not in ("title", "status", "notes", "deps"):
        die("field must be one of title|status|notes|deps")
    if field == "status" and value not in TASK_STATUSES:
        die(f"status must be one of {'|'.join(TASK_STATUSES)}: {value}")
    if field == "deps":
        t["deps"] = [d.strip() for d in value.split(",") if d.strip()]
    elif field == "notes":
        t["notes"] = [value] if value else []
    else:
        t[field] = value
    t["updated"] = now_iso()
    tasks_save(args.hp, data)
    print(f"{args.id}.{field} = {value}")


def cmd_task_done(args):
    data = tasks_load(args.hp)
    t = find_task(data, args.id)
    if not t:
        die(f"no such task id: {args.id}")
    t["status"] = "done"
    if args.note:
        t.setdefault("notes", [])
        if not isinstance(t["notes"], list):
            t["notes"] = [t["notes"]]
        t["notes"].append(args.note)
    t["updated"] = now_iso()
    tasks_save(args.hp, data)
    print(f"done {args.id}")


def cmd_task_drop(args):
    data = tasks_load(args.hp)
    t = find_task(data, args.id)
    if not t:
        die(f"no such task id: {args.id}")
    t["status"] = "dropped"
    t["updated"] = now_iso()
    tasks_save(args.hp, data)
    print(f"dropped {args.id}")


def unmet_deps(tasks, t):
    """The deps of `t` that are not finished — i.e. why it cannot be started yet.

    `deps` was write-only until this existed: two writers, no reader, no output that mentioned it.
    Measured on a consuming project, 4 of 98 tasks used it — which is the rational response to a
    field that does nothing, not a sign nobody wanted ordering. That project was keeping its
    ordering in a hand-written re-entry document instead, with the reasons attached.

    A dep naming no task is marked `?` rather than passed over: it is the same silent shape as a
    dangling `ref` — it validates, it sits there, and it quietly stops meaning anything."""
    by_id = {x.get("id"): x for x in tasks}
    out = []
    for d in t.get("deps") or []:
        dep = by_id.get(d)
        if dep is None:
            out.append(f"{d}?")
        elif dep.get("status") not in ("done", "dropped"):
            out.append(d)
    return out


def cmd_task_list(args):
    tasks = tasks_load(args.hp)["tasks"]
    if not args.all:
        want = (
            [s.strip() for s in args.status.split(",") if s.strip()]
            if args.status
            else list(OPEN_STATUSES)
        )
        bad = [s for s in want if s not in TASK_STATUSES]
        if bad:
            die(f"unknown status: {', '.join(bad)} (allowed: {'|'.join(TASK_STATUSES)})")
        tasks = [t for t in tasks if t.get("status") in want]
    if args.json:
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
        return
    everything = tasks_load(args.hp)["tasks"]
    for t in tasks:
        print(f"{t.get('id')}  [{t.get('status', '?')}]  {t.get('title', '')}")
        # A second line only when there is something to say — the same rule the directive notes
        # follow. A task with nothing in its way should read as one line.
        waiting = unmet_deps(everything, t)
        if waiting:
            print(f"    waiting on: {', '.join(waiting)}")


def cmd_task_show(args):
    t = find_task(tasks_load(args.hp), args.id)
    if not t:
        die(f"no such task id: {args.id}")
    if args.json:
        print(json.dumps(t, ensure_ascii=False, indent=2))
        return
    print(yaml.safe_dump(t, allow_unicode=True, sort_keys=False).rstrip())


# ev -> the ev its `ref` must point at.
REF_TARGET = {"outcome": "dispatch", "review-status": "review", "usage": "dispatch"}
REF_HINT = {
    "outcome": " — a task id is not a dispatch id; the launcher prints the id as `dispatch:<id>`, "
               "or pass `--ref task:<task-id>` to have it resolved",
    "review-status": "",
}


def check_ref(hp, e):
    """`ref` must name an event that actually exists in this ledger.

    Measured on a real ledger: 54% of outcomes never joined to a dispatch — half of them because
    a caller passed a task id, the other half because the scribe invented an id of the right
    shape. Both look like data and both vanish from the priors, so this is fail-closed."""
    target = REF_TARGET.get(e.get("ev"))
    if target is None:
        return None
    ref = e.get("ref")
    known = {x.get("id") for x in read_ledger(hp) if x.get("ev") == target}
    if ref in known:
        return None
    return (
        f"ev={e['ev']}: ref={ref!r} is not a known {target} id"
        f"{REF_HINT.get(e['ev'], '')}. Find it with `hippo log tail --ev {target}`"
    )


def resolve_ref(hp, ref):
    """`task:<task-id>` → the dispatch id it stands for. Anything else is passed through.

    What lands in the ledger is still a dispatch id — the contract of §3.2 is untouched. This only
    removes the grep: a task id is what an operator actually remembers, while a dispatch id is a
    hash they have to go find, and the old answer to that friction was to pass the task id *as* the
    ref, which stored a join that silently never resolved (half of the measured 54% of unjoined
    outcomes). Resolving at write time keeps the join real and lets the caller say what they know.

    Resolution is over the task's dispatches that have no outcome yet, because that is the one an
    outcome is about. Two candidates is a genuine ambiguity between parallel lanes, so it lists
    them and fails rather than guessing."""
    if not isinstance(ref, str) or not ref.startswith("task:"):
        return ref
    task = ref[len("task:"):]
    rows = read_ledger(hp)
    judged = judged_refs(rows)  # a lane's claim leaves its dispatch still awaiting the verdict
    hits = [e for e in rows if e.get("ev") == "dispatch" and e.get("task") == task]
    if not hits:
        die(f"--ref {ref}: no dispatch recorded for task {task!r}")
    open_ = [e for e in hits if e.get("id") not in judged]
    if len(open_) == 1:
        return open_[0]["id"]
    if not open_:
        listed = ", ".join(str(e.get("id")) for e in hits)
        die(f"--ref {ref}: every dispatch for task {task!r} already has an outcome ({listed}). "
            "Pass the dispatch id explicitly to record a second one.")
    listed = "\n".join(f"  {e.get('id')}  {one_line(e.get('scope', ''), 60)}" for e in open_)
    die(f"--ref {ref}: task {task!r} has {len(open_)} dispatches awaiting an outcome — "
        f"name one explicitly:\n{listed}")


def validate_scribe_event(e):
    """Extra rules for the clerk's own output. Main's writes never see these.

    The line is not "who is trusted" — it is who *observed* the value. The launcher builds `exec`
    from its own argv, so a vocabulary it is handed holds (0 malformed in 110, and `kind` has held
    the same way with no validation at all). The scribe infers `exec` from a transcript, and
    inference is where a vocabulary stops working: 12 malformed in 45, every one of them
    scribe-written — `background/CPU/sol-high`, `unknown/GPT-5.6/unknown`, `bash/unknown/unknown`.

    Constraining the clerk is not enforcement in the sense principle 3 guards against. The clerk is
    a component hippo spawns with its tools disabled and whose every event it already parses and
    can reject; it is not a party whose work is being restricted. Rejected events land in
    `failures/` like any other, so nothing disappears quietly.

    A codex launch belongs to the wrapper, which was there when it happened. The scribe recording
    one produces either a duplicate (measured: every confirmed pair was scribe-vs-launcher) or a
    record of a launch that bypassed the wrapper — and that is a gap worth seeing as a gap, not
    worth filling with an inferred row that then dilutes the priors. What the scribe alone can
    see, and must keep recording, is everything the wrapper cannot cover: fork, subagent,
    workflow, claude."""
    if e.get("ev") == "usage":
        # Same line as the codex-dispatch rule: the wrapper observed the cost from the banner
        # and rollout; a scribe would be inferring it from a paraphrase.
        return "ev=usage: the wrapper records usage — it was there when the lane ran"
    if e.get("ev") != "dispatch":
        return None
    # validate_event has already guaranteed the three-slot shape by the time this runs.
    parts = str(e.get("exec", "")).split("/")
    executor, effort = parts[0], parts[-1]
    if executor == "codex":
        return ("ev=dispatch: the scribe does not record codex launches — `hippo dispatch` "
                "records those when it runs them. If it did not run, the gap is the record.")
    if executor not in EXECUTORS:
        return (f"ev=dispatch: executor must be one of {'|'.join(sorted(EXECUTORS))}: "
                f"{executor!r} (it is the agent that did the work, not how it was launched)")
    if effort not in EFFORTS:
        return (f"ev=dispatch: effort must be one of {'|'.join(sorted(EFFORTS))}: {effort!r} "
                "(use `inherit` when the executor takes its setting from its parent)")
    return None


def check_scribe_outcome(hp, e):
    """Scribe-only, like validate_scribe_event: a second verdict for a dispatch is rejected.

    The prompt has always said "at most one outcome per dispatch", and the roster marks the
    judged ones — and the clerk re-judged anyway, measured on this repo's own ledger: one
    dispatch judged `revised` was re-judged `accepted` by two later scribe runs, and a verdict
    main had recorded through the CLI was restated by the scribe 26 seconds later. Same lesson
    as the dispatch roster (§3.5.5): a prompt cannot hold a rule its writer does not check.

    Main is different on purpose. A deliberate re-verdict (say, `revised` upgraded after
    rework) is main's call to make; it gets a stderr note, never a refusal — and the priors
    read the first verdict either way (the routing table is a first-pass rate)."""
    if e.get("ev") != "outcome":
        return None
    # A lane's self-report is a claim, not a verdict (§9.2): the scribe recording main's
    # explicit acceptance signal *after* a claim is the normal flow, not a re-judgment.
    if e.get("ref") in judged_refs(read_ledger(hp)):
        return (f"ev=outcome: ref={e.get('ref')!r} already has a verdict — the scribe does not "
                "re-judge (at most one outcome per dispatch; a second verdict belongs to main)")
    return None


def log_and_print(hp, e):
    err = validate_event(e) or check_ref(hp, e)
    if err:
        die(f"validation failed: {err}")
    rec = append_event(hp, e)
    print(json.dumps(rec, ensure_ascii=False))


def cmd_log(args):
    e = {"ev": args.ev}
    prior_verdicts = []
    if args.ev == "dispatch":
        e.update(id=args.id, kind=args.kind, exec=args.exec, scope=args.scope)
        if args.task:
            e["task"] = args.task
        if args.depth is not None:
            e["depth"] = args.depth
        if args.parent:
            e["parent"] = args.parent
    elif args.ev == "outcome":
        # Inside a dispatched lane, HIPPO_DISPATCH names the one dispatch the writer is (§9.2):
        # the lane does not have to know its own hash to report what it observed.
        ref = args.ref or os.environ.get("HIPPO_DISPATCH")
        if not ref:
            die("outcome: --ref is required (inside a dispatched lane HIPPO_DISPATCH supplies it)")
        e.update(ref=resolve_ref(args.hp, ref), result=args.result)
        for k in ("attr", "rework", "by", "note"):
            if getattr(args, k) is not None:
                e[k] = getattr(args, k)
        # Claims (src=executor) do not make this "a second verdict" — main judging a claimed
        # dispatch is the intended sequence, and the note would train the reader to skim.
        prior_verdicts = [
            x for x in read_ledger(args.hp)
            if x.get("ev") == "outcome" and x.get("ref") == e["ref"]
            and x.get("src") != "executor"
        ]
    elif args.ev == "review":
        e.update(id=args.id, base=args.base, source=args.source, findings=args.findings)
    elif args.ev == "review-status":
        e.update(ref=args.ref, addressed=args.addressed)
        if args.at:
            e["at"] = args.at
    elif args.ev == "directive":
        did = args.id
        if not did:
            if args.state != "active" or not args.text:
                die("directive: --id may be omitted only for a new (active) directive that carries --text")
            slug = re.sub(r"[^a-z0-9]+", "-", args.text.lower()).strip("-")[:20].strip("-")
            if not slug:
                # Text with no ascii letters (Korean, for one) slugs to the empty string, and the
                # old fallback turned every such directive into "directive-<hash>" — an id nobody
                # can recall or reuse. Refusing is the honest move: name it yourself.
                die(
                    "directive: --text has no [a-z0-9] to build an id from — pass --id explicitly "
                    "(lowercase kebab ascii, e.g. --id gpu-pinning)"
                )
            did = f"{slug}-{hashlib.sha1(args.text.encode()).hexdigest()[:4]}"
        e.update(id=did, state=args.state)
        if args.text:
            e["text"] = args.text
        if args.lifetime:
            e["lifetime"] = args.lifetime
        if getattr(args, "audience", None):
            e["audience"] = args.audience
        # Length is not warned about here: the set-wide notes emitted after the write name every
        # oversized directive by id and size, including this one. Saying it twice trains the
        # reader to skim the notes.
    log_and_print(args.hp, e)
    if args.ev == "directive":
        # After the write, so the notes describe the set the next session will actually carry.
        for note in directive_volume_notes(args.hp):
            print(note, file=sys.stderr)
    # Write-time notes (never refusals — the record always went through, principle 3):
    elif args.ev == "outcome" and prior_verdicts:
        # A deliberate re-verdict is legal for main; what it must not be is invisible. The
        # routing table is a *first-pass* rate, so a second verdict changes nothing there.
        first = prior_verdicts[0]
        print(
            f"note: {e['ref']} already had a verdict ({first.get('result')} at "
            f"{first.get('t')}) — recorded as a second one; priors read the first "
            "(first-pass rate).",
            file=sys.stderr,
        )
    elif args.ev == "review":
        # The one moment the closing half of the loop is on the caller's mind. Without this,
        # a recorded review stays "not fully addressed" in every distill, forever.
        print(
            f"note: when its findings are addressed, record `hippo log review-status "
            f"--ref {e['id']} --addressed full|partial|none [--at <sha>]`.",
            file=sys.stderr,
        )


def cmd_log_raw(args):
    try:
        e = json.loads(args.json)
    except json.JSONDecodeError as ex:
        die(f"JSON parse failed: {ex}")
    log_and_print(args.hp, e)


def cmd_directive_withdraw(args):
    d = directives(args.hp).get(args.id)
    if not d:
        die(f"no such directive id: {args.id}")
    log_and_print(args.hp, {"ev": "directive", "id": args.id, "state": "withdrawn"})


def cmd_directive_list(args):
    ds = list(directives(args.hp).values())
    if args.active:
        ds = [d for d in ds if d.get("state") == "active"]
    if args.json:
        print(json.dumps(ds, ensure_ascii=False, indent=2))
        return
    for d in ds:
        aud = d.get("audience")
        tag = f"/{aud}" if aud and aud != "all" else ""
        print(
            f"{d['id']}  [{d.get('state', '?')}/{d.get('lifetime', '?')}{tag}]  {d.get('text', '')}"
        )
    # Reviewing the set is the other moment the author can act on what it costs. On stderr, so
    # the listing itself stays a clean, pipeable record of the directives.
    for note in directive_volume_notes(args.hp):
        print(note, file=sys.stderr)


def cmd_log_tail(args):
    # Note: the `log` subparser's dest is ev, so args.ev == "tail" here — the filter flag
    # `--ev` steps aside as dest=ev_filter (the surface matches the old `ledger tail`).
    p = args.hp / "ledger.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    if args.ev_filter:
        keep = []
        for ln in lines:
            try:
                if json.loads(ln).get("ev") == args.ev_filter:
                    keep.append(ln)
            except json.JSONDecodeError:
                pass
        lines = keep
    for ln in lines[-args.n :]:
        print(ln)


def cmd_prior_show(args):
    p = args.hp / "PRIORS.md"
    print(
        p.read_text(encoding="utf-8").rstrip()
        if p.exists()
        else "not yet — run hippo prior distill"
    )


PRIOR_MIN_SAMPLE = 4


def event_time(e):
    try:
        return datetime.strptime(e.get("t", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def prior_facts(rows, now, prices=None):
    """Every number in PRIORS.md, computed here instead of by the clerk.

    Measured on a consuming project (293 events): all seven cells the clerk produced disagreed
    with the ledger, in both directions, and against the formula the clerk prompt itself states.
    Two of the errors changed the advice — `no-go` counted as failure invented a worst-performing
    cell that was really 2/2, and two verify cells read 100% while each hid a refutation. The join
    is deterministic (dispatch ⋈ outcome on ref, then count), so a model was the wrong instrument
    for it: principles 4 and 6. The clerk still writes every sentence of the page; it no longer
    does the sums, and it is no longer handed the raw ledger to do them from."""
    prices = prices or load_prices()
    disp = [e for e in rows if e.get("ev") == "dispatch"]
    # Self-reported outcomes are claims, not verdicts (§9.2): they enter no cell, no
    # attribution count, no unjoined count. The scorecard folds only judgments.
    outs = [e for e in rows if e.get("ev") == "outcome" and e.get("src") != "executor"]
    first = {}
    for e in outs:  # the ledger is append-only, so file order is chronological
        first.setdefault(e.get("ref"), e)
    known = {e.get("id") for e in disp}
    usage = {}
    for e in rows:  # one usage per dispatch from the wrapper; last wins if re-recorded
        if e.get("ev") == "usage":
            usage[e.get("ref")] = e

    cells, per_exec = {}, {}
    unpriced_models = collections.Counter()
    for d in disp:
        f = first.get(d.get("id"))
        if not f:
            continue
        for bucket, key in ((cells, (d.get("kind"), d.get("exec"))), (per_exec, d.get("exec"))):
            b = bucket.setdefault(key, {"judged": 0, "accepted": 0, "revised": 0, "refuted": 0,
                                        "no-go": 0, "lost": 0, "rework": 0,
                                        "tokens": 0, "usd": 0.0, "priced": 0, "unpriced": 0})
            b["judged"] += 1
            b[f.get("result")] = b.get(f.get("result"), 0) + 1
            b["rework"] += int(f.get("rework") or 0)
        # Cost lands on the kind × exec cell only (§9.6): tokens always; dollars only when
        # the sheet can price them honestly — an unpriced row is counted and named, not guessed.
        u = usage.get(d.get("id"))
        if u:
            b = cells[(d.get("kind"), d.get("exec"))]
            b["tokens"] += int(u.get("tokens") or 0)
            usd = price_usd(u, prices)
            if usd is None:
                b["unpriced"] += 1
                unpriced_models[u.get("model") or "(no model)"] += 1
            else:
                b["usd"] += usd
                b["priced"] += 1

    def rate(b):
        return b["judged"] - b["no-go"] - b["lost"]

    # Cells under the threshold are named but never given a rate: a percentage over n=1 reads as
    # evidence and is not one. Naming them keeps the omission visible instead of silent.
    lines = ["## routing — accepted / (judged − no-go − lost), per kind × exec", "",
             f"prices as of {prices.get('as_of') or 'unknown'} (prices.yaml); "
             "cost columns cover measured usage only",
             "",
             "| kind | exec | n | first-pass | revised | rework | tokens | $ | $/accepted "
             "| verdicts |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    thin = []
    for (kind, ex), b in sorted(cells.items(), key=lambda kv: -rate(kv[1])):
        n = rate(b)
        if n < PRIOR_MIN_SAMPLE:
            thin.append(f"{kind}×{ex} (n={n})")
            continue
        verdicts = ", ".join(f"{k} {b[k]}" for k in ("accepted", "revised", "refuted", "no-go",
                                                     "lost") if b[k])
        tok = f"{b['tokens']:,}" if b["tokens"] else "—"
        star = "*" if b["unpriced"] else ""
        usd_s = f"${b['usd']:.2f}{star}" if b["priced"] else ("—" + star)
        per_acc = (f"${b['usd'] / b['accepted']:.2f}"
                   if b["priced"] and b["accepted"] else "—")
        lines.append(f"| {kind} | {ex} | {n} | {b['accepted']}/{n} "
                     f"({100 * b['accepted'] / n:.1f}%) | {b['revised']} | {b['rework']} "
                     f"| {tok} | {usd_s} | {per_acc} | {verdicts} |")
    if thin:
        lines += ["", f"below the n={PRIOR_MIN_SAMPLE} threshold, no rate reported "
                      f"({len(thin)}): " + ", ".join(thin)]
    if unpriced_models:
        lines += ["", "unpriced usage (* above — model not on the price sheet, or no billing "
                      "breakdown): " + ", ".join(f"{m}×{c}" for m, c in
                                                 unpriced_models.most_common())]

    lines += ["", "## verification signal — refuted+revised share of judged, per exec", "",
              "| exec | judged | refuted+revised | rate |", "|---|---:|---:|---:|"]
    thin_exec = []
    for ex, b in sorted(per_exec.items(), key=lambda kv: -kv[1]["judged"]):
        bad = b["refuted"] + b["revised"]
        if b["judged"] < PRIOR_MIN_SAMPLE:
            thin_exec.append(f"{ex} (n={b['judged']})")
            continue
        lines.append(f"| {ex} | {b['judged']} | {bad} | {100 * bad / b['judged']:.1f}% |")
    if thin_exec:
        lines += ["", f"below the n={PRIOR_MIN_SAMPLE} threshold, no rate reported "
                      f"({len(thin_exec)}): " + ", ".join(thin_exec)]

    attr = collections.Counter(e.get("attr") for e in outs if e.get("result") != "accepted")
    unjoined = sum(1 for e in outs if e.get("ref") not in known)
    lines += ["", "## attribution of non-accepted outcomes", "",
              f"work {attr['work']} / brief {attr['brief']} / harness {attr['harness']} "
              f"/ unattributed {attr[None]}", "",
              f"## unjoined outcomes\n\n{unjoined}", "", "## open items — no outcome, launched "
              "more than 24h ago", ""]
    stale = []
    for d in disp:
        t = event_time(d)
        if d.get("id") in first or not t or (now - t) < timedelta(hours=24):
            continue
        h, m = divmod(int((now - t).total_seconds()) // 60, 60)
        stale.append(f"- {d.get('id')} ({h}h{m:02d}m)")
    lines += stale or ["(none)"]

    status = {}
    for e in rows:
        if e.get("ev") == "review-status":
            status[e.get("ref")] = e.get("addressed")
    open_reviews = [f"- {e.get('id')} base={e.get('base')} addressed={status.get(e.get('id'), 'none')}"
                    for e in rows if e.get("ev") == "review" and status.get(e.get("id")) != "full"]
    lines += ["", "## reviews not fully addressed", ""] + (open_reviews or ["(none)"])

    clerks = [e for e in rows if e.get("ev") == "clerk"]
    fails = sum(1 for e in clerks if e.get("ok") is False)
    tokens = sum(int(e.get("tokens") or 0) for e in clerks)
    lines += ["", "## clerk overhead", "",
              f"{len(clerks)} runs, {fails} failures, ~{tokens} tokens"]
    return "\n".join(lines)


def cmd_distill(args):
    hp = args.hp
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.days)
    kept = []
    for e in read_ledger(hp):
        t = event_time(e)
        if t and t >= cutoff:
            kept.append(e)
    priors = (
        (hp / "PRIORS.md").read_text(encoding="utf-8")
        if (hp / "PRIORS.md").exists()
        else "(none)"
    )
    # The raw ledger is deliberately not sent: everything the page needs is in the fact sheet,
    # and handing over 300 JSONL lines only offers something to compute from — badly — and to
    # quote from.
    # The generation time is on the sheet because the clerk may not invent numbers — without
    # it, a disciplined clerk correctly writes "Generated: unavailable" (observed 2026-08-02).
    payload = (
        f"# computed facts (generated {now_iso()}, window: {args.days} days, "
        f"{len(kept)} events)\n\n"
        + prior_facts(kept, now)
        + "\n\n# current PRIORS.md\n\n"
        + priors
        + "\n"
    )
    if not kept:
        # Same deterministic prefilter as the scribe (§3.5.3): there is nothing to distill from
        # an empty window, so do not spend a model call proving it.
        print(f"no ledger events in the last {args.days} days — nothing to distill")
        return
    out, err, rc, ms, tokens = run_clerk(
        hp, CLERKS / "distiller.md", payload, DISTILL_TIMEOUT
    )
    meter = {"ev": "clerk", "name": "distiller", "ms": ms, "tokens": tokens}
    if rc != 0 or not out.strip():
        reason = f"rc={rc}" if rc != 0 else "the clerk returned nothing"
        p = dump_failure(
            hp, "distill", f"rc={rc}\n--- stderr ---\n{err}\n--- stdout ---\n{out}"
        )
        append_event(hp, {**meter, "ok": False})
        die(f"distill failed ({reason}) — dump: {p}")
    tmp = hp / "PRIORS.md.tmp"
    tmp.write_text(out.strip() + "\n", encoding="utf-8")
    os.replace(tmp, hp / "PRIORS.md")
    append_event(hp, {**meter, "ok": True})
    print(f"PRIORS.md regenerated ({len(kept)} events / {args.days} days, {ms}ms)")


# --- scribe (DESIGN §3.5) -----------------------------------------------------


def worklog_append(hp, text):
    p = hp / "worklog.md"
    now = datetime.now()
    hdr = f"## {now.strftime('%Y-%m-%d')}"
    entry = f"- {now.strftime('%H:%M')} {text}"
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    if hdr in lines:
        i = lines.index(hdr) + 1
        while i < len(lines) and not lines[i].startswith("## "):
            i += 1
        while i > 0 and not lines[i - 1].strip():
            i -= 1
        lines.insert(i, entry)
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [hdr, "", entry]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_cursors(hp):
    """A failed read beats losing every cursor — dump the original and start empty."""
    p = hp / "cursors.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as ex:
        dump_failure(hp, "cursors", f"{type(ex).__name__}: {ex}\n")
        return {}
    if not isinstance(data, dict):
        dump_failure(hp, "cursors", f"cursors.json is not an object: {data!r}\n")
        return {}
    return data


def save_cursors(hp, cursors):
    # tmp + os.replace: dying mid-write never leaves a half-written cursors.json.
    # (tmp lives inside .hippo/ because os.replace requires the same filesystem.)
    p = hp / "cursors.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(cursors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, p)


DISPATCH_USAGE = (
    "usage: hippo dispatch --kind <kind> --scope <scope> [--task <task-id>] [--depth N] "
    "[--] <codex exec args...>\n"
    "       everything after -- goes to codex exec verbatim, even if it looks like a wrapper flag\n"
    "       --depth 0 (default): the lane is told not to re-delegate; --depth 1: it may spawn\n"
    "       children, which start at depth 0 (§9.5 — the clause is indexed, never enforced)"
)


def split_dispatch_argv(argv):
    """Strip dispatch's own flags and hand everything else to codex exec untouched.

    Why not argparse: the remaining arguments are codex's grammar (-m, -c k=v, -C dir …) and
    this parser has no business interpreting them. After `--`, even wrapper-shaped flags pass."""
    fields = {"kind": "", "scope": "", "task": "", "depth": ""}
    rest = []
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if a == "--":
            rest.extend(argv[i + 1 :])
            break
        for key in fields:
            name = f"--{key}"
            if a == name:
                if i + 1 >= n:
                    die(f"dispatch: {name} has no value\n{DISPATCH_USAGE}", 2)
                fields[key], i = argv[i + 1], i + 2
                break
            if a.startswith(name + "="):
                fields[key], i = a[len(name) + 1 :], i + 1
                break
        else:
            rest.append(a)
            i += 1
            continue
    if not fields["kind"] or not fields["scope"]:
        die(DISPATCH_USAGE, 2)
    if fields["depth"]:
        try:
            fields["depth"] = int(fields["depth"])
        except ValueError:
            die(f"dispatch: --depth must be an integer: {fields['depth']!r}\n{DISPATCH_USAGE}", 2)
    else:
        fields["depth"] = 0
    return fields["kind"], fields["scope"], fields["task"], fields["depth"], rest


def exec_label(rest):
    """Read (do not consume) model/effort from the arguments bound for codex — the point that
    already knows them from its own argv is exactly the point to collect them (principle 6)."""
    model = effort = ""
    for i, a in enumerate(rest):
        nxt = rest[i + 1] if i + 1 < len(rest) else ""
        if a in ("-m", "--model"):
            model = nxt
        elif a == "-c" and nxt.startswith("model_reasoning_effort="):
            effort = nxt[len("model_reasoning_effort=") :].strip('"')
    return f"codex/{model or 'unset'}/{effort or 'unset'}"


def run_dispatch(argv):
    """DESIGN §3.6. A failed record never blocks the launch — this surface's real job is running
    codex and the ledger is a side effect. But a lost record always makes a sound."""
    kind, scope, task, depth, rest = split_dispatch_argv(argv)
    did = "d" + os.urandom(16).hex()
    # A launch from inside a lane is a child: record who spawned it (§9.5 — an unintended
    # depth-2 becomes an event in the ledger, not a prohibition nobody can check).
    parent = os.environ.get("HIPPO_DISPATCH", "")
    hp = find_hippo()
    if hp is None:
        print("dispatch: no .hippo/ — skipping the dispatch record", file=sys.stderr)
    else:
        e = {"ev": "dispatch", "id": did, "kind": kind, "exec": exec_label(rest), "scope": scope,
             "depth": depth}
        if task:
            e["task"] = task
        if parent:
            e["parent"] = parent
        bad = validate_event(e)
        if bad:
            print(f"dispatch: record failed ({bad}) — the delegation proceeds anyway", file=sys.stderr)
        else:
            # The ledger line goes to stderr: stdout's first line belongs to the dispatch id (§3.6).
            print(json.dumps(append_event(hp, e, src="wrapper"), ensure_ascii=False), file=sys.stderr)
    print(f"dispatch:{did}", flush=True)
    # The lane inherits its own dispatch id (§9.2): every hippo write it makes arrives as
    # src=executor, and `log outcome` needs no --ref. Set even when the record failed — the
    # id was printed and is the lane's name either way. HIPPO_DEPTH indexes the
    # re-delegation clause the lane's capsule will carry (§9.5).
    os.environ["HIPPO_DISPATCH"] = did
    os.environ["HIPPO_DEPTH"] = str(depth)
    # Not execvp anymore (§9.6): the wrapper stays alive as a pass-through so it can observe
    # what the lane cost. Every line codex prints is forwarded unmodified — the wrapper still
    # interprets nothing bound for codex; it only *reads* the banner (session id, model) and
    # the "tokens used" footer as they stream by. stdin closed: left open, codex exec blocks.
    try:
        child = subprocess.Popen(
            ["codex", "exec", *rest],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, errors="replace",
        )
    except OSError as err:
        die(f"dispatch: could not run codex: {err}", 127)
    session_id = model = ""
    footer_total = None
    prev = ""
    assert child.stdout is not None
    for line in child.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        s = line.strip()
        if not session_id and s.startswith("session id:"):
            session_id = s.split(":", 1)[1].strip()
        elif not model and s.startswith("model:"):
            model = s.split(":", 1)[1].strip()
        elif prev == "tokens used":
            try:
                footer_total = int(s.replace(",", ""))
            except ValueError:
                pass
        prev = s
    rc = child.wait()
    if hp is not None and bad is None:
        usage = collect_usage(session_id, model, footer_total)
        if usage is not None:
            append_event(hp, {"ev": "usage", "ref": did, **usage}, src="wrapper")
    sys.exit(rc)


def _find_key(obj, key):
    """First value for `key` anywhere in a nested JSON object, or None."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def collect_usage(session_id, model, footer_total):
    """What the lane cost, from the best witness available (§9.6).

    Preferred: the rollout's final cumulative token count — it carries the billing breakdown
    (input / cached / output+reasoning). Fallback: the "tokens used" footer, total only.
    Neither there → None; a gap is a gap, and no number is ever invented."""
    u = None
    if session_id:
        hits = sorted((Path.home() / ".codex" / "sessions").glob(
            f"*/*/*/rollout-*-{session_id}.jsonl"))
        if hits:
            try:
                for line in hits[-1].read_text(encoding="utf-8",
                                               errors="replace").splitlines():
                    if "total_token_usage" not in line:
                        continue
                    try:
                        found = _find_key(json.loads(line), "total_token_usage")
                    except json.JSONDecodeError:
                        continue
                    if isinstance(found, dict):
                        u = found  # cumulative — the last one is the lane's total
            except OSError:
                u = None
    if u is not None:
        try:
            out = {
                "tokens": int(u["total_tokens"]),
                "tin": int(u["input_tokens"]),
                "tcached": int(u.get("cached_input_tokens") or 0),
                "tout": (int(u.get("output_tokens") or 0)
                         + int(u.get("reasoning_output_tokens") or 0)),
            }
        except (KeyError, TypeError, ValueError):
            out = None
        if out is not None:
            if model:
                out["model"] = model
            return out
    if footer_total is not None:
        out = {"tokens": footer_total}
        if model:
            out["model"] = model
        return out
    return None


PRICES_PATH = ROOT / "prices.yaml"


def load_prices(path=None):
    """The shipped price sheet (USD per 1M tokens) — refreshed at every release, dated so
    PRIORS can show how stale it is instead of being silently wrong."""
    p = Path(path) if path else PRICES_PATH
    if not p.exists():
        return {"as_of": None, "models": {}}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {"as_of": data.get("as_of"), "models": data.get("models") or {}}


def price_usd(u, prices):
    """Dollars for one usage event, or None when it cannot be priced honestly — no billing
    breakdown, or a model absent from the sheet. (tin − tcached)·input + tcached·cached +
    tout·output, all per 1M."""
    m = prices["models"].get(u.get("model"))
    if not m or u.get("tin") is None or u.get("tout") is None:
        return None
    tcached = u.get("tcached") or 0
    return ((u["tin"] - tcached) * m["input"] + tcached * m.get("cached", m["input"])
            + u["tout"] * m["output"]) / 1e6


def expire_turn_directives(hp):
    """A turn directive is live until the first Stop that *begins* after it was recorded.

    Running this before the scribe writes is the whole mechanism: the turn directives it is
    about to record are not in this sweep, so they survive to be injected into the next turn
    and are expired by the next Stop. One turn of life, for both writers — the scribe records
    at Stop (live through the following turn), main records mid-turn (live for the rest of it).
    `expired` rather than `withdrawn`: nobody changed their mind, the clock simply ran out."""
    n = 0
    for d in directives(hp).values():
        if d.get("state") == "active" and d.get("lifetime") == "turn":
            append_event(
                hp, {"ev": "directive", "id": d["id"], "state": "expired"}, src="scribe"
            )
            n += 1
    return n


def directive_roster(hp):
    """The live directive ids handed to the scribe with the digest.

    Without it the clerk coins a fresh id for an instruction that already has one, and the two
    sit in the ledger as unrelated directives — the update never lands. Reading the ledger from
    inside the clerk would cost a tool call and a lot of latency for what is a short list."""
    live = [d for d in directives(hp).values() if d.get("state") == "active"]
    if not live:
        return "(none yet)"
    return "\n".join(
        f"- {d['id']} ({d.get('lifetime', '?')}): {one_line(d.get('text', ''), 100)}"
        for d in live
    )


DISPATCH_ROSTER_N = 12


def dispatch_roster(hp):
    """The recent dispatches handed to the scribe with the digest.

    The clerk is told not to record a launch the wrapper already recorded, and until now its only
    way to tell was spotting the wrapper's trace in the digest — which is not something a digest
    guarantees. Measured on a real ledger (361 events): of 66 scribe-written dispatches, 30
    restated a wrapper launch under a *new* id and 6 reused the wrapper's id exactly. The rule was
    not so much broken as unanswerable, and it inflated the PRIORS denominator by ~1.4x.

    Same remedy as directive_roster: hand over the short list instead of asking it to go looking.
    It doubles as the set of ids an outcome may legally ref, which the digest also could not
    guarantee."""
    rows = read_ledger(hp)
    judged = judged_refs(rows)
    claims = executor_claims(rows)
    disp = [e for e in rows if e.get("ev") == "dispatch"][-DISPATCH_ROSTER_N:]
    if not disp:
        return "(none yet)"

    def marker(did):
        # A claimed dispatch is still awaiting the verdict — and naming the claim keeps the
        # clerk from reading the lane's own report in the digest as an acceptance signal.
        if did in judged:
            return ""
        if did in claims:
            return f"  [claims {claims[did]} — verdict pending]"
        return "  [no outcome yet]"

    return "\n".join(
        f"- {e.get('id')} ({e.get('kind', '?')}): {one_line(e.get('scope', ''), 80)}"
        + marker(e.get("id"))
        for e in disp
    )


def cmd_scribe(args):
    hp = args.hp
    lock = (hp / "scribe.lock").open("w")
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            # The loser on a session's final turn has no "next run", so it loses that tail
            # forever. Hence: wait briefly before giving up.
            if time.monotonic() >= deadline:
                return
            time.sleep(0.1)

    # Before anything else, and before the prefilter can return: a turn ended whether or not
    # this window had enough in it to be worth a model call.
    expire_turn_directives(hp)

    transcript = Path(args.transcript)
    if not transcript.exists():
        die(f"no such transcript: {transcript}")
    cursors = load_cursors(hp)
    since = int(cursors.get(args.session, 0))
    end = sum(1 for _ in transcript.open("r", encoding="utf-8", errors="replace"))

    digest_py = SCRIPTS / "digest_lite.py"
    if not digest_py.exists():
        die(f"no digest script: {digest_py}")
    r = subprocess.run(
        # sys.executable, not "python3": under `uv run --script` there is no
        # guarantee a python3 sits on PATH. digest_lite is stdlib-only.
        # --until-line: the cursor will advance to `end`, so the digest must stop at exactly
        # `end` too. Otherwise a line appended between the line count and the digest gets
        # summarized now and again on the next run (a duplicated window boundary).
        [
            sys.executable,
            str(digest_py),
            str(transcript),
            "--since-line",
            str(since),
            "--until-line",
            str(end),
        ],
        capture_output=True,
        text=True,
        timeout=SCRIBE_TIMEOUT,
    )
    if r.returncode != 0:
        die(f"digest failed (rc={r.returncode}): {r.stderr.strip()}")
    digest = r.stdout

    def save_cursor():
        cursors[args.session] = end
        save_cursors(hp, cursors)

    # 3. Deterministic prefilter: with no substantive activity, skip the model call entirely
    # (digest line shapes: "[123] TOOL Bash: …" / "[124] USER: …")
    if not any(SUBSTANTIVE.match(ln) for ln in digest.splitlines()):
        save_cursor()
        return

    payload = (
        f"# live directives\n\n{directive_roster(hp)}\n\n"
        f"# dispatches already recorded\n\n{dispatch_roster(hp)}\n\n"
        f"# transcript digest\n\n{digest}"
    )
    out, err, rc, ms, tokens = run_clerk(
        hp, CLERKS / "turn-scribe.md", payload, SCRIBE_TIMEOUT
    )
    meter = {"ev": "clerk", "name": "turn-scribe", "ms": ms, "tokens": tokens}

    def fail(reason):
        p = dump_failure(
            hp,
            "scribe",
            f"{reason}\nrc={rc}\n--- stderr ---\n{err}\n--- stdout ---\n{out}",
        )
        # DESIGN §3.5.6 — the cursor advances even on failure. The record for this window is the
        # dump under failures/ (the dump IS the record); holding the cursor back would re-bill the
        # same input to the model on every turn, forever.
        save_cursor()
        append_event(hp, {**meter, "ok": False}, src="scribe")
        die(f"scribe failed: {reason} — dump: {p}")

    if rc != 0:
        fail(f"clerk rc={rc}")
    obj = extract_json(out)
    if obj is None:
        fail("no JSON object found")
    if not isinstance(obj.get("worklog", ""), str) or not isinstance(
        obj.get("events", []), list
    ):
        fail("worklog must be a string and events must be an array")
    # Per-event isolation (§3.5.6): one bad event must not erase the rest of the turn. The
    # clerk's other events and its worklog line are still worth keeping, and the rejected event
    # is preserved in failures/ — which is what "the dump is the record" means.
    events = obj.get("events", [])
    for e in events:
        verr = (validate_event(e) or validate_scribe_event(e) or check_ref(hp, e)
                or check_scribe_outcome(hp, e))
        if verr:
            dump_failure(hp, "scribe", f"{verr}\n\n{json.dumps(e, ensure_ascii=False, indent=2)}\n")
            continue
        append_event(hp, e, src="scribe")
    if obj.get("worklog", "").strip():
        worklog_append(hp, obj["worklog"].strip())
    save_cursor()
    append_event(hp, {**meter, "ok": True}, src="scribe")


# --- argparse -----------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="hippo",
        description=(
            "hippo — a background organ for perception and memory. "
            "Facts go in through one door, `log <event>`; bare `hippo log` reads recent records; "
            "`directive` and `prior` are derived views recomputed from the ledger every time."
        ),
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("init", help="create .hippo/ (in cwd)").set_defaults(fn=cmd_init)

    s = sub.add_parser("status", help="one-block summary")
    s.add_argument(
        "--inject", action="store_true", help="SessionStart hook injection format (§6)"
    )
    s.set_defaults(fn=cmd_status)

    t = sub.add_parser("task", help="work registry").add_subparsers(dest="sub")
    a = t.add_parser("add", help="add a task")
    a.add_argument("id")
    a.add_argument("--title", required=True)
    a.add_argument("--status", default="pending", help="|".join(TASK_STATUSES))
    a.add_argument("--notes")
    a.add_argument("--deps", help="comma separated")
    a.set_defaults(fn=cmd_task_add, writes=True)
    a = t.add_parser("set", help="change a field (title|status|notes|deps)")
    a.add_argument("id")
    a.add_argument("field")
    a.add_argument("value")
    a.set_defaults(fn=cmd_task_set, writes=True)
    a = t.add_parser("done", help="mark as done")
    a.add_argument("id")
    a.add_argument("--note", help="append to notes")
    a.set_defaults(fn=cmd_task_done, writes=True)
    a = t.add_parser("list", help="list (default: pending+active)")
    a.add_argument("--status", help="comma separated multi-filter")
    a.add_argument("--all", action="store_true")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_task_list)
    a = t.add_parser("show", help="show one")
    a.add_argument("id")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_task_show)
    a = t.add_parser("drop", help="status=dropped")
    a.add_argument("id")
    a.set_defaults(fn=cmd_task_drop, writes=True)

    lg = sub.add_parser("log", help="record a ledger event (fail-closed validation)")
    lsub = lg.add_subparsers(dest="ev")
    a = lsub.add_parser("dispatch", help="launch a delegation")
    a.add_argument("--id", required=True)
    a.add_argument("--kind", required=True)
    a.add_argument("--exec", required=True, help="executor/model/effort (executor = the agent that did the work)")
    a.add_argument("--scope", required=True)
    a.add_argument("--task")
    a.add_argument("--depth", type=int, help="0 = leaf lane (told not to re-delegate), 1 = may spawn (§9.5)")
    a.add_argument("--parent", help="dispatch id of the lane that launched this one")
    a.set_defaults(fn=cmd_log, writes=True)
    a = lsub.add_parser("outcome", help="verdict on a delegation")
    a.add_argument(
        "--ref",
        help="dispatch id, or task:<task-id> for its dispatch still awaiting an outcome; "
             "inside a dispatched lane, defaults to $HIPPO_DISPATCH (your own dispatch)",
    )
    a.add_argument(
        "--result", required=True, choices=sorted(ENUMS[("outcome", "result")])
    )
    a.add_argument(
        "--attr",
        choices=sorted(ENUMS[("outcome", "attr")]),
        help="whose problem a non-accepted result was: work=the output itself, "
             "brief=the instructions, harness=infrastructure loss",
    )
    a.add_argument("--rework", type=int, help="repair round-trips before this verdict")
    a.add_argument("--by", help="who judged it, as executor/model (e.g. verify/opus)")
    a.add_argument("--note")
    a.set_defaults(fn=cmd_log, writes=True)
    a = lsub.add_parser("review", help="an external review reply")
    a.add_argument("--id", required=True)
    a.add_argument("--base", required=True, help="sha of the reviewed commit")
    a.add_argument("--source", required=True)
    a.add_argument("--findings", required=True, type=int)
    a.set_defaults(fn=cmd_log, writes=True)
    a = lsub.add_parser("review-status", help="how far the findings were addressed")
    a.add_argument("--ref", required=True, help="the review id this closes out")
    a.add_argument(
        "--addressed", required=True, choices=sorted(ENUMS[("review-status", "addressed")])
    )
    a.add_argument("--at", help="sha of the commit that addressed it")
    a.set_defaults(fn=cmd_log, writes=True)
    a = lsub.add_parser("raw", help="validate one JSON line, then append")
    a.add_argument("json")
    a.set_defaults(fn=cmd_log_raw, writes=True)
    a = lsub.add_parser("tail", help="last N lines (the default for bare `hippo log`)")
    a.add_argument("-n", type=int, default=20)
    a.add_argument("--ev", dest="ev_filter", help="filter by ev type")
    a.set_defaults(fn=cmd_log_tail)

    d = sub.add_parser("directive", help="operating directives (a view derived from the ledger)").add_subparsers(
        dest="sub"
    )
    a = d.add_parser("add", help="record a directive (ev=directive)")
    a.add_argument("--id", help="derived from --text when omitted (DESIGN §3.3 auto id)")
    a.add_argument("--text")
    a.add_argument(
        "--lifetime",
        choices=sorted(ENUMS[("directive", "lifetime")]),
        help="how long the directive lives",
    )
    a.add_argument(
        "--audience",
        choices=sorted(ENUMS[("directive", "audience")]),
        help="who it binds (default all): main = this session's judgment, "
             "executor = dispatched lanes, all = both",
    )
    a.add_argument(
        "--state", default="active", choices=sorted(ENUMS[("directive", "state")])
    )
    a.set_defaults(fn=cmd_log, ev="directive", writes=True)
    a = d.add_parser("list", help="list (derived from the ledger)")
    a.add_argument("--active", action="store_true")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_directive_list)
    a = d.add_parser("withdraw", help="withdraw a directive the user is done with")
    a.add_argument("id")
    a.set_defaults(fn=cmd_directive_withdraw, writes=True)

    pr = sub.add_parser("prior", help="the distilled surface").add_subparsers(dest="sub")
    pr.add_parser("show", help="print PRIORS.md").set_defaults(fn=cmd_prior_show)
    a = pr.add_parser("distill", help="run the distiller clerk → regenerate PRIORS.md")
    a.add_argument("--days", type=int, default=14)
    a.set_defaults(fn=cmd_distill, writes=True)

    # main() intercepts dispatch before argparse (the remaining arguments are codex's grammar
    # and this parser must not read them). Registering it here is only for listing and --help.
    sub.add_parser(
        "dispatch",
        help="launch codex exec + auto-record ev:dispatch",
        usage=DISPATCH_USAGE.splitlines()[0].removeprefix("usage: "),
        description=DISPATCH_USAGE,
    )

    a = sub.add_parser("scribe", help="internal surface the Stop hook calls detached")
    a.add_argument("--transcript", required=True)
    a.add_argument("--session", required=True)
    a.set_defaults(fn=cmd_scribe)

    tag_parsers(p)
    return p


def tag_parsers(p):
    """Make every subparser carry itself as a default (_parser) so die() can attach the usage of
    the command actually being used (m7)."""
    for act in p._actions:
        if isinstance(act, argparse._SubParsersAction):
            for sp in act.choices.values():
                sp.set_defaults(_parser=sp)
                tag_parsers(sp)


BARE_DEFAULT = {"task": "list", "log": "tail", "directive": "list", "prior": "show"}


def insert_default_sub(argv):
    """Bare-noun default: calling a noun on its own inserts its default read subcommand
    (task→list, log→tail, directive→list, prior→show).

    Inserted when argv[0] is a noun and the next token is absent, or starts with '-' but is not
    -h/--help. `hippo task -h` must print the noun's own help, and arguments that do not start
    with '-' (as in `hippo log raw '{…}'`) are left alone."""
    if argv and argv[0] in BARE_DEFAULT:
        nxt = argv[1] if len(argv) > 1 else None
        if nxt is None or (nxt.startswith("-") and nxt not in ("-h", "--help")):
            return [argv[0], BARE_DEFAULT[argv[0]], *argv[1:]]
    return argv


def parse_args_quietly(parser, argv):
    """Outside a .hippo project not even argparse's usage+rc2 may leak (§3.1, completely silent).
    -h/--help (a normal rc 0 exit) still prints anywhere (§3.3)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            args = parser.parse_args(argv)
    except SystemExit as ex:
        code = ex.code if isinstance(ex.code, int) else 1
        if code != 0 and find_hippo() is None:
            sys.exit(0)
        sys.stdout.write(out.getvalue())
        sys.stderr.write(err.getvalue())
        raise
    sys.stdout.write(out.getvalue())
    sys.stderr.write(err.getvalue())
    return args


def main():
    global ACTIVE_PARSER
    argv = sys.argv[1:]
    # dispatch does not exit silently outside a .hippo project: this surface's real job is
    # launching codex, and swallowing the launch because the record failed would make it a trap
    # rather than a wrapper.
    if argv and argv[0] == "dispatch":
        head = argv[1 : argv.index("--")] if "--" in argv else argv[1:]
        if not ({"-h", "--help"} & set(head)):
            run_dispatch(argv[1:])
            return
    parser = build_parser()
    args = parse_args_quietly(parser, insert_default_sub(sys.argv[1:]))
    ACTIVE_PARSER = getattr(args, "_parser", parser)
    if not hasattr(args, "fn"):
        if find_hippo() is None:
            sys.exit(0)  # outside a .hippo project: a completely silent no-op
        parser.print_help(sys.stderr)
        sys.exit(2)
    if args.fn is not cmd_init:
        hp = find_hippo()
        if hp is None:
            # Reads stay completely silent outside a project (§3.1). A *write* does not: the
            # caller typed it expecting a record, and silence reads as success. (Worktrees are
            # no longer the usual cause — find_hippo walks through their .git file, §9.1.)
            if getattr(args, "writes", False):
                print(
                    "hippo: no .hippo/ found from here — nothing was recorded. "
                    "Run `hippo init` at the project root, or check your cwd.",
                    file=sys.stderr,
                )
            sys.exit(0)
        args.hp = hp
    args.fn(args)


if __name__ == "__main__":
    main()
