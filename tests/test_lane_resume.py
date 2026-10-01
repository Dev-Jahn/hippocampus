"""A lane that stopped short (DESIGN §3.6): the cause on every triage line, the `resume:` line
that continues a lane's codex session, and the wrapper's own resume of a session that stopped
on a model-capacity error.

Contract under test: the triage line names the cause the judge named, in the one shape every
surface prints; a lane that ended killed, or whose cause a resume can clear, ends on the exact
`hippo dispatch … -- <codex args> resume <session> "<continuation>"` command — single dispatch
and batch lane alike, quoted so a hostile scope cannot break it, and absent when there is no
session to resume; and a lane whose codex exited non-zero on the measured capacity line is
resumed in place — the same session, up to len(CAPACITY_WAITS) times — staying one lane: one
record with its attempts, one raw log, the last attempt's report, one usage row (the rollout
is cumulative across a resume, so nothing is counted twice), one triage. A signal during the
wait ends it at once, killed, one during a resume reaches the resumed codex, and any other
failure is never resumed.

The lanes run in-process (hippo_cli.run_dispatch / run_batch) so CAPACITY_WAITS can be cut to
hundredths of a second by monkeypatch; no test here sleeps for the real schedule. The stub
codex is a python script on PATH: it records its argv, prints the measured banner, appends a
cumulative token_count row to its session's rollout, and fails on the capacity line for its
first $STUB_FAIL_RUNS runs.
"""
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import REPO_ROOT, read_ledger
from test_batch import _journal, _manifest, _outdir
from test_batch_harvest import ACCEPT, _jev, _mock
from test_lanes import WATCHDOG

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402

SID = "01a0f82c-1211-7261-8897-3c26f9bdc737"
PROMPT = "Implement the accounts CLI, its handler and the sync job; run the tests; report."
CAPACITY_LINE = "ERROR: Selected model is at capacity. Please try a different model."
DEFAULT = {"noul": 0.5, "choice": "none", "score": 1.0}

STUB = r'''#!/usr/bin/env python3
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

log = Path(os.environ["STUB_ARGV_LOG"])
runs = (len(log.read_text().splitlines()) if log.exists() else 0) + 1
with log.open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
sid = os.environ.get("STUB_SESSION", "")
err = sys.stderr
err.write("OpenAI Codex v0.159.2\n--------\nmodel: gpt-6.1-sol\n")
if sid:
    err.write(f"session id: {sid}\n")
err.write(f"--------\ncodex\nWorking on attempt {runs}.\n")
err.flush()
# The session's rollout: one more cumulative token_count row per run, as codex appends one to
# the same file when a session is resumed (measured, 0.159.2).
total = None
rollout = os.environ.get("STUB_ROLLOUT")
if rollout:
    p = Path(rollout)
    rows = p.read_text().splitlines() if p.exists() else []
    last = json.loads(rows[-1])["payload"]["info"]["total_token_usage"] if rows else {
        "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
        "reasoning_output_tokens": 0, "total_tokens": 0}
    now = {"input_tokens": last["input_tokens"] + 900,
           "cached_input_tokens": last["cached_input_tokens"] + 500,
           "output_tokens": last["output_tokens"] + 100,
           "reasoning_output_tokens": last["reasoning_output_tokens"],
           "total_tokens": last["total_tokens"] + 1000}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    with p.open("a") as f:
        f.write(json.dumps({"timestamp": stamp, "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": now}}}) + "\n")
    total = now["input_tokens"] - now["cached_input_tokens"] + now["output_tokens"]
out = None
args = sys.argv[1:]
for i, a in enumerate(args):
    if a in ("-o", "--output-last-message") and i + 1 < len(args):
        out = args[i + 1]
footer = f"tokens used\n{total:,}\n" if total is not None else ""
if runs == int(os.environ.get("STUB_HANG_RUN", "0")):
    import time
    err.write("exec\n/bin/zsh -lc 'sleep 60' in /w\n")
    err.flush()
    time.sleep(60)
if runs <= int(os.environ.get("STUB_FAIL_RUNS", "0")):
    if os.environ.get("STUB_MODE", "capacity") == "capacity":
        err.write("ERROR: Selected model is at capacity. Please try a different model.\n" * 2)
    else:
        err.write("ERROR: unexpected status 500 Internal Server Error\n")
    err.write(footer)
    sys.exit(1)
msg = f"Report from attempt {runs}.\n"
sys.stdout.write(msg)
if out:
    Path(out).write_text(msg)
err.write(footer)
'''


