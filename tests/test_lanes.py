"""The wrapper keeps its own lane record (DESIGN §3.6, 1.15.0).

Contract under test: codex's raw stderr goes whole to .hippo/lanes/<id>.log while the shell's
stderr gets the compact stream — one line per command and per agent message, throttled, and
never shaped like the prompts Claude Code's background-shell watchdog wakes main for; stdout
passes through byte for byte and is kept as <id>.out; .hippo/lanes/<id>.json holds the facts
the rollout cannot give back cheaply, updated while the lane runs; a SIGTERM/SIGHUP/SIGINT is
forwarded to codex's process group and the lane is recorded killed with its usage. Batch keeps
its per-entry .err shape and goes through the same machinery.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time

import pytest

from conftest import HIPPO_BIN, REPO_ROOT, read_ledger
from test_batch import _journal, _manifest, _outdir
from test_dispatch import _fake_rollout, _stub_codex

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402

# Claude Code 2.1.281's idle-shell watchdog, verbatim (the pattern array in its binary): the
# last line of a background shell that has not grown for 45s is matched against these, and a
# match wakes main.
WATCHDOG = [re.compile(p, re.I) for p in (
    r"\(y\/n\)", r"\[y\/n\]", r"\(yes\/no\)",
    r"\b(?:Do you|Would you|Shall I|Are you sure|Ready to)\b.*\? *$",
    r"Press (any key|Enter)", r"Continue\?", r"Overwrite\?")]

SESSION = "01234567-abcd-7000-8000-0123456789ab"
# codex 0.156.1's stderr, in the shape sixteen real lane logs show: the banner, the prompt
# echoed after `user`, then `codex` messages and `exec` blocks (the command on the next line
# as `<shell> -lc '<cmd>' in <cwd>`, then its output), and the "tokens used" footer. The prompt
# echo carries an `exec` of its own that is not a command.
REAL_STDERR = f"""\
Reading additional input from stdin...
OpenAI Codex v0.156.1
--------
workdir: /work
model: gpt-6-luna
provider: openai
approval: never
sandbox: danger-full-access
reasoning effort: medium
reasoning summaries: none
session id: {SESSION}
--------
user
Fix the parser.
exec
means nothing here
codex
I will read the parser first. Then the tests.
exec
/bin/zsh -lc 'sed -n 1,80p src/parser.py' in /work
 succeeded in 3ms:
def parse(x):
    return x
exec
/bin/zsh -lc "rg -n 'parse\\(' tests | head -5" in /work
 exited 1 in 12ms:

codex
Would you like me to continue with the tokenizer?
exec
/bin/zsh -lc "cat > src/tok.py <<'EOF'
print(1)
EOF" in /work
 succeeded in 1ms:

