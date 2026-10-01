"""A batch's two faces in Claude Code (DESIGN §3.6, 1.16.0).

Contract under test: `--batch <manifest> --entry <id>` runs one entry through the batch
machinery — the journal and its resume decision, the breaker, check, parent stamping, usage,
the kill trap — and prints `dispatch:<id>` first, beside other entries' processes on one
journal; `--dry-run` writes `<stem>.workflow.js`, a Workflow script that runs every entry
that way under a hippo:lane relay; `--watch` takes its window from the lane's record (a minute
for an `--entry` lane) unless `--for` says; a full run prints `batch:<bid>` first and keeps a
batch record that `--watch` and the agent-panel row read; the relay greps whichever id comes
first.
"""
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

import pytest
import yaml

from conftest import HIPPO_BIN, REPO_ROOT, read_ledger
from test_batch import _journal, _journal_path, _manifest, _outdir
from test_dispatch import _stub_codex
from test_lanes import _lanes, _record, _session, _statusline

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402

# A codex that logs `<model> <prompt>` per launch, runs one command, says its report, and
# prints the "tokens used" footer — SLOW seconds in between.
STUB = (
    "#!/bin/sh\n"
    "m=; prev=; last=\n"
    'for a in "$@"; do [ "$prev" = "-m" ] && m="$a"; prev="$a"; last="$a"; done\n'
    'printf \'%s %s\\n\' "$m" "$last" >> "${LAUNCHED:-/dev/null}"\n'
    "printf 'exec\\n/bin/zsh -lc \"pytest -q\" in /w\\n' >&2\n"
    'sleep "${SLOW:-0}"\n'
    "printf 'All green.\\n'\n"
    "printf 'tokens used\\n1200\\n' >&2\n"
)
# A codex that takes 4s to answer a SIGTERM: what is on record must not wait for it.
STUBBORN = "#!/bin/sh\ntrap 'sleep 4; exit 1' TERM\nsleep 300 &\nwait\n"

TWO = """\
    concurrency: 2
    defaults: {kind: impl, executor: codex, model: gpt-6-luna, effort: low}
    entries:
      - {id: one, scope: "lane one", prompt: "first brief"}
      - {id: two, scope: "lane two", prompt: "second brief", check: "true"}
    """


def _env(tmp_path, body=STUB, **extra):
    return {**os.environ, "PATH": _stub_codex(tmp_path, body), "HIPPO_DISPATCH": "",
            **{k: str(v) for k, v in extra.items()}}


def _hippo(project, env, *args, timeout=60):
    return subprocess.run([str(HIPPO_BIN), "dispatch", *args], cwd=project, env=env,
                          capture_output=True, text=True, timeout=timeout)


def _entry(project, env, manifest, eid, timeout=60):
    return _hippo(project, env, "--batch", str(manifest), "--entry", eid, timeout=timeout)