class Lab:
    """One test's stub codex, its argv log and its session's rollout."""

    def __init__(self, project, tmp_path):
        self.project = project
        self.cwd = str(project.resolve())  # what the wrapper's Path.cwd() reads
        self.argv_log = tmp_path / "argv.jsonl"
        self.rollout = (tmp_path / "home" / ".codex" / "sessions" / "2026" / "10" / "01"
                        / f"rollout-2026-10-01T15-53-53-{SID}.jsonl")
        self.rollout.parent.mkdir(parents=True)

    def argvs(self):
        if not self.argv_log.exists():
            return []
        return [json.loads(ln) for ln in self.argv_log.read_text().splitlines()]

    def record(self, did):
        return json.loads((self.project / ".hippo" / "lanes" / f"{did}.json").read_text())


@pytest.fixture
def lab(tmp_project, tmp_path, monkeypatch):
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "codex").write_text(STUB, encoding="utf-8")
    (stub / "codex").chmod(0o755)
    lab = Lab(tmp_project, tmp_path)
    monkeypatch.chdir(tmp_project)
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STUB_ARGV_LOG", str(lab.argv_log))
    monkeypatch.setenv("STUB_SESSION", SID)
    monkeypatch.setenv("STUB_ROLLOUT", str(lab.rollout))
    monkeypatch.setattr(hippo_cli, "CAPACITY_WAITS", (0.05, 0.05, 0.05))
    # run_dispatch installs the lane's handlers and exits; in-process they must be given back.
    before = {s: signal.getsignal(s) for s in hippo_cli.LANE_SIGNALS}
    yield lab
    for s, handler in before.items():
        signal.signal(s, handler)


def _judge(monkeypatch, tmp_path, answers, capture=None):
    for k, v in _jev(_mock(tmp_path, {"answers": answers, "default": DEFAULT}),
                     capture).items():
        monkeypatch.setenv(k, v)


def _dispatch(capfd, *codex_args, scope="accounts", task="impl/accounts"):
    with pytest.raises(SystemExit) as done:
        hippo_cli.run_dispatch(["--kind", "impl", "--scope", scope, "--task", task,
                                "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high",
                                "--skip-git-repo-check", *codex_args])
    out, err = capfd.readouterr()
    did = out.splitlines()[0].removeprefix("dispatch:")
    return done.value.code, did, out, err.splitlines()


def _rows(project, ev):
    return [e for e in read_ledger(project) if e.get("ev") == ev]


def _resume_line(argv):
    return "resume: " + shlex.join(argv) + f' "{hippo_cli.RESUME_CONTINUE}"'


# --------------------------------------------------------------------------
# the model-capacity resume (C)
# --------------------------------------------------------------------------

