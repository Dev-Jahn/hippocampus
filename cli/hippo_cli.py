# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""hippo CLI — ledger/task/directive recording and the clerk pipeline (DESIGN.md §3.2, §3.3, §3.5)."""

import argparse
import collections
import concurrent.futures
import contextlib
import fcntl
import hashlib
import io
import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
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
DISTILL_DAYS = 14  # the ledger window the distiller reads
# Auto-distill at Stop (DESIGN §3.5.8): PRIORS was stale in every one of 28 projects measured,
# `prior distill` having run 18 times ever. Due when the page is this old *and* this many new
# verdicts have landed since the last distiller run.
DISTILL_STALE_DAYS = 7
DISTILL_MIN_NEW = 5
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
    # triage: the judge's reading of a finished lane (§3.6) — written by the wrapper at lane
    # exit, single dispatch and batch alike. Evidence of a check rc's standing, never a verdict.
    "triage": ("ref", "route"),
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
    # p: the compact probabilities the route was computed from — done, blocked, ask, creep,
    # evidence, and risk (a 0-3 score). A question the reply did not answer is absent.
    # trimmed: the fields of the state that were shortened to fit the judge (§3.6).
    "triage": ("ref", "route", "verify", "cause", "p", "trimmed"),
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
# The placeholder words themselves showed up as values ("vehicle/gpt-6-sol/high"): the
# shape was right, so only naming them catches it.
EXEC_PLACEHOLDERS = {"executor", "vehicle", "model", "effort"}
# The two closed slots of exec. They are checked only on the scribe's own output (§3.5.6b), never
# on what main writes: a vocabulary holds where the writer *has* the value and fails where it has
# to infer one. Measured on a consuming project — the wrapper, which reads its own argv, produced
# 0 malformed exec in 110 dispatches; the scribe, reading a transcript, produced 12 in 45.
EXECUTORS = {"codex", "claude", "fork", "subagent", "workflow"}
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra", "inherit"}
ENUMS = {
    ("outcome", "result"): {"accepted", "revised", "refuted", "no-go", "lost"},
    ("outcome", "attr"): {"work", "brief", "harness"},
    ("directive", "state"): {"active", "withdrawn", "expired"},
    # audience is *who* a directive binds (§9.4). Absent = all — a narrow default would silently
    # hide a constraint from the worker that needed it. `lifetime` is still an allowed key (old
    # rows carry it) but no longer a value anything writes, so it is not enumerated (§3.2).
    ("directive", "audience"): {"main", "executor", "all"},
    # addressed is the one field reviews are folded on ("not fully addressed" in the fact
    # sheet), so it is closed like result: a free-form "fully" would read as open forever.
    ("review-status", "addressed"): {"full", "partial", "none"},
    ("triage", "route"): {"accept-candidate", "escalate", "no-go-candidate", "failed"},
    ("triage", "cause"): {"capability", "spec", "environment", "transient"},
}
TRIAGE_P_KEYS = ("done", "blocked", "ask", "creep", "evidence", "risk")
TRIAGE_TRIMMED = ("stderr_tail", "brief", "changes", "report")  # what fit_triage_state may cut
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
        if e["state"] == "active" and not e.get("text"):
            return "ev=directive state=active: required field missing: text"
    for (evn, field), allowed in ENUMS.items():
        if ev == evn and field in e and e[field] not in allowed:
            return f"ev={ev}: {field}={e[field]!r} — allowed: {', '.join(sorted(allowed))}"
    for f in INT_FIELDS:
        if f in e and (isinstance(e[f], bool) or not isinstance(e[f], int)):
            return f"ev={ev}: {f} must be an integer"
    if ev == "clerk" and not isinstance(e["ok"], bool):
        return "ev=clerk: ok must be true or false"
    if ev == "triage":
        if "verify" in e and not isinstance(e["verify"], bool):
            return "ev=triage: verify must be true or false"
        p = e.get("p", {})
        if not isinstance(p, dict) or any(
                k not in TRIAGE_P_KEYS or isinstance(v, bool) or not isinstance(v, (int, float))
                for k, v in p.items()):
            return f"ev=triage: p must be a flat map of numbers over {', '.join(TRIAGE_P_KEYS)}"
        cut = e.get("trimmed", [])
        if not isinstance(cut, list) or any(k not in TRIAGE_TRIMMED for k in cut):
            return f"ev=triage: trimmed must be a list of {', '.join(TRIAGE_TRIMMED)}"
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
    """$HIPPO_DIR when it names a directory; otherwise walk up from cwd looking for .hippo/ (the
    git root and $HOME are the ceiling).

    HIPPO_DIR is planted by the dispatch wrapper into a lane's environment so a lane reports to
    the ledger that launched it, wherever its cwd is — measured, 12% of lane outcomes (141 of
    1,167) were refused for a ref the lane's own `.hippo/` had never seen.

    A `.git` *directory* is a real repository root: stop there, never adopt a project from
    beyond it. A `.git` *file* marks a linked worktree, and by convention lanes live inside
    the repo (`.claude/worktrees/<name>`) — walk through it, so an executor calling hippo
    from its worktree resolves the project's real .hippo/ (§9.1)."""
    env = os.environ.get("HIPPO_DIR")
    if env and Path(env).is_dir():
        return Path(env)
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
    recording means.

    `lifetime` is retired (§3.2), but old rows carry it. A row whose latest active write said
    `turn` expired by the old rule at the next Stop, and nothing runs that sweep any more — so
    the view says it is expired instead of a migration rewriting the ledger."""
    cur = {}
    for e in read_ledger(hp):
        if e.get("ev") != "directive" or not e.get("id"):
            continue
        if e.get("src") == "executor":
            continue
        d = cur.setdefault(e["id"], {"id": e["id"]})
        if e.get("state") == "active":
            d.pop("lifetime", None)  # a re-add replaces the old lifetime, it does not inherit it
        d.update({k: v for k, v in e.items() if k not in ("ev", "src")})
    for d in cur.values():
        if d.get("state") == "active" and d.get("lifetime") == "turn":
            d["state"] = "expired"
    return cur


def tasks_load(hp):
    p = hp / "tasks.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None
    data = data or {}
    if not isinstance(data, dict) or not isinstance(data.get("tasks", []), list):
        die(f"malformed tasks.yaml: expected {{tasks: [...]}} ({p})")
    data.setdefault("tasks", [])
    return data


def write_durable(p, text):
    """Replace `p` with `text` so that a crash at any instant leaves the old file or the new
    one, never an empty or half-written one. Measured on b200 (2026-09-23): a node failure
    during the scribe's worklog rewrite left steno's 412KB worklog.md at 0 bytes — it was
    rewritten in place (truncate, then write), and the shared filesystem kept the truncation
    but not the data. A tmp file in the same directory (os.replace needs one filesystem),
    flushed and fsync'd before the rename, closes both halves of that window."""
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    try:  # the rename itself lives in the directory entry; best effort where dirs can't be opened
        fd = os.open(p.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def tasks_save(hp, data):
    write_durable(hp / "tasks.yaml",
                  yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100))


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


# --- the judge (DESIGN §3.9) --------------------------------------------------

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_TIMEOUT = 20  # seconds per request — measured 0.7–1.5s, so this is slack, not a budget
JEV_DIR = CLERKS / "jev"  # question specs live as text (principle 8)
# The model takes 32k tokens of state plus the longest question. 110k characters is ~28k tokens
# at 4 chars/token, which leaves the questions their room. The client owns the limit because
# the API's own answer to an oversize state is a 422 — a gate that fails silently is worse
# than one that says the state was too large.
JEV_STATE_BUDGET_CHARS = 110_000
JEV_RETRY_STATUS = (429, 529)  # the two transient ones; every other status is the answer
JEV_RETRY_WAIT = 2.0
JEV_SPECS = {}  # per-process cache: a spec file is read once


class _SpecVars(dict):
    """Leaves an unknown `{placeholder}` exactly as it was written — a spec is prose a person
    tunes, and most of the braces in it are not placeholders."""

    def __missing__(self, key):
        return "{" + key + "}"


def jev_spec(name):
    """Load `clerks/jev/<name>.yaml`. A missing or malformed spec dies: the specs are
    infrastructure, like a clerk prompt, not a runtime condition to be survived."""
    if name not in JEV_SPECS:
        p = JEV_DIR / f"{name}.yaml"
        if not p.exists():
            die(f"no jev spec: {p}")
        try:
            spec = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            die(f"malformed jev spec {p}: {one_line(e, 200)}")
        if not isinstance(spec, dict) or not isinstance(spec.get("questions"), dict):
            die(f"malformed jev spec {p}: a `questions` map is required")
        JEV_SPECS[name] = spec
    return JEV_SPECS[name]


def jev_questions(name, **variables):
    """The rendered questions map of a spec: `{var}` is substituted in the question id and in
    its instructions, criteria are copied verbatim. A caller fans out over several items by
    calling this once per item (`i=1`, `i=2`, …) and merging the maps — one narrow judgment
    per question is what the model is accurate at (jaggedness)."""
    path = JEV_DIR / f"{name}.yaml"
    out = {}
    for qid, q in jev_spec(name)["questions"].items():
        if not isinstance(q, dict):
            die(f"malformed jev spec {path}: question {qid} is not a map")
        try:
            rid = str(qid).format_map(_SpecVars(variables))
            body = dict(q)
            body["instructions"] = str(q.get("instructions", "")).format_map(
                _SpecVars(variables)
            )
        except (ValueError, IndexError) as e:
            die(f"malformed jev spec {path}: question {qid}: {e}")
        out[rid] = body
    return out


def jev_policy(name):
    """The spec's `policy` map. Thresholds sit in the text next to the questions they belong
    to, but they are read and applied by code: the judge answers, it never decides (§3.9)."""
    return jev_spec(name).get("policy") or {}


def jev_backend(_hp):
    """`live` when TYPESAFE_API_KEY is set and non-empty, `off` otherwise — and there is no
    setting. $HIPPO_JEV_BACKEND (live|mock|off) is a developer and test knob, never something a
    user is asked about: a machine with the key gets the judge, a machine without it gets the
    plugin exactly as it was. The clerk backend has a config.yaml override because a user picks
    between real backends there; here there is nothing to pick (§3.9)."""
    b = os.environ.get("HIPPO_JEV_BACKEND")
    if b:
        return str(b)
    return "live" if os.environ.get("TYPESAFE_API_KEY") else "off"


def jev_mock(questions, body):
    """Test backend → (answers, reason). Answers come from $HIPPO_JEV_MOCK_OUTPUT, and
    $HIPPO_JEV_MOCK_CAPTURE receives the request body that would have gone out — tests assert
    on what was actually asked, the way HIPPO_MOCK_CAPTURE does for the clerk."""
    capture = os.environ.get("HIPPO_JEV_MOCK_CAPTURE")
    if capture:
        Path(capture).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    path = os.environ.get("HIPPO_JEV_MOCK_OUTPUT")
    if not path:
        return None, "mock: no $HIPPO_JEV_MOCK_OUTPUT"
    try:
        mock = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, f"mock: {one_line(e, 200)}"
    canned = mock.get("answers") or {}
    default = mock.get("default") or {}
    answers = {}
    for qid, q in questions.items():
        if qid in canned:
            answers[qid] = canned[qid]
            continue
        kind = q.get("type")
        if kind not in default:
            return None, f"mock: no answer for {qid}"
        v = default[kind]
        if kind == "choice":
            answers[qid] = {"choice": v, "confidence": 1.0, "probabilities": {v: 1.0}}
        elif kind == "score":
            answers[qid] = {"score": v, "confidence": 1.0}
        else:
            answers[qid] = {"noul": v}
    return answers, None


def judge(hp, name, state, questions):
    """Ask the judge one map of typed questions over one state → (answers, meta).

    It never raises for a backend or a network problem: every caller has a path that runs
    without an answer, and the gap is recorded rather than filled (§3.9). Only a programming
    error — a missing or malformed spec — dies. `name` is the spec the questions came from,
    which is also what the caller's self-metering row is named after."""
    model = os.environ.get("HIPPO_JEV_MODEL") or "jev-latest"
    t0 = time.monotonic()

    def meta(ok, reason=None, tokens=0):
        return {"ok": ok, "reason": reason, "ms": int((time.monotonic() - t0) * 1000),
                "tokens": tokens, "model": model}

    backend = jev_backend(hp)
    if backend == "off":
        return None, meta(False, "off")
    size = len(json.dumps(state, ensure_ascii=False))
    if size > JEV_STATE_BUDGET_CHARS:
        # Never truncate. The state is what the question is about, so a silently shortened one
        # answers a different question — the caller continues as it would on any other failure.
        return None, meta(False, f"state exceeds jev budget ({size} chars)")
    body = {"model": model, "state": state, "questions": questions}
    if backend == "mock":
        answers, reason = jev_mock(questions, body)
        return (answers, meta(True)) if answers is not None else (None, meta(False, reason))
    if backend != "live":
        return None, meta(False, f"unknown jev backend: {backend}")
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        return None, meta(False, "no TYPESAFE_API_KEY")
    req = urllib.request.Request(
        JEV_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(req, timeout=JEV_TIMEOUT) as r:
                obj = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code in JEV_RETRY_STATUS and attempt == 0:
                time.sleep(JEV_RETRY_WAIT)
                continue
            # The body of a 422 names the malformed question, which is the whole diagnosis.
            try:
                detail = e.read().decode("utf-8", "replace")
            except OSError:
                detail = str(e.reason)
            return None, meta(False, one_line(f"http {e.code}: {detail}", 200))
        except (urllib.error.URLError, OSError, ValueError) as e:
            return None, meta(False, one_line(f"{type(e).__name__}: {e}", 200))
    answers = obj.get("answers") if isinstance(obj, dict) else None
    if not isinstance(answers, dict):
        return None, meta(False, "no answers in the response")
    usage = obj.get("usage") or {}
    tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    model = obj.get("model") or model  # what actually answered, for the metering row
    missing = [q for q in questions if q not in answers]
    if missing:
        return None, meta(False, f"no answer for {missing[0]}", tokens)
    return answers, meta(True, None, tokens)


def dump_failure(hp, kind, text):
    d = hp / "failures"
    d.mkdir(exist_ok=True)
    # A second-resolution timestamp alone lets two failures in the same second overwrite each other.
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    p = d / f"{stamp}-{kind}-{os.getpid()}-{uuid.uuid4().hex[:6]}.txt"
    p.write_text(text, encoding="utf-8")
    return p


def load_json_object(hp, name, kind):
    """A JSON object hippo generated under .hippo/: an unreadable one is dumped as a `kind`
    failure and read as empty — its writer writes it whole again."""
    p = hp / name
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as ex:
        dump_failure(hp, kind, f"{type(ex).__name__}: {ex}\n")
        return {}
    if not isinstance(data, dict):
        dump_failure(hp, kind, f"{name} is not an object: {data!r}\n")
        return {}
    return data


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
    # instead of every batch reinventing an absolute scratchpad path. The COMMON.md seed is
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
        "next: `hippo directive add --text \"…\"` for a standing instruction, "
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
# mentioned its keyword — automation that decides is the failure, visibility is the fix). Every
# live directive ages the same way: measured, of 75 live `phase` directives 63 were past 14 days
# and the phase-only nudge produced no withdrawals — the lifetime label was not what got read.
DIRECTIVE_AGE_SHOW_D = 14  # a directive this old carries its age in the capsule line
DIRECTIVE_STALE_NUDGE_D = 30  # this old, the volume notes ask whether it still holds


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
         if (t := event_time(d)) and (now - t).days >= DIRECTIVE_STALE_NUDGE_D),
        key=lambda x: -x[1],
    )
    if stale:
        listed = ", ".join(f"{i} ({n}d)" for i, n in stale)
        notes.append(
            f"note: directives {DIRECTIVE_STALE_NUDGE_D}d or older — {listed}. Still true? "
            "Withdraw the ones that are not (`hippo directive withdraw <id>`)."
        )
    return notes


# --- what the judge reads in the directives themselves (DESIGN §6, fourth rule) ------------
# The volume notes above count characters; nothing counted the *content* of the set. These do,
# and they stay on the same side of the line: a note on stderr after a write that already landed
# or after a listing, never a refusal and never a stored change. The thresholds sit in
# clerks/jev/directive.yaml, next to the questions they belong to.


def directive_as_state(d):
    """What the judge is told about one directive. `audience` is normalized because an absent
    one *is* `all` (§9.4): the question is whether the text agrees with the effective value."""
    return {
        "id": d.get("id", ""),
        "audience": d.get("audience") or "all",
        "text": one_line(d.get("text", "")),
    }


def directive_questions(ids, **variables):
    """The questions one request asks, picked out of the rendered `directive` spec.

    `jev_questions` renders every shape the spec carries (the three states a conflict is read in,
    the two subjects an axis is read on); a request asks the ids it needs and no others, because
    irrelevant material in a request is exactly what costs the model accuracy (§3.9)."""
    rendered = jev_questions("directive", **variables)
    return {qid: rendered[qid] for qid in ids}


def jev_directive_meter(hp, meta):
    """One self-metering row per request (§2, judge guardrails). A judge that failed has to be
    visible as a gap in the ledger rather than as silence on the terminal."""
    append_event(hp, {"ev": "clerk", "name": "jev-directive", "ok": meta["ok"],
                      "ms": meta["ms"], "tokens": meta["tokens"]})


def jev_noul(answers, qid):
    """The probability of one yes/no answer, or None when it is missing or malformed."""
    a = (answers or {}).get(qid)
    v = a.get("noul") if isinstance(a, dict) else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def jev_choice(answers, qid):
    """(option, confidence) of one choice answer, or (None, 0.0) when it is missing or malformed.
    A missing confidence reads as no confidence: every caller gates on it."""
    a = (answers or {}).get(qid)
    if not isinstance(a, dict) or not isinstance(a.get("choice"), str):
        return None, 0.0
    c = a.get("confidence")
    ok = isinstance(c, (int, float)) and not isinstance(c, bool)
    return a["choice"], float(c) if ok else 0.0


def directive_recheck(hp, a, b):
    """Stage 2: the one pair, alone in the state, asked again with the same wording.

    Stage 1 reads the whole set in one request, which is wide and cheap; the probe that measured
    it scored two unrelated pairs at 0.66-0.74 there. Nothing is reported until it survives being
    asked on its own."""
    answers, meta = judge(
        hp, "directive", {"a": directive_as_state(a), "b": directive_as_state(b)},
        directive_questions(["conflict"]),
    )
    jev_directive_meter(hp, meta)
    return jev_noul(answers, "conflict")


def directive_audience_notes(answers, d, policy, suffix="", prefix=""):
    """The audience line for one directive — or none, when the judge reads it the way it is
    already stored. A suggestion that agrees with the stored value is not news, and one the
    model is unsure of is a coin toss between three options. The note names the flag that acts
    on it: it is only worth printing if it says what to type."""
    stored = d.get("audience") or "all"
    pick, conf = jev_choice(answers, f"audience{suffix}")
    if pick is None or pick == stored or conf < float(policy.get("suggest_at", 1.0)):
        return []
    return [f"note: {prefix}audience reads as {pick} ({conf:.2f}) — stored as {stored}; "
            f"re-add with --audience {pick} if that is what was meant"]


def directive_content_notes(hp, new):
    """What the judge reads in the directive just written: a probable conflict with something
    already live, and an audience that reads differently from the stored value.

    The write has already landed and nothing here changes it — a note is the whole of it. With
    the judge off there is no request, no row and no note: the command is what it always was."""
    if jev_backend(hp) == "off":
        return []
    # Every live directive except this id. A re-add under the same --id is an update, so
    # comparing the text with its own previous version would report every edit as a conflict.
    live = [d for d in directives(hp).values()
            if d.get("state") == "active" and d.get("id") != new.get("id")]
    policy = jev_policy("directive")
    max_live = int(policy.get("max_live") or 0)
    if max_live and len(live) > max_live:
        return [f"note: {len(live)} live directives — more than the {max_live} the judge is "
                "asked about in one request, so the content notes are skipped. Withdraw the "
                "stale ones (`hippo directive withdraw <id>`)."]
    questions = directive_questions(["audience"])
    for i in range(len(live)):
        questions.update(directive_questions([f"conflict_{i}"], i=i))
    state = {"new": directive_as_state(new),
             "directives": [directive_as_state(d) for d in live]}
    answers, meta = judge(hp, "directive", state, questions)
    jev_directive_meter(hp, meta)
    if answers is None:
        return []
    recheck_at = float(policy.get("recheck_at", 1.0))
    report_at = float(policy.get("report_at", 1.0))
    flagged = []
    for i, d in enumerate(live):
        p = jev_noul(answers, f"conflict_{i}")
        if p is None or p < recheck_at:
            continue
        p2 = directive_recheck(hp, new, d)
        if p2 is not None and p2 >= report_at:
            flagged.append((p2, d))
    notes = [f"note: may conflict with {d['id']} ({p:.2f}): {one_line(d.get('text', ''), 80)}"
             for p, d in sorted(flagged, key=lambda x: -x[0])]
    return notes + directive_audience_notes(answers, new, policy)


def directive_hygiene_notes(hp):
    """`directive list`: the same reading over the whole live set — every pair of it, and each
    directive's own audience — whenever there is a judge. Automatic, never a flag: the flagged
    form was called 8 times in one project across 28 measured, and a note nobody asks for is a
    note nobody sees. With the judge off there is nothing — the listing and the volume notes are
    exactly what they were (§3.9)."""
    if jev_backend(hp) == "off":
        return []
    live = [d for d in directives(hp).values() if d.get("state") == "active"]
    policy = jev_policy("directive")
    max_live = int(policy.get("max_live") or 0)
    if max_live and len(live) > max_live:
        # Pairs grow as n². Saying so beats sending a request the model cannot hold and
        # reporting whatever comes back from the failure.
        return [f"hygiene: {len(live)} live directives — past {max_live} the pairs (n²) are "
                "more than one request can carry. Withdraw the stale ones first."]
    if not live:
        return []
    questions = {}
    for i in range(len(live)):
        questions.update(directive_questions([f"audience_{i}"], i=i))
        for j in range(i + 1, len(live)):
            questions.update(directive_questions([f"conflict_{i}_{j}"], i=i, j=j))
    answers, meta = judge(
        hp, "directive", {"directives": [directive_as_state(d) for d in live]}, questions
    )
    jev_directive_meter(hp, meta)
    if answers is None:
        return [f"hygiene: the judge did not answer ({meta['reason']})"]
    recheck_at = float(policy.get("recheck_at", 1.0))
    report_at = float(policy.get("report_at", 1.0))
    flagged = []
    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            p = jev_noul(answers, f"conflict_{i}_{j}")
            if p is None or p < recheck_at:
                continue
            p2 = directive_recheck(hp, live[i], live[j])
            if p2 is not None and p2 >= report_at:
                flagged.append((p2, live[i], live[j]))
    notes = [f"note: {a['id']} may conflict with {b['id']} ({p:.2f})"
             for p, a, b in sorted(flagged, key=lambda x: -x[0])]
    for i, d in enumerate(live):
        notes += directive_audience_notes(answers, d, policy, f"_{i}", f"{d['id']}: ")
    return notes


IN_FLIGHT_WINDOW_H = 24


def age_label(t, now):
    mins = int((now - t).total_seconds()) // 60
    return f"{mins // 60}h{mins % 60:02d}m"


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
    (§9.3), and the entry leaves this line only when main's verdict arrives. The judge's latest
    route rides the same way — evidence beside the claim, never a verdict (§3.6)."""
    rows = read_ledger(hp)
    judged = judged_refs(rows)
    claims = executor_claims(rows)
    triages = {e.get("ref"): e for e in rows if e.get("ev") == "triage"}
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
        parts = [age_label(t, now)]
        if claims.get(e.get("id")):
            parts.append(f"claims {claims[e.get('id')]}")
        tri = triages.get(e.get("id"))
        if tri:
            parts.append(f"triage {tri.get('route')}")
            if tri.get("verify") is True:
                parts.append("verify")
        tail = " · ".join(parts)
        out.append(f"{one_line(e.get('scope', ''), 44)} ({tail})")
    return out


SCRIBE_FAILING_AT = 3  # this many turn-scribe runs failed in a row earns a capsule line


def scribe_failing(hp):
    """The capsule line for a scribe that keeps failing, or None (§6, main's capsule only).

    Measured on b200 (2026-09-24): the scribe failed 7 runs in a row on an expired codex login
    and nothing said so — every dump sat in failures/, which nothing mentions until checkup
    runs. One failure is noise (a timeout, a flaky network), so the line waits for three in a
    row, and it quotes the newest scribe dump's first line: the reason, carrying the backend's
    own first words when it said any (clerk_run.sh puts the cause first)."""
    runs = [e.get("ok") for e in read_ledger(hp)
            if e.get("ev") == "clerk" and e.get("name") == "turn-scribe"]
    n = 0
    for ok in reversed(runs):
        if ok is not False:
            break
        n += 1
    if n < SCRIBE_FAILING_AT:
        return None
    # A dump name starts with its second-resolution stamp, so name order is time order.
    dumps = sorted((hp / "failures").glob("*-scribe-*"))
    head = (_read_text(dumps[-1]) or "").strip().splitlines() if dumps else []
    cause = f" — {one_line(head[0], 100)}" if head else ""
    return f"· scribe: the last {n} runs failed{cause} (.hippo/failures/)"


TASK_FLAGS = "task-flags.json"  # generated: open tasks the judge read as ended (§3.5.9)


def task_flags(hp):
    """task id → {t, p}: the open tasks a window's digest read as finished or abandoned, `t`
    being when the scribe read the task (§3.5.9). Only the scribe writes it, under its lock;
    what shows is derived from it and tasks.yaml at every read (`task_flag_shows`). An
    unreadable file is dumped and read as empty — the next judged window writes it whole again."""
    return load_json_object(hp, TASK_FLAGS, "task-flags")


def task_flag_shows(task, flag):
    """A flag shows while its task is open and untouched since the scribe read it. Every task
    write — `set`, `done`, `drop` — moves `updated`, so a later stamp is main having looked:
    the flag goes quiet by itself, and nobody deletes it by hand."""
    t = _iso_time(flag.get("t")) if isinstance(flag, dict) else None
    if task is None or t is None or task.get("status") not in OPEN_STATUSES:
        return False
    u = _iso_time(task["updated"]) if task.get("updated") else None
    return u is None or u <= t


def task_check(hp, tasks):
    """The capsule's `check:` line, or None (§6, main's capsule only): the flags that still
    show, in tasks.yaml order. Closing a task is main's call, so the line asks and never acts."""
    flags = task_flags(hp)
    ids = [t["id"] for t in tasks
           if isinstance(t.get("id"), str) and task_flag_shows(t, flags.get(t["id"]))]
    if not ids:
        return None
    if len(ids) == 1:
        return f"· check: {ids[0]} looks finished or abandoned — close it, or note what is left"
    return (f"· check: {', '.join(ids)} look finished or abandoned — close them, "
            "or note what is left")


