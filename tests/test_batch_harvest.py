"""Harvest triage over a batch wave (DESIGN §3.6) — the judge reads every lane report so main
reads one table instead of N reports.

Contract under test: the triage record at lane exit and what it carries, the route policy
computed in code, the state the judge actually receives (whole report, whole brief, the lane's
git changes) and the trim order when it does not fit, `--harvest` with its table/clusters/
verdicts file, `--resume --causes`, and — the rule that outranks all of it — that with the
judge off every one of these paths behaves exactly as it did before the judge existed.

Nothing here may reach the network: conftest pins HIPPO_JEV_BACKEND=off and the tests that
want a judge pin `mock`, whose answers come from $HIPPO_JEV_MOCK_OUTPUT.
"""
import json
import subprocess

import pytest

from conftest import read_ledger
from test_batch import _batch, _journal, _manifest, _outdir, _stub, _summary

# codex 0.144.6 (measured): the banner rides stderr. `FAIL` in the prompt makes this stub the
# failing half of a two-lane wave — one executable on PATH serves every entry.
HALF_STUB = """\
#!/bin/sh
for a in "$@"; do last="$a"; done
printf 'model: gpt-5.6-luna\\n' >&2
case "$last" in
  *FAIL*)
    printf 'ModuleNotFoundError: No module named pytest\\n' >&2
    printf 'the run failed: no such file tests/run.sh\\n'
    exit 1 ;;
esac
printf 'agent output\\n'
exit 0
"""

# Records the prompt of every lane it is launched for, so a --causes resume can be checked by
# what actually ran rather than by what the journal says ran.
RECORD_STUB = """\
#!/bin/sh
for a in "$@"; do last="$a"; done
printf '%s\\n' "$last" >> "$LAUNCHED"
exit 1
"""

# Answers that route a clean lane to accept-candidate under the shipped policy.
ACCEPT = {
    "claims_done": {"noul": 0.95},
    "reports_blocked": {"noul": 0.02},
    "needs_decision": {"noul": 0.03},
    "scope_creep": {"noul": 0.04},
    "evidence": {"noul": 0.91},
}
DEFAULT = {"noul": 0.5, "choice": "none", "score": 1.0}