def test_a_lane_at_capacity_resumes_its_session_and_stays_one_lane(lab, capfd, monkeypatch,
                                                                    tmp_path):
    monkeypatch.setenv("STUB_FAIL_RUNS", "2")
    capture = tmp_path / "sent.json"
    _judge(monkeypatch, tmp_path, ACCEPT, capture)
    code, did, out, err = _dispatch(capfd, PROMPT)
    assert code == 0, err

    # The launch as given, then the same session resumed twice — codex's own options first,
    # `-C` naming where the lane ran, then `resume <session> "<continuation>"`.
    first, *resumes = lab.argvs()
    capture_flag = first[1:3]
    assert first[0] == "exec" and capture_flag[0] == "--output-last-message"
    assert first[3:] == ["-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high",
                         "--skip-git-repo-check", PROMPT]
    assert resumes == [["exec", *capture_flag, "-m", "gpt-6.1-sol", "-c",
                        "model_reasoning_effort=high", "--skip-git-repo-check",
                        "-C", lab.cwd, "resume", SID,
                        hippo_cli.CAPACITY_CONTINUE]] * 2

    rec = lab.record(did)
    assert [(a["rc"], a["reason"]) for a in rec["attempts"]] == [
        (1, "capacity"), (1, "capacity"), (0, "exit")]
    assert all(a["started"] for a in rec["attempts"]) and "waiting" not in rec
    assert (rec["status"], rec["rc"], rec["codex_session"]) == ("exited", 0, SID)
    assert rec["codex_args"] == ["-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high",
                                 "--skip-git-repo-check", "-C", lab.cwd]

    # One raw log with a separator before each resume; the report is the last attempt's.
    log = Path(rec["log"]).read_text(encoding="utf-8")
    assert log.count(CAPACITY_LINE) == 4
    assert log.count(f"--- hippo: resume 1 of 3 of session {SID} after a model-capacity") == 1
    assert log.count(f"--- hippo: resume 2 of 3 of session {SID} after a model-capacity") == 1
    assert Path(rec["report"]).read_text(encoding="utf-8") == "Report from attempt 3.\n"
    assert out.splitlines()[1:] == ["Report from attempt 3."], "stdout passes through: only the last attempt printed"

    # One usage row for the whole lane: the rollout's count is cumulative across the resumes
    # (1000 a run), so it is read once at the end — not three cumulative reads summed.
    (u,) = _rows(lab.project, "usage")
    assert (u["ref"], u["tokens"], u["tin"], u["tcached"], u["tout"]) == (
        did, 3000, 2700, 1500, 300)

    # One triage, at the very end, over the last attempt's report.
    (t,) = _rows(lab.project, "triage")
    assert t["ref"] == did and t["route"] == "accept-candidate"
    assert [e["name"] for e in _rows(lab.project, "clerk")].count("jev-harvest") == 1
    assert json.loads(capture.read_text())["state"]["report"] == "Report from attempt 3.\n"

    # The stream says what it is waiting for, and no line of it reads as a prompt.
    waits = [ln for ln in err if "model at capacity" in ln]
    assert [ln.split(" · ", 2)[2] for ln in waits] == [
        "model at capacity — resuming in 0.05s (resume 1 of 3)",
        "model at capacity — resuming in 0.05s (resume 2 of 3)"]
    assert any(ln.endswith(f"resumed: session {SID} (resume 2 of 3)") for ln in err)
    for ln in err:
        assert not any(p.search(ln) for p in WATCHDOG), ln
    assert not any(ln.startswith("resume: ") for ln in err), "it ended well: nothing to resume"


def test_a_lane_still_at_capacity_after_three_resumes_ends_and_says_how(lab, capfd,
                                                                        monkeypatch, tmp_path):
    monkeypatch.setenv("STUB_FAIL_RUNS", "99")
    _judge(monkeypatch, tmp_path, {**ACCEPT, "cause": {"choice": "transient", "confidence": 0.9}})
    code, did, _, err = _dispatch(capfd, PROMPT)
    assert code == 1
    assert len(lab.argvs()) == 4, "the launch and three resumes, no more"
    rec = lab.record(did)
    assert [(a["rc"], a["reason"]) for a in rec["attempts"]] == [(1, "capacity")] * 4
    assert (rec["status"], rec["rc"], rec["cause"]) == ("exited", 1, "transient")
    (u,) = _rows(lab.project, "usage")
    assert u["tokens"] == 4000
    assert len(_rows(lab.project, "triage")) == 1

    # The cause on the triage line, then the command that resumes the session, last.
    assert any(ln.endswith("model at capacity — all 3 resumes used; the lane ends")
               for ln in err)
    triage = ("triage failed · cause transient (done .95 · blocked .02 · ask .03 · creep .04 "
              "· verify no)")
    assert err[-2] == f"dispatch: {triage}" and rec["triage"] == triage
    resume = _resume_line(["hippo", "dispatch", "--kind", "impl", "--scope",
                           "accounts (resumed)", "--task", "impl/accounts", "--",
                           "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high",
                           "--skip-git-repo-check", "-C", lab.cwd, "resume", SID])
    assert err[-1] == resume

    # --watch reads the same lines off the record: the attempts, the triage, the resume.
    lines = hippo_cli.watch_lines(rec, "ended")
    assert lines[0].startswith(f"lane {did} exited rc=1 after ")
    assert lines[1] == "attempts: " + " · ".join(["rc=1 model at capacity"] * 4)
    assert lines[2] == triage and lines[-1] == resume