tokens used
18,169
"""


def _stub_body(stderr_text, stdout_text="The parser is fixed.\n", tail=""):
    """A codex that prints `stderr_text` on stderr and `stdout_text` on stdout."""
    return ("#!/bin/sh\ncat >&2 <<'__HIPPO_STDERR__'\n" + stderr_text + "__HIPPO_STDERR__\n"
            + f"printf '%s' '{stdout_text}'\n" + tail)


def _run(project, tmp_path, body, scope="parser fix", env=None, timeout=60):
    full = {**os.environ, "PATH": _stub_codex(tmp_path, body), "HIPPO_DISPATCH": "",
            **(env or {})}
    return subprocess.run([str(HIPPO_BIN), "dispatch", "--kind", "impl", "--scope", scope,
                           "-m", "gpt-6-luna", "go"],
                          cwd=project, env=full, capture_output=True, timeout=timeout)


def _lanes(project):
    return project / ".hippo" / "lanes"


def _record(project, did):
    return json.loads((_lanes(project) / f"{did}.json").read_text(encoding="utf-8"))


def _did(stdout):
    first = stdout.splitlines()[0]
    first = first.decode() if isinstance(first, bytes) else first
    assert first.startswith("dispatch:d"), first
    return first.removeprefix("dispatch:")


def test_raw_stderr_goes_to_the_lane_log_and_the_shell_gets_the_compact_stream(
        tmp_project, tmp_path):
    proc = _run(tmp_project, tmp_path, _stub_body(REAL_STDERR))
    assert proc.returncode == 0, proc.stderr
    did = _did(proc.stdout)

    # The raw log is codex's stderr, byte for byte; stdout passes through and is kept.
    assert (_lanes(tmp_project) / f"{did}.log").read_text(encoding="utf-8") == REAL_STDERR
    assert proc.stdout.decode().splitlines()[1:] == ["The parser is fixed."]
    assert (_lanes(tmp_project) / f"{did}.out").read_text(encoding="utf-8") \
        == "The parser is fixed.\n"

    err = proc.stderr.decode().splitlines()
    assert json.loads(err[0])["ev"] == "dispatch", "the ledger line still leads"
    compact = err[1:]
    assert compact and all(ln.startswith("lane parser fix · ") for ln in compact), compact
    assert not any("succeeded in" in ln or ln == "tokens used" for ln in compact), \
        "no raw line reaches the shell"
    assert "started: gpt-6-luna · session " + SESSION in compact[0]
    assert compact[-1].endswith(f" · exited rc=0 · 3 cmds · 18,169 tokens · raw log "
                                f"{_lanes(tmp_project) / f'{did}.log'}")
    for ln in err:
        assert not any(p.search(ln) for p in WATCHDOG), ln

    rec = _record(tmp_project, did)
    assert (rec["id"], rec["scope"], rec["exec"]) == (did, "parser fix", "codex/gpt-6-luna/unset")
    assert isinstance(rec["pid"], int) and isinstance(rec["pgid"], int)
    assert rec["codex_session"] == SESSION
    assert rec["cmds"] == 3, "the prompt echo's `exec` is not a command"
    assert rec["last"] == "exec: cat > src/tok.py <<'EOF'", "a heredoc shows its first line"
    assert (rec["status"], rec["rc"]) == ("exited", 0)
    assert rec["started"] <= rec["last_at"] <= rec["ended"]
    assert rec["log"].endswith(f"{did}.log") and rec["report"].endswith(f"{did}.out")


def test_the_compact_stream_is_throttled_but_the_record_counts_every_command(
        tmp_project, tmp_path):
    blocks = "".join(f"exec\n/bin/zsh -lc 'echo {i}' in /work\n succeeded in 0ms:\n{i}\n"
                     for i in range(40))
    proc = _run(tmp_project, tmp_path, _stub_body(f"session id: {SESSION}\n" + blocks))
    assert proc.returncode == 0, proc.stderr
    did = _did(proc.stdout)
    execs = [ln for ln in proc.stderr.decode().splitlines() if " · exec: " in ln]
    # One line when the burst starts (the session line took the slot) and the newest pending
    # event at exit: forty commands in well under LANE_EVERY make at most two lines.
    assert 1 <= len(execs) <= 2, execs
    assert execs[-1].endswith("exec: echo 39"), "what is shown last is the newest"
    assert _record(tmp_project, did)["cmds"] == 40


def test_no_compact_line_can_read_as_a_prompt():
    for text in ("said: Shall I continue with the parser?", "exec: rm -i x  # Overwrite?",
                 "said: Continue? (y/n)", "exec: read -p 'Press any key' x",
                 "said: Press Enter to go on", "said: Are you sure ?  ",
                 "exec: read -r -p 'Apply the migration [y/N] ' ans",
                 "said: Answer (yes/no) in the summary.",
                 "said: I will express any key trade-offs in the report.",
                 "exec: printf 'compress enter\\n'"):
        assert any(p.search(text) for p in WATCHDOG), text  # the raw text would wake main
        assert not any(p.search(hippo_cli.unprompt(text)) for p in WATCHDOG), text


def test_the_command_line_is_read_from_codexs_own_shape():
    cmd = hippo_cli.codex_command
    assert cmd("/bin/zsh -lc 'git status --short' in /w/x") == "git status --short"
    assert cmd("/bin/bash -lc \"rg -n 'a b' src\" in /w") == "rg -n 'a b' src"
    assert cmd("/bin/zsh -lc \"cat > f <<'EOF'") == "cat > f <<'EOF'", "a multi-line first line"
    assert cmd("means nothing here") is None
    assert len(cmd("/bin/zsh -lc '" + "x" * 300 + "' in /w")) == hippo_cli.LANE_EXEC_CHARS
    assert hippo_cli.first_sentence("Done. Tests pass.") == "Done."
    assert hippo_cli.first_sentence("테스트가 통과했습니다. 다음은 문서입니다.") == "테스트가 통과했습니다."


def test_the_record_is_updated_while_a_long_lane_runs(tmp_project, tmp_path):
    body = (
        "#!/bin/sh\n"
        f"printf 'session id: {SESSION}\\nexec\\n/bin/zsh -lc \"pytest -q\" in /w\\n' >&2\n"
        "python3 -c 'import time; time.sleep(4)'\n"
        "printf 'exec\\n/bin/zsh -lc \"git diff\" in /w\\n' >&2\n"
        "python3 -c 'import time; time.sleep(4)'\n"
    )
    env = {**os.environ, "PATH": _stub_codex(tmp_path, body), "HIPPO_DISPATCH": ""}
    child = subprocess.Popen([str(HIPPO_BIN), "dispatch", "--kind", "impl", "--scope", "long",
                              "go"], cwd=tmp_project, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    try:
        did = _did(child.stdout.readline())
        seen, t_end = [], time.monotonic() + 30
        while child.poll() is None and time.monotonic() < t_end:
            rec = _record(tmp_project, did)
            if not seen or rec["cmds"] != seen[-1]["cmds"]:
                seen.append(rec)
            time.sleep(0.2)
        child.wait(timeout=30)
    finally:
        child.kill()
    running = [r for r in seen if "status" not in r]
    assert [r["cmds"] for r in running if r["cmds"]] == [1, 2], \
        "each command reaches the record mid-run"
    assert running[-1]["last"] == "exec: git diff"
    assert _record(tmp_project, did)["status"] == "exited"


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_a_signal_is_forwarded_and_the_lane_recorded_killed(tmp_project, tmp_path, sig):
    home = tmp_path / "home"
    _fake_rollout(home)
    marker = tmp_path / "grandchild.pid"
    body = (
        "#!/bin/sh\n"
        f"printf 'model: gpt-6-luna\\nsession id: {SESSION}\\n' >&2\n"
        "printf 'exec\\n/bin/zsh -lc \"sleep 300\" in /w\\n' >&2\n"
        "sleep 300 &\n"
        f"echo $! > {marker}\n"
        "wait\n"
    )
    env = {**os.environ, "PATH": _stub_codex(tmp_path, body), "HIPPO_DISPATCH": "",
           "HOME": str(home)}
    child = subprocess.Popen([str(HIPPO_BIN), "dispatch", "--kind", "impl", "--scope", "doomed",
                              "go"], cwd=tmp_project, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    try:
        did = _did(child.stdout.readline())
        t_end = time.monotonic() + 20
        while not (marker.exists() and _record(tmp_project, did)["pgid"]):
            assert time.monotonic() < t_end, "codex never started"
            time.sleep(0.1)
        os.kill(_record(tmp_project, did)["pid"], sig)
        _, err = child.communicate(timeout=30)
    finally:
        child.kill()
    assert child.returncode == 128 + sig
    rec = _record(tmp_project, did)
    # rc is codex's own answer to the signal (this stub's shell turns SIGINT into an exit).
    assert (rec["status"], rec["signal"]) == ("killed", sig.name) and rec["rc"] != 0
    assert rec["ended"]
    grandchild = int(marker.read_text())
    # codex's whole group went, not codex alone — even a child that ignores the signal
    # (a background job of sh ignores SIGINT).
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild, 0)
    (u,) = [e for e in read_ledger(tmp_project) if e.get("ev") == "usage"]
    assert u["ref"] == did and u["tokens"] == 1100000, "usage from the rollout, killed or not"
    assert err.decode().splitlines()[-1].split(" · ", 2)[2].startswith(
        f"killed by {sig.name} rc={rec['rc']} · 1 cmds · 1,100,000 tokens")


def test_old_lane_files_are_pruned_when_a_lane_starts(tmp_project, tmp_path):
    lanes = _lanes(tmp_project)
    lanes.mkdir()
    old, fresh = lanes / "dold.log", lanes / "dfresh.json"
    for p in (old, fresh):
        p.write_text("x", encoding="utf-8")
    week = time.time() - 8 * 86400
    os.utime(old, (week, week))
    proc = _run(tmp_project, tmp_path, "#!/bin/sh\nexit 0\n")
    assert proc.returncode == 0, proc.stderr
    assert not old.exists() and fresh.exists()


def test_an_unwritable_lanes_dir_costs_the_record_not_the_launch(tmp_project, tmp_path):
    lanes = _lanes(tmp_project)
    lanes.write_text("not a directory", encoding="utf-8")
    proc = _run(tmp_project, tmp_path, _stub_body(REAL_STDERR))
    assert proc.returncode == 0, proc.stderr
    err = proc.stderr.decode()
    assert ".hippo/lanes/ is not writable" in err
    assert err.splitlines()[-1].split(" raw log ", 1)[1].endswith(".log"), "it still names one"
    assert proc.stdout.decode().splitlines()[1:] == ["The parser is fixed."]


def test_batch_keeps_its_err_shape_and_writes_the_same_lane_records(tmp_project, tmp_path,
                                                                      run_hippo):
    manifest = _manifest(tmp_project, "lanes.yaml", """\
        concurrency: 2
        defaults: {kind: impl, executor: codex, model: gpt-6-luna, effort: low}
        entries:
          - {id: one, scope: "lane one", prompt: go}
          - {id: two, scope: "lane two", prompt: go}
        """)
    # A relative manifest, as it is usually given: the record is read from any cwd, so its
    # paths are absolute.
    proc = run_hippo(["dispatch", "--batch", manifest.name], cwd=tmp_project,
                     env={"HIPPO_DISPATCH": "",
                          "PATH": _stub_codex(tmp_path, _stub_body(REAL_STDERR))})
    assert proc.returncode == 0, proc.stderr
    for eid in ("one", "two"):
        assert (_outdir(manifest) / f"{eid}.err").read_text(encoding="utf-8") == REAL_STDERR
    exits = {r["id"]: r for r in _journal(manifest) if r["event"] == "exit"}
    for eid, scope in (("one", "lane one"), ("two", "lane two")):
        rec = _record(tmp_project, exits[eid]["dispatch"])
        assert (rec["scope"], rec["cmds"], rec["status"], rec["rc"]) == (scope, 3, "exited", 0)
        assert rec["log"] == str(_outdir(manifest) / f"{eid}.err")
        assert rec["report"] == str(_outdir(manifest) / f"{eid}.out")
        assert any(ln.startswith(f"lane {scope} · ") for ln in proc.stderr.splitlines())
    assert not any("succeeded in" in ln for ln in proc.stderr.splitlines())


def test_a_signal_to_a_batch_reaches_every_running_lane(tmp_project, tmp_path):
    manifest = _manifest(tmp_project, "doomed.yaml", """\
        concurrency: 2
        defaults: {kind: impl, executor: codex, model: gpt-6-luna, effort: low}
        entries:
          - {id: a, scope: "lane a", prompt: go}
          - {id: b, scope: "lane b", prompt: go}
          - {id: c, scope: "lane c", prompt: go}
        """)
    env = {**os.environ, "HIPPO_DISPATCH": "",
           "PATH": _stub_codex(tmp_path, "#!/bin/sh\nsleep 300 &\nwait\n")}
    child = subprocess.Popen([str(HIPPO_BIN), "dispatch", "--batch", str(manifest)],
                             cwd=tmp_project, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    try:
        t_end = time.monotonic() + 20
        while True:
            recs = [json.loads(p.read_text()) for p in _lanes(tmp_project).glob("d*.json")]
            if len(recs) == 2 and all(r["pgid"] for r in recs):
                break
            assert time.monotonic() < t_end, "the two lanes never started"
            time.sleep(0.1)
        os.kill(recs[0]["pid"], signal.SIGTERM)
        _, err = child.communicate(timeout=30)
    finally:
        child.kill()
    assert child.returncode == 128 + signal.SIGTERM
    recs = [json.loads(p.read_text()) for p in _lanes(tmp_project).glob("d*.json")]
    assert sorted((r["scope"], r["status"], r["signal"]) for r in recs) == [
        ("lane a", "killed", "SIGTERM"), ("lane b", "killed", "SIGTERM")], \
        "the third entry never launched"
    exits = [r for r in _journal(manifest) if r["event"] == "exit"]
    assert sorted(r["rc"] for r in exits) == [-15, -15]
    assert "stopped by SIGTERM" in err.decode()


def test_a_batch_gives_its_signals_back_before_the_harvest(tmp_project, tmp_path, monkeypatch,
                                                           capsys):
    """The harvest is a judge pass a signal must be able to stop: kept installed, the batch's
    forwarding handler swallowed a SIGTERM there and the batch exited 0."""
    manifest = _manifest(tmp_project, "calm.yaml", """\
        defaults: {kind: impl, executor: codex, model: gpt-6-luna, effort: low}
        entries:
          - {id: one, scope: "lane one", prompt: go}
        """)
    monkeypatch.chdir(tmp_project)
    monkeypatch.setenv("PATH", _stub_codex(tmp_path))
    before = {s: signal.getsignal(s) for s in hippo_cli.LANE_SIGNALS}
    during, real = {}, hippo_cli.run_harvest

    def harvest(*args, **kw):
        during.update({s: signal.getsignal(s) for s in hippo_cli.LANE_SIGNALS})
        return real(*args, **kw)

    monkeypatch.setattr(hippo_cli, "run_harvest", harvest)
    with pytest.raises(SystemExit) as done:
        hippo_cli.run_batch(["--batch", str(manifest)])
    assert done.value.code == 0, capsys.readouterr().err
    assert during == before


def test_a_child_codex_left_writing_to_stderr_does_not_hold_the_lane(tmp_project, tmp_path,
                                                                   run_hippo):
    """codex exits 0 and leaves a job logging to the stderr it inherited every 0.2s. The lane
    ends LANE_DRAIN after codex, not when that job does — and a batch does not kill it as
    timed out (measured before the fix: held to its timeout, recorded `killed`)."""
    body = ("#!/bin/sh\n"
            f"printf 'session id: {SESSION}\\n' >&2\n"
            "( i=0; while [ $i -lt 100 ]; do echo \"bg log line $i\" >&2; sleep 0.2; "
            "i=$((i+1)); done ) &\n"
            "printf 'All green.\\n'\n")
    manifest = _manifest(tmp_project, "chatty.yaml", """\
        defaults: {kind: impl, executor: codex, model: gpt-6-luna, effort: low}
        entries:
          - {id: chatty, scope: "chatty child", prompt: go, timeout: 10}
        """)
    t0 = time.monotonic()
    proc = run_hippo(["dispatch", "--batch", str(manifest)], cwd=tmp_project,
                     env={"HIPPO_DISPATCH": "", "PATH": _stub_codex(tmp_path, body)})
    took = time.monotonic() - t0
    (ex,) = [r for r in _journal(manifest) if r["event"] == "exit"]
    rec = _record(tmp_project, ex["dispatch"])
    try:
        os.killpg(rec["pgid"], signal.SIGKILL)  # the job, still logging
    except ProcessLookupError:
        pass
    assert proc.returncode == 0, proc.stderr
    assert took < 8, f"the lane waited {took:.1f}s on a child of codex"
    assert (ex["rc"], "timed_out" in ex) == (0, False)
    assert (rec["status"], rec["rc"], "timed_out" in rec) == ("exited", 0, False)


def test_lanes_starting_in_one_instant_all_keep_their_records(tmp_project):
    """Each start rewrites a stale statusline pointer through the same tmp file, and every
    rename but the first finds it gone (measured: 2 of 3 simultaneous starts lost their
    record). The pointer is best effort; the record is not."""
    import concurrent.futures
    import threading
    hp = tmp_project / ".hippo"
    ptr = hippo_cli.lanes_dir(hp) / ".statusline"
    gate = threading.Barrier(3)

    def start():
        gate.wait()
        return hippo_cli.lanes_dir(hp)

    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        for _ in range(30):
            ptr.write_text("/gone/1.14.0/scripts/lane_status.py", encoding="utf-8")
            assert [f.result() for f in [pool.submit(start) for _ in range(3)]] == \
                [hp / "lanes"] * 3
    assert ptr.read_text(encoding="utf-8") == str(REPO_ROOT / "scripts" / "lane_status.py")


def test_a_stop_is_on_record_before_a_slow_codex_exits(tmp_project, tmp_path):
    """Claude Code SIGKILLs the whole tree 1.5s after its SIGTERM. A codex slower than that to
    exit left no status at all, and the lane read as running; the stop goes on record at once."""
    body = ("#!/bin/sh\n"
            f"printf 'session id: {SESSION}\\n' >&2\n"
            "trap 'sleep 4; exit 1' TERM\n"
            "sleep 300 &\n"
            "wait\n")
    child, did = _launch(tmp_project, tmp_path, body)
    pgid = None
    try:
        t_end = time.monotonic() + 20
        while not (pgid := _record(tmp_project, did)["pgid"]):
            assert time.monotonic() < t_end, "codex never started"
            time.sleep(0.1)
        time.sleep(0.5)  # the stub's trap is set
        wrapper = _record(tmp_project, did)["pid"]  # bin/hippo's python, under uv
        os.kill(wrapper, signal.SIGTERM)
        t_end = time.monotonic() + 1.2
        while "status" not in _record(tmp_project, did):
            assert time.monotonic() < t_end, "the stop was not on record inside Claude Code's 1.5s"
            time.sleep(0.05)
        for pid in (wrapper, child.pid):  # Claude Code's SIGKILL to the tree, codex in its trap
            os.kill(pid, signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        child.kill()
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    rec = _record(tmp_project, did)
    assert (rec["status"], rec["signal"], rec.get("rc"), rec.get("ended")) == \
        ("killed", "SIGTERM", None, None)
    first = _watch(tmp_project, did, "--for", "0").stdout.splitlines()[0]
    assert first.startswith(f"lane {did} killed by SIGTERM after "), first


# --------------------------------------------------------------------------
# `hippo dispatch --watch <id> [--for SECONDS]` — reads .hippo/lanes/ only
# --------------------------------------------------------------------------

SLOW = (
    "#!/bin/sh\n"
    f"printf 'session id: {SESSION}\\nexec\\n/bin/zsh -lc \"pytest -q\" in /w\\n' >&2\n"
    "python3 -c 'import time; time.sleep(float(\"'\"${SLOW:-4}\"'\"))'\n"
    "printf 'All green.\\n'\n"
)


def _watch(project, *args, timeout=60):
    return subprocess.run([str(HIPPO_BIN), "dispatch", "--watch", *args], cwd=project,
                          capture_output=True, text=True, timeout=timeout)


def _launch(project, tmp_path, body=SLOW, env=None):
    full = {**os.environ, "PATH": _stub_codex(tmp_path, body), "HIPPO_DISPATCH": "",
            **(env or {})}
    child = subprocess.Popen([str(HIPPO_BIN), "dispatch", "--kind", "impl", "--scope",
                              "watched lane", "go"], cwd=project, env=full,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return child, _did(child.stdout.readline())


def test_watch_reports_a_running_lane_and_exits_3_when_its_time_is_up(tmp_project, tmp_path):
    child, did = _launch(tmp_project, tmp_path, env={"SLOW": "8"})
    try:
        # Past one LANE_EVERY: the command that followed the session line is on record.
        t0 = time.monotonic()
        proc = _watch(tmp_project, did, "--for", "4")
        assert 4 <= time.monotonic() - t0 < 7, "it blocks for --for, no longer"
        assert proc.returncode == hippo_cli.WATCH_RUNNING, proc.stderr
        (line,) = proc.stdout.splitlines()
        assert line.startswith(f"lane {did} running · ") and " · 1 cmds · last: " in line, line
        now = _watch(tmp_project, f"dispatch:{did}", "--for", "0")  # the printed token works too
        assert now.returncode == hippo_cli.WATCH_RUNNING and now.stdout.startswith(f"lane {did}")
    finally:
        child.communicate(timeout=30)


def test_watch_blocks_until_the_lane_ends_then_prints_its_final_lines(tmp_project, tmp_path):
    from test_batch_harvest import ACCEPT, _jev, _mock
    judge = _jev(_mock(tmp_path, {"answers": ACCEPT,
                                  "default": {"noul": 0.5, "choice": "none", "score": 1.0}}),
                 tmp_path / "sent.json")
    child, did = _launch(tmp_project, tmp_path, env=judge)
    try:
        t0 = time.monotonic()
        proc = _watch(tmp_project, did)  # the default window: far longer than the lane
        took = time.monotonic() - t0
    finally:
        child.communicate(timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert took < 15, "it returns when the lane ends, not when the window closes"
    assert child.poll() is not None, "the wrapper is gone before watch returns"
    lanes = _lanes(tmp_project)
    assert proc.stdout.splitlines() == [
        proc.stdout.splitlines()[0],
        "triage accept-candidate (done .95 · blocked .02 · ask .03 · creep .04 · verify no)",
        f"report: {lanes / f'{did}.out'}",
        f"raw log: {lanes / f'{did}.log'}",
    ]
    first = proc.stdout.splitlines()[0]
    assert first.startswith(f"lane {did} exited rc=0 after ") and \
        first.endswith(" · 1 cmds · watched lane"), first


def test_watch_names_a_lane_whose_wrapper_vanished(tmp_project, tmp_path):
    lanes = _lanes(tmp_project)
    lanes.mkdir()
    dead = subprocess.Popen(["true"])
    dead.wait()
    rec = {"id": "dgone", "scope": "s", "exec": "codex/x/y", "pid": dead.pid, "pgid": None,
           "started": "2026-09-24T00:00:00Z", "codex_session": None, "cmds": 4,
           "last": "exec: make", "last_at": "2026-09-24T00:01:00Z", "log": "/l", "report": "/r"}
    (lanes / "dgone.json").write_text(json.dumps(rec), encoding="utf-8")
    proc = _watch(tmp_project, "dgone")
    assert proc.returncode == 0
    assert proc.stdout.splitlines() == [
        f"lane dgone lost — its wrapper (pid {dead.pid}) is gone and recorded no end · 4 cmds "
        "· last: exec: make", "raw log: /l"]
    # Status recorded, `ended` not (SIGKILLed while the judge read): that lane has ended.
    (lanes / "dgone.json").write_text(json.dumps({**rec, "status": "killed", "rc": -15,
                                                  "signal": "SIGTERM"}), encoding="utf-8")
    lines = _watch(tmp_project, "dgone").stdout.splitlines()
    assert lines[0] == "lane dgone killed by SIGTERM rc=-15 after 1m00s · 4 cmds · s"
    assert lines[1:] == ["report: none — the lane printed no final message", "raw log: /l"]


@pytest.mark.parametrize("args, says", [
    (["dnope"], "no lane record"),
    (["dnope", "--for", "soon"], "--for takes seconds"),
    (["../../etc/passwd"], "a dispatch id is required"),
    (["dnope", "--kind", "x"], "unexpected argument"),
])
def test_watch_refuses_what_it_cannot_read(tmp_project, args, says):
    proc = _watch(tmp_project, *args)
    assert proc.returncode == 2 and says in proc.stderr, proc.stderr
    assert "usage: hippo dispatch --watch" in proc.stderr or says == "no lane record"


def test_watch_outside_a_project_says_where_records_live(uninitialized_dir):
    (uninitialized_dir / ".git").mkdir()
    proc = _watch(uninitialized_dir, "dabc")
    assert proc.returncode == 2 and "lane records live in .hippo/lanes/" in proc.stderr


def test_the_lane_agent_is_a_low_effort_sonnet_relay_with_bash_alone():
    """A frontmatter Claude Code cannot parse drops the agent without a word, and every lane
    launched through it would fail to start."""
    import yaml
    text = (REPO_ROOT / "agents" / "lane.md").read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    front = yaml.safe_load(m.group(1))
    assert (front["name"], front["model"], front["effort"], front["tools"]) == (
        "lane", "sonnet", "low", "Bash")
    assert "run_in_background: true" in text and "hippo dispatch --watch <id>" in text
    assert re.search(r"grep -m1 -o 'dispatch:d\[0-9a-f\]\*'", text)


def _lane_agent_wait(path, window=None):
    """The lane agent's wait on a background command it has no lane record for (lane.md step
    4), as the text gives it, with `<path>` filled in; `window` shortens its 540s. The sleep is
    cut to 1s so the suite stays fast — the loop is otherwise the one the agent runs."""
    text = (REPO_ROOT / "agents" / "lane.md").read_text(encoding="utf-8")
    (cmd,) = re.findall(r"^   `(f='<path>'.*)`$", text, re.M)
    cmd = cmd.replace("<path>", str(path)).replace("sleep 5", "sleep 1")
    return cmd.replace("-lt 540", f"-lt {window}") if window else cmd


def _hold(path, argv):
    """A process holding `path` open as its stdout, the way the Bash tool's background shell
    holds its output file."""
    with open(path, "a", encoding="utf-8") as out:  # closed here, so only the child holds it
        return subprocess.Popen(argv, stdout=out)


@pytest.mark.parametrize("shell", [s for s in ("/bin/bash", "/bin/zsh") if os.path.exists(s)])
def test_the_lane_agents_wait_blocks_until_the_command_lets_go_of_its_output(tmp_path, shell):
    """Inside a Workflow a finished agent's background command is killed (measured: the lane
    SIGTERMed 6s into an 8s run), so with no lane record to watch the agent must wait for the
    command itself. The wait ends when nothing holds the output file, prints its last lines and
    exits 0; still held at the window's end, it exits 3 for the agent to run it again."""
    out = tmp_path / "task.output"
    out.write_text("dispatch: no .hippo/ — skipping the dispatch record\n", encoding="utf-8")
    holder = _hold(out, ["sh", "-c", "sleep 2; echo last line"])
    t0 = time.monotonic()
    proc = subprocess.run([shell, "-c", _lane_agent_wait(out)], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert time.monotonic() - t0 >= 1.5 and holder.poll() is not None
    assert proc.stdout.splitlines()[-1] == "last line" and "no .hippo/" in proc.stdout

    holder = _hold(out, ["sleep", "30"])
    try:
        proc = subprocess.run([shell, "-c", _lane_agent_wait(out, window=1)],
                              capture_output=True, text=True, timeout=60)
        assert (proc.returncode, proc.stdout, proc.stderr) == (3, "", "")
    finally:
        holder.kill()
        holder.wait()


# --------------------------------------------------------------------------
# the agent-panel row: settings.json's subagentStatusLine → scripts/lane_status.py
# --------------------------------------------------------------------------

DID = "d" + "ab" * 16


def _statusline(cwd, ctx, env=None):
    """Run the plugin's own subagentStatusLine command the way Claude Code does: through a
    shell, in the project directory, with the rows as JSON on stdin."""
    cmd = json.loads((REPO_ROOT / "settings.json").read_text(encoding="utf-8"))
    return subprocess.run(["/bin/sh", "-c", cmd["subagentStatusLine"]["command"]], cwd=cwd,
                          input=json.dumps(ctx), capture_output=True, text=True, timeout=30,
                          env={**os.environ, "PWD": str(cwd), **(env or {})})


def _session(tmp_path, project, agents):
    """A main transcript and one subagent per (id, agentType, transcript text)."""
    sess = tmp_path / "sess"
    (sess / "subagents").mkdir(parents=True)
    for aid, kind, text in agents:
        (sess / "subagents" / f"agent-{aid}.meta.json").write_text(
            json.dumps({"agentType": kind, "description": "d"}), encoding="utf-8")
        (sess / "subagents" / f"agent-{aid}.jsonl").write_text(text, encoding="utf-8")
    rows = [{"id": aid, "type": "local_agent", "status": "running", "description": f"row {aid}",
             "cwd": str(project)} for aid, _, _ in agents]
    return {"session_id": "s", "transcript_path": f"{sess}.jsonl", "cwd": str(project),
            "columns": 120, "tasks": rows}


def _lane_record(project, **over):
    lanes = _lanes(project)
    lanes.mkdir(exist_ok=True)
    rec = {"id": DID, "scope": "pass2 tensorize", "exec": "codex/gpt-6-sol/high",
           "pid": os.getpid(), "pgid": None, "started": hippo_cli.now_iso(), "codex_session": None, "cmds": 14,
           "last": "exec: pytest -q tests/test_pass2.py", "last_at": hippo_cli.now_iso(),
           **over}
    (lanes / f"{DID}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_a_lane_row_shows_its_lane_and_every_other_row_keeps_its_default(tmp_project, tmp_path):
    hippo_cli.lanes_dir(tmp_project / ".hippo")  # what every lane start does: the pointer
    # Started 2h00m15s ago: a clock second passing while the status line runs cannot change
    # the rendered elapsed time (a lane started "now" read 0s or 1s).
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7215))
    _lane_record(tmp_project, started=started)
    lane_text = (json.dumps({"type": "user", "message": {"content": "hippo dispatch …"}}) + "\n"
                 + json.dumps({"tool_result": f"dispatch:{DID}"}) + "\n")
    ctx = _session(tmp_path, tmp_project, [
        ("alane", "hippo:lane", lane_text), ("aother", "general-purpose", f"dispatch:{DID}")])
    proc = _statusline(tmp_project, ctx)
    assert proc.returncode == 0, proc.stderr
    (line,) = proc.stdout.splitlines()
    assert json.loads(line) == {
        "id": "alane",
        "content": "codex · pass2 tensorize · 2h00m · 14 cmds · exec: pytest -q tests/test_pass2.py"}