def _mock(tmp_path, payload, name="jev.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _jev(mock_path, capture=None):
    env = {"HIPPO_JEV_BACKEND": "mock", "HIPPO_JEV_MOCK_OUTPUT": str(mock_path)}
    if capture is not None:
        env["HIPPO_JEV_MOCK_CAPTURE"] = str(capture)
    return env


def _records(manifest, event):
    return [r for r in _journal(manifest) if r.get("event") == event]


def _one_lane(project, prompt="do the thing tokens=1200", entry_id="ok-lane", args=None):
    return _manifest(project, "wave.yaml", f"""\
        defaults:
          kind: impl
          model: gpt-5.6-luna
        entries:
          - id: {entry_id}
            scope: "one lane"
            args: {json.dumps(args or [])}
            prompt: "{prompt}"
        """)


def _two_lanes(project):
    return _manifest(project, "wave.yaml", """\
        defaults:
          kind: impl
          model: gpt-5.6-luna
        entries:
          - id: ok-lane
            scope: "the lane that worked"
            prompt: "do the thing"
          - id: bad-lane
            scope: "the lane that did not"
            prompt: "FAIL at the thing"
        """)


def _table(proc):
    """The harvest table: stdout without its trailing JSON summary line."""
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return lines[:-1]


def _row(proc, entry_id):
    for i, line in enumerate(_table(proc)):
        if line.split()[0] == entry_id:
            return i, line.split()
    raise AssertionError(f"no {entry_id} row in:\n{proc.stdout}")


# --------------------------------------------------------------------------
# (a) triage at lane exit
# --------------------------------------------------------------------------

def test_triage_lands_at_lane_exit_with_its_route(tmp_project, tmp_path, run_hippo):
    manifest = _one_lane(tmp_project)
    capture = tmp_path / "sent.json"
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", HALF_STUB),
                       **_jev(_mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}),
                              capture)})
    assert proc.returncode == 0, proc.stderr

    tri = _records(manifest, "triage")
    assert len(tri) == 1, "one triage per lane exit"
    rec = tri[0]
    assert rec["route"] == "accept-candidate"
    assert rec["verify"] is False, "risk 1.0 and evidence .91 need no verification lane"
    assert rec["answers"]["claims_done"] == 0.95, "a noul compacts to its probability"
    assert rec["answers"]["cause"] == {"choice": "none", "confidence": 1.0}
    assert rec["answers"]["risk"] == {"score": 1.0, "confidence": 1.0}
    assert rec["trimmed"] == []
    assert rec["jev"]["ok"] is True
    assert rec["attempt"] == 1 and rec["dispatch"].startswith("d")

    assert "triage=accept-candidate" in proc.stderr, "the progress line carries the route"
    assert _summary(proc)["routes"] == {"accept-candidate": 1}

    # Large context is the point (§3.9): the whole report and the whole brief go in.
    sent = json.loads(capture.read_text(encoding="utf-8"))
    assert sent["state"]["report"].strip() == "agent output"
    assert sent["state"]["brief"] == "do the thing tokens=1200"
    assert sent["state"]["scope"] == "one lane"
    assert sent["state"]["exit"] == {"rc": 0, "check_rc": None, "timed_out": False}
    assert sent["state"]["check_output"] is None
    assert sent["state"]["changes"] is None, "a lane outside a git repository shows no changes"
    assert sent["state"]["claim"] is None
    assert "model: gpt-5.6-luna" not in sent["state"]["stderr_tail"], "banner noise is filtered"

    # Self-metering: one row per triage call, named after its spec (§2).
    clerk = [e for e in read_ledger(tmp_project) if e.get("ev") == "clerk"]
    assert [(e["name"], e["ok"], e["src"]) for e in clerk] == [("jev-harvest", True, "wrapper")]


# --------------------------------------------------------------------------
# (b) a dead judge is a gap, never a broken wave
# --------------------------------------------------------------------------

def test_a_dead_judge_leaves_a_gap_and_the_wave_still_passes(tmp_project, tmp_path, run_hippo):
    manifest = _one_lane(tmp_project)
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", HALF_STUB),
                       **_jev(tmp_path / "not-there.json")})
    assert proc.returncode == 0, "the judge is an addition — its failure is not the wave's"

    rec = _records(manifest, "triage")[0]
    assert rec["route"] is None and rec["answers"] is None
    assert rec["verify"] is None
    assert rec["jev"]["ok"] is False and rec["jev"]["reason"]
    assert "triage=-" in proc.stderr
    assert _summary(proc)["routes"] == {"-": 1}, "the gap is counted, not hidden"

    clerk = [e for e in read_ledger(tmp_project) if e.get("ev") == "clerk"]
    assert [(e["name"], e["ok"]) for e in clerk] == [("jev-harvest", False)]


# --------------------------------------------------------------------------
# (c) with no key there is no judge — and no difference
# --------------------------------------------------------------------------

def test_with_the_judge_off_the_wave_is_byte_for_byte_what_it_was(tmp_project, tmp_path,
                                                                  run_hippo):
    manifest = _one_lane(tmp_project)
    # conftest pins HIPPO_JEV_BACKEND=off, which is what a machine with no key resolves to.
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", HALF_STUB)})
    assert proc.returncode == 0
    assert _records(manifest, "triage") == [], "no judge, no record"
    assert "routes" not in _summary(proc)
    assert "triage=" not in proc.stderr, "no judge, no column"
    assert [e for e in read_ledger(tmp_project) if e.get("ev") == "clerk"] == []