def test_a_signal_during_the_wait_ends_the_lane_at_once_killed(lab, capfd, monkeypatch):
    monkeypatch.setenv("STUB_FAIL_RUNS", "99")
    monkeypatch.setattr(hippo_cli, "CAPACITY_WAITS", (60,))
    lanes = lab.project / ".hippo" / "lanes"
    sent = {}

    def stop_when_waiting():
        t_end = time.monotonic() + 30
        while time.monotonic() < t_end:
            for p in lanes.glob("d*.json"):
                rec = hippo_cli.read_record(p)
                if rec and rec.get("waiting"):
                    # Only into the lane's own handler — never the test runner's default.
                    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
                        sent["at"] = time.monotonic()
                        os.kill(os.getpid(), signal.SIGTERM)
                    return
            time.sleep(0.05)

    threading.Thread(target=stop_when_waiting, daemon=True).start()
    code, did, _, err = _dispatch(capfd, PROMPT)
    assert code == 128 + signal.SIGTERM and "at" in sent
    assert time.monotonic() - sent["at"] < 5, "the 60s wait was cut short"
    assert len(lab.argvs()) == 1, "nothing resumed after the stop"
    rec = lab.record(did)
    assert (rec["status"], rec["signal"], rec["rc"]) == ("killed", "SIGTERM", 1)
    assert [(a["rc"], a["reason"]) for a in rec["attempts"]] == [(1, "capacity")]
    assert "waiting" not in rec and rec["ended"]
    assert err[-2].split(" · ", 2)[2].startswith("killed by SIGTERM rc=1 · 0 cmds")
    assert err[-1].startswith("resume: hippo dispatch --kind impl --scope 'accounts (resumed)'")


def test_a_signal_during_a_resume_reaches_the_resumed_codex(lab, capfd, monkeypatch):
    """Each attempt is a process group of its own: the trap must follow the lane to the new
    one, or a stop would hit the dead first attempt and leave the resume running."""
    monkeypatch.setenv("STUB_FAIL_RUNS", "1")
    monkeypatch.setenv("STUB_HANG_RUN", "2")
    lanes = lab.project / ".hippo" / "lanes"
    sent = {}

    def stop_when_resumed():
        t_end = time.monotonic() + 30
        while time.monotonic() < t_end:
            for p in lanes.glob("d*.json"):
                rec = hippo_cli.read_record(p)
                if rec and len(rec.get("attempts") or []) == 2 and rec.get("cmds") == 1:
                    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
                        sent["at"] = time.monotonic()
                        os.kill(os.getpid(), signal.SIGTERM)
                    return
            time.sleep(0.05)

    threading.Thread(target=stop_when_resumed, daemon=True).start()
    code, did, _, _ = _dispatch(capfd, PROMPT)
    assert code == 128 + signal.SIGTERM and "at" in sent
    assert time.monotonic() - sent["at"] < 5, "the resumed codex was stopped, not waited out"
    rec = lab.record(did)
    assert (rec["status"], rec["signal"]) == ("killed", "SIGTERM")
    assert [(a["rc"], a["reason"]) for a in rec["attempts"]] == [
        (1, "capacity"), (-signal.SIGTERM, "signal")]


def test_the_watch_line_shows_the_wait():
    until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 42))
    rec = {"id": "dwait", "started": hippo_cli.now_iso(), "cmds": 13, "last": "said: x",
           "waiting": {"resume": 1, "of": 3, "until": until}}
    (line,) = hippo_cli.watch_lines(rec, "running")
    assert line.startswith("lane dwait running · ")
    assert " · 13 cmds · model at capacity — resume 1 of 3 in 4" in line, line


@pytest.mark.parametrize("mode, session", [("error", SID), ("capacity", "")])
def test_no_resume_without_the_capacity_line_or_a_session(lab, capfd, monkeypatch, tmp_path,
                                                          mode, session):
    """Another failure is not a capacity loss — a resume would repeat it. And with no session
    id there is nothing to resume: no attempt, and no resume line even when the cause would
    earn one."""
    monkeypatch.setenv("STUB_FAIL_RUNS", "99")
    monkeypatch.setenv("STUB_MODE", mode)
    monkeypatch.setenv("STUB_SESSION", session)
    _judge(monkeypatch, tmp_path, {**ACCEPT, "cause": {"choice": "transient", "confidence": 0.9}})
    code, did, _, err = _dispatch(capfd, PROMPT)
    assert code == 1 and len(lab.argvs()) == 1
    rec = lab.record(did)
    assert [(a["rc"], a["reason"]) for a in rec["attempts"]] == [
        (1, "exit" if mode == "error" else "capacity")]
    assert not any("resuming" in ln for ln in err)
    if session:
        assert err[-1].startswith("resume: "), "a transient cause with a session earns one"
    else:
        assert err[-1].startswith("dispatch: triage failed · cause transient")
        assert hippo_cli.resume_command(rec) is None


