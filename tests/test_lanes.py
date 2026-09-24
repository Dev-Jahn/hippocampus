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

# Claude Code 2.1.281's idle-shell watchdog, verbatim: the last line of a background shell that
# has not grown for 45s is matched against these, and a match wakes main.
WATCHDOG = [re.compile(p, re.I) for p in (
    r"Continue\?", r"Overwrite\?", r"Press (any key|Enter)", r"\(y/n\)",
    r"\b(?:Do you|Would you|Shall I|Are you sure|Ready to)\b.*\? *$")]

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
                 "said: Press Enter to go on", "said: Are you sure ?  "):
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
    proc = run_hippo(["dispatch", "--batch", str(manifest)], cwd=tmp_project,
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


def test_the_lane_agent_is_a_haiku_relay_with_bash_alone():
    """A frontmatter Claude Code cannot parse drops the agent without a word, and every lane
    launched through it would fail to start."""
    import yaml
    text = (REPO_ROOT / "agents" / "lane.md").read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    front = yaml.safe_load(m.group(1))
    assert (front["name"], front["model"], front["tools"]) == ("lane", "haiku", "Bash")
    assert "run_in_background: true" in text and "hippo dispatch --watch <id>" in text
    assert re.search(r"grep -m1 -o 'dispatch:d\[0-9a-f\]\*'", text)