def test_with_the_judge_off_harvest_prints_the_deterministic_table(tmp_project, tmp_path,
                                                                   run_hippo):
    manifest = _one_lane(tmp_project)
    env = {"PATH": _stub(tmp_path, "codex", HALF_STUB)}
    assert _batch(run_hippo, tmp_project, manifest, env=env).returncode == 0

    proc = _batch(run_hippo, tmp_project, manifest, "--harvest", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "judge off — no TYPESAFE_API_KEY" in proc.stderr
    assert _table(proc)[0].split()[:6] == ["id", "rc", "check", "claim", "route", "verify"]
    _, row = _row(proc, "ok-lane")
    assert row[:4] == ["ok-lane", "0", "-", "-"], "rc and check are read off the journal"
    assert row[4:7] == ["-", "-", "-"], "route, verify and the numbers need a judge"
    assert row[-1] == "wave.out/ok-lane.out"
    assert _records(manifest, "triage") == []
    assert not (tmp_project / "wave.verdicts.jsonl").exists(), "nothing judged, nothing to pipe"
    summary = json.loads(proc.stdout.splitlines()[-1])
    assert summary["harvested"] == 1 and summary["verdicts"] is None
    assert "routes" not in summary


# --------------------------------------------------------------------------
# (d) --harvest: the table, the cluster and the verdicts file
# --------------------------------------------------------------------------

def test_harvest_tables_two_lanes_and_writes_only_the_accept_row(tmp_project, tmp_path,
                                                                 run_hippo):
    manifest = _two_lanes(tmp_project)
    path = _stub(tmp_path, "codex", HALF_STUB)
    assert _batch(run_hippo, tmp_project, manifest, env={"PATH": path}).returncode == 1

    mock = _mock(tmp_path, {
        "answers": {**ACCEPT, "cause": {"choice": "environment", "confidence": 0.81}},
        "default": DEFAULT})
    proc = _batch(run_hippo, tmp_project, manifest, "--harvest",
                  env={"PATH": path, **_jev(mock)})
    assert proc.returncode == 0, proc.stderr
    assert "judge off" not in proc.stderr

    assert len(_records(manifest, "triage")) == 2
    bad_i, bad = _row(proc, "bad-lane")
    ok_i, ok = _row(proc, "ok-lane")
    assert bad_i < ok_i, "what needs main's eyes is read first"
    assert bad[1:3] == ["1", "-"] and bad[4:7] == ["failed", "c1", "environment"]
    assert bad[-1] == "wave.out/bad-lane.err", "a lane that died points at its stderr"
    assert ok[4] == "accept-candidate" and "done .95" in " ".join(ok)

    clusters = _records(manifest, "cluster")
    assert len(clusters) == 1
    assert clusters[0]["cluster"] == "c1" and clusters[0]["members"] == ["bad-lane"]
    assert clusters[0]["cause"] == "environment"
    assert "ModuleNotFoundError" in clusters[0]["excerpt"]

    footer = proc.stdout
    assert "c1 · environment · 1 lane · " in footer
    assert "--resume --causes environment" in footer
    assert "hippo log outcome --from-batch" in footer

    verdicts = (tmp_project / "wave.verdicts.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(ln) for ln in verdicts.splitlines() if ln.strip()]
    assert len(rows) == 1, "a failed lane needs a diagnosis, not a verdict"
    assert rows[0]["entry"] == "ok-lane" and rows[0]["result"] == "accepted"
    assert rows[0]["attempt"] == 1
    assert rows[0]["note"] == ("triage accept-candidate: done 0.95, check -; "
                               "confirmed by main")

    summary = json.loads(proc.stdout.splitlines()[-1])
    assert summary["routes"] == {"failed": 1, "accept-candidate": 1}
    assert summary["clusters"] == 1


def test_harvest_refuses_to_launch_anything(tmp_project, tmp_path, run_hippo):
    manifest = _one_lane(tmp_project)
    for flag in ("--resume", "--fresh", "--dry-run"):
        proc = _batch(run_hippo, tmp_project, manifest, "--harvest", flag,
                      env={"PATH": _stub(tmp_path, "codex", HALF_STUB)})
        assert proc.returncode == 2 and "launches nothing" in proc.stderr
    proc = _batch(run_hippo, tmp_project, manifest, "--harvest",
                  env={"PATH": _stub(tmp_path, "codex", HALF_STUB)})
    assert proc.returncode == 2 and "no journal yet" in proc.stderr


@pytest.mark.parametrize("same, expected", [(0.9, ["c1"]), (0.1, ["c1", "c2"])])
def test_clustering_merges_failures_one_fix_would_clear(tmp_project, tmp_path, run_hippo,
                                                        same, expected):
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
          model: gpt-5.6-luna
        entries:
          - id: bad-one
            scope: "first failure"
            prompt: "FAIL one"
          - id: bad-two
            scope: "second failure"
            prompt: "FAIL two"
        """)
    path = _stub(tmp_path, "codex", HALF_STUB)
    assert _batch(run_hippo, tmp_project, manifest, env={"PATH": path}).returncode == 1

    capture = tmp_path / "sent.json"
    mock = _mock(tmp_path, {
        "answers": {**ACCEPT, "cause": {"choice": "environment", "confidence": 0.8},
                    "same_0": {"noul": same}},
        "default": DEFAULT})
    proc = _batch(run_hippo, tmp_project, manifest, "--harvest",
                  env={"PATH": path, **_jev(mock, capture)})
    assert proc.returncode == 0, proc.stderr

    clusters = _records(manifest, "cluster")
    assert [c["cluster"] for c in clusters] == expected
    if expected == ["c1"]:
        assert clusters[0]["members"] == ["bad-one", "bad-two"]
    else:
        assert [c["members"] for c in clusters] == [["bad-one"], ["bad-two"]]

    # The last request out is the clustering one: the second failure against the first.
    sent = json.loads(capture.read_text(encoding="utf-8"))
    assert list(sent["questions"]) == ["same_0"]
    assert sent["state"]["a"]["id"] == "bad-two"
    assert [r["id"] for r in sent["state"]["reps"]] == ["bad-one"]
    assert "ModuleNotFoundError" in sent["state"]["a"]["excerpt"]

    clerk = [e["name"] for e in read_ledger(tmp_project) if e.get("ev") == "clerk"]
    assert clerk.count("jev-cluster") == 1, "one metering row per clustering call"


# --------------------------------------------------------------------------
# (e) --resume --causes
# --------------------------------------------------------------------------

def _seed_triage(manifest, entry_id, cause):
    path = manifest.parent / f"{manifest.stem}.journal.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": "2026-09-23T00:00:00Z", "event": "triage", "id": entry_id,
                            "attempt": 1, "dispatch": "d" + entry_id, "route": "failed",
                            "verify": False,
                            "answers": {"cause": {"choice": cause, "confidence": 0.9}},
                            "trimmed": [], "jev": {"ok": True}}) + "\n")


def test_resume_causes_relaunches_only_what_a_relaunch_could_clear(tmp_project, tmp_path,
                                                                   run_hippo):
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
          model: gpt-5.6-luna
        entries:
          - id: lane-tr
            scope: "timed out"
            prompt: "FAIL transiently"
          - id: lane-cap
            scope: "got it wrong"
            prompt: "FAIL at the work"
          - id: lane-new
            scope: "never triaged"
            prompt: "FAIL unjudged"
        """)
    launched = tmp_path / "launched.txt"
    env = {"PATH": _stub(tmp_path, "codex", RECORD_STUB), "LAUNCHED": str(launched)}
    assert _batch(run_hippo, tmp_project, manifest, env=env).returncode == 1
    assert len(launched.read_text(encoding="utf-8").splitlines()) == 3
    launched.unlink()

    _seed_triage(manifest, "lane-tr", "transient")
    _seed_triage(manifest, "lane-cap", "capability")
    proc = _batch(run_hippo, tmp_project, manifest, "--resume", "--causes", "transient",
                  env=env)
    assert proc.returncode == 1, "the relaunched lane fails again"

    ran = launched.read_text(encoding="utf-8")
    assert "FAIL transiently" in ran
    assert "FAIL unjudged" in ran, "an entry with no triage is never dropped by the filter"
    assert "FAIL at the work" not in ran
    skips = [r for r in _records(manifest, "skip") if r.get("why")]
    assert [(r["id"], r["why"]) for r in skips] == [("lane-cap", "cause capability")]
    assert _summary(proc)["skipped"] == 1


def test_causes_needs_a_resume_and_a_known_cause(tmp_project, tmp_path, run_hippo):
    manifest = _one_lane(tmp_project)
    env = {"PATH": _stub(tmp_path, "codex", HALF_STUB)}
    proc = _batch(run_hippo, tmp_project, manifest, "--causes", "transient", env=env)
    assert proc.returncode == 2 and "needs --resume" in proc.stderr
    proc = _batch(run_hippo, tmp_project, manifest, "--resume", "--causes", "flaky", env=env)
    assert proc.returncode == 2 and "--causes takes a comma list" in proc.stderr


# --------------------------------------------------------------------------
# (f) the budget — trimmed loudly, never silently
# --------------------------------------------------------------------------

def test_an_oversize_report_is_trimmed_from_the_head_and_named(tmp_project, tmp_path,
                                                               run_hippo):
    big = """\
#!/bin/sh
printf 'HEAD-MARKER\\n'
i=0
while [ $i -lt 3000 ]; do
  printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n'
  i=$((i+1))
done
printf 'TAIL-MARKER\\n'
"""
    manifest = _one_lane(tmp_project)
    capture = tmp_path / "sent.json"
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", big),
                       **_jev(_mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}),
                              capture)})
    assert proc.returncode == 0, proc.stderr

    rec = _records(manifest, "triage")[0]
    assert rec["trimmed"] == ["report"], "what was cut is named — nothing shortens silently"
    assert rec["route"] == "accept-candidate", "a trimmed state is still judged"
    sent = json.loads(capture.read_text(encoding="utf-8"))
    report = sent["state"]["report"]
    assert "TAIL-MARKER" in report, "a lane's summary of itself is at the end"
    assert "HEAD-MARKER" not in report
    assert len(json.dumps(sent["state"], ensure_ascii=False)) <= 110_000