def test_a_lane_that_resumes_an_earlier_lanes_session_pays_only_its_own_tokens(
        lab, capfd, monkeypatch):
    """The rollout already holds the first lane's count when its resume starts (measured: the
    resumed lane recorded the session's whole 7,653,151, the first lane's 416,650 included)."""
    past = {"input_tokens": 414185, "cached_input_tokens": 349440, "output_tokens": 2465,
            "reasoning_output_tokens": 1137, "total_tokens": 416650}
    lab.rollout.write_text(json.dumps({"timestamp": "2026-10-01T15:59:09.261Z",
                                       "type": "event_msg", "payload": {
                                           "type": "token_count",
                                           "info": {"total_token_usage": past}}}) + "\n")
    code, did, _, _ = _dispatch(capfd, "resume", SID, hippo_cli.RESUME_CONTINUE,
                                scope="accounts (resumed)")
    assert code == 0
    (u,) = _rows(lab.project, "usage")
    assert (u["tokens"], u["tin"], u["tcached"], u["tout"]) == (1000, 900, 500, 100)
    # A lane launched as a resume resumes the same way: its own `resume …` is not kept.
    assert lab.record(did)["codex_args"] == [
        "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high", "--skip-git-repo-check",
        "-C", lab.cwd]


# --------------------------------------------------------------------------
# a batch lane goes through the same machinery
# --------------------------------------------------------------------------

def _wave(project, timeout=3600):
    return _manifest(project, "wave.yaml", f"""\
        defaults: {{kind: impl, executor: codex, model: gpt-6.1-sol, effort: high,
                   args: ["--skip-git-repo-check"]}}
        entries:
          - {{id: acct, scope: "accounts", prompt: "{PROMPT}", task: impl/accounts,
             timeout: {timeout}}}
        """)


def _batch(capfd, manifest):
    with pytest.raises(SystemExit) as done:
        hippo_cli.run_batch(["--batch", str(manifest)])
    out, err = capfd.readouterr()
    return done.value.code, out, err.splitlines()


def test_a_batch_lane_at_capacity_resumes_through_the_same_machinery(lab, capfd, monkeypatch):
    monkeypatch.setenv("STUB_FAIL_RUNS", "1")
    manifest = _wave(lab.project)
    code, _, err = _batch(capfd, manifest)
    assert code == 0, err
    first, resumed = lab.argvs()
    assert first == ["exec", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high",
                     "--skip-git-repo-check", "--", PROMPT]
    assert resumed == ["exec", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high",
                       "--skip-git-repo-check", "-C", lab.cwd, "resume", SID,
                       hippo_cli.CAPACITY_CONTINUE]
    (ex,) = [r for r in _journal(manifest) if r["event"] == "exit"]
    assert (ex["rc"], ex["attempt"], ex["tokens"]) == (0, 1, 2000), "one exit, at the end"
    rec = lab.record(ex["dispatch"])
    assert [(a["rc"], a["reason"]) for a in rec["attempts"]] == [(1, "capacity"), (0, "exit")]
    assert (_outdir(manifest) / "acct.out").read_text() == "Report from attempt 2.\n"
    err_log = (_outdir(manifest) / "acct.err").read_text()
    assert err_log.count(f"--- hippo: resume 1 of 3 of session {SID}") == 1
    (u,) = _rows(lab.project, "usage")
    assert (u["ref"], u["tokens"]) == (ex["dispatch"], 2000)
    assert any("model at capacity — resuming in 0.05s (resume 1 of 3)" in ln for ln in err)


