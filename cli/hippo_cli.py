# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""hippo CLI — ledger/task/directive recording and the clerk pipeline (DESIGN.md §3.2, §3.3, §3.5)."""

import argparse
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
}
# Per-ev key whitelist — anything else is rejected. A field that is not in the schema
# (§3.2) would silently poison the derived aggregates (the distiller).
ALLOWED = {
    "dispatch": ("id", "kind", "exec", "scope", "task"),
    "outcome": ("ref", "result", "attr", "rework", "by", "note"),
    "review": ("id", "base", "source", "findings"),
    "review-status": ("ref", "addressed", "at"),
    "directive": ("id", "text", "lifetime", "state"),
    "clerk": ("name", "ok", "ms", "tokens"),
}
# Fields only the writer stamps. Rejected if a caller (clerk output, log raw, environment)
# supplies them: the scribe reads an untrusted transcript, so it must not be able to forge
# the timestamp or the source.
WRITER_ONLY = ("t", "src")
SRC_VALUES = ("scribe", "cli", "wrapper")  # DESIGN §3.2
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
ENUMS = {
    ("outcome", "result"): {"accepted", "revised", "refuted", "no-go", "lost"},
    ("outcome", "attr"): {"work", "brief", "harness"},
    ("directive", "lifetime"): {"turn", "phase", "durable"},
    ("directive", "state"): {"active", "withdrawn", "expired"},
}
# A directive id is a handle the scribe and the user both have to type from memory, so it is
# kebab ASCII or nothing. An id derived from non-ASCII text collapses to the empty string, and
# an id that is empty (or spelled differently every time) cannot supersede anything.
DIRECTIVE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
INT_FIELDS = ("findings", "rework", "ms", "tokens")


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
    """Walk up from cwd looking for .hippo/ (the git root and $HOME are the ceiling)."""
    d = Path.cwd().resolve()
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        home = None
    for p in [d, *d.parents]:
        if (p / ".hippo").is_dir():
            return p / ".hippo"
        if (p / ".git").exists():
            break
        if p == home:  # never go above $HOME — we could adopt someone else's project
            break
    return None


def resolve_src(src=None):
    """src ∈ scribe|cli|wrapper (DESIGN §3.2). Only scripts/dispatch.sh declares itself a
    wrapper through HIPPO_SRC — and even that value dies loudly if it is off the whitelist."""
    v = src or os.environ.get("HIPPO_SRC") or "cli"
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


def directives(hp):
    """id → current state (the last event for an id is the truth — no derived file)."""
    cur = {}
    for e in read_ledger(hp):
        if e.get("ev") != "directive" or not e.get("id"):
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
    # instead of every wave reinventing an absolute scratchpad path.
    (hp / "briefs").mkdir()
    (hp / "ledger.jsonl").touch()
    tasks_save(hp, {"tasks": []})
    print(f"created: {hp}")
    print(
        "next: `hippo directive add --text \"…\" --lifetime durable` for a standing instruction, "
        "`hippo task add <type>/<slug> --title …` for work, `hippo status` to see both.",
    )


# Nudge thresholds for the directive block (DESIGN §6). None of these is enforced: every active
# directive is injected in full, whatever the totals say. They exist so the surfaces that touch
# directives can name the cost while the author can still do something about it — silently folding
# a ruling away is the failure mode principle 9 is about, and a cap is just a quieter way to lose it.
DIRECTIVE_TEXT_NUDGE = 200  # one directive this long is asking to be compressed
DIRECTIVE_COUNT_NUDGE = 8  # this many live at once is asking for a hygiene pass
DIRECTIVE_TOTAL_NUDGE = 1600  # total characters resident in every session from here on


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
            f"note: {len(live)} live directives, {total} chars — all of it is injected into every "
            "session from here on. Compress them, or withdraw the stale ones "
            "(`hippo directive withdraw <id>`)."
        )
    return notes


def status_lines(hp):
    """DESIGN §6 resident surface (header + live directives + last)."""
    data = tasks_load(hp)
    n_open = sum(1 for t in data["tasks"] if t.get("status") in OPEN_STATUSES)
    live = [d for d in directives(hp).values() if d.get("state") == "active"]

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
    for d in ordered:
        lines.append(f"· live({d.get('lifetime') or '?'}): {one_line(d.get('text', ''))}")
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
    for t in tasks:
        print(f"{t.get('id')}  [{t.get('status', '?')}]  {t.get('title', '')}")


def cmd_task_show(args):
    t = find_task(tasks_load(args.hp), args.id)
    if not t:
        die(f"no such task id: {args.id}")
    if args.json:
        print(json.dumps(t, ensure_ascii=False, indent=2))
        return
    print(yaml.safe_dump(t, allow_unicode=True, sort_keys=False).rstrip())


# ev -> the ev its `ref` must point at.
REF_TARGET = {"outcome": "dispatch", "review-status": "review"}
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
    judged = {e.get("ref") for e in rows if e.get("ev") == "outcome"}
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


def log_and_print(hp, e):
    err = validate_event(e) or check_ref(hp, e)
    if err:
        die(f"validation failed: {err}")
    rec = append_event(hp, e)
    print(json.dumps(rec, ensure_ascii=False))