def live_directives(hp, reader):
    """The active directives addressed to `reader` (§9.4): `all`, and absent, reach both."""
    return [d for d in directives(hp).values() if d.get("state") == "active"
            and (d.get("audience") or "all") in (reader, "all")]


def directive_lines(live):
    """One `· live: …` line per directive, in ledger order and in full.

    Every active directive appears whole: a directive that is invisible at session start is
    effectively not there (principle 9, read backwards), and that is as true of the ninth one as
    of the first. Volume is handled by warning the author at `directive add` time, not by
    dropping text here. Age is the whole staleness mechanism (nothing expires by itself — the
    verdict stays with main and the user), so it is shown only once it is worth a glance."""
    now = datetime.now(timezone.utc)
    out = []
    for d in live:
        t = event_time(d)
        age = (now - t).days if t else 0
        label = f"live({age}d)" if age >= DIRECTIVE_AGE_SHOW_D else "live"
        out.append(f"· {label}: {one_line(d.get('text', ''))}")
    return out


def status_lines(hp, compacted=False, transcript=None):
    """DESIGN §6 resident surface (header + live directives + in flight + last).

    Audience (§9.4): inside a dispatched lane (HIPPO_DISPATCH set) the capsule carries the
    directives addressed to executors; everywhere else, the ones addressed to main. `all`
    (and absent, its default) reaches both. `compacted` is SessionStart's source=compact: main's
    capsule then closes on the line that points at the summary's `## hippo deltas` (§3.4), and
    its in-flight line adds the native runs main's `transcript` shows still out."""
    data = tasks_load(hp)
    n_open = sum(1 for t in data["tasks"] if t.get("status") in OPEN_STATUSES)
    reader = "executor" if os.environ.get("HIPPO_DISPATCH") else "main"
    live = live_directives(hp, reader)

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
    lines += directive_lines(live)
    # Nothing flying → no line. The capsule only spends a line on a question that has an answer.
    flying = in_flight(hp)
    if compacted and reader == "main":
        flying += native_in_flight(transcript)
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
    else:
        check = task_check(hp, data["tasks"])
        if check:
            lines.append(check)
        failing = scribe_failing(hp)
        if failing:
            lines.append(failing)
        # The grammar, where main re-reads after a compaction. Measured: 405 `--help` calls in
        # 19 projects, 59 of Codex's 96 within 30 tool calls of a compaction — the moment this
        # capsule re-arrives. A lane has its `report:` line instead.
        lines.append(
            "· cli: task add|set|done|list · log dispatch|outcome|review|review-status "
            "· directive add|withdraw · prior · dispatch [--batch] — /hippo:hippo has the flags"
        )
        if compacted:
            # The summary lands in main's context before this capsule does (measured), so the
            # pointer reads "above". "Has", not "ends with": the host appends its own paragraphs
            # after the summary (measured). Worded as a condition because Codex fires
            # SessionStart(compact) too but gets no PreCompact, so its summary has no such section.
            lines.append(
                "· compact: if the summary above has a `## hippo deltas` section, run those "
                "commands first — they are proposals; skip any that are wrong"
            )
    return lines


def subagent_lines(hp):
    """What SubagentStart injects into a native subagent (§3.4): the live directives addressed
    to executors, and nothing else — nothing at all when there is none.

    Not a lane's capsule. No `report:` line: a native worker runs without HIPPO_DISPATCH, so its
    `log outcome` would land as src=cli — main's verdict on its own work — while the scribe
    already records the run and main's verdict at the end of the turn (§3.5.3c). No depth line:
    HIPPO_DEPTH indexes wrapper lanes (§9.5), and a subagent's nesting is main's call in its
    brief. No tasks, in-flight or last: those are main's state, and a worker acts on them
    through main's brief, not beside it (principle 2)."""
    live = live_directives(hp, "executor")
    if not live:
        return []
    return [f"[hippo] directives {len(live)} live — the user's standing rules for this project",
            *directive_lines(live)]


PRECOMPACT_CAP = 3000  # chars of the whole text: it is appended to every compaction's instructions
PRECOMPACT_ITEM = 80  # chars of a task's notes or a directive's text per list line


def precompact_lines(hp):
    """What PreCompact appends to the compaction instructions (§3.4): end the summary with
    `## hippo deltas` — the exact commands that would record what this conversation changed and
    hippo's lists do not show yet — then those lists.

    Measured (2026-09-24, Claude Code 2.1.281, manual and auto compaction): the summarizer wrote
    a requested section like this with the right statuses, but its formatting drifted (bullets
    added, a prefix dropped). So each line asks for a whole command main can read, check and run
    or skip after the compaction — never a shape a parser depends on; the capsule's `compact:`
    line is where main is told to (status_lines). With this text (haiku, manual /compact) the
    section held the three commands the conversation called for, and main, resumed, ran them.

    Main's audience only (§9.4): a manual /compact writes this text into the transcript main
    reads next. Open tasks run most-recently-updated first, the ones this conversation most
    likely touched; past the cap, items are cut from the ends of the lists and counted."""
    head = [
        "hippo: end the summary with a section headed exactly `## hippo deltas`: one line per "
        "change this conversation made that hippo's lists below do not show yet, each written as "
        "the exact command that records it —",
        "  hippo task done <id> --note '…'   (a listed task that is finished)",
        "  hippo task set <id> notes '…'   (a listed task that moved on; replaces its notes)",
        "  hippo directive withdraw <id>   (only if the user said so)",
        "  hippo directive add --id <id> --text '…'   (the user changed it)",
        "  edit <file>: '<old>' → '<new>'   (a memory or doc line that is now false)",
        "or the single line `none` if nothing changed. They are run after the compaction, so "
        "write each one complete.",
    ]

    def notes_of(t):
        n = t.get("notes") or []
        return " / ".join(map(str, n)) if isinstance(n, list) else str(n)

    tasks = sorted((t for t in tasks_load(hp)["tasks"] if t.get("status") in OPEN_STATUSES),
                   key=lambda t: str(t.get("updated") or ""), reverse=True)
    t_items = [" — ".join(filter(None, (f"- {t.get('id')}", one_line(t.get("title", ""), 100),
                                        one_line(notes_of(t), PRECOMPACT_ITEM))))
               for t in tasks]
    d_items = [f"- {d['id']} — {one_line(d.get('text', ''), PRECOMPACT_ITEM)}"
               for d in live_directives(hp, "main")]
    t_head, d_head = "open tasks (id — title — notes):", "live directives (id — text):"
    # Directives first into the budget: a handful at most, and never folded away elsewhere (§6).
    room = PRECOMPACT_CAP - len("\n".join([*head, t_head, d_head])) - 60  # 60: the cut line
    kept = {}
    for name, items in (("d", d_items), ("t", t_items)):
        kept[name] = []
        for it in items:
            if len(it) + 1 > room:
                break
            kept[name].append(it)
            room -= len(it) + 1
    lines = [*head, t_head, *(kept["t"] if t_items else ["(none open)"]),
             d_head, *(kept["d"] if d_items else ["(none live)"])]
    cut = len(t_items) - len(kept["t"]) + len(d_items) - len(kept["d"])
    if cut:
        lines.append(f"({cut} more not shown: `hippo task list` / `hippo directive list`)")
    return lines


def cmd_status(args):
    hp = args.hp
    if args.inject:
        # The hook names its moment in HIPPO_INJECT — internal, never a flag (§3.4): SessionStart
        # passes its source, SubagentStart `subagent`, PreCompact `precompact`.
        moment = os.environ.get("HIPPO_INJECT", "")
        transcript = os.environ.get("HIPPO_TRANSCRIPT")
        # An agent's own compaction reaches both compaction hooks as main's (§3.4): it is asked
        # for no deltas, and once compacted gets what SubagentStart gave it — a fork too, since
        # the capsule it carried from main is what its compaction summarized away.
        agent = (compaction_agent(transcript, os.environ.get("HIPPO_PROMPT"),
                                  os.environ.get("HIPPO_TRIGGER"))
                 if moment in ("precompact", "compact") else None)
        if agent is not None:
            lines = ([] if moment == "precompact" or agent.get("agentType") == "hippo:lane"
                     else subagent_lines(hp))
        else:
            lines = (subagent_lines(hp) if moment == "subagent"
                     else precompact_lines(hp) if moment == "precompact"
                     else status_lines(hp, compacted=moment == "compact", transcript=transcript))
        if lines:
            print("\n".join(lines))
        return
    print("\n".join(status_lines(hp)))
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
REF_TARGET = {"outcome": "dispatch", "review-status": "review", "usage": "dispatch",
              "triage": "dispatch"}
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
    if isinstance(ref, str) and ref.startswith(NATIVE_PREFIX):
        # main saw the id at launch and reached for the CLI: the row lands when the turn ends
        # (§3.5.3c), and so does the verdict main just typed — the clerk reads it off the turn.
        return (f"ev={e['ev']}: ref={ref!r} is not recorded yet — a native run (Agent, fork, "
                "Workflow) lands in the ledger at the end of the turn, and hippo records your "
                "verdict from the conversation then: no call needed")
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
    them and fails rather than guessing. An unjudged native row (`ag-`, §3.5.3c) is no candidate
    while any other dispatch is: the scribe tags every subagent whose brief names the task, and
    most of those — look-ups, surveys, checks — never get a verdict (the reason `prior_facts`
    leaves them out of the open items), so counting them would make `task:` fail for good the
    first time a subagent named the task (13 such rows for one task in a replayed mlx-vlm
    session). Alone, a native row is still the one."""
    if not isinstance(ref, str) or not ref.startswith("task:"):
        return ref
    task = ref[len("task:"):]
    rows = read_ledger(hp)
    judged = judged_refs(rows)  # a lane's claim leaves its dispatch still awaiting the verdict
    hits = [e for e in rows if e.get("ev") == "dispatch" and e.get("task") == task]
    if not hits:
        die(f"--ref {ref}: no dispatch recorded for task {task!r}")
    open_ = [e for e in hits if e.get("id") not in judged]
    open_ = [e for e in open_ if not str(e.get("id", "")).startswith(NATIVE_PREFIX)] or open_
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
    if e.get("ev") == "triage":
        # The judge read the lane's own report at its exit; the scribe would be restating a
        # route from a paraphrase of it.
        return "ev=triage: the wrapper records triage — it was there when the lane exited"
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


# What a bulk verdict row may say, and nothing else. ref is derived from the journal and
# t/src stay writer-stamped, so a row can never smuggle an identity the journal did not mint.
BULK_ROW_KEYS = ("entry", "attempt", "result", "note", "attr", "rework", "by")


def log_outcome_bulk(args):
    """`log outcome --from-batch <journal>`: verdict rows as stdin JSON-lines, resolved
    through the batch journal (§3.6) — serialization after verification, never verification.

    One call replaces the N scalar calls a judged batch used to take (measured: 222), but the
    narrowing is the point, not the batching: a row resolves only through this journal's
    latest exited attempt, only onto a dispatch whose executor claim still awaits main's
    verdict. Everything exceptional — an earlier attempt, a deliberate re-verdict, a lane
    that never claimed — keeps the scalar command, where the exception stays visible in the
    typing. Fail-closed and total like load_manifest: every problem lands with its line
    number, and nothing is appended until the whole input validates."""
    scalars = [f for f in ("ref", "result", "attr", "rework", "by", "note")
               if getattr(args, f) is not None]
    if scalars:
        die("outcome --from-batch: the scalar flags are the other mode: "
            + ", ".join("--" + s for s in scalars), 2)
    jp = Path(args.from_batch)
    if not jp.is_file():
        die(f"outcome --from-batch: no such journal: {jp}", 2)
    exits, latest = {}, {}
    for line in jp.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "exit" and isinstance(rec.get("attempt"), int):
            exits.setdefault((rec.get("id"), rec["attempt"]), []).append(rec)
            latest[rec.get("id")] = max(latest.get(rec.get("id"), 0), rec["attempt"])
    if not exits:
        die(f"outcome --from-batch: no exit records in {jp} — nothing is judgeable yet", 2)

    ledger = read_ledger(args.hp)
    known = {e.get("id") for e in ledger if e.get("ev") == "dispatch"}
    judged = judged_refs(ledger)
    claims = executor_claims(ledger)
    rows, problems, seen = [], [], {}
    for n, raw in enumerate(sys.stdin.read().splitlines(), 1):
        if not raw.strip():
            continue
        where = f"line {n}"
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as ex:
            problems.append(f"{where}: JSON parse failed: {ex}")
            continue
        if not isinstance(row, dict):
            problems.append(f"{where}: a row must be a JSON object")
            continue
        for k in sorted(set(row) - set(BULK_ROW_KEYS), key=repr):
            problems.append(f"{where}: key not allowed: {k} (allowed: "
                            f"{', '.join(BULK_ROW_KEYS)} — ref is derived from the journal, "
                            "t/src are writer-stamped)")
        entry, attempt, note = row.get("entry"), row.get("attempt"), row.get("note")
        bad_shape = False
        if not isinstance(entry, str) or not entry.strip():
            problems.append(f"{where}: entry is required (the journal's per-entry id)")
            bad_shape = True
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            problems.append(f"{where}: attempt must be a positive integer")
            bad_shape = True
        if not isinstance(note, str) or not note.strip():
            # Scalar mode leaves the note optional; a bulk row is judged sight-unseen by
            # every later reader, so it carries its rationale or it does not land.
            problems.append(f"{where}: note is required in bulk mode — the row is a verdict "
                            "and carries its own rationale")
            bad_shape = True
        if bad_shape:
            continue
        if entry in seen:
            problems.append(f"{where}: duplicate row for entry {entry!r} "
                            f"(first at line {seen[entry]})")
            continue
        seen[entry] = n
        if entry not in latest:
            problems.append(f"{where}: entry {entry!r} has no exit in the journal")
            continue
        if attempt != latest[entry]:
            problems.append(f"{where}: attempt {attempt} is not entry {entry!r}'s latest "
                            f"exited attempt ({latest[entry]}) — earlier attempts keep the "
                            "scalar command")
            continue
        recs = exits[(entry, attempt)]
        if len(recs) > 1:
            problems.append(f"{where}: the journal holds {len(recs)} exits for {entry!r} "
                            f"attempt {attempt} — ambiguous; judge by dispatch id with the "
                            "scalar command")
            continue
        ref = recs[0].get("dispatch")
        e = {"ev": "outcome", "ref": ref, "result": row.get("result"), "note": note}
        for k in ("attr", "rework", "by"):
            if k in row:
                e[k] = row[k]
        bad = validate_event(e)
        if bad:
            problems.append(f"{where}: {bad}")
            continue
        # check_ref's semantic against one ledger read: N rows must not cost N file reads —
        # that ceremony is the thing this mode exists to remove.
        if ref not in known:
            problems.append(f"{where}: {entry!r} resolved to {ref!r}, which this ledger never "
                            "recorded (the launch's record failed) — the gap is the record")
            continue
        if ref in judged:
            problems.append(f"{where}: {ref} already has a verdict — a deliberate re-verdict "
                            "keeps the scalar command")
            continue
        if ref not in claims:
            problems.append(f"{where}: {ref} has no executor claim yet — judging an unclaimed "
                            "lane keeps the scalar command")
            continue
        rows.append(e)
    if problems:
        die(f"outcome --from-batch: {len(problems)} problem(s), nothing written\n"
            + "\n".join(f"  - {p}" for p in problems), 2)
    if not rows:
        die("outcome --from-batch: no verdict rows on stdin", 2)
    written = 0
    if not args.dry_run:
        for e in rows:
            append_event(args.hp, e)
            written += 1
    # Recomputed from the file, not inferred: a bulk call from inside a lane writes claims
    # (src=executor), and claims judge nothing — the re-read tells that truth by itself.
    after = read_ledger(args.hp) if written else ledger
    judged_after, claims_after = judged_refs(after), executor_claims(after)
    journal_refs = {r.get("dispatch") for recs in exits.values() for r in recs}
    summary = {"src": resolve_src(), "validated": len(rows), "written": written,
               "remaining_pending": sum(1 for r in journal_refs
                                        if r in claims_after and r not in judged_after),
               "results": dict(collections.Counter(e["result"] for e in rows))}
    if args.dry_run:
        summary["dry_run"] = True
    print(json.dumps(summary, ensure_ascii=False))


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
        if args.from_batch:
            return log_outcome_bulk(args)
        if args.dry_run:
            die("outcome: --dry-run belongs to --from-batch", 2)
        if args.result is None:  # required in scalar mode; bulk rows carry their own
            die("outcome: --result is required", 2)
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
            print("note: lifetime is no longer recorded — a directive lives until "
                  "'hippo directive withdraw <id>'", file=sys.stderr)
        if getattr(args, "audience", None):
            e["audience"] = args.audience
        # Length is not warned about here: the set-wide notes emitted after the write name every
        # oversized directive by id and size, including this one. Saying it twice trains the
        # reader to skim the notes.
    log_and_print(args.hp, e)
    if args.ev == "directive":
        # After the write, so the notes describe the set the next session will actually carry.
        # The judge's notes come first because they are about this directive; the volume notes
        # are about the set it just joined. A withdrawal carries no text to read, so only an
        # active write is judged.
        if e.get("state") == "active" and e.get("text"):
            for note in directive_content_notes(args.hp, e):
                print(note, file=sys.stderr)
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
    else:
        for d in ds:
            aud = d.get("audience")
            tag = f"/{aud}" if aud and aud != "all" else ""
            print(
                f"{d['id']}  [{d.get('state', '?')}{tag}]  "
                f"{d.get('text', '')}"
            )
    # Reviewing the set is the other moment the author can act on what it costs. On stderr, so
    # the listing itself stays a clean, pipeable record of the directives. The judge reads the
    # content beside the volume notes, by itself, for the human-facing listing; a --json read is
    # a machine's, and stays free of the second's latency and of any note, as it always was.
    notes = [] if args.json else directive_hygiene_notes(args.hp) + directive_volume_notes(args.hp)
    for note in notes:
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
        else f"not yet — the scribe writes it once {DISTILL_MIN_NEW} verdicts have landed "
        "(or run hippo prior distill)"
    )


PRIOR_MIN_SAMPLE = 4


def event_time(e):
    try:
        return datetime.strptime(e.get("t", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def prior_cells(rows, prices=None):
    """The kind × exec cells: the dispatch ⋈ first-outcome join, counted and priced.

    Both readers of the ledger's routing evidence come through here — PRIORS.md renders these
    cells, and the batch plan (§3.6) reads the same ones to move a suggestion. A second
    implementation of the join would be a second answer to the same question."""
    prices = prices or load_prices()
    first = {}
    # Self-reported outcomes are claims, not verdicts (§9.2): they enter no cell, no
    # attribution count, no unjoined count. The scorecard folds only judgments.
    for e in rows:  # the ledger is append-only, so file order is chronological
        if e.get("ev") == "outcome" and e.get("src") != "executor":
            first.setdefault(e.get("ref"), e)
    usage = {}
    for e in rows:  # the last row per (dispatch, model): a native run's rows are cumulative,
        if e.get("ev") == "usage":  # one per model (§3.5.3c); a codex lane has one model
            usage.setdefault(e.get("ref"), {})[e.get("model")] = e

    cells = {}
    for d in rows:
        if d.get("ev") != "dispatch":
            continue
        f = first.get(d.get("id"))
        if not f:
            continue
        b = cells.setdefault((d.get("kind"), d.get("exec")),
                             {"judged": 0, "accepted": 0, "revised": 0, "refuted": 0,
                              "no-go": 0, "lost": 0, "rework": 0, "tokens": 0, "usd": 0.0,
                              "priced": 0, "unpriced": 0,
                              "unpriced_models": collections.Counter()})
        b["judged"] += 1
        b[f.get("result")] = b.get(f.get("result"), 0) + 1
        b["rework"] += int(f.get("rework") or 0)
        # Cost lands on the kind × exec cell only (§9.6): tokens always; dollars only when
        # the sheet can price them honestly — an unpriced row is counted and named, not guessed.
        # A dispatch is priced only when every model it ran on is: half a bill is no price.
        us = list(usage.get(d.get("id"), {}).values())
        if us:
            b["tokens"] += sum(int(u.get("tokens") or 0) for u in us)
            usd = [price_usd(u, prices) for u in us]
            if None in usd:
                b["unpriced"] += 1
                for u, x in zip(us, usd):
                    if x is None:
                        b["unpriced_models"][u.get("model") or "(no model)"] += 1
            else:
                b["usd"] += sum(usd)
                b["priced"] += 1
    return cells


def prior_n(b):
    """The denominator of a first-pass rate: a no-go never started and a lost result was never
    seen, so neither is a judgment of the work."""
    return b["judged"] - b["no-go"] - b["lost"]


AGREEMENT_ROUTES = ("accept-candidate", "escalate", "no-go-candidate", "failed")
AGREEMENT_RESULTS = ("accepted", "revised", "refuted", "no-go", "lost")


def triage_agreement(rows):
    """The judge measured the way PRIORS measures an executor: each triaged dispatch's route
    against main's first verdict on it (§3.6). The route that counts is the latest one recorded
    before that verdict — what main had in front of it when judging; a wrapper re-read after
    the verdict may have been shaped by it. A scribe triage (a native run's report, read at
    Stop — §3.5.3c) counts wherever it lands: it is written after a verdict main typed that
    same turn, main never sees it, and it never reaches the clerk that reads the verdict off
    the transcript, so it is an independent reading either way. A ledger with no triage row
    gets no section: with no key the page is exactly what it was."""
    tri, first = {}, {}
    for e in rows:  # chronological, so "before the verdict" is file order
        ref = e.get("ref")
        if e.get("ev") == "triage" and (ref not in first or e.get("src") == "scribe"):
            tri[ref] = e.get("route")
        elif e.get("ev") == "outcome" and e.get("src") != "executor":
            first.setdefault(ref, e.get("result"))
    if not tri:
        return []
    table = {r: collections.Counter() for r in AGREEMENT_ROUTES}
    for ref, route in tri.items():
        if ref in first and route in table:
            table[route][first[ref]] += 1
    lines = ["", "## triage agreement — the judge's route against main's first verdict", ""]
    shown = [r for r in AGREEMENT_ROUTES if sum(table[r].values()) >= PRIOR_MIN_SAMPLE]
    thin = [f"{r} (n={sum(table[r].values())})" for r in AGREEMENT_ROUTES
            if 0 < sum(table[r].values()) < PRIOR_MIN_SAMPLE]
    if shown:
        lines += ["| route | n | " + " | ".join(AGREEMENT_RESULTS) + " |",
                  "|---|---:|" + "---:|" * len(AGREEMENT_RESULTS)]
        for r in shown:
            lines.append(f"| {r} | {sum(table[r].values())} | "
                         + " | ".join(str(table[r][k]) for k in AGREEMENT_RESULTS) + " |")
    elif not thin:
        lines.append("(no triaged dispatch has a verdict yet)")
    if thin:
        lines += ["", f"below the n={PRIOR_MIN_SAMPLE} threshold, no row reported "
                      f"({len(thin)}): " + ", ".join(thin)]
    acc = table["accept-candidate"]
    b = sum(acc.values())
    if b:
        a = acc["accepted"] + acc["revised"]
        rate = f" ({100 * a / b:.1f}%)" if b >= PRIOR_MIN_SAMPLE else f" (n={b}, no rate)"
        lines += ["", f"accept-candidate precision {a}/{b}{rate} — accepted or revised of "
                      "accept-candidates that got a verdict"]
    return lines


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

    cells = prior_cells(rows, prices)
    # Per exec is the same join one axis wider, so it is folded from the cells rather than
    # counted a second time — one counting rule, one place it can be wrong.
    per_exec, unpriced_models = {}, collections.Counter()
    for (_, ex), b in cells.items():
        p = per_exec.setdefault(ex, {"judged": 0, "accepted": 0, "revised": 0, "refuted": 0,
                                     "no-go": 0, "lost": 0})
        for k in p:
            p[k] += b[k]
        unpriced_models.update(b["unpriced_models"])

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
    for (kind, ex), b in sorted(cells.items(), key=lambda kv: -prior_n(kv[1])):
        n = prior_n(b)
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

    # Refs, not claim events: one lane may claim twice, and the number main acts on is how
    # many judgments are owed. Measured (algo200): 222 claims-only outcomes rendered a fully
    # empty page with no hint that data was waiting on main.
    pending = {e.get("ref") for e in rows
               if e.get("ev") == "outcome" and e.get("src") == "executor"} - set(first)
    if pending:
        lines += ["", f"claims pending verdict: {len(pending)} — executor self-reports "
                      "awaiting main's judgment; they enter no cell above (§9.2)"]

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

    lines += triage_agreement(rows)

    attr = collections.Counter(e.get("attr") for e in outs if e.get("result") != "accepted")
    unjoined = sum(1 for e in outs if e.get("ref") not in known)
    lines += ["", "## attribution of non-accepted outcomes", "",
              f"work {attr['work']} / brief {attr['brief']} / harness {attr['harness']} "
              f"/ unattributed {attr[None]}", "",
              f"## unjoined outcomes\n\n{unjoined}", "", "## open items — no outcome, launched "
              "more than 24h ago", ""]
    stale, native = [], 0
    for d in disp:
        t = event_time(d)
        if d.get("id") in first or not t or (now - t) < timedelta(hours=24):
            continue
        # hippo records every native run (§3.5.3c) — a quick look-up that nobody judged is
        # not a forgotten lane, and a list of them would bury the lanes that are.
        if str(d.get("id", "")).startswith(NATIVE_PREFIX):
            native += 1
            continue
        h, m = divmod(int((now - t).total_seconds()) // 60, 60)
        stale.append(f"- {d.get('id')} ({h}h{m:02d}m)")
    lines += stale or ["(none)"]
    if native:
        lines += ["", f"native runs (`{NATIVE_PREFIX}`) with no verdict, left out of this list: "
                      f"{native}"]

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
    # Broken down by name: the turn clerk, the judge gate and the distiller are different
    # instruments at wildly different prices, and one total hides which one is spending.
    by_name = collections.Counter(e.get("name") or "?" for e in clerks)
    detail = f" ({', '.join(f'{n} {c}' for n, c in by_name.most_common())})" if by_name else ""
    lines += ["", "## clerk overhead", "",
              f"{len(clerks)} runs{detail}, {fails} failures, ~{tokens} tokens"]
    return "\n".join(lines)


def distill(hp, days, src=None):
    """Regenerate PRIORS.md from the last `days` of ledger → (ok, message). The distiller row
    is its own meter, failed or not; a failure leaves its dump under failures/."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    kept = []
    for e in read_ledger(hp):
        t = event_time(e)
        if t and t >= cutoff:
            kept.append(e)
    if not kept:
        # Same deterministic prefilter as the scribe (§3.5.3): there is nothing to distill from
        # an empty window, so do not spend a model call proving it.
        return True, f"no ledger events in the last {days} days — nothing to distill"
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
        f"# computed facts (generated {now_iso()}, window: {days} days, "
        f"{len(kept)} events)\n\n"
        + prior_facts(kept, now)
        + "\n\n# current PRIORS.md\n\n"
        + priors
        + "\n"
    )
    out, err, rc, ms, tokens = run_clerk(
        hp, CLERKS / "distiller.md", payload, DISTILL_TIMEOUT
    )
    meter = {"ev": "clerk", "name": "distiller", "ms": ms, "tokens": tokens}
    if rc != 0 or not out.strip():
        reason = f"rc={rc}" if rc != 0 else "the clerk returned nothing"
        p = dump_failure(
            hp, "distill", f"rc={rc}\n--- stderr ---\n{err}\n--- stdout ---\n{out}"
        )
        append_event(hp, {**meter, "ok": False}, src=src)
        return False, f"distill failed ({reason}) — dump: {p}"
    write_durable(hp / "PRIORS.md", out.strip() + "\n")
    append_event(hp, {**meter, "ok": True}, src=src)
    return True, f"PRIORS.md regenerated ({len(kept)} events / {days} days, {ms}ms)"