def test_a_lane_row_follows_the_last_id_its_agent_named_and_shows_the_end(tmp_project,
                                                                          tmp_path):
    hippo_cli.lanes_dir(tmp_project / ".hippo")
    _lane_record(tmp_project, status="killed", signal="SIGTERM", rc=-15,
                 started="2026-09-24T00:00:00Z", ended="2026-09-24T00:03:05Z")
    other = "d" + "cd" * 16
    text = f"prompt quoting dispatch:{other}\n--watch {DID}\n"
    ctx = _session(tmp_path, tmp_project, [("alane", "hippo:lane", text)])
    ctx["columns"] = 40
    (sub := tmp_project / "deep" / "er").mkdir(parents=True)
    ctx["tasks"][0]["cwd"] = str(sub)  # the record is found walking up, as the CLI finds .hippo
    content = json.loads(_statusline(sub, ctx).stdout)["content"]
    assert content == "codex · pass2 tensorize · killed by SIG…" and len(content) == 40
    ctx["columns"] = 0
    content = json.loads(_statusline(sub, ctx).stdout)["content"]
    assert content == "codex · pass2 tensorize · killed by SIGTERM rc=-15 · 3m05s · 14 cmds"


def test_a_lane_row_whose_wrapper_died_stops_counting(tmp_project, tmp_path):
    """SIGKILLed before codex exited: the stop is on record but no rc, and the row neither
    prints `rc=None` nor keeps a timer running. A wrapper that recorded nothing and is gone
    shows `lost`, as `--watch` says."""
    hippo_cli.lanes_dir(tmp_project / ".hippo")
    dead = subprocess.Popen(["true"])
    dead.wait()
    ctx = _session(tmp_path, tmp_project, [("alane", "hippo:lane", f"dispatch:{DID}\n")])
    _lane_record(tmp_project, pid=dead.pid, status="killed", signal="SIGTERM",
                 started="2026-09-24T00:00:00Z", last_at="2026-09-24T00:00:16Z")
    assert json.loads(_statusline(tmp_project, ctx).stdout)["content"] == \
        "codex · pass2 tensorize · killed by SIGTERM · 16s · 14 cmds"
    _lane_record(tmp_project, pid=dead.pid, started="2026-09-24T00:00:00Z",
                 last_at="2026-09-24T00:00:16Z")
    assert json.loads(_statusline(tmp_project, ctx).stdout)["content"] == (
        "codex · pass2 tensorize · lost, its wrapper is gone · 16s · 14 cmds · "
        "exec: pytest -q tests/test_pass2.py")