def cmd_log(args):
    e = {"ev": args.ev}
    if args.ev == "dispatch":
        e.update(id=args.id, kind=args.kind, exec=args.exec, scope=args.scope)
        if args.task:
            e["task"] = args.task
    elif args.ev == "outcome":
        e.update(ref=resolve_ref(args.hp, args.ref), result=args.result)
        for k in ("attr", "rework", "by", "note"):
            if getattr(args, k) is not None:
                e[k] = getattr(args, k)
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
        # Length is not warned about here: the set-wide notes emitted after the write name every
        # oversized directive by id and size, including this one. Saying it twice trains the
        # reader to skim the notes.
    log_and_print(args.hp, e)
    if args.ev == "directive":
        # After the write, so the notes describe the set the next session will actually carry.
        for note in directive_volume_notes(args.hp):
            print(note, file=sys.stderr)


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
        print(
            f"{d['id']}  [{d.get('state', '?')}/{d.get('lifetime', '?')}]  {d.get('text', '')}"
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


def cmd_distill(args):
    hp = args.hp
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    kept = []
    for e in read_ledger(hp):
        try:
            t = datetime.strptime(e.get("t", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if t >= cutoff:
            kept.append(json.dumps(e, ensure_ascii=False))
    priors = (
        (hp / "PRIORS.md").read_text(encoding="utf-8")
        if (hp / "PRIORS.md").exists()
        else "(none)"
    )
    payload = (
        f"# ledger events (last {args.days} days, {len(kept)})\n\n"
        + "\n".join(kept)
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
    "usage: hippo dispatch --kind <kind> --scope <scope> [--task <task-id>] "
    "[--] <codex exec args...>\n"
    "       everything after -- goes to codex exec verbatim, even if it looks like a wrapper flag"
)


def split_dispatch_argv(argv):
    """Strip dispatch's own flags and hand everything else to codex exec untouched.

    Why not argparse: the remaining arguments are codex's grammar (-m, -c k=v, -C dir …) and
    this parser has no business interpreting them. After `--`, even wrapper-shaped flags pass."""
    kind = scope = task = ""
    rest = []
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if a == "--":
            rest.extend(argv[i + 1 :])
            break
        for name, key in (("--kind", "kind"), ("--scope", "scope"), ("--task", "task")):
            if a == name:
                if i + 1 >= n:
                    die(f"dispatch: {name} has no value\n{DISPATCH_USAGE}", 2)
                val, i = argv[i + 1], i + 2
                break
            if a.startswith(name + "="):
                val, i = a[len(name) + 1 :], i + 1
                break
        else:
            rest.append(a)
            i += 1
            continue
        if key == "kind":
            kind = val
        elif key == "scope":
            scope = val
        else:
            task = val
    if not kind or not scope:
        die(DISPATCH_USAGE, 2)
    return kind, scope, task, rest


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
    kind, scope, task, rest = split_dispatch_argv(argv)
    did = "d" + os.urandom(16).hex()
    hp = find_hippo()
    if hp is None:
        print("dispatch: no .hippo/ — skipping the dispatch record", file=sys.stderr)
    else:
        e = {"ev": "dispatch", "id": did, "kind": kind, "exec": exec_label(rest), "scope": scope}
        if task:
            e["task"] = task
        bad = validate_event(e)
        if bad:
            print(f"dispatch: record failed ({bad}) — the delegation proceeds anyway", file=sys.stderr)
        else:
            # The ledger line goes to stderr: stdout's first line belongs to the dispatch id (§3.6).
            print(json.dumps(append_event(hp, e, src="wrapper"), ensure_ascii=False), file=sys.stderr)
    print(f"dispatch:{did}", flush=True)
    # Close stdin before handing over — left open, codex exec blocks waiting for input.
    with open(os.devnull) as devnull:
        os.dup2(devnull.fileno(), 0)
    try:
        os.execvp("codex", ["codex", "exec", *rest])
    except OSError as err:
        die(f"dispatch: could not run codex: {err}", 127)


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
    judged = {e.get("ref") for e in rows if e.get("ev") == "outcome"}
    disp = [e for e in rows if e.get("ev") == "dispatch"][-DISPATCH_ROSTER_N:]
    if not disp:
        return "(none yet)"
    return "\n".join(
        f"- {e.get('id')} ({e.get('kind', '?')}): {one_line(e.get('scope', ''), 80)}"
        + ("" if e.get("id") in judged else "  [no outcome yet]")
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
        verr = validate_event(e) or check_ref(hp, e)
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
    a.set_defaults(fn=cmd_log, writes=True)
    a = lsub.add_parser("outcome", help="verdict on a delegation")
    a.add_argument(
        "--ref",
        required=True,
        help="dispatch id, or task:<task-id> for its dispatch still awaiting an outcome",
    )
    a.add_argument(
        "--result", required=True, choices=sorted(ENUMS[("outcome", "result")])
    )
    a.add_argument("--attr", choices=sorted(ENUMS[("outcome", "attr")]))
    a.add_argument("--rework", type=int)
    a.add_argument("--by")
    a.add_argument("--note")
    a.set_defaults(fn=cmd_log, writes=True)
    a = lsub.add_parser("review", help="an external review reply")
    a.add_argument("--id", required=True)
    a.add_argument("--base", required=True, help="sha of the reviewed commit")
    a.add_argument("--source", required=True)
    a.add_argument("--findings", required=True, type=int)
    a.set_defaults(fn=cmd_log, writes=True)
    a = lsub.add_parser("review-status", help="how far the findings were addressed")
    a.add_argument("--ref", required=True)
    a.add_argument("--addressed", required=True, help="e.g. full|partial|none")
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
            # caller typed it expecting a record, and silence reads as success. The usual cause
            # is cwd — a worktree holds a .git file, so the walk-up stops before the project.
            if getattr(args, "writes", False):
                print(
                    "hippo: no .hippo/ found from here — nothing was recorded. "
                    "Run `hippo init`, or check your cwd (a worktree is not the project root).",
                    file=sys.stderr,
                )
            sys.exit(0)
        args.hp = hp
    args.fn(args)


if __name__ == "__main__":
    main()