def cmd_distill(args):
    ok, msg = distill(args.hp, args.days)
    if not ok:
        die(msg)
    print(msg)


def distill_due(hp):
    """§3.5.8: PRIORS is missing or older than DISTILL_STALE_DAYS, and DISTILL_MIN_NEW verdicts
    (outcomes that are not executor claims — the ones PRIORS reads) landed after the last
    distiller row. Counted from the ledger, never a counter file. A failed run resets the count
    too: a dead clerk must not be re-billed at every Stop (the cursor rule of §3.5.6)."""
    p = hp / "PRIORS.md"
    if p.exists() and time.time() - p.stat().st_mtime < DISTILL_STALE_DAYS * 86400:
        return False
    new = 0
    for e in read_ledger(hp):
        if e.get("ev") == "clerk" and e.get("name") == "distiller":
            new = 0
        elif e.get("ev") == "outcome" and e.get("src") != "executor":
            new += 1
    return new >= DISTILL_MIN_NEW


def auto_distill(hp):
    """The scribe's last step, on its success and failure paths alike: it already holds the
    lock and runs detached, so the distiller's 300s blocks nothing."""
    if distill_due(hp):
        _, msg = distill(hp, DISTILL_DAYS, src="scribe")
        print(f"auto-distill: {msg}", file=sys.stderr)


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
    write_durable(p, "\n".join(lines) + "\n")


def load_cursors(hp):
    """A failed read beats losing every cursor — dump the original and start empty."""
    return load_json_object(hp, "cursors.json", "cursors")


def save_cursors(hp, cursors):
    write_durable(hp / "cursors.json", json.dumps(cursors, ensure_ascii=False, indent=2) + "\n")


DISPATCH_USAGE = (
    "usage: hippo dispatch --kind <kind> --scope <scope> [--task <task-id>] [--depth N] "
    "[--fast] [--] <codex exec args...>\n"
    "       everything after -- goes to codex exec verbatim, even if it looks like a wrapper flag\n"
    "       --depth 0 (default): the lane is told not to re-delegate; --depth 1: it may spawn\n"
    "       children, which start at depth 0 (§9.5 — the clause is indexed, never enforced)\n"
    '       --fast: launch on codex\'s fast service tier (-c service_tier="fast"); '
    "the exec axis is unchanged\n"
    "       batch form: hippo dispatch --batch <manifest.yaml> [--dry-run]\n"
    "       watch form: hippo dispatch --watch <dispatch-id> [--for SECONDS] — blocks until the\n"
    "       lane ends (exit 0, its final lines) or SECONDS pass (default 540; exit 3, its state)"
)


def split_dispatch_argv(argv):
    """Strip dispatch's own flags and hand everything else to codex exec untouched.

    Why not argparse: the remaining arguments are codex's grammar (-m, -c k=v, -C dir …) and
    this parser has no business interpreting them. After `--`, even wrapper-shaped flags pass."""
    fields = {"kind": "", "scope": "", "task": "", "depth": ""}
    fast = False
    rest = []
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if a == "--":
            rest.extend(argv[i + 1 :])
            break
        if a == "--fast":
            fast, i = True, i + 1
            continue
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
    return fields["kind"], fields["scope"], fields["task"], fields["depth"], fast, rest


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


FANOUT_BUDGET_USD = 500.0  # per parent per 24h — override: config.yaml dispatch.max_wave_usd
FANOUT_RESERVE_MTOK = (1.0, 0.2)  # nominal (input, output) Mtok reserved per unfinished child


def _reserve_usd(model, prices):
    """Nominal launch-time reservation for a lane whose real cost is not yet measured.

    A burst launches everything before anything finishes, so a breaker fed only measured
    usage would see $0 exactly when it matters. The nominal token figure is a guard's
    arithmetic, not data — nothing of it reaches the ledger, and the moment the lane exits,
    its measured usage replaces the reservation. An unknown model reserves at the most
    expensive tier on the sheet: a typo must not dodge the breaker. A lane on a `legacy` model
    reserves at that model's own price; a legacy row is never the tier a typo is read as."""
    mtin, mtout = FANOUT_RESERVE_MTOK
    m = prices["models"].get(model)
    if m is None:
        tiers = routable_models(prices).values()
        if not tiers:
            return None
        return max(mtin * v["input"] + mtout * v["output"] for v in tiers)
    return mtin * m["input"] + mtout * m["output"]


def fanout_verdict(hp, parent, child_model):
    """The fan-out circuit breaker (§3.6): the one check that lives inside this service —
    denominated in dollars, never in lanes. Returns (None | "warn" | "stop", msg); the caller
    decides what a verdict becomes — single dispatch dies on stop, a batch must keep
    collecting the children already running.

    Guards exactly one measured disaster shape: a lane machine-gunning expensive children
    through the sanctioned path (the 336k-token re-delegation spiral, and its §9.5 sequel).
    A thousand luna-class children clear a budget two dozen astra-class ones exhaust — count was
    the wrong axis, price × count is the real one. Lane-origin launches only: main is never
    gated — a session-launched batch of any size is main's judgment, and gating it would be the
    enforcement principle 3 rejects. A lane that bypasses the wrapper still succeeds; this
    stops accidents, not adversaries, and every measured failure was an accident."""
    if not parent or hp is None:
        return None, ""
    prices = load_prices()
    if not prices["models"]:
        return None, ""  # no sheet, no cost reasoning — the breaker cannot price what it cannot see
    budget = float((config(hp).get("dispatch") or {}).get("max_wave_usd")
                   or FANOUT_BUDGET_USD)
    now = datetime.now(timezone.utc)
    rows = read_ledger(hp)
    usage = {e.get("ref"): e for e in rows if e.get("ev") == "usage"}
    measured = reserved = 0.0
    n = 0
    for e in rows:
        if e.get("ev") != "dispatch" or e.get("parent") != parent:
            continue
        t = event_time(e)
        if not t or (now - t) > timedelta(hours=IN_FLIGHT_WINDOW_H):
            continue
        n += 1
        u = usage.get(e.get("id"))
        usd = price_usd(u, prices) if u else None
        if usd is not None:
            measured += usd
        else:
            model = (str(e.get("exec", "")).split("/") + ["", ""])[1]
            reserved += _reserve_usd(model, prices) or 0.0
    total = measured + reserved + (_reserve_usd(child_model, prices) or 0.0)
    if total > budget:
        return "stop", (
            f"dispatch: this lane's children would reach ~${total:.0f} of its ${budget:.0f} budget — "
            f"lane {parent} has {n} children in 24h (${measured:.2f} measured + "
            f"${reserved:.0f} reserved for lanes still running). Stop and report instead: "
            "`hippo log outcome --result no-go --note '…'`; main decides — the budget is "
            ".hippo/config.yaml dispatch.max_wave_usd.")
    if total >= budget / 2:
        return "warn", (
            f"dispatch: note — lane {parent}'s children are at ~${total:.0f} of its "
            f"${budget:.0f} budget ({n} children in 24h, ${measured:.2f} measured).")
    return None, ""


def check_fanout(hp, parent, child_model):
    """The single-dispatch face of the breaker: die on stop, stderr on warn."""
    verdict, msg = fanout_verdict(hp, parent, child_model)
    if verdict == "stop":
        die(msg, 2)
    if verdict == "warn":
        print(msg, file=sys.stderr)


def lane_path():
    """$PATH for a lane: this plugin's bin/ first, so a lane's bare `hippo` is the hippo that
    launched it on either host (Codex puts no plugin bin/ on PATH) and no brief has to pin a
    versioned cache path — one that the next plugin update deletes."""
    bin_dir = str(ROOT / "bin")
    parts = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and d != bin_dir]
    return os.pathsep.join([bin_dir, *parts])


# --- lanes: what a running codex lane is doing (DESIGN §3.6) ------------------

LANE_KEEP_DAYS = 7  # a lane's files outlive it by a week; the next lane start prunes them
LANE_EVERY = 3.0  # seconds: at most one compact line — and one record write — this often
LANE_EXEC_CHARS = 100
LANE_SAID_CHARS = 120
LANE_SCOPE_CHARS = 48
LANE_KILL_GRACE = 5.0  # seconds codex gets after a forwarded signal before its group is killed
LANE_DRAIN = 2.0  # seconds stderr may stay open after codex exits (a child it left behind)
LANE_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
LANE_STATUSLINE = ".statusline"  # the pointer the plugin's subagentStatusLine follows (§3.6)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# codex runs every command through the user's shell and prints it on the line after an `exec`
# header as `/bin/zsh -lc '<command>' in <cwd>` — 1,657 of 1,657 in sixteen real lane logs
# (0.156.1). Only a line of that shape counts as a command, so a bare `exec` inside some
# command's output never does.
CODEX_EXEC_RE = re.compile(r"^(?:\S*/)?(?:ba|z|da|k)?sh -l?c (['\"])(.*?)(?:\1 in [/~].*)?$")
SENTENCE_RE = re.compile(r"^(.+?[.!?。！？])(?=\s|$)")