def test_the_statusline_runs_no_pointer_it_cannot_trust(tmp_path):
    """The command runs, as the user, whatever script the pointer names — so it looks only
    where find_hippo looks (a .git directory and $HOME stop the walk: an ancestor's .hippo/
    may be someone else's), and runs only a `…/scripts/lane_status.py` the user owns, named by
    a pointer the user owns (ownership needs a second user to test; the rest is here)."""
    shared, ran = tmp_path / "shared", tmp_path / "ran"
    planted = shared / "x" / "scripts" / "lane_status.py"
    other = shared / "other.py"
    planted.parent.mkdir(parents=True)
    for script in (planted, other):
        script.write_text(f"open({str(ran)!r}, 'w').write('x')\n", encoding="utf-8")
    ptr = shared / ".hippo" / "lanes" / ".statusline"
    ptr.parent.mkdir(parents=True)
    ptr.write_text(str(planted), encoding="utf-8")
    (repo := shared / "repo" / "src").mkdir(parents=True)
    (shared / "repo" / ".git").mkdir()
    (home := shared / "home" / "proj").mkdir(parents=True)
    (plain := shared / "plain").mkdir()
    ctx = {"transcript_path": str(tmp_path / "s.jsonl"), "tasks": [{"id": "a1"}]}

    for cwd, env in ((repo, None), (home, {"HOME": str(home.parent)})):
        proc = _statusline(cwd, ctx, env)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", ""), cwd
        assert not ran.exists(), f"the walk from {cwd} went past its ceiling"
    assert _statusline(plain, ctx).returncode == 0 and ran.exists(), \
        "reached with no ceiling between, the same pointer is followed"
    ran.unlink()
    ptr.write_text(str(other), encoding="utf-8")
    assert _statusline(plain, ctx).returncode == 0 and not ran.exists(), \
        "a pointer to anything but a lane_status.py is not run"