def _popen(project, env, *args):
    return subprocess.Popen([str(HIPPO_BIN), "dispatch", *args], cwd=project, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _first_id(child, prefix):
    line = child.stdout.readline().strip()
    assert re.fullmatch(rf"{prefix}:{prefix[0]}[0-9a-f]{{32}}", line), line
    return line.split(":", 1)[1]


def _until(cond, what, limit=20.0):
    t_end = time.monotonic() + limit
    while not (got := cond()):
        assert time.monotonic() < t_end, what
        time.sleep(0.05)
    return got


def _launched(path):
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# --------------------------------------------------------------------------
# 1. --entry: one entry through the batch machinery
# --------------------------------------------------------------------------

def test_an_entry_runs_alone_through_the_batch_machinery(tmp_project, tmp_path):
    manifest = _manifest(tmp_project, "wf.yaml", TWO)
    launched = tmp_path / "launched.txt"
    proc = _entry(tmp_project, _env(tmp_path, LAUNCHED=launched, HIPPO_DISPATCH="dorch"),
                  manifest, "two")
    assert proc.returncode == 0, proc.stderr
    first = proc.stdout.splitlines()[0]
    assert re.fullmatch(r"dispatch:d[0-9a-f]{32}", first), "the relay greps this, as for a lane"
    did = first.removeprefix("dispatch:")
    assert _launched(launched) == ["gpt-6-luna second brief"], "that entry, and only it"

    journal = _journal(manifest)
    assert [(r["event"], r["id"]) for r in journal] == [("launch", "two"), ("exit", "two")]
    ex = journal[1]
    assert (ex["dispatch"], ex["attempt"], ex["rc"], ex["check_rc"], ex["tokens"]) == \
        (did, 1, 0, 0, 1200)
    rows = read_ledger(tmp_project)
    (row,) = [e for e in rows if e.get("ev") == "dispatch"]
    assert (row["id"], row["scope"], row["exec"], row["parent"]) == \
        (did, "lane two", "codex/gpt-6-luna/low", "dorch")
    (usage,) = [e for e in rows if e.get("ev") == "usage"]
    assert (usage["ref"], usage["tokens"]) == (did, 1200)

    rec = _record(tmp_project, did)
    assert rec["face"] == "workflow", "what --watch reads its window off"
    assert (rec["status"], rec["rc"], rec["check_rc"]) == ("exited", 0, 0)
    assert rec["check"] == str((_outdir(manifest) / "two.check").absolute())
    assert not list(_lanes(tmp_project).glob("b*.json")), "one entry keeps no batch record"

    lines = _hippo(tmp_project, os.environ, "--watch", did).stdout.splitlines()
    assert lines[0].startswith(f"lane {did} exited rc=0 after ") and lines[0].endswith(
        " · 1 cmds · lane two")
    assert lines[1] == f"check: pass · {rec['check']}", "the relay's reply carries the check"


def test_entry_processes_of_one_manifest_share_its_journal(tmp_project, tmp_path):
    manifest = _manifest(tmp_project, "wf.yaml", TWO)
    env = _env(tmp_path, SLOW=1)
    kids = [_popen(tmp_project, env, "--batch", str(manifest), "--entry", eid)
            for eid in ("one", "two")]
    for kid in kids:
        _, err = kid.communicate(timeout=60)
        assert kid.returncode == 0, err
    journal = _journal(manifest)  # every line parses: no append landed inside another
    assert sorted((r["event"], r["id"], r["attempt"]) for r in journal) == [
        ("exit", "one", 1), ("exit", "two", 1), ("launch", "one", 1), ("launch", "two", 1)]

    # The full run reads what the two processes wrote: both done, nothing to launch.
    full = _hippo(tmp_project, env, "--batch", str(manifest))
    assert full.returncode == 0, full.stderr
    summary = json.loads(full.stdout.splitlines()[-1])
    assert (summary["launched"], summary["skipped"], summary["harvested"]) == (0, 2, 2)


def test_the_manifest_lock_holds_across_processes(tmp_project):
    manifest = _manifest(tmp_project, "lock.yaml", TWO)
    journal = _journal_path(manifest)
    append = (f"import sys, pathlib; sys.path.insert(0, {str(REPO_ROOT / 'cli')!r}); "
              "import hippo_cli; m, j, n, size = sys.argv[1:5]; "
              "[hippo_cli.journal_append(pathlib.Path(m), pathlib.Path(j), "
              "{'event': 'x', 'n': n, 'k': k, 'pad': 'y' * int(size)}) for k in range(30)]")

    def writer(n, size):
        return subprocess.Popen([sys.executable, "-c", append, str(manifest), str(journal),
                                 str(n), str(size)])

    with hippo_cli.batch_lock(manifest):
        held = writer("held", 10)
        time.sleep(1.5)
        assert held.poll() is None, "an append went through another process's lock"
    assert held.wait(timeout=30) == 0

    big = [writer(n, 100_000) for n in range(4)]
    assert [w.wait(timeout=60) for w in big] == [0] * 4
    recs = _journal(manifest)
    assert len(recs) == 30 + 4 * 30 and all(len(r["pad"]) in (10, 100_000) for r in recs)


def test_a_done_entry_launches_nothing_and_prints_its_final_lines(tmp_project, tmp_path):
    manifest = _manifest(tmp_project, "wf.yaml", TWO)
    launched = tmp_path / "launched.txt"
    env = _env(tmp_path, LAUNCHED=launched)
    first = _entry(tmp_project, env, manifest, "one")
    assert first.returncode == 0, first.stderr
    did = first.stdout.splitlines()[0].removeprefix("dispatch:")

    again = _entry(tmp_project, env, manifest, "one")
    assert again.returncode == 0, again.stderr
    lines = again.stdout.splitlines()
    # The same id, so the relay's watch ends at once on the lane that did the work.
    assert lines[0] == f"dispatch:{did}"
    assert lines[1].startswith(f"lane {did} exited rc=0 after ")
    assert lines[2:] == [f"report: {_outdir(manifest) / 'one.out'}",
                         f"raw log: {_outdir(manifest) / 'one.err'}"]
    assert _launched(launched) == ["gpt-6-luna first brief"], "never relaunched"
    assert [r["event"] for r in _journal(manifest)] == ["launch", "exit", "skip"]


def _seed_failure(manifest, eid, cause):
    with _journal_path(manifest).open("a", encoding="utf-8") as f:
        for rec in ({"event": "launch", "id": eid, "attempt": 1, "dispatch": "dold"},
                    {"event": "exit", "id": eid, "attempt": 1, "dispatch": "dold", "rc": 1,
                     "check_rc": None},
                    {"event": "triage", "id": eid, "attempt": 1, "dispatch": "dold",
                     "route": "failed", "answers": {"cause": {"choice": cause,
                                                             "confidence": 0.9}}}):
            f.write(json.dumps({"t": "2026-10-01T00:00:00Z", **rec}) + "\n")


def test_a_held_entry_launches_nothing_and_exits_with_its_own_code(tmp_project, tmp_path):
    manifest = _manifest(tmp_project, "wf.yaml", TWO)
    _seed_failure(manifest, "one", "capability")
    _seed_failure(manifest, "two", "transient")
    launched = tmp_path / "launched.txt"
    env = _env(tmp_path, LAUNCHED=launched)

    held = _entry(tmp_project, env, manifest, "one")
    assert held.returncode == hippo_cli.BATCH_HELD == 5, held.stderr
    assert held.stdout.splitlines() == [
        "entry one held — its last triage named cause capability: a relaunch buys the same "
        "failure; a different brief, then a new entry"]
    assert _launched(launched) == []
    assert _journal(manifest)[-1]["why"] == "cause capability"

    relaunched = _entry(tmp_project, env, manifest, "two")  # a cause a relaunch could clear
    assert relaunched.returncode == 0, relaunched.stderr
    assert _launched(launched) == ["gpt-6-luna second brief"]
    assert [r["attempt"] for r in _journal(manifest) if r["event"] == "exit" and
            r["id"] == "two"] == [1, 2]


def test_a_signal_reaches_the_entrys_one_lane(tmp_project, tmp_path):
    manifest = _manifest(tmp_project, "wf.yaml", TWO)
    child = _popen(tmp_project, _env(tmp_path, STUBBORN), "--batch", str(manifest),
                   "--entry", "one")
    pgid = None
    try:
        did = _first_id(child, "dispatch")
        pgid = _until(lambda: _record(tmp_project, did)["pgid"], "codex never started")
        time.sleep(0.3)  # the stub's trap is set
        os.kill(_record(tmp_project, did)["pid"], signal.SIGTERM)
        _until(lambda: "status" in _record(tmp_project, did), "the stop is not on record",
               limit=1.2)
        child.communicate(timeout=30)
    finally:
        child.kill()
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    assert child.returncode == 128 + signal.SIGTERM
    rec = _record(tmp_project, did)
    assert (rec["status"], rec["signal"], rec["face"]) == ("killed", "SIGTERM", "workflow")
    (ex,) = [r for r in _journal(manifest) if r["event"] == "exit"]
    assert ex["dispatch"] == did and ex["rc"] != 0


UNROUTED = """\
    defaults: {kind: impl, executor: codex, effort: low}
    entries:
      - {id: routed, scope: "routed by the plan", prompt: "a brief the plan read"}
      - {id: bare, scope: "never planned", prompt: "a brief nobody read"}
    """


def test_an_unrouted_entry_takes_the_routing_its_dry_run_recorded(tmp_project, tmp_path):
    manifest = _manifest(tmp_project, "wf.yaml", UNROUTED)
    (tmp_project / "wf.plan.jsonl").write_text(json.dumps(
        {"id": "routed", "suggested": {"model": "gpt-6.1-sol", "effort": "high"}}) + "\n",
        encoding="utf-8")
    launched = tmp_path / "launched.txt"
    env = _env(tmp_path, LAUNCHED=launched)  # the judge is off: nothing could plan it now

    proc = _entry(tmp_project, env, manifest, "routed")
    assert proc.returncode == 0, proc.stderr
    assert _launched(launched) == ["gpt-6.1-sol a brief the plan read"]
    (row,) = [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]
    assert row["exec"] == "codex/gpt-6.1-sol/high"

    bare = _entry(tmp_project, env, manifest, "bare")
    assert bare.returncode == 2 and "model is required" in bare.stderr, bare.stderr
    assert "no plan record routes bare and the judge is off" in bare.stderr
    assert bare.stdout == "" and len(_launched(launched)) == 1, "never launched unrouted"


def test_an_unrouted_entry_with_no_plan_record_is_planned_alone(tmp_project, tmp_path):
    from test_batch_plan import DEFAULT, EASY, _mock
    manifest = _manifest(tmp_project, "wf.yaml", UNROUTED)
    kept = {"id": "routed", "suggested": {"model": "gpt-6.1-sol", "effort": "high"}}
    plan = tmp_project / "wf.plan.jsonl"
    plan.write_text(json.dumps(kept) + "\n", encoding="utf-8")
    launched = tmp_path / "launched.txt"
    env = _env(tmp_path, LAUNCHED=launched, HIPPO_JEV_BACKEND="mock",
               HIPPO_JEV_MOCK_OUTPUT=_mock(tmp_path, {"answers": EASY, "default": DEFAULT}))
    proc = _entry(tmp_project, env, manifest, "bare")
    assert proc.returncode == 0, proc.stderr
    cheap = hippo_cli.price_ladder(hippo_cli.load_prices())["cheap"]
    assert _launched(launched) == [f"{cheap} a brief nobody read"]
    recs = [json.loads(ln) for ln in plan.read_text(encoding="utf-8").splitlines()]
    assert recs[0] == kept, "the dry run's records stay"
    assert (recs[1]["id"], recs[1]["suggested"]["model"]) == ("bare", cheap)


def test_an_entry_running_elsewhere_is_not_launched_twice(tmp_project, tmp_path):
    manifest = _manifest(tmp_project, "wf.yaml", """\
        defaults: {kind: impl, executor: codex, model: gpt-6-luna, effort: low}
        entries:
          - {id: slow, scope: "slow lane", prompt: "take your time"}
        """)
    launched = tmp_path / "launched.txt"
    env = _env(tmp_path, LAUNCHED=launched, SLOW=4)
    child = _popen(tmp_project, env, "--batch", str(manifest), "--entry", "slow")
    try:
        did = _first_id(child, "dispatch")
        _until(lambda: _record(tmp_project, did)["pgid"], "codex never started")
        twin = _entry(tmp_project, env, manifest, "slow")
        assert twin.returncode == 0, twin.stderr
        assert twin.stdout.splitlines()[0] == f"dispatch:{did}", "its relay watches that lane"
        assert "running already" in twin.stdout
        full = _hippo(tmp_project, env, "--batch", str(manifest))
        assert full.returncode == 0, full.stderr
        assert "still running in another process: slow" in full.stderr
        assert len(_launched(launched)) == 1
    finally:
        child.communicate(timeout=30)
    assert child.returncode == 0


# --------------------------------------------------------------------------
# 2. the Workflow face — `--dry-run` writes <stem>.workflow.js
# --------------------------------------------------------------------------

def _face_parts(path):
    """(meta, lanes, concurrency) read back off the script: `meta` is JSON on the first line —
    a pure literal by construction — and every lane one JSON object per line."""
    text = path.read_text(encoding="utf-8")
    head, _ = text.split("\n", 1)
    assert head.startswith("export const meta = "), "meta comes first"
    meta = json.loads(head.removeprefix("export const meta = "))
    lanes = [json.loads(m) for m in re.findall(r"^  (\{.*\}),$", text, re.M)]
    conc = int(re.search(r"^const CONCURRENCY = (\d+)$", text, re.M).group(1))
    return text, meta, lanes, conc


def test_the_dry_run_writes_a_workflow_face_main_can_run(tmp_project, tmp_path):
    manifest = _manifest(tmp_project, "wave.yaml", """\
        concurrency: 3
        defaults: {kind: impl, executor: codex, model: gpt-6-luna, effort: low}
        entries:
          - {id: a, scope: "lane a", prompt: go}
          - {id: b, scope: "lane b", kind: verify, prompt: go}
          - {id: c, scope: "lane c", prompt: go}
        """)
    proc = _hippo(tmp_project, _env(tmp_path), "--batch", manifest.name, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    face = (tmp_project / "wave.workflow.js").resolve()
    assert json.loads(proc.stdout.splitlines()[-1])["workflow"] == str(face)
    (hint,) = [ln for ln in proc.stderr.splitlines() if "Workflow(" in ln]
    assert f'Workflow({{scriptPath: "{face}"}})' in hint
    assert hint.index("Workflow(") < hint.index("hippo dispatch --batch wave.yaml"), \
        "the harvest comes after the Workflow"

    text, meta, lanes, conc = _face_parts(face)
    assert meta == {"name": "hippo-batch-wave",
                    "description": "3 codex lanes of wave.yaml, 3 at once — hippo dispatch "
                                   "--batch, one hippo:lane relay per entry",
                    "phases": [{"title": "impl"}, {"title": "verify"}]}
    assert conc == 3
    assert [(lane["id"], lane["label"], lane["phase"]) for lane in lanes] == [
        ("a", "lane a", "impl"), ("b", "lane b", "verify"), ("c", "lane c", "impl")]
    for lane in lanes:
        assert shlex.split(lane["cmd"]) == ["hippo", "dispatch", "--batch",
                                            str(manifest.resolve()), "--entry", lane["id"]]
    assert "agentType: 'hippo:lane', label: lane.label, phase: lane.phase" in text
    for banned in ("Date.now", "Math.random", "new Date(", "require(", "import ", "process.",
                   "${", "`"):
        assert banned not in text, banned


HOSTILE = ['quote " and \\ backslash', "it's </script><!-- ${x} `tick`",
           "line sep para", "多言語 scope " + "x" * 80]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_the_workflow_face_parses_and_runs_with_hostile_strings(tmp_path, run_hippo):
    """Every string a manifest carries is JSON-encoded, so no scope, kind or path can end a
    string or a line early; the script parses as a module and runs as the Workflow runtime
    runs a body — top-level await and return inside an async context — with at most
    `concurrency` relays at once, and returns entry id → reply."""
    project = tmp_path / 'it\'s "the" project '
    project.mkdir()
    assert run_hippo(["init"], cwd=project).returncode == 0
    kinds = ["impl", 'k"in\\d\nnew', "impl x", "verify"]
    entries = [{"id": f"e{i}", "scope": s, "kind": kinds[i], "prompt": "go"}
               for i, s in enumerate(HOSTILE)]
    entries.append({"id": "e4", "scope": "plain", "prompt": "go"})
    manifest = project / "m.yaml"
    manifest.write_text(yaml.safe_dump(
        {"concurrency": 2, "defaults": {"kind": "impl", "executor": "codex",
                                        "model": "gpt-6-luna"}, "entries": entries}),
        encoding="utf-8")
    proc = run_hippo(["dispatch", "--batch", str(manifest), "--dry-run"], cwd=project)
    assert proc.returncode == 0, proc.stderr
    face = project / "m.workflow.js"
    head, body = face.read_text(encoding="utf-8").split("\n", 1)
    assert all(ord(ch) < 128 for ch in head + body), "nothing but ASCII reaches the script"

    # The runtime wraps the body in an async function; node checks it the same way.
    module = tmp_path / "face.mjs"
    module.write_text(f"{head}\nexport async function run() {{\n{body}\n}}\n", encoding="utf-8")
    check = subprocess.run(["node", "--check", str(module)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    runner = tmp_path / "run.mjs"
    runner.write_text(
        "import {meta, run} from './face.mjs'\n"
        "const calls = []; let live = 0, peak = 0\n"
        "globalThis.agent = async (prompt, opts) => {\n"
        "  calls.push({prompt, opts}); live++; peak = Math.max(peak, live)\n"
        "  await new Promise(r => setTimeout(r, 30)); live--\n"
        "  return 'reply ' + opts.label\n"
        "}\n"
        "const out = await run()\n"
        "process.stdout.write(JSON.stringify({meta, calls, out, peak}))\n", encoding="utf-8")
    ran = subprocess.run(["node", str(runner)], capture_output=True, text=True, cwd=tmp_path)
    assert ran.returncode == 0, ran.stderr
    got = json.loads(ran.stdout)

    assert got["meta"]["phases"] == [{"title": k} for k in dict.fromkeys(kinds)]
    assert got["peak"] == 2, "the manifest's concurrency, and no more"
    want = {f"e{i}": hippo_cli.one_line(e["scope"], hippo_cli.LANE_SCOPE_CHARS)
            for i, e in enumerate(entries)}
    assert got["out"] == {eid: f"reply {label}" for eid, label in want.items()}
    assert list(got["out"]) == list(want), "replies in entry order"
    for call, en in zip(got["calls"], entries):
        assert call["opts"] == {"agentType": "hippo:lane", "label": want[en["id"]],
                                "phase": en.get("kind", "impl")}
        assert shlex.split(call["prompt"]) == ["hippo", "dispatch", "--batch",
                                               str(manifest.resolve()), "--entry", en["id"]]


# --------------------------------------------------------------------------
# 3. the watch window is the record's to choose, `--for` still wins
# --------------------------------------------------------------------------

def _running_record(project, did, **over):
    lanes = _lanes(project)
    lanes.mkdir(exist_ok=True)
    rec = {"id": did, "scope": "watched", "exec": "codex/gpt-6-luna/low", "pid": os.getpid(),
           "pgid": None, "started": hippo_cli.now_iso(), "codex_session": None, "cmds": 3,
           "last": "exec: pytest -q", "last_at": hippo_cli.now_iso(), "log": "/l",
           "report": "/r", **over}
    (lanes / f"{did}.json").write_text(json.dumps(rec), encoding="utf-8")


def _timed_watch(capsys, *argv):
    t0 = time.monotonic()
    with pytest.raises(SystemExit) as done:
        hippo_cli.run_watch(["--watch", *argv])
    return done.value.code, time.monotonic() - t0, capsys.readouterr().out


def test_the_watch_window_is_the_records_unless_for_says(tmp_project, monkeypatch, capsys):
    monkeypatch.chdir(tmp_project)
    monkeypatch.setattr(hippo_cli, "WATCH_FOR", 30)
    monkeypatch.setattr(hippo_cli, "WATCH_FOR_WORKFLOW", 0.5)
    _running_record(tmp_project, "dface", face="workflow")
    _running_record(tmp_project, "dplain")
    assert hippo_cli.watch_window({"face": "workflow"}) == 0.5
    assert hippo_cli.watch_window({}) == 30

    code, took, out = _timed_watch(capsys, "dface")
    assert code == hippo_cli.WATCH_RUNNING and 0.4 <= took < 5, took
    assert re.fullmatch(r"lane dface running · \d+s · 3 cmds · last: exec: pytest -q\n", out)
    code, took, _ = _timed_watch(capsys, "dface", "--for", "1.5")
    assert code == hippo_cli.WATCH_RUNNING and took >= 1.4, "an explicit --for wins"
    code, took, _ = _timed_watch(capsys, "dplain", "--for", "0")
    assert code == hippo_cli.WATCH_RUNNING and took < 1


# --------------------------------------------------------------------------
# 4. the one-row face — `batch:<bid>` and the batch record
# --------------------------------------------------------------------------

def test_a_full_run_names_its_batch_first_and_keeps_its_record(tmp_project, tmp_path):
    manifest = _manifest(tmp_project, "wf.yaml", TWO)
    proc = _hippo(tmp_project, _env(tmp_path), "--batch", manifest.name)
    assert proc.returncode == 0, proc.stderr
    first, *rest = proc.stdout.splitlines(keepends=True)
    assert re.fullmatch(r"batch:b[0-9a-f]{32}\n", first)
    bid = first.strip().removeprefix("batch:")
    rec = _record(tmp_project, bid)
    exits = {r["id"]: r["dispatch"] for r in _journal(manifest) if r["event"] == "exit"}
    assert set(exits) == {"one", "two"}
    assert {k: rec[k] for k in ("id", "manifest", "stem", "total", "todo", "skipped",
                                "launched", "running", "ok", "failed", "done", "running_ids",
                                "status", "rc", "journal")} == {
        "id": bid, "manifest": str(manifest), "stem": "wf", "total": 2, "todo": 2,
        "skipped": 0, "launched": 2, "running": 0, "ok": 2, "failed": 0, "done": 2,
        "running_ids": [], "status": "exited", "rc": 0, "journal": str(_journal_path(manifest))}
    assert rec["started"] <= rec["ended"]
    assert re.fullmatch(r"lane (one|two): exited rc=0( · check pass)?", rec["last"])
    harvest = _lanes(tmp_project) / f"{bid}.out"
    assert rec["harvest"] == str(harvest)
    assert harvest.read_text(encoding="utf-8") == "".join(rest), "the harvest, kept whole"
    for did in exits.values():
        assert "face" not in _record(tmp_project, did), "a full run's lanes poll as before"

    lines = _hippo(tmp_project, os.environ, "--watch", f"batch:{bid}").stdout.splitlines()
    assert re.fullmatch(r"batch wf exited rc=0 after \d+s · 2 launched · 2 ok · 0 failed · "
                        r"0 skipped", lines[0])
    assert lines[1:] == [ln.rstrip("\n") for ln in rest], "a short harvest is printed whole"


def test_the_batch_record_follows_its_lanes(tmp_project, tmp_path):
    manifest = _manifest(tmp_project, "wf.yaml", """\
        concurrency: 1
        defaults: {kind: impl, executor: codex, model: gpt-6-luna, effort: low}
        entries:
          - {id: one, scope: "lane one", prompt: go}
          - {id: two, scope: "lane two", prompt: go}
        """)
    child = _popen(tmp_project, _env(tmp_path, SLOW=1.5), "--batch", str(manifest))
    seen, watched = [], None
    try:
        bid = _first_id(child, "batch")
        while child.poll() is None:
            rec = _record(tmp_project, bid)
            if not seen or rec != seen[-1]:
                seen.append(rec)
            if watched is None and rec["running"] == 1 and rec["done"] == 0:
                watch = _hippo(tmp_project, os.environ, "--watch", bid, "--for", "0")
                assert watch.returncode == hippo_cli.WATCH_RUNNING, watch.stderr
                watched = watch.stdout.strip()
            time.sleep(0.05)
        _, err = child.communicate(timeout=30)
    finally:
        child.kill()
    assert child.returncode == 0, err
    seen.append(_record(tmp_project, bid))
    states = [(r["running"], r["done"], r.get("status")) for r in seen]
    assert (1, 0, None) in states and (1, 1, None) in states and states[-1] == (0, 2, "exited")
    first_lane = next(r for r in seen if r["running"] == 1 and r["done"] == 0)
    (did,) = first_lane["running_ids"]
    assert _record(tmp_project, did)["scope"] == "lane one"
    assert re.fullmatch(r"batch wf running · \d+s · 1 running · 0/2 done · 0 failed · "
                        r"last: lane one: (started|exec: pytest -q)", watched or ""), watched


def test_a_long_harvest_is_named_by_path(tmp_path):
    harvest = tmp_path / "b.out"
    rec = {"id": "bx", "stem": "wide", "status": "exited", "rc": 1,
           "started": "2026-10-02T00:00:00Z", "ended": "2026-10-02T00:12:03Z",
           "launched": 50, "ok": 48, "failed": 2, "skipped": 0, "harvest": str(harvest),
           "manifest": "/m.yaml", "journal": "/m.journal.jsonl"}
    head = "batch wide exited rc=1 after 12m03s · 50 launched · 48 ok · 2 failed · 0 skipped"
    harvest.write_text("".join(f"row {i}\n" for i in range(hippo_cli.HARVEST_INLINE_LINES)),
                       encoding="utf-8")
    assert hippo_cli.batch_watch_lines(rec, "ended", tmp_path) == [head] + [
        f"row {i}" for i in range(hippo_cli.HARVEST_INLINE_LINES)]
    harvest.write_text("".join(f"row {i}\n" for i in range(60)) + '{"total": 50}\n',
                       encoding="utf-8")
    assert hippo_cli.batch_watch_lines(rec, "ended", tmp_path) == [
        head, f"harvest: {harvest} · 61 lines — read it before any lane", '{"total": 50}']


def test_a_signal_to_a_full_batch_is_on_its_record_at_once(tmp_project, tmp_path):
    manifest = _manifest(tmp_project, "wf.yaml", TWO)
    child = _popen(tmp_project, _env(tmp_path, STUBBORN), "--batch", str(manifest))
    pgids = []
    try:
        bid = _first_id(child, "batch")
        rec = _until(lambda: (r := _record(tmp_project, bid))["running"] == 2 and r,
                     "the two lanes never started")
        pgids = [_record(tmp_project, d)["pgid"] for d in rec["running_ids"]]
        time.sleep(0.3)  # the stubs' traps are set
        os.kill(rec["pid"], signal.SIGTERM)
        # Claude Code SIGKILLs the tree 1.5s after its SIGTERM; the lanes take 4s to go.
        _until(lambda: _record(tmp_project, bid).get("status") == "killed",
               "the stop is not on the batch record", limit=1.2)
        child.communicate(timeout=30)
    finally:
        child.kill()
        for pgid in pgids:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, TypeError):
                pass
    assert child.returncode == 128 + signal.SIGTERM
    rec = _record(tmp_project, bid)
    assert (rec["status"], rec["signal"], rec["rc"], rec["running"]) == \
        ("killed", "SIGTERM", 143, 0)
    lines = _hippo(tmp_project, os.environ, "--watch", bid).stdout.splitlines()
    assert lines[0].startswith("batch wf killed by SIGTERM rc=143 after ")
    assert lines[1] == f"the journal holds every exit; `hippo dispatch --batch {manifest}` resumes"


def test_a_batch_row_reads_the_batch_and_only_its_running_lanes(tmp_project, tmp_path):
    hippo_cli.lanes_dir(tmp_project / ".hippo")  # the statusline pointer, as a lane start makes
    lanes = _lanes(tmp_project)
    bid, did, decoy = "b" + "12" * 16, "d" + "ab" * 16, "d" + "cd" * 16
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7215))
    t = hippo_cli.now_iso()
    batch = {"id": bid, "manifest": "/w/wide.yaml", "stem": "wide", "pid": os.getpid(),
             "started": started, "total": 3, "todo": 3, "skipped": 0, "launched": 2,
             "running": 1, "ok": 1, "failed": 0, "done": 1, "running_ids": [did],
             "last": "lane a: exited rc=0", "last_at": "2026-01-01T00:00:00Z",
             "journal": "/w/wide.journal.jsonl", "harvest": None}
    (lanes / f"{bid}.json").write_text(json.dumps(batch), encoding="utf-8")
    (lanes / f"{did}.json").write_text(json.dumps(
        {"id": did, "scope": "pass2 tensorize", "pid": os.getpid(), "started": started,
         "cmds": 4, "last": "exec: pytest -q", "last_at": t}), encoding="utf-8")
    # Newer still, but not one of the batch's running lanes: never read.
    (lanes / f"{decoy}.json").write_text(json.dumps(
        {"id": decoy, "scope": "someone else", "last": "exec: rm", "last_at": "2099-01-01T00:00:00Z"}),
        encoding="utf-8")
    text = f"hippo dispatch --batch wide.yaml\nbatch:{bid}\n--watch {bid}\n"
    ctx = _session(tmp_path, tmp_project, [("arow", "hippo:lane", text)])
    ctx["columns"] = 0
    (line,) = _statusline(tmp_project, ctx).stdout.splitlines()
    assert json.loads(line) == {"id": "arow", "content": (
        "codex batch · wide · 2h00m · 1 running · 1/3 done · 0 failed · "
        "last: pass2 tensorize: exec: pytest -q")}

    (lanes / f"{bid}.json").write_text(json.dumps(
        {**batch, "running": 0, "running_ids": [], "done": 3, "ok": 2, "failed": 1,
         "status": "exited", "rc": 1, "started": "2026-10-02T00:00:00Z",
         "ended": "2026-10-02T00:12:03Z"}), encoding="utf-8")
    assert json.loads(_statusline(tmp_project, ctx).stdout)["content"] == \
        "codex batch · wide · exited rc=1 · 12m03s · 0 running · 3/3 done · 1 failed"


def _relay_grep(path):
    """The id grep of lane.md step 2, as the text gives it, `<path>` filled in."""
    text = (REPO_ROOT / "agents" / "lane.md").read_text(encoding="utf-8")
    (cmd,) = re.findall(r"`(grep -m1 [^`]*<path>)`", text)
    return subprocess.run(["/bin/sh", "-c", cmd.replace("<path>", shlex.quote(str(path)))],
                          capture_output=True, text=True).stdout


def test_the_relay_watches_whichever_id_comes_first(tmp_path):
    bid, did = "b" + "12" * 16, "d" + "ab" * 16
    out = tmp_path / "task.output"
    out.write_text("dispatch --batch: no .hippo/ — skipping the ledger records\n"
                   f"batch:{bid}\nlane x · 3s · exec: echo dispatch:{did}\n", encoding="utf-8")
    assert _relay_grep(out) == f"batch:{bid}\n"
    out.write_text(f'{{"ev": "dispatch", "id": "{did}"}}\ndispatch:{did}\nbatch:{bid}\n',
                   encoding="utf-8")
    assert _relay_grep(out) == f"dispatch:{did}\n"
    out.write_text("dispatch --batch: m.yaml: model is required\nbatch:b\n", encoding="utf-8")
    assert _relay_grep(out) == "", "no id: the relay waits on the file instead"