def test_a_batch_entrys_timeout_bounds_the_waits_too(lab, capfd, monkeypatch):
    monkeypatch.setenv("STUB_FAIL_RUNS", "99")
    monkeypatch.setattr(hippo_cli, "CAPACITY_WAITS", (60, 60, 60))
    code, _, err = _batch(capfd, _wave(lab.project, timeout=20))
    assert code == 1 and len(lab.argvs()) == 1
    assert any(ln.endswith("model at capacity — not resumed: the entry's timeout ends first")
               for ln in err)


def test_a_signal_to_a_batch_reaches_a_lane_in_its_wait(lab, capfd, monkeypatch):
    monkeypatch.setenv("STUB_FAIL_RUNS", "99")
    monkeypatch.setattr(hippo_cli, "CAPACITY_WAITS", (60,))
    manifest = _wave(lab.project)
    lanes = lab.project / ".hippo" / "lanes"
    sent = {}

    def stop_when_waiting():
        t_end = time.monotonic() + 30
        while time.monotonic() < t_end:
            for p in lanes.glob("d*.json"):
                rec = hippo_cli.read_record(p)
                if rec and rec.get("waiting"):
                    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
                        sent["at"] = time.monotonic()
                        os.kill(os.getpid(), signal.SIGTERM)
                    return
            time.sleep(0.05)

    threading.Thread(target=stop_when_waiting, daemon=True).start()
    code, _, _ = _batch(capfd, manifest)
    assert code == 128 + signal.SIGTERM and "at" in sent
    assert time.monotonic() - sent["at"] < 5
    (ex,) = [r for r in _journal(manifest) if r["event"] == "exit"]
    rec = lab.record(ex["dispatch"])
    assert (rec["status"], rec["signal"], rec["rc"], ex["rc"]) == ("killed", "SIGTERM", 1, 1)
    assert len(lab.argvs()) == 1


# --------------------------------------------------------------------------
# the resume line (B)
# --------------------------------------------------------------------------

def test_a_batch_lane_with_a_transient_cause_ends_on_its_resume_line(lab, capfd, monkeypatch,
                                                                     tmp_path):
    """A batch lane resumes as a single dispatch: the entry's labels, its codex options and
    `-C` naming the entry's cwd — the line is run from main's directory, not the lane's."""
    monkeypatch.setenv("STUB_FAIL_RUNS", "99")
    monkeypatch.setenv("STUB_MODE", "error")
    _judge(monkeypatch, tmp_path, {**ACCEPT, "cause": {"choice": "transient", "confidence": 0.9}})
    work = lab.project / "wt"
    work.mkdir()
    manifest = _manifest(lab.project, "wave.yaml", f"""\
        defaults: {{kind: fix, executor: codex, model: gpt-6-luna, effort: medium}}
        entries:
          - {{id: one, scope: "it's $(rm -rf ~) `x`", prompt: "{PROMPT}", cwd: wt, depth: 1,
             args: ["--skip-git-repo-check", "-c", "sandbox_permissions=[\\"a b\\"]"]}}
        """)
    code, out, err = _batch(capfd, manifest)
    assert code == 1
    (ex,) = [r for r in _journal(manifest) if r["event"] == "exit"]
    rec = lab.record(ex["dispatch"])
    assert rec["triage"].startswith("triage failed · cause transient (") and \
        rec["cause"] == "transient"
    assert f"[1/1] one rc=1 check=- {ex['dispatch']} triage=failed cause=transient" in err
    lines = hippo_cli.watch_lines(rec, "ended")
    assert lines[-1] == _resume_line([
        "hippo", "dispatch", "--kind", "fix", "--scope", "it's $(rm -rf ~) `x` (resumed)",
        "--depth", "1", "--", "-m", "gpt-6-luna", "-c", "model_reasoning_effort=medium",
        "--skip-git-repo-check", "-c", 'sandbox_permissions=["a b"]', "-C", str(work.resolve()),
        "resume", SID])
    # The harvest's route column names the cause too, as it always did for a failure.
    assert any(ln.split()[:5] == ["one", "1", "-", "-", "failed"] and "transient" in ln
               for ln in out.splitlines())