def lane_elapsed(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def unprompt(text):
    """A compact line that cannot read as an interactive prompt.

    Claude Code wakes main when a background shell has not grown for 45s and its last line
    looks like a prompt: `(y/n)`, `[y/n]`, `(yes/no)`, `Do you|Would you|Shall I|Are you
    sure|Ready to … ?` at the end, `Press any key|Enter` (anywhere, `express any key` too),
    `Continue?` or `Overwrite?` — seven patterns, read from the 2.1.281 binary. A lane
    thinking for a minute after `said: Shall I start with the parser?` would wake main for
    nothing. Each shape needs a literal `?`, the plain space after `press` or the plain slash
    in `y/n`, so exactly those are swapped for look-alikes — a person reads the same line."""
    text = text.replace("?", "\uff1f")
    text = re.sub(r"(?i)(press) (any key|enter)", "\\1\u00a0\\2", text)
    return re.sub(r"(?i)\b(y|yes)/(n|no)\b", "\\1\u2215\\2", text)


def codex_command(line):
    """The command on the line after an `exec` header, or None when the line has not that shape."""
    m = CODEX_EXEC_RE.match(line)
    return None if m is None else one_line(m.group(2), LANE_EXEC_CHARS)


def first_sentence(text):
    s = one_line(text)
    m = SENTENCE_RE.match(s)
    return one_line(m.group(1) if m else s, LANE_SAID_CHARS)


def lanes_dir(hp):
    """.hippo/lanes/, with files a week old pruned — a new lane start is the one moment this
    directory is written anyway, so no schedule is needed (§4) — and the pointer the plugin's
    subagentStatusLine follows kept current: Claude Code substitutes no ${CLAUDE_PLUGIN_ROOT}
    in a plugin's settings.json (measured, 2.1.281), and the wrapper is the one process that
    knows where this plugin lives. Called once per wrapper run: batch lanes share it."""
    d = hp / "lanes"
    d.mkdir(exist_ok=True)
    cutoff = time.time() - LANE_KEEP_DAYS * 86400
    for p in d.iterdir():
        try:
            if p.name != LANE_STATUSLINE and p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass  # a directory, or a concurrent prune got there first
    ptr, script = d / LANE_STATUSLINE, str(SCRIPTS / "lane_status.py")
    if _read_text(ptr) != script:
        # Lanes started in the same instant rewrite it through one tmp file, and every loser's
        # rename fails (measured: 2 of 3 simultaneous starts). The winner wrote the same path,
        # so only a pointer still not ours makes a sound — never the lane's record.
        try:
            write_durable(ptr, script)
        except OSError as err:
            if _read_text(ptr) != script:
                print(f"dispatch: {ptr} not written ({err}) — agent-panel rows keep their "
                      "default", file=sys.stderr)
    return d


def lanes_dir_or_note(hp):
    """lanes_dir, or None with one stderr line: an unwritable .hippo/lanes/ costs the lane its
    record, never its launch."""
    try:
        return lanes_dir(hp)
    except OSError as err:
        print(f"dispatch: .hippo/lanes/ is not writable ({err}) — no lane record; the raw log "
              "goes to a temp file", file=sys.stderr)
        return None


class Lane:
    """A codex lane as its wrapper sees it (§3.6). It reads codex's raw stderr line by line and
    turns it into two things: the compact event stream on the wrapper's stderr (one line per
    command and per agent message, at most one per LANE_EVERY), and the record in
    .hippo/lanes/<id>.json — the few facts the rollout cannot give back cheaply. It also owns
    the lane's signal: codex runs in a session of its own, so a signal reaches it only here."""

    def __init__(self, did, scope, exec_, record, log, report, out=None):
        self.path = record  # None: no .hippo/, no record — the stream still runs
        self.out = out or sys.stderr
        self.label = one_line(scope, LANE_SCOPE_CHARS)
        self.t0 = time.monotonic()
        self.rec = {"id": did, "scope": scope, "exec": exec_, "pid": os.getpid(), "pgid": None,
                    "started": now_iso(), "codex_session": None, "cmds": 0, "last": None,
                    "last_at": None, "log": str(Path(log).absolute()),
                    "report": str(Path(report).absolute()) if report else None}
        self.session_id = self.model = ""
        self.footer_total = None
        self.tail = collections.deque(maxlen=400)  # what triage reads of stderr — never more
        self.signal = None
        self._signal_at = None
        self._forced = False
        self._prev = ""
        self._header = None
        self._pending = None
        self._said = float("-inf")
        self._write_failed = False
        self.write()

    def launched(self, pgid):
        self.rec["pgid"] = pgid
        if self.signal is not None:  # the signal came before the group existed
            self.kill(self.signal)
        self.write()

    def feed(self, line):
        """One raw stderr line: the banner facts usage collection has always read, then the
        two event shapes."""
        self.tail.append(line)
        s = ANSI_RE.sub("", line).strip()
        if not self.session_id and s.startswith("session id:"):
            self.session_id = s.split(":", 1)[1].strip()
            self.rec["codex_session"] = self.session_id
            self._event(f"started: {self.model or 'codex'} · session {self.session_id}")
        elif not self.model and s.startswith("model:"):
            self.model = s.split(":", 1)[1].strip()
        elif self._prev == "tokens used":
            try:
                self.footer_total = int(s.replace(",", ""))
            except ValueError:
                pass
        header, self._header = self._header, None
        if s in ("exec", "codex"):
            self._header = s
        elif header == "exec":
            cmd = codex_command(s)
            if cmd is not None:
                self.rec["cmds"] += 1
                self._event(f"exec: {cmd}")
        elif header == "codex":
            if s:
                self._event(f"said: {first_sentence(s)}")
            else:
                self._header = header  # the message starts on the next non-blank line
        self._prev = s

    def _event(self, text):
        self.rec["last"], self.rec["last_at"] = text, now_iso()
        self._pending = text
        self.tick()

    def say(self, text):
        """One line of the compact stream, never shaped like a prompt."""
        line = f"lane {self.label} · {lane_elapsed(time.monotonic() - self.t0)} · {text}"
        self.out.write(unprompt(line) + "\n")
        self.out.flush()

    def tick(self):
        """Emit the newest pending event and write the record, when the cadence allows — and a
        stop at once, whatever the cadence (see stop)."""
        stopped = self.signal is not None and "status" not in self.rec
        if stopped:
            self.rec.update(status="killed", signal=signal.Signals(self.signal).name)
        if self._pending is not None and time.monotonic() - self._said >= LANE_EVERY:
            self._said = time.monotonic()
            self.say(self._pending)
            self._pending = None
            self.write()
        elif stopped:
            self.write()

    def write(self):
        if self.path is None:
            return
        try:
            write_durable(self.path, json.dumps(self.rec, ensure_ascii=False))
        except OSError as err:
            if not self._write_failed:  # a lost record makes a sound, once
                self._write_failed = True
                print(f"dispatch: lane record not written ({err}) — the lane runs on",
                      file=sys.stderr)

    def stop(self, signum):
        """A signal to the wrapper: remembered, and forwarded to codex's process group. The
        record learns of it on pump_lane's next wake (≤0.5s), not when codex has exited: Claude
        Code SIGKILLs the tree 1.5s after its SIGTERM, and a codex slower than that to exit left
        no status at all (measured, a stub taking 3s). Not written from here: a handler that
        interrupts a write in progress would rewrite the same tmp file under it. rc follows
        in finish, when there is one."""
        if self.signal is None:
            self.signal, self._signal_at = signum, time.monotonic()
        self.kill(signum)

    def kill(self, signum):
        if self.rec["pgid"]:
            try:
                os.killpg(self.rec["pgid"], signum)
            except (ProcessLookupError, PermissionError):
                pass

    def overdue(self):
        """True once: codex outlived a forwarded signal by LANE_KILL_GRACE."""
        if self._signal_at is None or self._forced:
            return False
        if time.monotonic() - self._signal_at < LANE_KILL_GRACE:
            return False
        self._forced = True
        return True

    def finish(self, rc, status):
        """Status and rc go on record before anything slow — usage, the judge: Claude Code's own
        kill SIGKILLs the whole tree 1.5s after its SIGTERM (measured, 2.1.281)."""
        if self._pending is not None:
            self.say(self._pending)
            self._pending = None
        self.rec.update({"status": status, "rc": rc})
        if self.signal is not None:
            self.rec["signal"] = signal.Signals(self.signal).name
            self.kill(signal.SIGKILL)  # a stopped lane leaves nothing of its group behind
        self.write()

    def close(self, triage_line=None):
        """`ended` is written last, so a reader that sees it has every final line."""
        if triage_line:
            self.rec["triage"] = triage_line
        self.rec["ended"] = now_iso()
        self.write()


def pump_lane(child, lane, raw, deadline=None):
    """Read codex's stderr to its end — every line into the raw log (bytes, unmodified), every
    line through the lane's parser — while the compact stream keeps its cadence through
    codex's silences: a reader thread feeds a queue and this loop wakes twice a second. Stops
    LANE_DRAIN after codex exits even if a child it left behind still holds stderr open — and
    keeps writing to it: a cutoff checked only on a silent poll never fired for a child that
    logs every 0.2s, and held a batch lane until its timeout killed it (measured). What codex
    itself left in the pipe is one buffer, read in milliseconds.
    Returns whether `deadline` (monotonic) had to kill the lane."""
    lines = queue.Queue()

    def read():
        for line in iter(child.stderr.readline, b""):
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    timed_out, exited_at = False, None
    while True:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            line = b""
        if line is None:
            return timed_out
        if line:
            raw.write(line)
            lane.feed(line.decode("utf-8", errors="replace"))
        lane.tick()
        now = time.monotonic()
        if deadline is not None and not timed_out and now > deadline:
            timed_out = True
            lane.kill(signal.SIGKILL)
        if lane.overdue():
            lane.kill(signal.SIGKILL)
        if exited_at is None and child.poll() is not None:
            exited_at = now
        if exited_at is not None and now - exited_at > LANE_DRAIN:
            return timed_out


def tee_stdout(src, copy):
    """codex's stdout, passed through byte for byte and kept in `copy`: the agent's final
    message is the lane's report, and a lane launched through hippo:lane has no other reader
    for it. A reader of ours that went away costs the pass-through, never the copy."""
    dst = sys.stdout.buffer
    alive = True
    while True:
        chunk = src.read1(65536)
        if not chunk:
            return
        if alive:
            try:
                dst.write(chunk)
                dst.flush()
            except (BrokenPipeError, OSError):
                alive = False
        if copy is not None:
            try:
                copy.write(chunk)
                copy.flush()
            except (ValueError, OSError):  # closed under us: codex exited, a child of it did not
                return


def run_dispatch(argv):
    """DESIGN §3.6. A failed record never blocks the launch — this surface's real job is running
    codex and the ledger is a side effect. But a lost record always makes a sound."""
    # After a `--` every token belongs to codex — a --batch there is not ours to read.
    head = argv[: argv.index("--")] if "--" in argv else argv
    if "--batch" in head:
        return run_batch(argv)
    if "--watch" in head:
        return run_watch(argv)
    kind, scope, task, depth, fast, rest = split_dispatch_argv(argv)
    if fast:
        # Prepended, so a caller's own -c service_tier=… later in argv still wins (codex takes
        # the last -c for a key). exec_label never reads it: the tier is a launch condition,
        # not a routing identity, and the exec axis stays codex/model/effort.
        rest = ["-c", 'service_tier="fast"', *rest]
    did = "d" + os.urandom(16).hex()
    # A launch from inside a lane is a child: record who spawned it (§9.5 — an unintended
    # depth-2 becomes an event in the ledger, not a prohibition nobody can check).
    parent = os.environ.get("HIPPO_DISPATCH", "")
    hp = find_hippo()
    check_fanout(hp, parent, exec_label(rest).split("/")[1])
    bad = "no .hippo/"
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
    # The lane's files (§3.6): codex's raw stderr, its stdout (the report) and the record,
    # written before the id is printed so a watcher never races the record into existence.
    # With no .hippo/ there is no record and the raw log goes to a temp file the last line names.
    ld = lanes_dir_or_note(hp) if hp is not None else None
    if ld is not None:
        log, report_copy, record = ld / f"{did}.log", ld / f"{did}.out", ld / f"{did}.json"
    else:
        fd, name = tempfile.mkstemp(prefix=f"hippo-{did}-", suffix=".log")
        os.close(fd)
        log, report_copy, record = Path(name), None, None
    lane = Lane(did, scope, exec_label(rest), record, log, report_copy)
    print(f"dispatch:{did}", flush=True)
    # The lane inherits its own dispatch id (§9.2): every hippo write it makes arrives as
    # src=executor, and `log outcome` needs no --ref. Set even when the record failed — the
    # id was printed and is the lane's name either way. HIPPO_DEPTH indexes the
    # re-delegation clause the lane's capsule will carry (§9.5).
    os.environ["HIPPO_DISPATCH"] = did
    os.environ["HIPPO_DEPTH"] = str(depth)
    if hp is not None:
        os.environ["HIPPO_DIR"] = str(hp)
    os.environ["PATH"] = lane_path()
    # The judge, when there is one (§3.6): notes about the brief before the launch, and the
    # lane's final message — captured to a file, so stdout stays untouched — for triage after.
    on = jev_backend(hp) != "off"
    brief, report, own_report = None, None, False
    if on:
        brief = prompt_of(rest)
        if brief is not None:
            dispatch_launch_notes(hp, kind, scope, brief, rest)
        report = output_last_message(rest)
        if report is None:
            fd, name = tempfile.mkstemp(prefix="hippo-report-", suffix=".txt")
            os.close(fd)
            # Prepended like --fast: an exec-level option, ahead of any subcommand codex takes.
            report, own_report, rest = Path(name), True, ["--output-last-message", name, *rest]
    # A pass-through, not an exec (§9.6): the wrapper stays alive to observe the lane. codex's
    # banner, per-command trace and "tokens used" footer ride *stderr* (measured, 0.144.6 and
    # 0.156.1) and run to megabytes, so stderr goes whole to the raw log and the shell gets the
    # compact stream instead; stdout passes through byte for byte and is kept as the report.
    # The wrapper interprets nothing bound for codex. stdin closed: left open, codex exec
    # blocks. A session of its own, so the lane's signal is the wrapper's to forward — from
    # here on, not before: a signal during the launch notes takes its default action, rather
    # than being swallowed while codex starts anyway only to be killed.
    for s in LANE_SIGNALS:
        signal.signal(s, lambda signum, _frame: lane.stop(signum))
    try:
        child = subprocess.Popen(
            ["codex", "exec", *rest], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as err:
        lane.finish(127, "exited")
        lane.close()
        die(f"dispatch: could not run codex: {err}", 127)
    lane.launched(child.pid)
    with contextlib.ExitStack() as files:
        copy = files.enter_context(report_copy.open("wb")) if report_copy else None
        tee = threading.Thread(target=tee_stdout, args=(child.stdout, copy), daemon=True)
        tee.start()
        pump_lane(child, lane, files.enter_context(log.open("wb")))
        rc = child.wait()
        tee.join(timeout=LANE_DRAIN)
    status = "killed" if lane.signal is not None else "exited"
    lane.finish(rc, status)
    usage = collect_usage(lane.session_id, lane.model, lane.footer_total)
    if bad is None and usage is not None:
        append_event(hp, {"ev": "usage", "ref": did, **usage}, src="wrapper")
    how = f"killed by {lane.rec['signal']}" if lane.signal is not None else "exited"
    tokens = f" · {usage['tokens']:,} tokens" if usage else ""
    lane.say(f"{how} rc={rc} · {lane.rec['cmds']} cmds{tokens} · raw log {log}")
    line = None
    if on:
        ex = {"rc": rc, "check_rc": None}
        state = triage_state(scope, kind, brief, ex,
                             executor_claims(read_ledger(hp)).get(did) if hp else None,
                             _read_text(report) or None, strip_codex_noise("".join(lane.tail)),
                             None, lane_dir(rest, Path.cwd()))
        t = triage(hp, state, ex, did if bad is None else None)
        line = triage_line(t) if t["route"] else None
        print(f"dispatch: {line}" if line else
              f"dispatch: no triage — the judge did not answer ({t['jev']['reason']})",
              file=sys.stderr)
        if own_report:
            report.unlink(missing_ok=True)
    lane.close(line)
    sys.exit(128 + lane.signal if lane.signal is not None else rc)


WATCH_FOR = 540  # seconds: under the Bash tool's 600s ceiling, so one call never outlives it
WATCH_POLL = 1.0
WATCH_RUNNING = 3  # exit status while the lane runs on; 0 once it ended, 2 for no such lane
WATCH_USAGE = "usage: hippo dispatch --watch <dispatch-id> [--for SECONDS]"


def parse_watch_argv(argv):
    did, secs, i = None, float(WATCH_FOR), 0
    while i < len(argv):
        a = argv[i]
        if a in ("--watch", "--for") and i + 1 < len(argv):
            val, i = argv[i + 1], i + 2
        elif a.split("=", 1)[0] in ("--watch", "--for") and "=" in a:
            a, val = a.split("=", 1)
            i += 1
        else:
            die(f"dispatch --watch: unexpected argument {a!r}\n{WATCH_USAGE}", 2)
        if a == "--watch":
            did = val.removeprefix("dispatch:")
            continue
        try:
            secs = float(val)
        except ValueError:
            secs = -1.0
        if secs < 0:
            die(f"dispatch --watch: --for takes seconds, 0 or more: {val!r}\n{WATCH_USAGE}", 2)
    if not did or not re.fullmatch(r"[A-Za-z0-9_-]+", did):
        die(f"dispatch --watch: a dispatch id is required, as printed on `dispatch:<id>`\n"
            f"{WATCH_USAGE}", 2)
    return did, secs


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def watch_state(rec):
    """ended | running | lost. `ended` is the wrapper's last write (Lane.close), and a wrapper
    that died after recording its status — SIGKILLed before codex exited, or while the judge
    was still reading — has ended too; one that recorded nothing and is gone is lost."""
    if rec.get("ended"):
        return "ended"
    if pid_alive(rec.get("pid")):
        return "running"
    return "ended" if rec.get("status") else "lost"


def _iso_seconds(a, b=None):
    try:
        t0 = datetime.strptime(a, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        t1 = (datetime.strptime(b, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
              if b else datetime.now(timezone.utc))
    except (TypeError, ValueError):
        return 0
    return (t1 - t0).total_seconds()


def watch_lines(rec, state):
    """What `--watch` prints: one line while the lane runs, its final lines once it ended."""
    did, cmds = rec.get("id"), rec.get("cmds", 0)
    last = rec.get("last") or "no event yet"
    if state == "running":
        took = lane_elapsed(_iso_seconds(rec.get("started")))
        return [f"lane {did} running · {took} · {cmds} cmds · last: {last}"]
    if state == "lost":
        return [f"lane {did} lost — its wrapper (pid {rec.get('pid')}) is gone and recorded no "
                f"end · {cmds} cmds · last: {last}", f"raw log: {rec.get('log')}"]
    took = lane_elapsed(_iso_seconds(rec.get("started"), rec.get("ended") or rec.get("last_at")))
    how = (f"killed by {rec['signal']}" if rec.get("signal") else
           "timed out" if rec.get("timed_out") else rec.get("status", "ended"))
    rc = "" if rec.get("rc") is None else f" rc={rec['rc']}"  # none: SIGKILLed before codex exited
    lines = [f"lane {did} {how}{rc} after {took} · {cmds} cmds · {rec.get('scope')}"]
    if rec.get("triage"):
        lines.append(rec["triage"])
    report = rec.get("report")
    if report and (_read_text(Path(report)) or "").strip():
        lines.append(f"report: {report}")
    else:
        lines.append("report: none — the lane printed no final message")
    lines.append(f"raw log: {rec.get('log')}")
    return lines


def run_watch(argv):
    """`hippo dispatch --watch <id> [--for SECONDS]` (§3.6): block until the lane's record says
    it ended or SECONDS pass, then print its state. Reads .hippo/lanes/ only and writes
    nothing, so the hippo:lane agent can loop it in the foreground — a blocking call is what
    keeps that agent's turn open, and an open turn is one final notification instead of an
    interim one plus an extra wake-up (measured)."""
    did, secs = parse_watch_argv(argv)
    hp = find_hippo()
    if hp is None:
        die("dispatch --watch: no .hippo/ found from here — lane records live in .hippo/lanes/", 2)
    path = hp / "lanes" / f"{did}.json"
    deadline = time.monotonic() + secs
    while True:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            die(f"dispatch --watch: no lane record {path}", 2)
        except (OSError, json.JSONDecodeError):
            rec = None  # replaced whole on every write, so this is a passing moment
        state = watch_state(rec) if isinstance(rec, dict) else "running"
        if state != "running":
            # The wrapper exits right after its last write; waiting for that lets the shell that
            # ran it finish inside this call, not after the caller's turn.
            gone_by = time.monotonic() + LANE_DRAIN
            while pid_alive(rec.get("pid")) and time.monotonic() < gone_by:
                time.sleep(0.1)
            print("\n".join(watch_lines(rec, state)))
            sys.exit(0)
        left = deadline - time.monotonic()
        if left <= 0:
            if isinstance(rec, dict):
                print(watch_lines(rec, state)[0])
            sys.exit(WATCH_RUNNING)
        time.sleep(min(WATCH_POLL, left))


def prompt_of(rest):
    """The prompt, read where codex's grammar puts it — last — or None when the last token is
    not one. Short or flag-shaped, it is something else, and a judgment about the wrong text is
    worse than none."""
    last = rest[-1] if rest else ""
    return last if not last.startswith("-") and len(last) > 40 else None


def output_last_message(rest):
    """The caller's own `--output-last-message` / `-o` file, or None when there is none."""
    for i, a in enumerate(rest):
        if a == "--":
            break
        if a in ("-o", "--output-last-message") and i + 1 < len(rest):
            return Path(rest[i + 1])
        if a.startswith("--output-last-message="):
            return Path(a.split("=", 1)[1])
    return None


def dispatch_launch_notes(hp, kind, scope, brief, rest):
    """The plan's two readings of one brief, as stderr notes before the launch: a routed tier
    two steps from what the difficulty demands, and a clash with a directive the lane will
    obey. Notes only — the launch goes ahead whatever they say."""
    ans, _ = route_brief(hp, scope, kind, brief)
    scores = plan_scores(ans)
    _, model, effort = exec_label(rest).split("/")
    if scores is not None:
        prices = load_prices()
        note = tier_note(scores, jev_policy("route"), model, effort,
                         price_ladder(prices), prices, verb="launched on")
        if note:
            print(f"dispatch: note — this brief {note}", file=sys.stderr)
    for note in brief_conflicts(hp, brief, executor_directives(hp)):
        print(f"dispatch: note — {note}", file=sys.stderr)


# --- batch dispatch (DESIGN §3.6) -----------------------------------------------

BATCH_USAGE = (
    "usage: hippo dispatch --batch <manifest.yaml> [--dry-run]\n"
    "       the manifest is per-batch data, authored fresh like a brief — never standing config\n"
    "       the journal beside it decides what a run does: none yet → launch every entry; some\n"
    "       entries unfinished → relaunch those a relaunch could clear; all done → launch\n"
    "       nothing. Every run ends with the harvest table. To start over, delete\n"
    "       <manifest>.journal.jsonl\n"
    "       --dry-run launches nothing: it prints the plan (difficulty, suggested exec, notes)"
)
# Retired in 1.14.0 with zero measured calls across 28 projects (DESIGN §4): named so a caller
# typing one learns what replaced it instead of reading a bare usage line.
RETIRED_BATCH_FLAGS = ("--harvest", "--plan", "--resume", "--fresh", "--concurrency", "--causes")

# Everything a manifest entry may set, with the built-in value where one exists. kind and
# model have no default on purpose: they are the axes PRIORS routes on, so the author chooses.
# cwd is the child's working directory and its check's; a lane may name its worktree with
# codex's own `-C` in args instead.
MANIFEST_DEFAULTS = {"kind": None, "executor": "codex", "model": None, "effort": "medium",
                     "depth": 0, "task": "", "args": [], "briefs": [], "check": "",
                     "timeout": 3600, "cwd": ""}
ENTRY_KEYS = ("id", "scope", "brief", "prompt", "vars")
BATCH_EXECUTORS = ("codex",)  # the adapters that exist — not the ledger vocabulary
# Named so a manifest written for the retired adapter learns what replaced it (DESIGN §4):
# ~28k tokens of fixed cache creation per `claude -p` call, and 3 wrapper rows ever.
RETIRED_CLAUDE = ("executor claude retired in 1.15.0 (claude -p lanes cost ~28k tokens of "
                  "fixed cache creation per call): delegate Claude work with the Agent tool — "
                  "hippo records it at the end of the turn")
CHECK_TIMEOUT = 600
# The ids double as journal keys and <id>.out/.err filenames, so a path-shaped id must not
# validate — the slug alphabet plus the separators an author would reasonably type — and an
# unbounded one must not either: past the filesystem's 255-byte name cap the launch OSErrors
# mid-batch, after the dispatch row already landed.
ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ENTRY_ID_MAX = 100

# append_event was written for one writer per process; batch is the one multi-writer surface.
# One lock for ledger and journal both, reentrant on purpose: each file must interleave in
# whole lines, AND the breaker's admit-then-record region re-acquires it through the append
# helpers — RLock keeps it one lock serving both duties.
BATCH_LOCK = threading.RLock()


def parse_batch_argv(argv):
    """→ (manifest, dry_run). The whole surface: what a run does is read off the journal."""
    manifest, dry_run = None, False
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if a == "--batch":
            if i + 1 >= n:
                die(f"dispatch --batch: --batch has no value\n{BATCH_USAGE}", 2)
            manifest, i = argv[i + 1], i + 2
        elif a == "--dry-run":
            dry_run, i = True, i + 1
        elif a.split("=", 1)[0] in RETIRED_BATCH_FLAGS:
            die(f"dispatch --batch: {a.split('=', 1)[0]} was retired in 1.14.0 — the journal decides what a run "
                f"does, and concurrency is the manifest key\n{BATCH_USAGE}", 2)
        else:
            die(BATCH_USAGE, 2)
    if not manifest:
        die(BATCH_USAGE, 2)
    return manifest, dry_run


def load_manifest(mp, plan=False):
    """(concurrency, fully-resolved entries). Fail-closed and total: every problem is
    collected, then one die — a manifest that half-validates must not launch half a batch.

    With `plan`, `model` and `effort` may be left unset: the plan pass is about to suggest them
    (a dry run, or a launch with the judge on). Everything else validates exactly as it does
    for a launch."""
    if not mp.is_file():
        die(f"dispatch --batch: no such manifest: {mp}", 2)
    try:
        data = yaml.safe_load(mp.read_text(encoding="utf-8"))
    except yaml.YAMLError as ex:
        die(f"dispatch --batch: manifest parse failed: {ex}", 2)
    if not isinstance(data, dict):
        die(f"dispatch --batch: manifest must be a YAML mapping: {mp}", 2)
    problems = []
    # YAML keys need not be strings (an int, or `on:` read as a bool) — sort by repr so a
    # mixed-type key set still joins the problem list instead of raising TypeError.
    for k in sorted(set(data) - {"concurrency", "defaults", "entries"}, key=repr):
        problems.append(f"unknown top-level key: {k}")
    conc = data.get("concurrency", 8)
    if isinstance(conc, bool) or not isinstance(conc, int) or conc < 1:
        problems.append(f"concurrency must be a positive integer: {conc!r}")
        conc = 8
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        problems.append("defaults must be a mapping")
        defaults = {}
    for k in sorted(set(defaults) - set(MANIFEST_DEFAULTS), key=repr):
        problems.append(f"defaults: unknown key: {k}")
    base = {**MANIFEST_DEFAULTS,
            **{k: v for k, v in defaults.items() if k in MANIFEST_DEFAULTS}}
    raw = data.get("entries")
    if not isinstance(raw, list) or not raw:
        problems.append("entries must be a non-empty list")
        raw = []

    def text_of(path, where, what):
        p = Path(path)  # relative paths resolve from the invocation cwd, like `$(cat …)`
        if not p.is_file():
            problems.append(f"{where}: {what} file not found: {path}")
            return ""
        try:
            return p.read_text(encoding="utf-8")
        except OSError as ex:
            problems.append(f"{where}: {what} file unreadable: {path} ({ex})")
            return ""

    entries, seen, retired = [], set(), False
    for i, en in enumerate(raw):
        where = f"entry {i + 1}"
        if not isinstance(en, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        scope = en.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            problems.append(f"{where}: scope is required")
            scope = ""
        eid = en.get("id")
        if eid is None:
            eid = re.sub(r"[^a-z0-9]+", "-", scope.lower()).strip("-")
            if scope and not eid:
                problems.append(f"{where}: scope slugs to nothing — an explicit id is required")
        elif not isinstance(eid, str) or not ENTRY_ID_RE.match(eid):
            problems.append(f"{where}: id must be [A-Za-z0-9._-] and start with an "
                            f"alphanumeric: {eid!r}")
            eid = ""
        if len(eid) > ENTRY_ID_MAX:
            problems.append(f"{where}: id must be at most {ENTRY_ID_MAX} chars "
                            f"(ids become filenames): {eid[:40]!r}…")
            eid = ""
        if eid:
            where = f"entry {i + 1} ({eid})"
            if eid in seen:
                problems.append(f"{where}: duplicate id")
            seen.add(eid)
        for k in sorted(set(en) - set(MANIFEST_DEFAULTS) - set(ENTRY_KEYS), key=repr):
            problems.append(f"{where}: unknown key: {k}")
        cfg = {**base, **{k: v for k, v in en.items() if k in MANIFEST_DEFAULTS}}
        # Unset is exactly None, so `model: ""` is still the error it is for a launch.
        unset = {f for f in ("model", "effort") if plan and cfg[f] is None}
        for f in ("kind", "model"):
            if f not in unset and (not isinstance(cfg[f], str) or not cfg[f].strip()):
                problems.append(f"{where}: {f} is required (in defaults or the entry)")
        if cfg["executor"] == "claude":
            retired = True  # one line for the whole manifest, however many entries name it
        elif cfg["executor"] not in BATCH_EXECUTORS:
            problems.append(f"{where}: executor must be {'|'.join(BATCH_EXECUTORS)} "
                            f"(the adapters that exist): {cfg['executor']!r}")
        if "effort" not in unset and (
                not isinstance(cfg["effort"], str) or not cfg["effort"].strip()):
            problems.append(f"{where}: effort must be a non-empty string: {cfg['effort']!r}")
        for f in ("depth", "timeout"):
            if isinstance(cfg[f], bool) or not isinstance(cfg[f], int):
                problems.append(f"{where}: {f} must be an integer: {cfg[f]!r}")
        for f in ("task", "check", "cwd"):
            if not isinstance(cfg[f], str):
                problems.append(f"{where}: {f} must be a string: {cfg[f]!r}")
                cfg[f] = ""
        args = cfg["args"]
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            problems.append(f"{where}: args must be a list of strings")
            args = []
        briefs = cfg["briefs"]
        if not isinstance(briefs, list) or any(not isinstance(b, str) for b in briefs):
            problems.append(f"{where}: briefs must be a list of file paths")
            briefs = []
        parts = [text_of(b, where, "briefs") for b in briefs]
        brief = en.get("brief")
        if brief is not None:
            if not isinstance(brief, str):
                problems.append(f"{where}: brief must be a file path")
            else:
                parts.append(text_of(brief, where, "brief"))
        ptext = en.get("prompt")
        if ptext is not None and not isinstance(ptext, str):
            problems.append(f"{where}: prompt must be a string")
            ptext = None
        if brief is None and ptext is None:
            problems.append(f"{where}: at least one of brief/prompt is required")
        if ptext is not None:
            parts.append(ptext)
        prompt = "".join(parts)
        vars_ = en.get("vars")
        if vars_ is None:
            vars_ = {}
        if not isinstance(vars_, dict) or any(
            not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, (str, int))
            for k, v in vars_.items()
        ):
            problems.append(f"{where}: vars must be a flat str -> str|int map")
            vars_ = {}
        check, cwd = cfg["check"], cfg["cwd"]
        # Literal {k} tokens only — code braces in a prompt pass through untouched.
        for k, v in vars_.items():
            prompt = prompt.replace("{" + k + "}", str(v))
            check = check.replace("{" + k + "}", str(v))
            cwd = cwd.replace("{" + k + "}", str(v))
        # Resolved now, from the invocation cwd: the worktree exists before the batch call
        # (dispatch skill §5), and a missing one would otherwise fail mid-batch as an rc 127.
        cwd = Path(cwd).resolve() if cwd else Path.cwd()
        if not cwd.is_dir():
            problems.append(f"{where}: cwd is not a directory: {cfg['cwd']}")
        entries.append({"id": eid, "scope": scope, "kind": cfg["kind"],
                        "executor": cfg["executor"], "model": cfg["model"],
                        "effort": cfg["effort"], "depth": cfg["depth"], "task": cfg["task"],
                        "args": args, "check": check, "timeout": cfg["timeout"],
                        "prompt": prompt, "cwd": str(cwd)})
    if retired:
        problems.append(RETIRED_CLAUDE)
    if problems:
        die(f"dispatch --batch: {mp}: {len(problems)} problem(s)\n"
            + "\n".join(f"  - {p}" for p in problems), 2)
    return conc, entries


def adapter_argv(en):
    """The launch shape (prompt always last, behind `--`: a brief opening with `---`
    frontmatter or `-m ` is otherwise argv, not prompt)."""
    return ["codex", "exec", "-m", en["model"], "-c",
            f"model_reasoning_effort={en['effort']}", *en["args"], "--", en["prompt"]]


def codex_usage(err_path):
    """Banner and footer land in <id>.err (measured 0.144.6: "session id: …", "model: …" and
    the two-line "tokens used" footer all ride stderr) — the same prev-line walk as
    run_dispatch, then the same rollout/footer collection."""
    try:
        text = err_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    session_id = model = ""
    footer_total = None
    prev = ""
    for line in text.splitlines():
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
    return collect_usage(session_id, model, footer_total)


def journal_state(journal):
    """Previous attempts per id, the ids that are DONE (latest exit line has rc==0 and
    check_rc null-or-0), and the latest exit and triage record per id. A relaunch mints a NEW
    dispatch id — two launches are two facts, and two triages of one lane are two facts too:
    the latest is what a reader reads, and neither replaces the other in the file."""
    attempts, latest_exit, latest_triage = {}, {}, {}
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") in ("launch", "exit") and isinstance(rec.get("attempt"), int):
            attempts[rec.get("id")] = max(attempts.get(rec.get("id"), 0), rec["attempt"])
        if rec.get("event") == "exit":
            latest_exit[rec.get("id")] = rec
        elif rec.get("event") == "triage":
            latest_triage[rec.get("id")] = rec
    done = {i for i, r in latest_exit.items()
            if r.get("rc") == 0 and r.get("check_rc") in (None, 0)}
    return attempts, done, latest_exit, latest_triage


# --- harvest triage (DESIGN §3.6 — the judge reads, main routes) --------------

TRIAGE_STDERR_TAIL = 4000
TRIAGE_GIT_LINES = 80
TRIAGE_GIT_TIMEOUT = 30
# Trim order and caps for an over-budget state, applied only as far as the budget needs.
TRIAGE_TRIM = (("stderr_tail", 1000), ("brief", 6000), ("changes", 3000))
# A structured report (a Workflow's JSON result) is fitted by cutting every string in it to one
# length. Below about a sentence per item it no longer says what the result said, so a report
# that needs a shorter cut is left whole and the judge refuses it as over budget.
TRIAGE_LEAF_MIN = 120
CLUSTER_EXCERPT_LINES = 40
CLUSTER_ERROR_RE = re.compile(r"(?i)(error|traceback|failed|exception|no such|not found)")
# codex's stderr opens with a launch banner and closes with the "tokens used" footer. Neither
# says anything about why a lane ended as it did, and accuracy drops with irrelevant material
# in the state (§3.9) — so exactly this is filtered out, and nothing else is.
CODEX_NOISE_RE = re.compile(
    r"^(?:\[[^]]*\]\s*)?(?:-{3,}$|workdir:|model:|provider:|approval:|sandbox:"
    r"|reasoning effort:|reasoning summaries:|session id:|tokens used$|OpenAI Codex v)"
)
# Reading order for the table: what needs main's eyes first, what needs them last.
ROUTE_ORDER = ("escalate", "no-go-candidate", "failed", "accept-candidate")
CAUSES = ("capability", "spec", "environment", "transient")
# The two a relaunch can actually clear. A capability or spec failure needs a different brief,
# and re-running it unchanged buys the same failure twice.
RELAUNCHABLE_CAUSES = ("transient", "environment")


def _num(v):
    """A number the judge actually returned, or None. A bool is not one."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _thr(policy, key, default):
    """A threshold from the spec's `policy` map, or the built-in. The spec is where a person
    tunes it; the comparison happens here, in code, always (§2 judge guardrails)."""
    v = _num(policy.get(key))
    return default if v is None else v


def _read_text(path):
    """A lane's output file, or None when there is none — a gap stays a gap."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def stderr_excerpt(path, limit=TRIAGE_STDERR_TAIL):
    """The tail of a lane's stderr file with the codex banner and footer dropped."""
    text = _read_text(path)
    return None if text is None else strip_codex_noise(text, limit)


def strip_codex_noise(text, limit=TRIAGE_STDERR_TAIL):
    """The tail of stderr text with the codex banner and footer dropped."""
    keep, prev = [], ""
    for ln in text.splitlines():
        noise = CODEX_NOISE_RE.match(ln.strip()) or (
            prev == "tokens used" and ln.strip().replace(",", "").isdigit())
        prev = ln.strip()
        if not noise:
            keep.append(ln)
    return "\n".join(keep)[-limit:]


def lane_dir(args, cwd):
    """Where the lane worked: `-C <worktree>` when its codex args carry one (dispatch skill §5),
    resolved the way codex resolves it — against the child's own cwd — else that cwd: the
    entry's `cwd` in a batch."""
    for i, a in enumerate(args or []):
        if a == "--":
            break
        raw = None
        if a in ("-C", "--cd") and i + 1 < len(args):
            raw = args[i + 1]
        elif a.startswith("--cd="):
            raw = a[len("--cd="):]
        elif a.startswith("-C") and len(a) > 2:
            raw = a[2:]
        if raw:
            p = Path(raw)
            return p if p.is_absolute() else Path(cwd) / p
    return Path(cwd)


def git_changes(d):
    """What the lane actually changed: `git status --short` plus `git diff --stat HEAD`, or
    None when that directory is not a git repository. A git failure never breaks a harvest —
    the point of reading the tree is to catch scope creep the report did not mention, and an
    unreadable tree is simply no evidence either way."""
    def git(*args):
        try:
            r = subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True,
                               timeout=TRIAGE_GIT_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    status = git("status", "--short")
    if status is None:
        return None
    # A repository with no commit yet has no HEAD to diff against; its status still counts.
    diff = git("diff", "--stat", "HEAD") or ""
    return "\n".join(status.splitlines()[:TRIAGE_GIT_LINES]
                     + diff.splitlines()[:TRIAGE_GIT_LINES])


def cap_leaves(v, cap):
    """`v` with every string in it longer than `cap` cut to its first `cap` characters and an
    ellipsis; keys, items and every other value as they were."""
    if isinstance(v, str):
        return v if len(v) <= cap else v[:cap] + "…"
    if isinstance(v, list):
        return [cap_leaves(x, cap) for x in v]
    if isinstance(v, dict):
        return {k: cap_leaves(x, cap) for k, x in v.items()}
    return v


def fit_triage_state(state):
    """Fit a triage state to JEV_STATE_BUDGET_CHARS, trimming in one fixed order and only as
    far as the budget needs → the list of what was trimmed. `report` goes last. A text report
    is cut from the HEAD, because a lane's summary of itself is at the end. A structured one —
    a Workflow's JSON result — keeps its shape: every key and item stays, and every string in
    it is cut to the longest length that fits, keeping the head of each, where a finding or a
    field states its point. Nothing is shortened silently: what was cut is named in the triage
    record, and an oversize state that no trim rescues reaches `judge` and comes back as the
    over-budget failure it is (§3.9)."""
    def size():
        return len(json.dumps(state, ensure_ascii=False))

    trimmed = []
    for key, cap in TRIAGE_TRIM:
        if size() <= JEV_STATE_BUDGET_CHARS:
            return trimmed
        v = state.get(key)
        if isinstance(v, str) and len(v) > cap:
            state[key] = v[:cap]
            trimmed.append(key)
    report = state.get("report")
    if size() <= JEV_STATE_BUDGET_CHARS or not report:
        return trimmed
    if isinstance(report, str):
        trimmed.append("report")
        while state["report"] and size() > JEV_STATE_BUDGET_CHARS:
            state["report"] = state["report"][size() - JEV_STATE_BUDGET_CHARS + 512:]
        return trimmed
    # The longest cut that fits, by bisection; no string is longer than the whole report.
    low, high = TRIAGE_LEAF_MIN, len(json.dumps(report, ensure_ascii=False))
    state["report"] = cap_leaves(report, low)
    if size() > JEV_STATE_BUDGET_CHARS:
        state["report"] = report
        return trimmed
    while low < high:
        mid = (low + high + 1) // 2
        state["report"] = cap_leaves(report, mid)
        if size() <= JEV_STATE_BUDGET_CHARS:
            low = mid
        else:
            high = mid - 1
    state["report"] = cap_leaves(report, low)
    trimmed.append("report")
    return trimmed


def triage_state(scope, kind, brief, ex, claim, report, stderr_tail, check_output, workdir):
    """One finished lane, whole, as the judge receives it.

    Large context is the instrument's point (§3.9): the entire report, the entire brief and
    the entire check output go in. Code filters only what is irrelevant — codex's banner
    noise — and never shrinks what is not. `workdir=None` reads no tree: a native run's
    changes come from its own transcript instead (§3.5.3c), because main's checkout also holds
    main's work."""
    return {
        "scope": scope,
        "kind": kind,
        "brief": brief,
        "exit": {"rc": ex.get("rc"), "check_rc": ex.get("check_rc"),
                 "timed_out": bool(ex.get("timed_out"))},
        "claim": claim,
        "report": report,
        "stderr_tail": stderr_tail,
        "check_output": check_output,
        "changes": None if workdir is None else git_changes(workdir),
    }


def triage_answers(questions, answers):
    """The reply compacted to what the journal keeps: a noul is its probability, a choice and
    a score keep their value and their confidence. An answer whose shape is not what the
    question asked for lands as null — the reader sees the gap, never an invented number."""
    out = {}
    for qid, q in questions.items():
        a, kind, val = answers.get(qid), q.get("type"), None
        if isinstance(a, dict):
            if kind == "noul":
                val = _num(a.get("noul"))
            elif kind == "choice" and isinstance(a.get("choice"), str):
                val = {"choice": a["choice"], "confidence": _num(a.get("confidence"))}
            elif kind == "score" and _num(a.get("score")) is not None:
                val = {"score": _num(a["score"]), "confidence": _num(a.get("confidence"))}
        out[qid] = val
    return out


def triage_route(ex, ans, policy):
    """The route and the verify hint → (route, verify). Computed here and only here: the judge
    answers, the policy decides (§2). `failed` comes from the exit codes rather than from any
    probability — a rc is a fact and outranks a judgment about one. A probability the reply
    did not carry fails its comparison, so a half-answered lane lands in `escalate`, the
    bucket that costs main one read, and never in accept."""
    def ge(v, t):
        return isinstance(v, float) and v >= t

    def le(v, t):
        return isinstance(v, float) and v <= t

    risk = ans.get("risk")
    risk = risk.get("score") if isinstance(risk, dict) else None
    evidence = ans.get("evidence")
    if ex.get("rc") != 0 or ex.get("check_rc") not in (None, 0):
        route = "failed"
    elif ge(ans.get("reports_blocked"), _thr(policy, "no_go_at", 0.7)):
        route = "no-go-candidate"
    elif (ge(ans.get("claims_done"), _thr(policy, "done_at", 0.7))
          and le(ans.get("reports_blocked"), _thr(policy, "blocked_below", 0.2))
          and le(ans.get("needs_decision"), _thr(policy, "decision_below", 0.3))
          and le(ans.get("scope_creep"), _thr(policy, "creep_below", 0.3))):
        route = "accept-candidate"
    else:
        route = "escalate"
    # A hint that this lane deserves a verification lane (dispatch skill §4), never a gate.
    verify = ge(risk, _thr(policy, "verify_risk_at", 2.0)) or (
        isinstance(evidence, float) and evidence < _thr(policy, "evidence_below", 0.3))
    return route, verify


# The ev:triage `p` map ← the harvest questions it compacts. risk is the 0-3 score.
TRIAGE_P = (("done", "claims_done"), ("blocked", "reports_blocked"), ("ask", "needs_decision"),
            ("creep", "scope_creep"), ("evidence", "evidence"))


def triage(hp, state, ex, ref, src="wrapper"):
    """One calibrated read of a whole finished lane — the one triage single dispatch, batch
    and the scribe's native runs all use (§3.6, §3.5.3c) → {answers, route, verify, trimmed,
    jev}.

    The judge reads the report so that main does not have to; what comes back is a route —
    evidence of the same standing as a check rc, never a verdict. It lands as `ev:triage` on
    the dispatch it read, stamped with whoever observed the lane's exit (the wrapper, or the
    scribe reading a native run's notification), so PRIORS can measure the judge against
    main's verdicts. A judge failure is a null route and an ok:false metering row, and no
    triage row: the gap is the record, and the lane is untouched."""
    trimmed = fit_triage_state(state)
    questions = jev_questions("harvest")
    answers, meta = judge(hp, "harvest", state, questions)
    out = {"answers": None, "route": None, "verify": None, "trimmed": trimmed, "jev": meta}
    if hp is not None:
        with BATCH_LOCK:
            append_event(hp, {"ev": "clerk", "name": "jev-harvest", "ok": meta["ok"],
                              "ms": meta["ms"], "tokens": meta["tokens"]}, src=src)
    if answers is None:
        return out
    ans = triage_answers(questions, answers)
    route, verify = triage_route(ex, ans, jev_policy("harvest"))
    out.update(answers=ans, route=route, verify=verify)
    if hp is None or not ref:
        return out
    risk = ans.get("risk") if isinstance(ans.get("risk"), dict) else {}
    p = {k: ans.get(q) for k, q in TRIAGE_P}
    p["risk"] = risk.get("score")
    e = {"ev": "triage", "ref": ref, "route": route, "verify": verify,
         "p": {k: v for k, v in p.items() if isinstance(v, float)}}
    if triage_cause(out) in CAUSES:
        e["cause"] = triage_cause(out)
    if trimmed:
        e["trimmed"] = trimmed
    with BATCH_LOCK:
        bad = validate_event(e) or check_ref(hp, e)
        if bad:
            print(f"{'native' if src == 'scribe' else 'dispatch'}: triage record failed ({bad})",
                  file=sys.stderr)
        else:
            append_event(hp, e, src=src)
    return out


def triage_line(t):
    """The route and the numbers behind it, as one stderr line reads them."""
    a = t["answers"] or {}
    nums = " · ".join(f"{label} {_pp(a.get(q))}" for label, q in
                      (("done", "claims_done"), ("blocked", "reports_blocked"),
                       ("ask", "needs_decision"), ("creep", "scope_creep")))
    return f"triage {t['route']} ({nums} · verify {'yes' if t['verify'] else 'no'})"


def lane_report(en, outdir):
    """What the lane said: its stdout, which codex keeps for the agent's own output (the
    banner and footer ride stderr)."""
    return _read_text(outdir / f"{en['id']}.out")


def triage_entry(hp, en, ex, outdir, claim, jrnl):
    """A batch lane's triage → its journal record. The files in the outdir are the lane."""
    eid = en["id"]
    state = triage_state(en.get("scope"), en.get("kind"), en.get("prompt"), ex, claim,
                         lane_report(en, outdir),
                         stderr_excerpt(outdir / f"{eid}.err"),
                         _read_text(outdir / f"{eid}.check"),
                         lane_dir(en.get("args"), en["cwd"]))
    t = triage(hp, state, ex, ex.get("dispatch"))
    rec = {"t": now_iso(), "event": "triage", "id": eid, "attempt": ex.get("attempt"),
           "dispatch": ex.get("dispatch"), **t}
    jrnl(rec)
    return rec


def triage_cause(rec):
    """The cause a triage record names, or None when there is no triage or it named none."""
    c = ((rec or {}).get("answers") or {}).get("cause")
    return c.get("choice") if isinstance(c, dict) else None


def cluster_excerpt(en, outdir):
    """What one failure looks like, in as few lines as still identify it: the tail of the
    check output when there was a check, else of stderr, plus the first line of the report
    that names an error. Short on purpose — the question asked of it is sameness, and the
    rest of a report is volume without signal for that one."""
    text = _read_text(outdir / f"{en['id']}.check")
    if text is None:
        text = stderr_excerpt(outdir / f"{en['id']}.err") or ""
    parts = ["\n".join(text.splitlines()[-CLUSTER_EXCERPT_LINES:])]
    for ln in (_read_text(outdir / f"{en['id']}.out") or "").splitlines():
        if CLUSTER_ERROR_RE.search(ln):
            parts.append(ln)
            break
    return "\n".join(p for p in parts if p.strip())


def cluster_failures(hp, failed, jrnl):
    """Greedy one-pass clustering of a batch's failures → id → cluster name (§3.6).

    Each failure is asked once against the representatives found so far, and the first one
    above `same_cause_at` takes it. One pass and a high threshold, because the reason to
    cluster is to stop paying N repair lanes for one defect (measured: 130 identical import
    failures, one missing pytest.ini) — not to find the optimal partition. A wrong merge
    hides a defect behind another one's diagnosis; a wrong split costs a second read."""
    at = _thr(jev_policy("failure-cluster"), "same_cause_at", 0.7)
    reps, members = [], []
    for item in failed:
        hit = None
        if reps:
            questions = {}
            for k in range(len(reps)):
                questions.update(jev_questions("failure-cluster", k=k))
            state = {"a": item, "reps": reps}
            answers, meta = judge(hp, "failure-cluster", state, questions)
            if hp is not None:
                with BATCH_LOCK:
                    append_event(hp, {"ev": "clerk", "name": "jev-cluster", "ok": meta["ok"],
                                      "ms": meta["ms"], "tokens": meta["tokens"]},
                                 src="wrapper")
            # A judge failure is not evidence of sameness: the entry keeps its own cluster.
            for k in range(len(reps)):
                a = (answers or {}).get(f"same_{k}")
                p = _num(a.get("noul")) if isinstance(a, dict) else None
                if p is not None and p >= at:
                    hit = k
                    break
        if hit is None:
            reps.append(item)
            members.append([item["id"]])
        else:
            members[hit].append(item["id"])
    out = {}
    for k, (rep, ids) in enumerate(zip(reps, members), 1):
        name = f"c{k}"
        out.update({i: name for i in ids})
        jrnl({"t": now_iso(), "event": "cluster", "cluster": name, "cause": rep["cause"],
              "members": ids, "excerpt": one_line(rep["excerpt"], 200)})
    return out


def _pp(v):
    """A probability as the table prints it: `.91`, or `-` when there is none."""
    return f"{v:.2f}".lstrip("0") if isinstance(v, float) else "-"


def _rel(p, cwd):
    try:
        return str(Path(p).relative_to(cwd))
    except ValueError:
        return str(p)


def check_mark(check_rc):
    return "-" if check_rc is None else ("pass" if check_rc == 0 else "fail")


def harvest_row(r, clusters, outdir, cwd):
    """One table line. The report column points where the diagnosis actually is: stderr for a
    lane that died, the check output when only the check did, the report otherwise."""
    en, ex, ans = r["en"], r["ex"], r["answers"]
    if ex.get("rc") != 0:
        ext = "err"
    elif ex.get("check_rc") not in (None, 0):
        ext = "check"
    else:
        ext = "out"
    cause = ans.get("cause") if isinstance(ans.get("cause"), dict) else {}
    if r["route"] == "failed":
        route = f"failed {clusters.get(en['id'], '-')} {cause.get('choice') or '-'}"
        numbers = f"cause {_pp(cause.get('confidence'))}"
    elif ans:
        route = r["route"] or "-"
        numbers = (f"done {_pp(ans.get('claims_done'))} "
                   f"blocked {_pp(ans.get('reports_blocked'))} "
                   f"ask {_pp(ans.get('needs_decision'))} "
                   f"creep {_pp(ans.get('scope_creep'))}")
    else:
        route, numbers = r["route"] or "-", "-"
    report = _rel(outdir / f"{en['id']}.{ext}", cwd)
    return [en["id"], str(ex.get("rc")), check_mark(ex.get("check_rc")),
            r["claim"] or "-", route, "yes" if r["verify"] else "-", numbers,
            f"→ {report}"]


# A finding starts at a bullet, a numbered item or a heading and runs to the next one — the
# shape a verification report actually has. Prose with none of them is not a list of findings,
# and splitting it on sentences would invent boundaries the lane did not write.
FINDING_START_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)]|#{1,6})\s+")
# One request carries two questions per finding. The cap is the request's size, not a judgment
# about the report: past it the first 60 are ranked and the rest are named on stderr.
FINDINGS_MAX = 60


def split_findings(report):
    """A verifier's report split into findings. Nothing is paraphrased and nothing is dropped:
    a finding is the lines from its bullet to the next one, so what gets ranked is what the
    lane wrote."""
    blocks = []
    for ln in (report or "").splitlines():
        if FINDING_START_RE.match(ln):
            blocks.append([ln])
        elif blocks:
            blocks[-1].append(ln)
    return [t for t in ("\n".join(b).strip() for b in blocks) if t]


def rank_findings(hp, en, report, attempt, jrnl):
    """A verification lane's findings, scored and sorted → the ranking (§3.6).

    A verifier is told to report everything and let the collection side filter (dispatch skill
    §4), and that filtering was a main turn per verifier. Here it is one request that scores
    each finding on its own, so main reads the top of a sorted list instead of the whole
    report. Severity and reality are separate questions on purpose: a confident style
    preference is not a blocking defect."""
    findings = split_findings(report)
    if not findings:
        return []  # a prose report has no findings to rank, and nothing is asked about it
    if len(findings) > FINDINGS_MAX:
        print(f"{en['id']}: {len(findings)} findings — ranking the first {FINDINGS_MAX}",
              file=sys.stderr)
        findings = findings[:FINDINGS_MAX]
    questions = {}
    for k in range(len(findings)):
        questions.update(jev_questions("verify", k=k))
    answers, meta = judge(hp, "verify", {"scope": en.get("scope"), "findings": findings},
                          questions)
    if hp is not None:
        with BATCH_LOCK:
            append_event(hp, {"ev": "clerk", "name": "jev-verify", "ok": meta["ok"],
                              "ms": meta["ms"], "tokens": meta["tokens"]}, src="wrapper")
    if answers is None:
        # The ok:false row is the record. An empty ranking would read as a report that found
        # nothing, which is the opposite of what happened.
        return []
    ans = triage_answers(questions, answers)
    ranked = []
    for k, text in enumerate(findings):
        sev = ans.get(f"severity_{k}")
        ranked.append({"k": k, "severity": sev.get("score") if isinstance(sev, dict) else None,
                       "real": ans.get(f"real_{k}"),
                       # Folded to one line: the head is a table cell, and the whole finding
                       # is still in the lane's own report.
                       "head": one_line(text, 100)})

    def order(f):
        # An unanswered finding sorts last: a gap is not a severity of zero.
        return (-(f["severity"] if f["severity"] is not None else -1.0),
                -(f["real"] if f["real"] is not None else -1.0))

    ranked.sort(key=order)
    jrnl({"t": now_iso(), "event": "findings", "id": en["id"], "attempt": attempt,
          "ranked": ranked})
    return ranked


def finding_line(f):
    """One ranked finding, printed under its entry's row in the harvest table."""
    sev = f"{f['severity']:.1f}" if isinstance(f["severity"], float) else "-"
    return f"  ▸ {sev} real {_pp(f['real'])}  {f['head']}"


def run_harvest(mp, entries, journal, outdir, hp, fresh=()):
    """The end of every batch run (DESIGN §3.6): read everything, launch nothing → the counts
    the run's summary line carries.

    Every exited entry is read — its latest attempt's triage when the lane exit already wrote
    one, a fresh triage when it did not (the judge was off or failed then) — the failures are
    clustered, and the result is one table main can scan instead of N reports main would have
    to open. With the judge off the deterministic half still prints: the table is a reading of
    the journal first and a reading of the judge second, and the half that needs no key must
    not vanish with it."""
    on = jev_backend(hp) != "off"
    if not on:
        print("harvest: judge off — no TYPESAFE_API_KEY; showing the deterministic part only",
              file=sys.stderr)
    cwd = Path.cwd()
    _, _, exits, triages = journal_state(journal)
    claims = executor_claims(read_ledger(hp)) if hp is not None else {}

    def jrnl(rec):
        with journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    rows, failed = [], []
    for en in entries:
        ex = exits.get(en["id"])
        if ex is None:
            continue  # never exited: there is nothing to read yet
        r = {"en": en, "ex": ex, "claim": claims.get(ex.get("dispatch")),
             "route": None, "verify": None, "answers": {}, "findings": []}
        rec = triages.get(en["id"])
        # A triage of this very attempt is reused: a second read of an unchanged lane would be
        # a second ev:triage row for one fact. One that failed is asked again — on a later
        # run, not seconds after the same judge just failed at this run's lane exit.
        reuse = rec and rec.get("attempt") == ex.get("attempt") and (
            rec.get("route") or en["id"] in fresh)
        if not reuse:
            rec = triage_entry(hp, en, ex, outdir, r["claim"], jrnl) if on else None
        if rec:
            r.update(route=rec["route"], verify=rec["verify"], answers=rec["answers"] or {})
        if on and en.get("kind") == "verify":
            r["findings"] = rank_findings(hp, en, lane_report(en, outdir),
                                          ex.get("attempt"), jrnl)
        rows.append(r)
        if r["route"] == "failed":
            failed.append({"id": en["id"], "cause": triage_cause(r),
                           "excerpt": cluster_excerpt(en, outdir)})
    clusters = cluster_failures(hp, failed, jrnl) if failed else {}

    rows.sort(key=lambda r: ROUTE_ORDER.index(r["route"])
              if r["route"] in ROUTE_ORDER else len(ROUTE_ORDER))
    head = ["id", "rc", "check", "claim", "route", "verify", "key numbers", "→ report"]
    table = [harvest_row(r, clusters, outdir, cwd) for r in rows]
    widths = [max(len(line[i]) for line in [head] + table) for i in range(len(head))]

    def fmt(line):
        return "  ".join(c.ljust(w) for c, w in zip(line, widths)).rstrip()

    ranked = any(r["findings"] for r in rows)
    top = int(_thr(jev_policy("verify"), "show_top", 5)) if ranked else 0
    print(fmt(head))
    for r, line in zip(rows, table):
        print(fmt(line))
        # A verification lane's findings ride under its own row, worst first: the table stays
        # one read, and the ranking is next to the lane it is about.
        for f in r["findings"][:top]:
            print(finding_line(f))

    counts = collections.Counter(r["route"] or "-" for r in rows)
    verdicts = mp.parent / f"{mp.stem}.verdicts.jsonl"
    vrows = [{"entry": r["en"]["id"], "attempt": r["ex"].get("attempt"), "result": "accepted",
              "note": f"triage accept-candidate: done {r['answers'].get('claims_done'):.2f}, "
                      f"check {check_mark(r['ex'].get('check_rc'))}; confirmed by main"}
             for r in rows if r["route"] == "accept-candidate"
             and isinstance(r["answers"].get("claims_done"), float)]
    if on:
        # Rewritten every harvest, never appended to: a stale row from an earlier harvest
        # would be piped into the ledger as this one's verdict.
        verdicts.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in vrows),
                            encoding="utf-8")
    if rows and on:
        print()
        print("routes: " + ", ".join(f"{k} {n}" for k, n in counts.most_common()))
    by_cluster = collections.Counter(clusters.values())
    for name in sorted(by_cluster, key=lambda n: int(n[1:])):
        # Assignment is greedy, so a cluster's first member is the representative it was
        # opened with — the excerpt every other member was judged the same as.
        rep = next(f for f in failed if clusters.get(f["id"]) == name)
        n = by_cluster[name]
        print(f"{name} · {rep['cause'] or '-'} · {n} lane{'' if n == 1 else 's'} · "
              f"{one_line(rep['excerpt'], 120)}")
    relaunchable = sorted({f["cause"] for f in failed
                           if f["cause"] in RELAUNCHABLE_CAUSES})
    if relaunchable:
        print(f"hippo dispatch --batch {_rel(mp, cwd)}  # a rerun relaunches the "
              f"{', '.join(relaunchable)} failures")
    if vrows:
        print(f"hippo log outcome --from-batch {_rel(journal, cwd)} < {_rel(verdicts, cwd)}")
    out = {"harvested": len(rows), "clusters": len(by_cluster),
           "verdicts": str(verdicts) if on else None}
    if on and rows:
        out["routes"] = dict(counts)
    return out