# --------------------------------------------------------------------------
# (g) changes — what the lane actually touched
# --------------------------------------------------------------------------

def test_changes_read_the_lane_worktree_and_are_null_outside_one(tmp_project, tmp_path,
                                                                 run_hippo):
    lane = tmp_path / "lane"
    lane.mkdir()
    for args in (["init", "-q"], ["add", "kernel.py"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"]):
        if args[0] == "add":
            (lane / "kernel.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(lane), *args], check=True, capture_output=True)
    (lane / "kernel.py").write_text("x = 2\n", encoding="utf-8")

    manifest = _one_lane(tmp_project, args=["-C", str(lane)])
    capture = tmp_path / "sent.json"
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", HALF_STUB),
                       **_jev(_mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}),
                              capture)})
    assert proc.returncode == 0, proc.stderr
    changes = json.loads(capture.read_text(encoding="utf-8"))["state"]["changes"]
    assert "kernel.py" in changes, "the -C worktree is where a codex lane worked"
    assert "M kernel.py" in changes and "1 file changed" in changes


def test_the_outdir_files_are_where_the_state_comes_from(tmp_project, tmp_path, run_hippo):
    """A check that ran puts its output in the state, and its rc in the exit object."""
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
          model: gpt-5.6-luna
          check: "echo CHECK-SAYS-NO; exit 3"
        entries:
          - id: ok-lane
            scope: "one lane"
            prompt: "do the thing"
        """)
    capture = tmp_path / "sent.json"
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", HALF_STUB),
                       **_jev(_mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}),
                              capture)})
    assert proc.returncode == 1, "a failing check is still a failed lane"
    sent = json.loads(capture.read_text(encoding="utf-8"))
    assert "CHECK-SAYS-NO" in sent["state"]["check_output"]
    assert sent["state"]["exit"] == {"rc": 0, "check_rc": 3, "timed_out": False}
    assert _records(manifest, "triage")[0]["route"] == "failed", "a rc outranks a judgment"
    assert (_outdir(manifest) / "ok-lane.check").exists()