def test_the_resume_line_survives_a_shell_with_hostile_text(tmp_path):
    """Run through /bin/sh, the line hands `hippo` exactly the argv it was made of — a scope or
    an argument carrying quotes, `$(…)`, backticks, globs or a newline is data, never shell."""
    scope = "it's \"$(touch pwned)\" `touch pwned2` *; echo\nnext"
    args = ["-m", "gpt-6-luna", "-c", "x='$HOME'", "-C", "/w/a b"]
    rec = {"status": "killed", "codex_session": SID, "codex_args": args, "kind": "impl",
           "scope": scope, "task": "feat/x;y"}
    cmd = hippo_cli.resume_command(rec)
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "hippo").write_text('#!/bin/sh\nfor a in "$@"; do printf \'%s\\0\' "$a"; done\n')
    (fake / "hippo").chmod(0o755)
    run = subprocess.run(["/bin/sh", "-c", cmd], cwd=tmp_path, capture_output=True,
                         env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"})
    assert run.returncode == 0, run.stderr
    assert run.stdout.decode().split("\0")[:-1] == [
        "dispatch", "--kind", "impl", "--scope", f"{scope} (resumed)", "--task", "feat/x;y",
        "--", *args, "resume", SID, hippo_cli.RESUME_CONTINUE]
    assert not list(tmp_path.glob("pwned*"))


def test_which_lanes_get_a_resume_line():
    rec = {"codex_session": SID, "codex_args": ["-C", "/w"], "kind": "impl", "scope": "s"}
    line = hippo_cli.resume_command
    assert line({**rec, "status": "killed"})
    assert line({**rec, "status": "exited", "cause": "transient"})
    assert line({**rec, "status": "exited", "cause": "environment"})
    assert line({**rec, "status": "exited", "cause": "capability"}) is None
    assert line({**rec, "status": "exited"}) is None
    assert line({**rec, "status": "killed", "codex_session": None}) is None, "no session"
    assert line({**rec, "status": "killed", "codex_args": None}) is None, "no prompt found"
    assert line({k: v for k, v in rec.items() if k != "kind"} | {"status": "killed"}) is None
    assert "--scope 's (resumed)'" in line({**rec, "status": "killed",
                                            "scope": "s (resumed)"}), "never twice"


def test_codex_args_lose_the_prompt_and_name_the_working_root():
    take = hippo_cli.codex_resume_args
    assert take(["-m", "x", "do it"], "/w") == ["-m", "x", "-C", "/w"]
    assert take(["-m", "x", "--", "-starts like a flag"], "/w") == ["-m", "x", "-C", "/w"]
    assert take(["-C", "rel", "--skip-git-repo-check", "go"], "/w") == [
        "-C", "rel", "--skip-git-repo-check"], "a -C of its own is kept, not doubled"
    assert take(["--cd=/x", "go"], "/w") == ["--cd=/x"]
    assert take(["-m", "x", "resume", SID, "go on"], "/w") == ["-m", "x", "-C", "/w"]
    assert take(["-m", "x", "--json"], "/w") is None, "no prompt last: nothing to resume with"
    assert take([], "/w") is None


def test_the_triage_line_names_the_cause_when_the_judge_named_one():
    t = {"route": "escalate", "verify": False, "answers": {
        "claims_done": 0.78, "reports_blocked": 0.69, "needs_decision": 0.18,
        "scope_creep": 0.73, "cause": {"choice": "environment", "confidence": 0.8}}}
    assert hippo_cli.triage_line(t) == (
        "triage escalate · cause environment (done .78 · blocked .69 · ask .18 · creep .73 · "
        "verify no)")
    t["answers"]["cause"] = {"choice": "none", "confidence": 1.0}
    assert hippo_cli.triage_line(t).startswith("triage escalate (done .78")


def test_the_harvest_names_a_cause_on_a_lane_that_did_not_fail(tmp_path):
    """A measured escalation carried cause environment (rc 0, b200-cluster 2026-10-01): the
    route column names it, as it always named a failure's."""
    r = {"en": {"id": "x"}, "ex": {"rc": 0, "check_rc": None}, "claim": None,
         "route": "escalate", "verify": False,
         "answers": {"claims_done": 0.78, "cause": {"choice": "environment"}}}
    assert hippo_cli.harvest_row(r, {}, tmp_path, tmp_path)[4] == "escalate environment"
    r["answers"]["cause"] = {"choice": "none"}
    assert hippo_cli.harvest_row(r, {}, tmp_path, tmp_path)[4] == "escalate"