# --- the plan (DESIGN §3.6 — the judge measures the brief, code prices it) ------

PLAN_TIERS = ("cheap", "mid", "top")


def price_ladder(prices):
    """The codex lane's three tiers, read off the gpt rows' *distinct input prices*: the
    lowest price level is `cheap`, the highest is `top`, the second highest is `mid`. Levels,
    not rows — two generations of one model can sit at the same price (the sheet has carried
    fable-5 and fable-5-1, opus-4-8 and opus-5), and "second most expensive row" would make
    `mid` a `top` twin. Within a level the sheet's first row wins, because the sheet lists the
    current model first. A `legacy` row is no level at all: back on the sheet, gpt-5.6-sol at
    $4 would be `mid` in place of gpt-6-sol at $2. Read at call time, so a price refresh moves
    the ladder — the frozen version of this is the routing.yaml the NOT-list retired (§4)."""
    by_price = {}
    for m, v in routable_models(prices).items():
        if str(m).startswith("gpt-"):
            by_price.setdefault(float((v or {}).get("input", 0.0)), m)
    if not by_price:
        return {}
    levels = [by_price[p] for p in sorted(by_price)]
    # A sheet carrying one or two gpt levels still has three tiers: they
    # collapse onto what exists rather than naming a model that does not.
    return dict(zip(PLAN_TIERS, (levels[0], levels[max(0, len(levels) - 2)], levels[-1])))