def test_a_lane_row_before_the_id_is_known_says_so(tmp_project, tmp_path):
    hippo_cli.lanes_dir(tmp_project / ".hippo")
    ctx = _session(tmp_path, tmp_project, [("alane", "hippo:lane", "just the prompt\n")])
    assert json.loads(_statusline(tmp_project, ctx).stdout)["content"] == \
        "codex · row alane · starting"


def test_the_statusline_is_silent_where_no_lane_ever_started(tmp_project, tmp_path):
    ctx = _session(tmp_path, tmp_project, [("alane", "hippo:lane", f"dispatch:{DID}")])
    proc = _statusline(tmp_project, ctx)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", ""), \
        "no pointer, no python: every row keeps its default"


def test_every_lane_start_points_the_statusline_at_this_plugin(tmp_project, tmp_path):
    ptr = _lanes(tmp_project) / ".statusline"
    ptr.parent.mkdir()
    ptr.write_text("/gone/1.14.0/scripts/lane_status.py", encoding="utf-8")
    week = time.time() - 30 * 86400
    os.utime(ptr, (week, week))
    proc = _run(tmp_project, tmp_path, "#!/bin/sh\nexit 0\n")
    assert proc.returncode == 0, proc.stderr
    assert ptr.read_text(encoding="utf-8") == str(REPO_ROOT / "scripts" / "lane_status.py")