def entry_exec(en):
    """The exec an entry already names, or None when the plan is being asked to fill it in."""
    return f"{en['executor']}/{en['model']}/{en['effort'] or '-'}" if en["model"] else None


def plan_scores(ans):
    """The three difficulty scores, or None when the reply did not carry all three. A score
    that did not come back is not a low one, so a partial reply suggests nothing at all."""
    out = []
    for qid in ("scope", "novelty", "spec"):
        a = ans.get(qid)
        v = _num(a.get("score")) if isinstance(a, dict) else None
        if v is None:
            return None
        out.append(v)
    return out


def plan_tier(scores, policy):
    """The tier this brief's difficulty demands, before any evidence. The judge scored the
    three ladders; every comparison against them happens here (§2 judge guardrails)."""
    scope, novelty, spec = scores
    if (novelty >= _thr(policy, "top_novelty", 2.0) or spec >= _thr(policy, "top_spec", 2.0)
            or scope >= _thr(policy, "top_scope", 2.5)):
        return "top"
    if novelty >= _thr(policy, "mid_novelty", 1.0) or scope >= _thr(policy, "mid_scope", 1.5):
        return "mid"
    return "cheap"


def plan_kinds():
    """The kind vocabulary, read from the spec's own `kind_fit` criteria. The scribe's table is
    copied into exactly one place, and a second copy in code is how two of them drift."""
    return set(jev_spec("route")["questions"]["kind_fit"].get("criteria") or {})


def plan_evidence(cells, kind, ex):
    """What the ledger says about this kind × exec → (the text the table prints, the first-pass
    rate or None). A cell under the sample threshold is named with its n rather than dropped:
    the reader must be able to tell a thin cell from an absent one (§3.6b), and neither of them
    moves a suggestion."""
    b = cells.get((kind, ex))
    if b is None:
        return "no evidence", None
    n = prior_n(b)
    if n < PRIOR_MIN_SAMPLE:
        return f"no evidence (n={n})", None
    return f"priors {kind}×{ex} {b['accepted']}/{n}", b["accepted"] / n


def plan_adjust(en, effort, tier, ladder, cells, policy):
    """Difficulty picked a tier; the ledger moves it at most one step → (tier, evidence, note).

    The candidate's own record is read first: a tier this kind keeps failing at argues against
    itself more directly than a cheaper tier's record argues for the drop. Then the cheapest
    tier whose record clears the bar, which is the question §9.6 said routing actually asks."""
    def ex_of(t):
        return f"{en['executor']}/{ladder[t]}/{effort}"

    i = PLAN_TIERS.index(tier)
    text, rate = plan_evidence(cells, en["kind"], ex_of(tier))
    below = _thr(policy, "bump_below", 0.5)
    if rate is not None and rate < below and i + 1 < len(PLAN_TIERS):
        up = PLAN_TIERS[i + 1]
        note = f"{tier} → {up}: {text} is under {below:.2f} first-pass"
        return up, plan_evidence(cells, en["kind"], ex_of(up))[0], note
    at = _thr(policy, "drop_at", 0.8)
    for t in PLAN_TIERS[:i]:
        t_text, t_rate = plan_evidence(cells, en["kind"], ex_of(t))
        if t_rate is not None and t_rate >= at:
            return t, t_text, f"{tier} → {t}: {t_text} is at or over {at:.2f} first-pass"
    return tier, text, None


def model_tier(model, ladder, prices):
    """The tier a model sits on, read by its price level against the ladder's ends — a model
    between them is `mid`. None off the sheet: no guess. None on a `legacy` row too: a replaced
    model's price is not a tier (gpt-5.6-luna costs twice gpt-6-luna and would read `mid`)."""
    m = routable_models(prices).get(model) if ladder else None
    if m is None:
        return None
    price = float(m.get("input", 0.0))
    if price <= float(prices["models"][ladder["cheap"]].get("input", 0.0)):
        return "cheap"
    if price >= float(prices["models"][ladder["top"]].get("input", 0.0)):
        return "top"
    return "mid"


def tier_note(scores, policy, model, effort, ladder, prices, verb="routed to"):
    """A routed model two tiers away from what the brief's difficulty demands → one note, or
    None. Never a gate: main routed it, and main may know what the brief does not say."""
    demand, have = plan_tier(scores, policy), model_tier(model, ladder, prices)
    if have is None or abs(PLAN_TIERS.index(demand) - PLAN_TIERS.index(have)) < 2:
        return None
    scope, novelty, spec = scores
    return (f"reads {demand}-tier (scope {scope:.1f}, novelty {novelty:.1f}, spec {spec:.1f}) "
            f"— {verb} {model}/{effort}")


def route_brief(hp, scope, kind, brief):
    """The route spec asked over one brief → (compact answers, meta), metered as jev-plan."""
    questions = jev_questions("route")
    answers, meta = judge(hp, "route", {"scope": scope, "kind": kind, "brief": brief},
                          questions)
    if hp is not None:
        append_event(hp, {"ev": "clerk", "name": "jev-plan", "ok": meta["ok"],
                          "ms": meta["ms"], "tokens": meta["tokens"]}, src="wrapper")
    return (triage_answers(questions, answers) if answers is not None else {}), meta


def executor_directives(hp):
    """The live directives a lane's capsule will carry: audience executor or all (§9.4)."""
    if hp is None:
        return []
    return [d for d in directives(hp).values() if d.get("state") == "active"
            and (d.get("audience") or "all") in ("executor", "all")]


def brief_conflicts(hp, brief, live):
    """The brief beside the directives its lane will obey, one question per directive → the
    notes for those at or over `report_at`, worst first. A brief and a standing rule that
    contradict each other are a NO-GO the lane cannot avoid (measured) — cheaper said before
    the launch than read in the report after it."""
    if not brief or not live:
        return []
    questions = {}
    for i in range(len(live)):
        questions.update(jev_questions("brief-check", i=i))
    answers, meta = judge(hp, "brief-check",
                          {"brief": brief, "directives": [directive_as_state(d) for d in live]},
                          questions)
    if hp is not None:
        append_event(hp, {"ev": "clerk", "name": "jev-brief", "ok": meta["ok"],
                          "ms": meta["ms"], "tokens": meta["tokens"]}, src="wrapper")
    at = _thr(jev_policy("brief-check"), "report_at", 0.7)
    hits = [(jev_noul(answers, f"conflict_{i}"), d) for i, d in enumerate(live)]
    return [f"brief may conflict with directive {d['id']} ({p:.2f}): "
            f"{one_line(d.get('text', ''), 80)}"
            for p, d in sorted((h for h in hits if h[0] is not None and h[0] >= at),
                               key=lambda h: -h[0])]


def plan_entry(hp, en, cells, ladder, policy, prices, live):
    """One entry: one request about its brief and one about the directives its lane will
    obey, then everything code derives from the answers (§3.6) → the table row, the notes it
    earned and the `.plan.jsonl` record."""
    ans, meta = route_brief(hp, en.get("scope"), en.get("kind"), en.get("prompt"))
    scores = plan_scores(ans)
    notes, tier, model, effort = [], None, None, None
    if scores is not None and en["model"]:
        note = tier_note(scores, policy, en["model"], en["effort"], ladder, prices)
        if note:
            notes.append(note)
    if scores is not None and ladder:
        effort = "high" if scores[1] >= _thr(policy, "high_effort_novelty", 2.0) else "medium"
        tier, evidence, note = plan_adjust(en, effort, plan_tier(scores, policy), ladder,
                                           cells, policy)
        model = ladder[tier]
        if note:
            notes.append(note)
    else:
        # No difficulty, no suggestion — inventing a tier out of a failed request is the one
        # thing this must not do. What the entry already routes to is still worth its evidence.
        cur = entry_exec(en)
        evidence = plan_evidence(cells, en["kind"], cur)[0] if cur else "-"
    verifiable = ans.get("verifiable")
    if (isinstance(verifiable, float) and verifiable < _thr(policy, "check_below", 0.3)
            and not en["check"]):
        notes.append("add a check — the brief names no machine-verifiable completion")
    fit = ans.get("kind_fit") if isinstance(ans.get("kind_fit"), dict) else {}
    conf = fit.get("confidence")
    # PRIORS aggregates on kind, so a stray tag is a column of one — worth saying while the
    # manifest is still being edited, and only when the judge is confident about the reading.
    if (en["kind"] not in plan_kinds() and isinstance(conf, float)
            and conf >= _thr(policy, "kind_at", 0.7)):
        notes.append(f'kind "{en["kind"]}" reads as {fit["choice"]} ({conf:.2f})')
    notes += brief_conflicts(hp, en["prompt"], live)

    def cell(qid):
        a = ans.get(qid)
        v = a.get("score") if isinstance(a, dict) else None
        return f"{v:.2f}" if isinstance(v, float) else "-"

    row = [en["id"], cell("scope"), cell("novelty"), cell("spec"), _pp(verifiable),
           entry_exec(en) or "-",
           f"{en['executor']}/{model}/{effort}" if model else "-", evidence]
    rec = {"t": now_iso(), "id": en["id"],
           "difficulty": {q: ans.get(q) for q in ("scope", "novelty", "spec", "verifiable")},
           "kind_fit": ans.get("kind_fit"), "suggested": {"model": model, "effort": effort},
           "evidence": evidence, "jev": meta}
    return {"id": en["id"], "row": row, "notes": notes, "rec": rec, "tier": tier}


def plan_pass(mp, entries, hp, out):
    """The plan (DESIGN §3.6): the judge measures each brief, code prices it → the rows, printed
    to `out` — stdout for a dry run, stderr ahead of a launch.

    §9.6 turned routing into "the cheapest exec that clears the bar", and PRIORS answers half
    of it — what a kind × exec has cost and returned. The half it cannot know before a launch
    is how hard *this* brief is. The judge measures the brief, code maps that onto the price
    sheet and the ledger's cells: the suggestion is computed fresh per batch and expires with
    it, which is the shape routing.yaml was retired in favour of (§4)."""
    on = jev_backend(hp) != "off"
    prices = load_prices()
    cells = prior_cells(read_ledger(hp), prices) if hp is not None else {}
    policy = jev_policy("route")
    live = executor_directives(hp) if on else []

    ladder = price_ladder(prices)
    print("ladder codex: "
          + (" · ".join(f"{t} {ladder[t]}" for t in PLAN_TIERS) if ladder
             else "no codex model on the price sheet — no suggestion"), file=out)

    rows = []
    for en in entries:
        if on:
            rows.append(plan_entry(hp, en, cells, ladder, policy, prices, live))
            continue
        # The deterministic half: what the manifest already routes to, and what the ledger
        # says about it. It must not vanish with the key (§3.9).
        cur = entry_exec(en)
        rows.append({"id": en["id"], "notes": [], "rec": None, "tier": None,
                     "row": [en["id"], "-", "-", "-", "-", cur or "-", "-",
                             plan_evidence(cells, en["kind"], cur)[0] if cur else "-"]})

    head = ["id", "scope", "novelty", "spec", "verifiable", "exec now", "suggested",
            "evidence"]
    table = [r["row"] for r in rows]
    widths = [max(len(line[i]) for line in [head] + table) for i in range(len(head))]
    for line in [head] + table:
        print("  ".join(c.ljust(w) for c, w in zip(line, widths)).rstrip(), file=out)
    notes = [(r["id"], n) for r in rows for n in r["notes"]]
    if notes:
        print(file=out)
        for eid, note in notes:
            print(f"{eid}: {note}", file=out)

    if on:
        # Rewritten every run, never appended to: a batch has one routing decision, and a
        # stale record beside a fresh one would be joined as this one's.
        plan = mp.parent / f"{mp.stem}.plan.jsonl"
        plan.write_text("".join(json.dumps(r["rec"], ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
    return rows


def run_dry(mp, entries, hp):
    """`--batch <manifest> --dry-run`: launches nothing, prints the plan. The manifest is not
    modified — main edits it, or launches it as it is and lets an unrouted entry take the
    suggestion."""
    on = jev_backend(hp) != "off"
    if not on:
        print("--dry-run: judge off — no TYPESAFE_API_KEY; showing the deterministic part only",
              file=sys.stderr)
    rows = plan_pass(mp, entries, hp, sys.stdout)
    tiers = collections.Counter(r["tier"] for r in rows if r["tier"])
    summary = {"total": len(entries), "suggested": sum(tiers.values()),
               "plan": str(mp.parent / f"{mp.stem}.plan.jsonl") if on else None,
               "manifest": str(mp)}
    if tiers:
        summary["tiers"] = dict(tiers)
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(0)


def resume_split(entries, journal):
    """What a run over an existing journal relaunches → (attempts, done, todo, held).

    Done entries (last exit and check passed) stay done. The rest relaunch unless their latest
    triage named a cause a relaunch cannot clear — `capability` or `spec` needs a different
    brief, and running it unchanged buys the same failure twice. No triage, or a triage that
    named no cause, relaunches: a filter that cannot read the cause must not be the reason a
    lane is dropped."""
    attempts, done, _, triages = journal_state(journal)
    todo, held = [], []
    for en in entries:
        if en["id"] in done:
            continue
        cause = triage_cause(triages.get(en["id"]))
        if cause in CAUSES and cause not in RELAUNCHABLE_CAUSES:
            held.append((en, cause))
        else:
            todo.append(en)
    return attempts, done, todo, held


def run_batch(argv):
    """DESIGN §3.6, batch: the deterministic half of a batch of lanes — fan-out, concurrency,
    id capture, parent stamping, usage collection, breaker checks, journaling, resume and the
    harvest — in one wrapper call, with no mode flags: the journal beside the manifest decides
    what a run does. Selection (the manifest) and judgment (verdicts) stay with the model: a
    check result or a triage route is journal evidence, and batch never writes ev:outcome."""
    manifest, dry_run = parse_batch_argv(argv)
    mp = Path(manifest)
    hp = find_hippo()
    on = jev_backend(hp) != "off"
    # An unrouted entry is valid exactly when the plan pass will run over it before launch.
    concurrency, entries = load_manifest(mp, plan=dry_run or on)
    if hp is None:
        print("dispatch --batch: no .hippo/ — skipping the ledger records", file=sys.stderr)
    if dry_run:
        run_dry(mp, entries, hp)
    journal = mp.parent / f"{mp.stem}.journal.jsonl"
    outdir = mp.parent / f"{mp.stem}.out"
    total = len(entries)

    attempts, done_ids, todo, held = {}, set(), entries, []
    if journal.exists():
        attempts, done_ids, todo, held = resume_split(entries, journal)
        if todo or held:
            print(f"resuming {mp}: {len(todo)} to relaunch, {len(held)} skipped",
                  file=sys.stderr)
        by_cause = {}
        for en, cause in held:
            by_cause.setdefault(cause, []).append(en["id"])
        for cause, ids in by_cause.items():
            print(f"skipped {len(ids)} (cause {cause}): {', '.join(ids)} — a different brief, "
                  "then a new entry", file=sys.stderr)

    if todo and on:
        # Auto-routing: the plan runs over what is about to launch, and an entry that left
        # model unset takes the suggestion. The table goes to stderr — stdout is the harvest's.
        rows = {r["id"]: r for r in plan_pass(mp, todo, hp, sys.stderr)}
        unrouted = []
        for en in todo:
            if en["model"] is None:
                suggested = rows[en["id"]]["rec"]["suggested"]
                if suggested["model"] is None:
                    unrouted.append(en["id"])
                else:
                    en["model"], en["effort"] = suggested["model"], suggested["effort"]
        if unrouted:
            die(f"dispatch --batch: {mp}: model is required — the judge suggested none for "
                f"{', '.join(unrouted)}; set it in defaults or the entry", 2)
    outdir.mkdir(parents=True, exist_ok=True)

    parent = os.environ.get("HIPPO_DISPATCH", "")
    stop = threading.Event()
    state = {"launched": 0, "ok": 0, "failed": 0, "done": 0, "warned": False,
             "stopped": False}
    ld = lanes_dir_or_note(hp) if hp is not None else None
    # A signal to the batch (§3.6) stops further launches and is forwarded to every running
    # lane's process group; each lane then records itself killed, as a single dispatch does.
    running, signaled = set(), []

    def on_signal(signum, _frame):
        with BATCH_LOCK:
            if not signaled:
                signaled.append(signum)
            stop.set()
            for lane in running:
                lane.stop(signum)

    # Only while lanes can run: the harvest after them is a judge pass that a signal must be
    # able to stop, as it could before 1.15.0 (measured: kept installed, a SIGTERM during the
    # harvest was swallowed and the batch exited 0).
    prev = {s: signal.signal(s, on_signal) for s in LANE_SIGNALS}

    def jrnl(rec):
        with BATCH_LOCK:
            with journal.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def run_entry(en, attempt):
        if stop.is_set():
            return
        did = "d" + os.urandom(16).hex()
        e = {"ev": "dispatch", "id": did, "kind": en["kind"],
             "exec": f"{en['executor']}/{en['model']}/{en['effort']}",
             "scope": en["scope"], "depth": en["depth"]}
        if en["task"]:
            e["task"] = en["task"]
        if parent:
            e["parent"] = parent
        bad = validate_event(e)
        # Verdict and dispatch record are ONE locked region: the verdict prices the batch from
        # the ledger, so each admitted child's row must land before the next verdict reads.
        # Priced outside the lock, every worker admits against the pre-batch total (measured:
        # 4 sol children through a $20 budget).
        with BATCH_LOCK:
            verdict, msg = fanout_verdict(hp, parent, en["model"])
            if verdict == "stop":
                first = not state["stopped"]
                state["stopped"] = True
                stop.set()
                if first:
                    print(msg, file=sys.stderr)
                    jrnl({"t": now_iso(), "event": "stopped", "reason": msg})
                return
            if verdict == "warn":
                first = not state["warned"]
                state["warned"] = True
                if first:
                    print(msg, file=sys.stderr)
            if bad:
                # Same stance as single dispatch: a lost record makes a sound, never a blocked launch.
                print(f"dispatch --batch: {en['id']}: record failed ({bad}) — the launch "
                      "proceeds anyway", file=sys.stderr)
                jrnl({"t": now_iso(), "event": "record_failed", "id": en["id"],
                      "dispatch": did, "reason": bad})
            elif hp is not None:
                append_event(hp, e, src="wrapper")

        jrnl({"t": now_iso(), "event": "launch", "id": en["id"], "attempt": attempt,
              "dispatch": did})
        with BATCH_LOCK:
            state["launched"] += 1
        out_p, err_p = outdir / f"{en['id']}.out", outdir / f"{en['id']}.err"
        env = {**os.environ, "HIPPO_DISPATCH": did, "HIPPO_DEPTH": str(en["depth"]),
               "PATH": lane_path(), **({"HIPPO_DIR": str(hp)} if hp is not None else {})}
        cmd = adapter_argv(en)
        timed_out = False
        # The same lane machinery as a single dispatch (§3.6): <id>.err keeps its shape (the
        # raw stderr, byte for byte), the compact stream joins the batch's own stderr, and the
        # record lands in .hippo/lanes/ under the dispatch id.
        lane = Lane(did, en["scope"], e["exec"], ld / f"{did}.json" if ld else None, err_p, out_p)
        with out_p.open("wb") as fo, err_p.open("wb") as fe:
            try:
                child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fo,
                                         stderr=subprocess.PIPE, env=env, cwd=en["cwd"],
                                         start_new_session=True)
            except OSError as oe:
                fe.write(f"could not run {cmd[0]}: {oe}\n".encode())
                rc = 127
            else:
                with BATCH_LOCK:
                    running.add(lane)
                    if signaled:  # it launched as the signal landed
                        lane.stop(signaled[0])
                lane.launched(child.pid)
                deadline = None if en["timeout"] is None else time.monotonic() + en["timeout"]
                timed_out = pump_lane(child, lane, fe, deadline)
                rc = child.wait()
                with BATCH_LOCK:
                    running.discard(lane)
        if timed_out:
            lane.rec["timed_out"] = True
        lane.finish(rc, "killed" if lane.signal is not None or timed_out else "exited")

        # Cost was incurred whatever rc says; a parse gap stays a gap in the ledger too.
        usage = codex_usage(err_p)
        # A usage row must join to a dispatch row: when the dispatch record failed, the
        # tokens still reach the journal below, just not the ledger.
        if usage is not None and hp is not None and bad is None:
            with BATCH_LOCK:
                append_event(hp, {"ev": "usage", "ref": did, **usage}, src="wrapper")

        check_rc = None
        if en["check"]:
            try:
                r = subprocess.run(en["check"], shell=True, cwd=en["cwd"],
                                   capture_output=True, text=True, timeout=CHECK_TIMEOUT)
                check_rc, check_out = r.returncode, (r.stdout or "") + (r.stderr or "")
            except subprocess.TimeoutExpired:
                check_rc, check_out = -1, f"timeout {CHECK_TIMEOUT}s\n"
            (outdir / f"{en['id']}.check").write_text(check_out, encoding="utf-8")

        rec = {"t": now_iso(), "event": "exit", "id": en["id"], "attempt": attempt,
               "dispatch": did, "rc": rc, "check_rc": check_rc}
        if timed_out:
            rec["timed_out"] = True
        rec["tokens"] = usage["tokens"] if usage else None
        jrnl(rec)

        # Triage at lane exit (§3.6): one calibrated read of the whole report, while the
        # files are hot and main is not in the loop. With the judge off it does not exist —
        # no record, no column, no changed line.
        route = None
        if on:
            claim = executor_claims(read_ledger(hp)).get(did) if hp is not None else None
            t = triage_entry(hp, en, rec, outdir, claim, jrnl)
            route = t["route"]
        lane.close(triage_line(t) if route else None)

        ok = rc == 0 and check_rc in (None, 0)
        with BATCH_LOCK:
            state["ok" if ok else "failed"] += 1
            state["done"] += 1
            line = (f"[{state['done']}/{len(todo)}] {en['id']} rc={rc} "
                    f"check={check_mark(check_rc)} {did}")
            if on:
                # A triage that failed shows as "-": the gap is a fact about the batch too.
                line += f" triage={route or '-'}"
            print(line, file=sys.stderr)

    for en in entries:
        if en["id"] in done_ids:
            jrnl({"t": now_iso(), "event": "skip", "id": en["id"]})
    for en, cause in held:
        jrnl({"t": now_iso(), "event": "skip", "id": en["id"], "why": f"cause {cause}"})
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = [pool.submit(run_entry, en, attempts.get(en["id"], 0) + 1) for en in todo]
            for f in concurrent.futures.as_completed(futs):
                f.result()  # a wrapper bug dies loudly, never as a silently thinner batch
    for s, handler in prev.items():
        signal.signal(s, handler)
    if signaled:
        # The harvest is a judge pass over every lane; a batch being stopped does not start one.
        # The journal holds every exit, so the same command resumes.
        print(f"dispatch --batch: stopped by {signal.Signals(signaled[0]).name} — running lanes "
              f"were signalled and recorded; `hippo dispatch --batch {mp}` resumes",
              file=sys.stderr)
        sys.exit(128 + signaled[0])

    harvest = run_harvest(mp, entries, journal, outdir, hp, {en["id"] for en in todo})
    summary = {"total": total, "launched": state["launched"], "ok": state["ok"],
               "failed": state["failed"], "skipped": total - len(todo),
               "stopped": state["stopped"], **harvest, "journal": str(journal),
               "outdir": str(outdir)}
    print(json.dumps(summary, ensure_ascii=False))
    # The rc is about this run's launches: a harvest-only run launched nothing, so nothing
    # it did failed — what the lanes did is in the table.
    sys.exit(2 if state["stopped"] else (1 if state["failed"] else 0))


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


def routable_models(prices):
    """The sheet's rows a lane may be routed to — every row but the `legacy` ones. A replaced
    model keeps its row only so the usage recorded on it stays priced; `price_usd` reads the
    whole sheet, and anything that ranks or picks a model reads this."""
    return {m: v for m, v in prices["models"].items() if not (v or {}).get("legacy")}


def directive_roster(hp):
    """The live directive ids handed to the scribe with the digest.

    Without it the clerk coins a fresh id for an instruction that already has one, and the two
    sit in the ledger as unrelated directives — the update never lands. Reading the ledger from
    inside the clerk would cost a tool call and a lot of latency for what is a short list."""
    live = [d for d in directives(hp).values() if d.get("state") == "active"]
    if not live:
        return "(none yet)"
    return "\n".join(
        f"- {d['id']}: {one_line(d.get('text', ''), 100)}"
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


# --- native runs (DESIGN §3.5.3c) -------------------------------------------------
# Most delegation now goes through the host's own subagents, which the wrapper cannot wrap —
# 30 days on one machine, 49 `hippo log dispatch` calls, nearly all beside an Agent call, and
# not one of those runs' costs reached PRIORS. A Claude Code session keeps every end of such a
# run on disk: the launch and its notifications in main's transcript, and each agent's own
# transcript (model, effort, token usage, edits) under <session>/subagents/. The scribe reads
# them at Stop and records a native run the way the wrapper records a codex lane; the one slot
# that needs a reading of the brief — the kind — comes from the clerk, which reads the turn
# anyway. Nothing here asks the judge except the triage, so it all works without a key.

NATIVE_PREFIX = "ag-"
NATIVE_TOOLS = ("Agent", "Task")  # Task is the Agent tool's older name
LAUNCH_TOOLS = (*NATIVE_TOOLS, "Workflow")
# Track B's plugin agent only babysits a codex lane, which the wrapper already records.
NATIVE_SKIP = ("hippo:lane",)
NATIVE_LIST_H = 24  # a run older than this is no longer listed for the clerk
NATIVE_BRIEF_CHARS = 300
# Judge calls per window: it bounds the detached scribe's extra work (~1s a call, §3.9).
NATIVE_TRIAGE_MAX = 8
NATIVE_EDIT_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")
NATIVE_EDITS_HEAD = ("files this agent edited with its edit tools; shell edits of files it "
                     "never read are not visible")
# A notification's header opens with these, in this order. <tool-use-id> names the call the
# agent is answering — its launch or a SendMessage to it, on Claude Code 2.1.237 and 2.1.280
# alike — and is missing when the agent resumed on its own background work, or (once, on
# 2.1.237) on the end of an agent a SendMessage reached while it was still running; the task-id
# always names the agent.
TASK_NOTE_RE = re.compile(r"<task-notification>\s*<task-id>([^<]*)</task-id>"
                          r"(?:\s*<tool-use-id>([^<]*)</tool-use-id>)?")
TASK_STATUS_RE = re.compile(r"<status>([^<]*)</status>")
# An agent that ends its turn with its own background work still running notifies with this
# note and notifies again when it is done (measured, 43 on this machine): an interim result.
TASK_INTERIM_RE = re.compile(r"<note>[^<]*background work of its own still running")
TaskNote = collections.namedtuple("TaskNote", "line task tuid status report interim t")


def _message_texts(content):
    """The text a message carries: a string, or the text blocks of a list. Never a tool_result
    — that is a tool's output, quoting whatever it happened to read."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
            and isinstance(b.get("text"), str)]


def _flat_text(v):
    s = "\n".join(_message_texts(v)).strip()
    return s or None


def _read_json(p):
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _iso_time(s):
    try:
        t = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _scope_key(text):
    return one_line(text).casefold()


def _line_time(raw):
    """The top-level timestamp of one transcript line, or None."""
    try:
        rec = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return _iso_time(rec.get("timestamp")) if isinstance(rec, dict) else None


def task_notes(text, line, t=None):
    """The <task-notification> blocks one message carries → [TaskNote]. Status and the interim
    note are read in the header, before `<result>`; the report is everything up to the
    block's last `</result>`, so a report that quotes a tag keeps its text. A block with no
    status — a Monitor's event — is no notification of an end."""
    heads = list(TASK_NOTE_RE.finditer(text))
    out = []
    for k, m in enumerate(heads):
        block = text[m.end(): heads[k + 1].start() if k + 1 < len(heads) else len(text)]
        start, stop = block.find("<result>"), block.rfind("</result>")
        head = block if start < 0 else block[:start]
        status = TASK_STATUS_RE.search(head)
        if status is None:
            continue
        report = block[start + len("<result>"): stop].strip() if 0 <= start < stop else ""
        out.append(TaskNote(line, m.group(1).strip(), (m.group(2) or "").strip() or None,
                            status.group(1).strip(), report or None,
                            bool(TASK_INTERIM_RE.search(head)), t))
    return out


def native_scan(path, end=None, main=True, marks=()):
    """One streaming pass over a Claude Code transcript — main's, or an agent's for the runs
    it launched itself → (launches, notes, times), `times` holding for each line number in
    `marks` the last line's own timestamp at or before it (a quarter of the lines carry none).

    `launches` maps an agentId (Agent/Task) or a runId (Workflow) to the call and its launch
    result, for every call the host confirmed: measured on Claude Code 2.1.281, a background
    call's tool_result line carries `toolUseResult` with `status: async_launched` and the id
    (91 on this machine), a foreground Agent call's carries `status: completed` with the
    report inline — a launch and a completion on one pair of lines (2) — and a call that
    failed carries an error string and launched nothing. `notes` is every <task-notification>,
    on a user line (main idle) or a `queued_command` attachment (main mid-turn); the
    `queue-operation` lines around a queued one are bookkeeping and are not read, and Bash
    background tasks notify in the same shape under ids no launch has. A line that does not
    parse is skipped, never raised: the prefilter is a substring test, so a quoted tag costs
    one json.loads."""
    launches, notes, pending, times, stamped = {}, [], {}, {}, None
    try:
        f = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return launches, notes, times
    with f:
        for i, raw in enumerate(f, 1):
            if end is not None and i > end:
                break
            # A file-history snapshot names a timestamp but has none of its own, only its
            # snapshot's: the one such line kind here (397 of 397 on this machine).
            if '"timestamp"' in raw and ('"file-history-snapshot"' not in raw or _line_time(raw)):
                stamped = raw
            if i in marks:
                times[i] = _line_time(stamped)
            call = '"tool_use"' in raw and any(f'"{t}"' in raw for t in LAUNCH_TOOLS)
            result = bool(pending) and any(t in raw for t in pending)
            note = "<task-notification>" in raw
            if not (call or result or note):
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            # In main's transcript a sidechain line is a subagent's own traffic (older hosts
            # wrote it there), not main's launch; in an agent's transcript every line is one.
            if not isinstance(rec, dict) or (main and rec.get("isSidechain")):
                continue
            kind, msg = rec.get("type"), rec.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if call and kind == "assistant" and isinstance(content, list):
                for b in content:
                    if (isinstance(b, dict) and b.get("type") == "tool_use"
                            and b.get("name") in LAUNCH_TOOLS and isinstance(b.get("id"), str)
                            and isinstance(b.get("input"), dict)):
                        pending[b["id"]] = b
            if result and kind == "user" and isinstance(content, list):
                tur = rec.get("toolUseResult")
                tur = tur if isinstance(tur, dict) else {}
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"
                            and b.get("tool_use_id") in pending):
                        continue
                    use = pending.pop(b["tool_use_id"])
                    wf = use["name"] == "Workflow"
                    key, status = tur.get("runId" if wf else "agentId"), tur.get("status")
                    if status not in ("async_launched", "completed") or not isinstance(key, str):
                        continue
                    t = _iso_time(rec.get("timestamp"))
                    launches[key] = {"tool": use["name"], "tuid": use["id"], "input": use["input"],
                                     "result": tur, "line": i, "t": t}
                    if status == "completed" and not wf:
                        report = _flat_text(tur.get("content")) or _flat_text(b.get("content"))
                        notes.append(TaskNote(i, key, use["id"], status, report, False, t))
            if note:
                for text in _note_texts(rec):
                    notes += task_notes(text, i, _iso_time(rec.get("timestamp")))
    return launches, notes, times


def _note_texts(rec):
    """The texts a transcript line can deliver a <task-notification> in: a user message, or a
    `queued_command` attachment."""
    msg = rec.get("message")
    if rec.get("type") == "user" and isinstance(msg, dict):
        return _message_texts(msg.get("content"))
    att = rec.get("attachment")
    if rec.get("type") == "attachment" and isinstance(att, dict) \
            and att.get("type") == "queued_command":
        return _message_texts(att.get("prompt"))
    return []


def native_run(launch, key, session, parent=None):
    """One run as the scribe needs it, from its launch. An agent's executor comes from its
    meta.json (written at launch: `isFork`, `agentType`), a Workflow run is `workflow`; the
    brief is the call's prompt, or a Workflow's script."""
    inp, tur, sub = launch["input"], launch["result"], session / "subagents"
    run = {"id": NATIVE_PREFIX + key, "key": key, "line": launch["line"], "t": launch["t"],
           "tuid": launch["tuid"], "parent": parent["id"] if parent else None, "notes": [],
           "ref": None, "meta": {}, "resolved": None, "skip": False}
    if launch["tool"] == "Workflow":
        brief, sp = inp.get("script"), tur.get("scriptPath")
        if not isinstance(brief, str) and isinstance(sp, str) and sp:
            brief = _read_text(Path(sp))
        return {**run, "executor": "workflow", "scope": one_line(tur.get("workflowName")),
                "brief": brief, "files": sub / "workflows" / key,
                "summary": session / "workflows" / f"{key}.json"}
    meta = _read_json(sub / f"agent-{key}.meta.json")
    atype = meta.get("agentType") or inp.get("subagent_type")
    brief = inp.get("prompt") if isinstance(inp.get("prompt"), str) else tur.get("prompt")
    return {**run, "executor": "fork" if meta.get("isFork") is True or atype == "fork"
            else "subagent", "scope": one_line(inp.get("description") or tur.get("description")),
            "brief": brief if isinstance(brief, str) else None, "meta": meta,
            "resolved": tur.get("resolvedModel"), "skip": atype in NATIVE_SKIP,
            "files": sub / f"agent-{key}.jsonl"}


def native_index(transcript, since, end):
    """DESIGN §3.5.3c: every native run main's transcript launched up to `end` → {id: run},
    each flagged with what the window (since, end] did to it — `seen` (launched or notified
    in it), `done_now` (a notification in it) and `first_now` (its answer to its brief became
    complete in it, `native_answer`; `answer` holds that answer's notifications). The whole
    transcript is read, because a completion in this window may belong to a launch long before
    the cursor.

    Nested runs — an agent's own Agent calls — live in that agent's transcript, and its
    subagents/ entry's meta.json names the parent (`parentAgentId`); they are indexed through
    their parent, carry `parent = ag-<parentAgentId>`, and take the parent's window: a parent
    notifies as done only once no child of its own is still running. A Workflow's own agents
    are not runs — the run is one."""
    launches, notes, times = native_scan(transcript, end, marks=(since, end))
    session = transcript.with_suffix("")
    runs, by_note = {}, {}
    for key, launch in launches.items():
        run = native_run(launch, key, session)
        runs[run["id"]] = run
        by_note[launch["result"].get("taskId") if run["executor"] == "workflow" else key] = run
        by_note[launch["tuid"]] = run
    for n in notes:
        run = by_note.get(n.task) or by_note.get(n.tuid)
        if run is not None:
            run["notes"].append(n)
    for run in runs.values():
        run["seen"] = run["line"] > since or any(n.line > since for n in run["notes"])
        run["done_now"] = any(n.line > since for n in run["notes"])
        run["answer"] = native_answer(run, end, times.get(end))
        run["first_now"] = (run["answer"] is not None
                            and native_answer(run, since, times.get(since)) is None)
    sub = session / "subagents"
    parents = ({_read_json(p).get("parentAgentId") for p in sub.glob("agent-*.meta.json")}
               if runs and sub.is_dir() else set())
    queue = [r for r in runs.values() if r["key"] in parents]
    while queue:
        parent = queue.pop()
        kids, kid_notes, _ = native_scan(parent["files"], main=False)
        for key, launch in kids.items():
            if launch["tool"] == "Workflow" or NATIVE_PREFIX + key in runs:
                continue
            run = native_run(launch, key, session, parent)
            run["notes"] = [n for n in kid_notes if n.task == key or n.tuid == launch["tuid"]]
            run["answer"] = native_answer(run, None, times.get(end))
            run.update(seen=parent["seen"], done_now=parent["done_now"],
                       first_now=parent["first_now"] and run["answer"] is not None)
            runs[run["id"]] = run
            if key in parents:
                queue.append(run)
    return runs


def native_answer(run, upto, at):
    """The notifications that answer the run's brief, once that answer is complete as of line
    `upto` of the transcript that holds them (None: all of it) and time `at` → [TaskNote], or
    None while it is not.

    The answer is the run's notifications up to the first after its first that names a call
    other than its launch: that one answers a SendMessage to the agent, and a resumed agent's
    later report is not its answer to the brief. The answer is complete at its first final
    notification, and is every one up to it, in order: an interim report before it can be the
    agent's whole report, and the final one a line about its watcher. When every one is
    interim — the agent stopped with background work of its own still running — it is
    complete once the agent has answered a SendMessage (main moved on from the answer), or
    once it has sat idle since the last one with all that work ended (`native_settled`); the
    host's final notification cannot be waited for. Measured (mlx-vlm, 2026-09-23): in 3 of 3
    such runs the work left was a `tail -f` Monitor that expired 5-11 minutes after the agent's
    last report; the host queued the expiry in main's transcript and never delivered it to the
    idle agent, so none resumed and no final notification came in the two days the session ran
    on — while each interim report was the agent's whole report."""
    notes = [n for n in run["notes"] if upto is None or n.line <= upto]
    cut = next((k for k, n in enumerate(notes) if k and n.tuid not in (None, run["tuid"])),
               len(notes))
    chain = notes[:cut]
    final = next((k for k, n in enumerate(chain) if not n.interim), None)
    if final is not None:
        return chain[:final + 1]
    if not chain or cut < len(notes):
        return chain or None
    done = native_settled(run, chain[-1].t)
    return chain if done is not None and at is not None and done <= at else None


def native_settled(run, since):
    """When the agent, idle since `since`, was done — read from its own transcript: the time
    by which every piece of background work it had started ended, with no user or assistant
    line after `since` before then (it was not resumed, by a message or by that work). None
    when that never happened, when `since` is unknown, or when the transcript cannot be read.
    A task ends with its own notification (one with a status) or the agent's TaskStop; a
    Monitor with neither at its deadline: the host kills it at the `timeoutMs` its launch
    result states, whatever the call asked for — 0 for a `persistent` one, which has none
    (hosts 2.1.246-258 ran those, and each ended with a notification). A Monitor's events carry
    no status, its expiry included, and one after `since` only moves that end later, to its own
    time: an expiry that wakes the idle agent just after the deadline leaves the run unsettled
    before it (measured, an agent gets its expiry from 0.40s before the deadline to 0.27s
    after). Never earlier: the host can queue a notice for an idle agent and write it into the
    agent's transcript, under its own earlier time, only once the agent is resumed (measured,
    mlx-vlm: an expiry written almost 9 hours after its time). Anything else — a background
    Bash command, an agent or a workflow of its own — has no deadline: it runs until it says it
    ended. A fixed moment, so a window that finds the run settled is never contradicted by a
    later one."""
    memo = run.setdefault("settled", {})
    if since is None or since in memo:
        return memo.get(since)
    memo[since] = None
    try:
        f = run["files"].open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    pending, work, ended, late, resumed = {}, {}, {}, {}, None
    with f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            t = _iso_time(rec.get("timestamp")) if isinstance(rec, dict) else None
            if t is None:
                continue
            kind, msg, tur = rec.get("type"), rec.get("message"), rec.get("toolUseResult")
            if kind in ("user", "assistant") and t > since and resumed is None:
                resumed = t
            content = msg.get("content") if isinstance(msg, dict) else None
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                if kind == "assistant" and b.get("type") == "tool_use":
                    pending[b.get("id")] = b.get("name")
                    if b.get("name") == "TaskStop" and isinstance(inp.get("task_id"), str):
                        ended.setdefault(inp["task_id"], t)
                if not (kind == "user" and b.get("type") == "tool_result" and t <= since
                        and b.get("tool_use_id") in pending and isinstance(tur, dict)):
                    continue
                name = pending.pop(b["tool_use_id"])
                tid = tur.get("backgroundTaskId") or tur.get("taskId") or (
                    tur.get("agentId") if tur.get("status") == "async_launched" else None)
                if isinstance(tid, str):
                    ms = _num(tur.get("timeoutMs")) if name == "Monitor" else None
                    work[tid] = t + timedelta(milliseconds=ms) if ms else None
            for text in _note_texts(rec):
                for n in task_notes(text, 0):
                    ended.setdefault(n.task, t)
                for m in TASK_NOTE_RE.finditer(text) if t > since else ():
                    late.setdefault(m.group(1).strip(), []).append(t)
    # Each task's end: its own notification, else its deadline or a later notice after `since`;
    # None: still running.
    ends = [ended.get(tid) or deadline and max([deadline, *late.get(tid, ())])
            for tid, deadline in work.items()]
    done = None if None in ends else max([since, *ends])
    memo[since] = done if done is not None and (resumed is None or done < resumed) else None
    return memo[since]


TRANSCRIPT_FLUSH_WAIT = 0.25  # s; Claude Code writes a transcript on a 100ms flush (2.1.282)
# User lines no model turn follows: a local command's (/compact, /context), bash mode's (`!cmd`)
# and an Esc interrupt's — measured over this machine's transcripts, none ever followed by a
# model turn; the next one needs a new prompt, which takes a new prompt id.
NO_TURN_MARKS = ("<command-name>", "<local-command-stdout>", "<bash-input>", "<bash-stdout>",
                 "[Request interrupted by user")


def compaction_agent(transcript, prompt_id, trigger=None):
    """DESIGN §3.4: whose compaction fired this hook → the compacting agent's meta.json ({} when
    it has none), or None when it is main's or cannot be told apart.

    An agent's own compaction reaches PreCompact and SessionStart(compact) as main's: main's
    session and transcript, no agent_id, and the prompt_id main is at — the host stamps one
    prompt id per process, whoever writes (measured, 2.1.282). A compaction starts right before
    a request, and nothing it writes lands before SessionStart and PostCompact have returned
    (measured), so at both hooks the compacting side is still at a compaction point
    (`_compaction_point`). Main's side decides, read once the host's 100ms flush has passed:

    - the hook's prompt not in main's transcript yet is a prompt main just took — a manual
      /compact gets one of its own (measured): main's;
    - main at the hook's prompt and at a compaction point could be the one compacting: main's,
      whatever the agents show — main losing its request is the worse error;
    - main at the hook's prompt and not at a compaction point, or already past that prompt
      (compacting, it could not have moved on), is not compacting: the compaction is that of an
      agent at a compaction point — any `agent-*.jsonl` under the session's subagents/, a
      Workflow's agents included; the one whose line is the latest;
    - a PreCompact with trigger `manual` is main's: only the user types /compact, into main.

    Left as main's: an agent compacting while main, at the same prompt, waits on its reply, and
    one compacting while main's own compaction runs."""
    if trigger == "manual" or not transcript or not prompt_id:
        return None
    transcript = Path(transcript)
    agents = sorted(transcript.with_suffix("").glob("subagents/**/agent-*.jsonl"))
    if not agents:
        return None  # no agent ever launched, or not a Claude Code transcript
    time.sleep(TRANSCRIPT_FLUSH_WAIT)
    prompts = _main_prompts(transcript)
    if prompt_id not in prompts:
        return None
    if prompts[-1] == prompt_id:
        main = _latest_turn(transcript)
        if not main or _compaction_point(main):
            return None
    at = [(recs[-1].get("timestamp") or "", path) for path in agents
          for recs in [_latest_turn(path, main=False)] if _compaction_point(recs)]
    if not at:
        return None
    path = max(at)[1]
    return _read_json(path.with_name(path.name.removesuffix(".jsonl") + ".meta.json"))


def _compaction_point(recs):
    """Whether a transcript ending in `recs` (`_latest_turn`) is where a compaction can start:
    in a user line a model turn follows, with no call of its latest response still out — the
    host sends no request until every one is answered, so an agent main runs in the foreground
    beside a call already back keeps main from compacting (measured: two foreground Agent calls
    in one message, one answered 100s before the other). A new prompt after that response drops
    a call left unanswered by a process that died. Not one either: a Workflow agent's end, the
    result of its StructuredOutput call (178 of 178 on this machine, none followed by a line)."""
    if not recs or recs[-1]["type"] != "user":
        return False
    last = recs[-1]
    if (_flat_text((last.get("message") or {}).get("content")) or "").startswith(NO_TURN_MARKS):
        return False
    calls, out = {}, set()
    for rec in recs:
        blocks = _blocks(rec)
        if rec["type"] == "assistant":
            uses = {b["id"]: b.get("name") for b in blocks
                    if b.get("type") == "tool_use" and isinstance(b.get("id"), str)}
            calls |= uses
            out |= set(uses)
        else:
            answered = {b.get("tool_use_id") for b in blocks if b.get("type") == "tool_result"}
            out = out - answered if answered else set()
    return not out and not any(
        calls.get(b.get("tool_use_id")) == "StructuredOutput" and not b.get("is_error")
        for b in _blocks(last) if b.get("type") == "tool_result")


def native_in_flight(transcript):
    """§6: main's native runs still out, for its capsule after a compaction → ["scope (executor
    · age)"] — the summary can drop a launch, and main then does not know a result is owed.

    Out: launched in the background (`async_launched`), with its answer to the brief not
    complete yet (`native_answer`, the scribe's rule: an interim notification — the agent
    stopped with background work of its own still running — promises a final one, unless that
    work has ended or main has messaged the agent since), and no TaskStop of main's naming it.
    A run main stopped never notifies (measured, mlx-vlm: 4 of 4 Workflow runs, their run files
    `killed`, none notified across two later restarts); one that died with its process is
    notified `stopped` when the session resumes (measured, 2.1.282). A foreground call is not
    here: main compacts only once it has returned."""
    if not transcript:
        return []
    transcript = Path(transcript)
    launches, notes, _ = native_scan(transcript)
    if not launches:
        return []
    stopped = {b["input"].get("task_id") for rec in _main_calls(transcript, "TaskStop")
               for b in _blocks(rec)
               if b.get("name") == "TaskStop" and isinstance(b.get("input"), dict)}
    now = datetime.now(timezone.utc)
    out = []
    for key, launch in launches.items():
        task = launch["result"].get("taskId") if launch["tool"] == "Workflow" else key
        if launch["result"].get("status") != "async_launched" or task and task in stopped:
            continue
        run = native_run(launch, key, transcript.with_suffix(""))
        run["notes"] = [n for n in notes if n.task == task or n.tuid == launch["tuid"]]
        if run["skip"] or native_answer(run, None, now) is not None:
            continue
        age = f" · {age_label(run['t'], now)}" if run["t"] else ""
        out.append(f"{one_line(run['scope'] or key, 44)} ({run['executor']}{age})")
    return out


def _main_calls(transcript, tool):
    """Main's assistant records that call `tool`."""
    try:
        f = transcript.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for raw in f:
            if f'"{tool}"' not in raw or '"tool_use"' not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if (isinstance(rec, dict) and rec.get("type") == "assistant"
                    and not rec.get("isSidechain")):
                yield rec


def _blocks(rec):
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _main_prompts(transcript):
    """The promptIds of main's user lines, in the order main took them."""
    prompts = {}
    try:
        f = transcript.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return []
    with f:
        for raw in f:
            if '"promptId"' not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if (isinstance(rec, dict) and rec.get("type") == "user" and not rec.get("isSidechain")
                    and isinstance(rec.get("promptId"), str)):
                prompts[rec["promptId"]] = None
    return list(prompts)


def _tail_lines(path, keep, n=128):
    """The last `n` raw lines of a file that `keep` passes — the whole file is streamed, only
    they are held."""
    tail = collections.deque(maxlen=n)
    try:
        f = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return tail
    with f:
        for raw in f:
            if keep(raw):
                tail.append(raw)
    return tail


def _latest_turn(path, main=True):
    """The user and assistant records of a Claude Code transcript from just after its
    second-latest response to the end, oldest first — its latest response whole: that is every
    line with the response's message id, and a response's lines interleave with its tools'
    results (485 of 6,390 responses on this machine). The host's meta messages are kept (a
    channel message arrives as one and starts a turn, measured); in main's, the sidechain lines
    older hosts wrote there for a subagent are left out (an agent's own transcript is all
    sidechain). Only the tail is parsed."""
    out, seen, latest = [], False, None
    for raw in reversed(_tail_lines(path, lambda raw: '"user"' in raw or '"assistant"' in raw)):
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not (isinstance(rec, dict) and rec.get("type") in ("user", "assistant")
                and not (main and rec.get("isSidechain"))):
            continue
        if rec["type"] == "assistant":
            mid = (rec.get("message") or {}).get("id")
            if seen and (mid is None or mid != latest):
                break
            seen, latest = True, mid
        out.append(rec)
    return out[::-1]


def native_refs(rows, runs):
    """Point each run at the dispatch that records it: its own `ag-` row, or one main wrote
    itself (src=cli) for the same run — within the same 24h, with a scope equal to the run's
    description, case and spacing aside. That match is exact on purpose, and it is the limit:
    a scope main paraphrased is not recognized, so the run is listed and recorded a second time
    — one duplicate row — where a similarity guess would cost a real run its record (§3.5.6b
    measured those). Main does paraphrase: all 4 of its rows for Agent runs in this repo's
    ledger did, so in practice this catches a row main wrote from the launch's own words."""
    ids = {e.get("id") for e in rows if e.get("ev") == "dispatch"}
    cli = [e for e in rows if e.get("ev") == "dispatch" and e.get("src") == "cli"
           and not str(e.get("id", "")).startswith(NATIVE_PREFIX)]
    taken = {r["ref"] for r in runs.values() if r["ref"]}
    for run in runs.values():
        if run["id"] in ids:
            run["ref"] = run["id"]
        if run["ref"]:
            continue
        key = _scope_key(run["scope"])
        for e in cli:
            t = event_time(e)
            if (e.get("id") in taken or not key or _scope_key(e.get("scope")) != key
                    or (run["t"] and (t is None or abs(t - run["t"]) > timedelta(
                        hours=NATIVE_LIST_H)))):
                continue
            run["ref"] = e.get("id")
            taken.add(run["ref"])
            break


def native_open(hp, transcript, since, end):
    """Step 3c's first half, before the clerk → {runs, listed, window, recorded}, or None when
    the transcript launched nothing (a Codex rollout never does: its spawn_agent children are
    the clerk's, from the digest, as before). `listed` is what the clerk is asked for: each run
    with no row yet, launched within NATIVE_LIST_H — a run the clerk skips stays listed next
    window. `window` is the top-level runs this window touched, which is when a fork, subagent
    or workflow dispatch under any other id can only be a restatement of one. `recorded`
    collects the runs this clerk output gives a row (`native_record`)."""
    runs = native_index(transcript, since, end)
    if not runs:
        return None
    native_refs(read_ledger(hp), runs)
    now = datetime.now(timezone.utc)
    listed = {r["id"]: r for r in runs.values() if not r["ref"] and not r["skip"]
              and (r["t"] is None or now - r["t"] <= timedelta(hours=NATIVE_LIST_H))}
    window = [r for r in runs.values() if r["seen"] and not r["parent"]]
    return {"runs": runs, "listed": listed, "window": window, "recorded": set()}


def native_section(native):
    """The payload section that asks the clerk for each listed run's kind (turn-scribe.md
    rule 1). Absent when nothing is listed, so a window with no native run is what it was."""
    if not native or not native["listed"]:
        return ""
    return "# native runs to record\n\n" + "\n".join(
        f"- {r['id']} · {r['executor']} · {r['scope']} · brief: "
        f"{one_line(r['brief'], NATIVE_BRIEF_CHARS)}" for r in native["listed"].values()) + "\n\n"


def native_model(m):
    """`claude-opus-5-5[1m]` → `claude-opus-5-5`, `claude-haiku-4-5-20251001` →
    `claude-haiku-4-5`: the prices.yaml key. The context tag and the snapshot date name the
    same model."""
    return re.sub(r"-\d{8}$", "", re.sub(r"(?:\[[^\]]*\])+$", "", m.strip()))


def _count(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def native_stats(run):
    """One pass over the run's own transcript(s) — an agent's, or every agent's of a Workflow
    run — cached on the run → {usage: {model: Counter(tin, tcached, tout)}, msgs:
    Counter(model), effort: Counter, edits: [path]}.

    Measured on this machine (Claude Code 2.1.281): one API message spans several assistant
    lines whose output_tokens grow as it streams, so a message counts once, at its last line.
    A fork's transcript opens with the parent's own launching message copied in (main's
    message.id, main's usage) ahead of its first user line — counted, it would bill main's call
    to the fork — so nothing before the first user line counts. `effort` is a top-level field
    of each assistant line, absent on haiku. A `<synthetic>` model line is the host's, not an
    API call. Files an agent changed through the shell show only as `edited_text_file`
    attachments, and only when it had read them first."""
    if "stats" in run:
        return run["stats"]
    usage, msgs, effort, edits = {}, collections.Counter(), collections.Counter(), {}
    files = (sorted(run["files"].glob("agent-*.jsonl")) if run["executor"] == "workflow"
             else [run["files"]])
    for path in files:
        last, started = {}, False
        try:
            f = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for i, raw in enumerate(f):
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                kind = rec.get("type") if isinstance(rec, dict) else None
                started = started or kind == "user"
                if not started:
                    continue
                if kind == "attachment":
                    att = rec.get("attachment")
                    if (isinstance(att, dict) and att.get("type") == "edited_text_file"
                            and isinstance(att.get("filename"), str)):
                        edits[att["filename"]] = None
                msg = rec.get("message")
                if kind != "assistant" or not isinstance(msg, dict):
                    continue
                model = msg.get("model")
                if isinstance(model, str) and model and not model.startswith("<"):
                    last[msg.get("id") or i] = (model, msg.get("usage"), rec.get("effort"))
                for b in msg.get("content") if isinstance(msg.get("content"), list) else []:
                    inp = b.get("input") if isinstance(b, dict) else None
                    if (isinstance(inp, dict) and b.get("type") == "tool_use"
                            and b.get("name") in NATIVE_EDIT_TOOLS):
                        p = inp.get("file_path") or inp.get("notebook_path")
                        if isinstance(p, str):
                            edits[p] = None
        for model, u, eff in last.values():
            model = native_model(model)
            msgs[model] += 1
            if isinstance(eff, str) and re.fullmatch(r"[a-z]+", eff):
                effort[eff] += 1
            if isinstance(u, dict):
                c = usage.setdefault(model, collections.Counter())
                read = _count(u.get("cache_read_input_tokens"))
                c["tin"] += (_count(u.get("input_tokens")) + read
                             + _count(u.get("cache_creation_input_tokens")))
                c["tcached"] += read
                c["tout"] += _count(u.get("output_tokens"))
    run["stats"] = {"usage": usage, "msgs": msgs, "effort": effort, "edits": list(edits)}
    return run["stats"]


def native_exec(run):
    """exec for a run, from what it ran on: its most-used model (a Workflow run's across its
    agents), else the model the host resolved at launch — an observation too — and its effort,
    `inherit` when no line records one. None when no model is readable yet: the row waits."""
    stats = native_stats(run)
    model = stats["msgs"].most_common(1)[0][0] if stats["msgs"] else run["resolved"]
    model = native_model(model) if isinstance(model, str) else ""
    if not re.fullmatch(r"[^/\s]+", model):
        return None
    effort = stats["effort"].most_common(1)[0][0] if stats["effort"] else "inherit"
    return f"{run['executor']}/{model}/{effort}"


def native_task(hp, brief):
    """The one task id in tasks.yaml the brief names as a whole token; zero or several → None."""
    ids = {t.get("id") for t in tasks_load(hp)["tasks"] if isinstance(t.get("id"), str)}
    hits = [tid for tid in ids if re.search(rf"(?<![\w/-]){re.escape(tid)}(?![\w/-])",
                                            brief or "")]
    return hits[0] if len(hits) == 1 else None


def native_record(hp, native, run, kind):
    """The dispatch row for a listed run, the clerk's kind on everything code observed → None
    when it landed, else the reason it did not. The run stays listed for the next window."""
    if kind not in plan_kinds():
        return (f"ev=dispatch: kind {kind!r} for {run['id']} is not one of "
                f"{' '.join(sorted(plan_kinds()))} — the run stays listed")
    exec_ = native_exec(run)
    if exec_ is None:
        return f"ev=dispatch: no model readable for {run['id']} yet — the run stays listed"
    e = {"ev": "dispatch", "id": run["id"], "kind": kind, "exec": exec_, "scope": run["scope"]}
    task = native_task(hp, run["brief"])
    if task:
        e["task"] = task
    if run["parent"]:
        e["parent"] = run["parent"]
    err = validate_event(e)
    if err:
        return err
    append_event(hp, e, src="scribe")
    run["ref"] = run["id"]
    native["listed"].pop(run["id"], None)
    native["recorded"].add(run["id"])
    return None


def native_take(hp, e, native, alias):
    """The clerk's event, when it concerns a native run → (True, reason or None); anything
    else → (False, None) and the ordinary rules apply.

    A listed id gets its row from `native_record` — the clerk's kind, nothing else of its:
    exec, scope, task and parent are observed. A fork, subagent or workflow dispatch under any
    other id, in a window that touched a native run, is a restatement and is dumped — on
    Claude Code the Agent and Workflow tools are the only way one starts, and hippo indexed
    those. A restatement still says which run it meant when that is unambiguous: its scope is
    the description of one run — this window's first, then any indexed one — or, naming none,
    the window touched one run and this output did not already record that run under its own
    id (had it, the restatement meant another run: a verdict on an older run must not move onto
    the only one in sight). That run is recorded with the restatement's kind if it has no row
    yet, and an outcome naming the restated id is moved to the run's row — the verdict main
    typed must not be lost to the clerk's choice of id.

    An outcome naming a listed run whose dispatch this output did not record — refused, or
    skipped — is dumped with that reason: the verdict was in this window's digest, and the next
    clerk will not see it, so the dump is its only record."""
    if (native and isinstance(e, dict) and e.get("ev") == "outcome"
            and isinstance(e.get("ref"), str) and e["ref"] in native["listed"]):
        return True, (f"ev=outcome: {e['ref']} has no row — its dispatch was refused or "
                      "skipped in this output, so this dump is the only record of the verdict")
    if (not native or not isinstance(e, dict) or e.get("ev") != "dispatch"
            or not isinstance(e.get("id"), str)):
        return False, None
    did = e["id"]
    if did in native["listed"]:
        return True, native_record(hp, native, native["listed"][did], e.get("kind"))
    if did.startswith(NATIVE_PREFIX):
        return True, (f"ev=dispatch: {did} is not under `# native runs to record` — "
                      "recorded already, or no run hippo found")
    window = native["window"]
    if not window or str(e.get("exec", "")).split("/")[0] not in ("fork", "subagent", "workflow"):
        return False, None
    key = _scope_key(e.get("scope"))
    top = [r for r in native["runs"].values() if not r["parent"]]
    hits = ([r for r in window if key and _scope_key(r["scope"]) == key]
            or [r for r in top if key and _scope_key(r["scope"]) == key]
            or [r for r in window if len(window) == 1 and r["id"] not in native["recorded"]])
    run = hits[0] if len(hits) == 1 else None
    reason = ("ev=dispatch: hippo lists every Agent, fork and Workflow run under "
              "`# native runs to record` — record it there, under its listed id")
    if run is not None and not run["ref"] and run["id"] in native["listed"]:
        err = native_record(hp, native, run, e.get("kind"))
        reason += f"; {run['id']} not recorded from it either: {err}" if err else ""
    if run is not None and run["ref"]:
        alias[did] = run["ref"]
        reason += f"; its run is {run['ref']}, and an outcome naming {did} lands there"
    return True, reason


def native_workflow_result(run):
    """A Workflow run's whole result — the run file holds it; the notification's copy is cut
    at ~8k. A JSON result goes to the judge as the structure it is, not as a string of JSON
    whose every quote and non-ASCII character is escaped, so one that does not fit is trimmed
    by its structure, never cut through the middle (`fit_triage_state`)."""
    res = _read_json(run["summary"]).get("result")
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except ValueError:
            return res.strip() or None
    return res


def worktree_changes(hp, wt):
    """What an isolated agent changed, as git facts from its own worktree, or None when no base
    can be decided honestly — the caller then uses the edit list. A worktree whose HEAD never
    left where it was created holds all of its work uncommitted: the base is HEAD. One that
    moved (commits, a reset) is read against its merge-base with the main checkout's HEAD —
    unless its HEAD is already in main's history, where the fork point is gone."""
    if not wt.is_dir():
        return None

    def git(d, *a):
        try:
            r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True,
                               timeout=TRIAGE_GIT_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    head = git(wt, "rev-parse", "HEAD")
    moved = set((git(wt, "reflog", "show", "--format=%H", "HEAD") or "").split())
    if not head or not moved:
        return None
    base = head
    if moved != {head}:
        common = {git(d, "rev-parse", "--path-format=absolute", "--git-common-dir")
                  for d in (wt, hp.parent)}
        main = git(hp.parent, "rev-parse", "HEAD")
        if not main or len(common) != 1 or None in common:
            return None
        if git(wt, "merge-base", "--is-ancestor", head, main) is not None:
            return None
        base = git(wt, "merge-base", head, main)
        if not base:
            return None
    status, diff = git(wt, "status", "--short") or "", git(wt, "diff", "--stat", base) or ""
    return "\n".join([f"git in the agent's worktree {wt}, against {base[:12]}:",
                      *status.splitlines()[:TRIAGE_GIT_LINES],
                      *diff.splitlines()[:TRIAGE_GIT_LINES]])


def native_changes(hp, run):
    wt = run["meta"].get("worktreePath")
    facts = worktree_changes(hp, Path(wt)) if isinstance(wt, str) and wt else None
    if facts is not None:
        return facts
    edits = native_stats(run)["edits"]
    return f"{NATIVE_EDITS_HEAD}:\n" + ("\n".join(edits[:TRIAGE_GIT_LINES]) or "(none)")


def native_triage(hp, run, ref, kind):
    """A run's answer to its brief, read the way the wrapper reads a lane at exit (§3.6) → True
    when the judge was asked. brief = the call's prompt (a Workflow's script), report = the
    answer's <result>s in order — its notifications up to its first final one, or its interim
    ones when no final one came (`native_answer`) — or a Workflow's whole result; rc 0 only
    when it completed, and the changes from the run's own transcript or worktree. A state over
    the judge's budget is fitted like any lane's (`fit_triage_state`). When the judge does not
    answer — it refuses a state no fitting rescued as over budget — its reason goes to
    stderr."""
    answer = run["answer"]
    ex = {"rc": 0 if answer[-1].status == "completed" else 1, "check_rc": None}
    if run["executor"] == "workflow":
        report = native_workflow_result(run)
    else:
        report = "\n\n".join(n.report for n in answer if n.report) or None
    if not run["brief"] or (ex["rc"] == 0 and not report):
        print(f"native: {ref} has no brief or no report to read — no triage", file=sys.stderr)
        return False
    state = triage_state(run["scope"], kind, run["brief"], ex, None, report, None, None, None)
    state["changes"] = native_changes(hp, run)
    t = triage(hp, state, ex, ref, src="scribe")
    if t["route"] is None:
        print(f"native: {ref} no triage — the judge did not answer ({t['jev']['reason']})",
              file=sys.stderr)
    return True


def native_settle(hp, native):
    """Step 3c's second half, after the clerk: what each recorded run cost, and — with the
    judge on — what its answer to its brief says.

    Usage is written for every run with a row that notified in this window, or that has
    notified and still has no usage row (its row landed in a later window than its
    completion). The rows are cumulative per model; one equal to the last row for (ref, model)
    is not written again, so re-reading a window writes nothing twice, and a resumed agent
    gets a new row at its next completion. Triage reads only a run's answer to its brief
    (`native_answer`), only in the window where that answer became complete, at most once per
    dispatch and NATIVE_TRIAGE_MAX calls per window: a later notification — a resumed
    agent's, or a final one after an answer made of interim ones — is never triage material,
    even when the first reading failed (the gap is the record)."""
    rows = read_ledger(hp)
    runs = native["runs"]
    native_refs(rows, runs)
    kinds = {e.get("id"): e.get("kind") for e in rows if e.get("ev") == "dispatch"}
    last = {(e.get("ref"), e.get("model")): e for e in rows if e.get("ev") == "usage"}
    costed = {ref for ref, _ in last}
    triaged = {e.get("ref") for e in rows if e.get("ev") == "triage"}
    judge_on, asked = jev_backend(hp) != "off", 0
    now = datetime.now(timezone.utc)
    for run in runs.values():
        ref = run["ref"]
        if not ref or run["skip"] or not run["notes"]:
            continue
        recent = run["t"] is None or now - run["t"] <= timedelta(hours=NATIVE_LIST_H)
        if run["done_now"] or (ref not in costed and recent):
            for model, c in sorted(native_stats(run)["usage"].items()):
                e = {"ev": "usage", "ref": ref, "model": model, "tokens": c["tin"] + c["tout"],
                     "tin": c["tin"], "tcached": c["tcached"], "tout": c["tout"]}
                prev = last.get((ref, model)) or {}
                if all(prev.get(k) == e[k] for k in ("tokens", "tin", "tcached", "tout")):
                    continue
                if not (validate_event(e) or check_ref(hp, e)):
                    last[(ref, model)] = append_event(hp, e, src="scribe")
        if (judge_on and run["first_now"] and ref not in triaged
                and asked < NATIVE_TRIAGE_MAX and native_triage(hp, run, ref, kinds.get(ref))):
            asked += 1
            triaged.add(ref)


def native_guard(hp, fn, *a):
    """Run one piece of step 3c; a bug in it is dumped to failures/, never raised. The step is
    an addition: a crash here would cost the window its clerk, and a scribe that crashes never
    advances its cursor past the window that broke it."""
    try:
        return fn(*a)
    except Exception:  # noqa: BLE001 — dumped, never swallowed
        p = dump_failure(hp, "native", traceback.format_exc())
        print(f"native: step failed — dump: {p}", file=sys.stderr)
        return None


def task_ends(hp, digest):
    """Step 9 (judge on only): does this window's digest show an open task's work finished or
    abandoned? One request over every open task, then that one task alone beside the digest for
    each answer at or over `recheck_at`; a recheck at or over `flag_at` is a flag, shown by the
    capsule and never applied (§3.5.9, §6). Its own requests, never the gate's: the gate's
    recall was measured on a digest-only state.

    Only the scribe writes the file, and its caller holds scribe.lock: the file is read, merged
    with this window's flags, pruned of every flag that no longer shows, and written whole —
    two sessions' scribes never lose each other's flags. A request that fails is metered, named
    on stderr and flags nothing; the clerk has already run."""
    if jev_backend(hp) == "off":
        return
    tasks = [t for t in tasks_load(hp)["tasks"]
             if t.get("status") in OPEN_STATUSES and isinstance(t.get("id"), str)]
    if not tasks:
        return
    read_at = now_iso()  # a flag is about the tasks as read now: any later write supersedes it
    policy = jev_policy("task-end")
    recheck_at, flag_at = float(policy.get("recheck_at", 1.0)), float(policy.get("flag_at", 1.0))

    def as_state(t):
        return {"id": t["id"], "title": t.get("title") or "", "notes": t.get("notes") or []}

    def ask(state, questions):
        answers, meta = judge(hp, "task-end", state, questions)
        append_event(hp, {"ev": "clerk", "name": "jev-task-end", "ok": meta["ok"],
                          "ms": meta["ms"], "tokens": meta["tokens"]}, src="scribe")
        if answers is None:
            print(f"jev-task-end: {meta['reason']}", file=sys.stderr)
        return answers

    first = ask({"digest": digest, "tasks": [as_state(t) for t in tasks]},
                {f"done_{i}": jev_questions("task-end", i=i)[f"done_{i}"]
                 for i in range(len(tasks))})
    if first is None:
        return
    recheck = {"done": jev_questions("task-end")["done"]}
    flags = {}
    for i, t in enumerate(tasks):
        p1 = jev_noul(first, f"done_{i}")
        if p1 is None or p1 < recheck_at:
            continue
        p2 = jev_noul(ask({"digest": digest, "task": as_state(t)}, recheck), "done")
        if p2 is not None and p2 >= flag_at:
            flags[t["id"]] = {"t": read_at, "p": p2}
    by_id = {t["id"]: t for t in tasks_load(hp)["tasks"] if isinstance(t.get("id"), str)}
    kept = {tid: f for tid, f in {**task_flags(hp), **flags}.items()
            if task_flag_shows(by_id.get(tid), f)}
    if kept or (hp / TASK_FLAGS).exists():
        write_durable(hp / TASK_FLAGS, json.dumps(kept, ensure_ascii=False, indent=2) + "\n")


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

    # 3c. Native runs (DESIGN §3.5.3c), in every mode: indexed before anything can return, so a
    # completion in a window the prefilter skips still gets its cost recorded. The rows need
    # the clerk's kind; usage and triage follow it (settle), on every path out of here.
    native = native_guard(hp, native_open, hp, transcript, since, end)

    def settle():
        if native:
            native_guard(hp, native_settle, hp, native)

    # 3. Deterministic prefilter: with no substantive activity, skip the model call entirely
    # (digest line shapes: "[123] TOOL Bash: …" / "[124] USER: …")
    if not any(SUBSTANTIVE.match(ln) for ln in digest.splitlines()):
        settle()
        save_cursor()
        return

    # 3b. The judge gate (DESIGN §3.5.3b): five yes/no judgments over the digest, metered and
    # handed to the clerk as advisory hints. It never decides whether the clerk runs — measured
    # on 90 real windows, a skip rule at any useful floor lost ~14% of real events to save ~1
    # clerk call in 30 (almost every window that passes the prefilter holds substantive work).
    # With the backend off the gate does not exist — no row, no note, no change in behavior.
    hints = ""
    if jev_backend(hp) != "off":
        questions = jev_questions("scribe-gate")
        answers, jmeta = judge(hp, "scribe-gate", {"digest": digest}, questions)
        append_event(
            hp,
            {"ev": "clerk", "name": "jev-gate", "ok": jmeta["ok"], "ms": jmeta["ms"],
             "tokens": jmeta["tokens"]},
            src="scribe",
        )
        if answers is None:
            print(f"jev-gate: {jmeta['reason']}", file=sys.stderr)
        else:
            probs = {q: float(answers[q]["noul"]) for q in questions
                     if isinstance(answers.get(q), dict)
                     and isinstance(answers[q].get("noul"), (int, float))}
            if probs:
                hints = (
                    "# gate hints\n\nadvisory probabilities from a separate judge over the "
                    "same digest; the digest is the only evidence\n\n"
                    + "\n".join(f"- {q}: {p:.2f}" for q, p in probs.items())
                    + "\n\n"
                )

    payload = (
        f"# live directives\n\n{directive_roster(hp)}\n\n"
        f"# dispatches already recorded\n\n{dispatch_roster(hp)}\n\n"
        f"{native_section(native)}"
        f"{hints}"
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
        settle()
        save_cursor()
        append_event(hp, {**meter, "ok": False}, src="scribe")
        auto_distill(hp)
        task_ends(hp, digest)
        die(f"scribe failed: {reason} — dump: {p}")

    if rc != 0:
        # The backend's own first words ride the reason line (clerk_run.sh puts the cause
        # first on stderr): it is the line the capsule quotes once the failures repeat (§6).
        cause = next((ln.strip() for ln in err.splitlines() if ln.strip()), "")
        fail(f"clerk rc={rc}" + (f": {cause}" if cause else ""))
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
    # Dispatches first: an outcome in the same output may name one — a listed native run's
    # above all — and its ref has to exist when it is checked. Listed native runs lead, so a
    # restatement (native_take) is read knowing which runs this output recorded under their own
    # ids. The sort is stable.
    listed = native["listed"] if native else {}

    def order(e):
        if not (isinstance(e, dict) and e.get("ev") == "dispatch"):
            return 2
        return 0 if isinstance(e.get("id"), str) and e["id"] in listed else 1

    events = sorted(obj.get("events", []), key=order)
    alias = {}  # a restated dispatch id → the native run's row (native_take)
    for e in events:
        # lifetime is retired (§3.2) and the prompt no longer asks for it; a clerk that still
        # says `turn` must not make its directive invisible to the view.
        if isinstance(e, dict) and e.get("ev") == "directive":
            e.pop("lifetime", None)
        if (isinstance(e, dict) and e.get("ev") == "outcome" and isinstance(e.get("ref"), str)
                and e["ref"] in alias):
            e["ref"] = alias[e["ref"]]
        taken, verr = native_guard(hp, native_take, hp, e, native, alias) or (False, None)
        if not taken:
            verr = (validate_event(e) or validate_scribe_event(e) or check_ref(hp, e)
                    or check_scribe_outcome(hp, e))
            if not verr:
                append_event(hp, e, src="scribe")
        if verr:
            dump_failure(hp, "scribe", f"{verr}\n\n{json.dumps(e, ensure_ascii=False, indent=2)}\n")
    if obj.get("worklog", "").strip():
        worklog_append(hp, obj["worklog"].strip())
    settle()
    save_cursor()
    append_event(hp, {**meter, "ok": True}, src="scribe")
    auto_distill(hp)
    # 9. Open tasks that look ended (DESIGN §3.5.9) — last, on both paths: it reads nothing
    # the scribe wrote (the clerk never writes tasks.yaml) and nothing waits on it.
    task_ends(hp, digest)


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
        "--inject", action="store_true", help="what the hooks inject (§3.4, §6)"
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
        "--result", choices=sorted(ENUMS[("outcome", "result")]),
        help="required in scalar mode; with --from-batch each stdin row carries its own",
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
    a.add_argument(
        "--from-batch", metavar="JOURNAL",
        help="bulk mode (§3.6): read verdict rows as JSON-lines on stdin "
             '({"entry": …, "attempt": …, "result": …, "note": …} + optional attr/rework/by), '
             "resolved through this batch journal's latest exited attempts onto still-unjudged "
             "claims — serialization of verdicts already reached individually, never judgment",
    )
    a.add_argument("--dry-run", action="store_true",
                   help="with --from-batch: validate the whole input, write nothing")
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
    # Retired (§3.2): accepted so old callers keep working, never stored.
    a.add_argument("--lifetime", help=argparse.SUPPRESS)
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
    a.add_argument("--days", type=int, default=DISTILL_DAYS)
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
