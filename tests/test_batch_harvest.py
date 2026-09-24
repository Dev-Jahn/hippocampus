"""Harvest triage over a batch (DESIGN §3.6) — the judge reads every lane report so main
reads one table instead of N reports.

Contract under test: the triage record at lane exit and the ev:triage row beside it, the route
policy computed in code, the state the judge actually receives (whole report, whole brief, the
lane's git changes) and the trim order when it does not fit, the harvest every run ends with —
its table, clusters and verdicts file — what a rerun over an existing journal relaunches and
what it skips by cause, and — the rule that outranks all of it — that with the judge off every
one of these paths keeps its deterministic half and nothing judged appears.

Nothing here may reach the network: conftest pins HIPPO_JEV_BACKEND=off and the tests that
want a judge pin `mock`, whose answers come from $HIPPO_JEV_MOCK_OUTPUT.
"""
import json
import subprocess

import pytest

from conftest import read_ledger
from test_batch import _batch, _journal, _manifest, _outdir, _stub

# codex 0.144.6 (measured): the banner rides stderr. `FAIL` in the prompt makes this stub the
# failing half of a two-lane wave — one executable on PATH serves every entry.
HALF_STUB = """\
#!/bin/sh
for a in "$@"; do last="$a"; done
printf 'model: gpt-6-luna\\n' >&2
case "$last" in
  *FAIL*)
    printf 'ModuleNotFoundError: No module named pytest\\n' >&2
    printf 'the run failed: no such file tests/run.sh\\n'
    exit 1 ;;
esac
printf 'agent output\\n'
exit 0
"""

# Records the prompt of every lane it is launched for, so a rerun can be checked by what
# actually ran rather than by what the journal says ran.
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
          model: gpt-6-luna
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
          model: gpt-6-luna
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


def _summary(proc):
    return json.loads(proc.stdout.splitlines()[-1])


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
    assert "model: gpt-6-luna" not in sent["state"]["stderr_tail"], "banner noise is filtered"

    # Self-metering: one row per call, named after its spec (§2) — the plan pass reads the
    # brief before launch, the triage reads the lane after it, and the harvest reuses that.
    clerk = [e for e in read_ledger(tmp_project) if e.get("ev") == "clerk"]
    assert [(e["name"], e["ok"], e["src"]) for e in clerk] == [("jev-plan", True, "wrapper"),
                                                               ("jev-harvest", True, "wrapper")]

    # The same reading lands in the ledger, joined to the dispatch it read (§3.2).
    (row,) = [e for e in read_ledger(tmp_project) if e.get("ev") == "triage"]
    (d,) = [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]
    assert row["ref"] == d["id"] == rec["dispatch"] and row["src"] == "wrapper"
    assert row["route"] == "accept-candidate" and row["verify"] is False
    assert row["p"] == {"done": 0.95, "blocked": 0.02, "ask": 0.03, "creep": 0.04,
                        "evidence": 0.91, "risk": 1.0}
    assert "cause" not in row, "a cause of none is no cause"

    # The run ends with the table, the summary line last.
    _, ok = _row(proc, "ok-lane")
    assert ok[4] == "accept-candidate"


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
    assert len(_records(manifest, "triage")) == 1, "a failed read is not retried in the same run"

    clerk = [e for e in read_ledger(tmp_project) if e.get("ev") == "clerk"]
    assert [(e["name"], e["ok"]) for e in clerk] == [("jev-plan", False), ("jev-harvest", False)]
    assert [e for e in read_ledger(tmp_project) if e.get("ev") == "triage"] == [], \
        "a judge failure writes no triage row — the clerk row is the gap"


# --------------------------------------------------------------------------
# (c) with no key there is no judge — and no difference
# --------------------------------------------------------------------------

def test_with_the_judge_off_nothing_judged_appears(tmp_project, tmp_path, run_hippo):
    manifest = _one_lane(tmp_project)
    # conftest pins HIPPO_JEV_BACKEND=off, which is what a machine with no key resolves to.
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", HALF_STUB)})
    assert proc.returncode == 0
    assert _records(manifest, "triage") == [], "no judge, no record"
    assert "routes" not in _summary(proc)
    assert "triage=" not in proc.stderr, "no judge, no column"
    assert "ladder" not in proc.stderr, "no judge, no plan pass before the launch"
    assert [e for e in read_ledger(tmp_project) if e.get("ev") in ("clerk", "triage")] == []
    assert not (tmp_project / "wave.plan.jsonl").exists()


def test_with_the_judge_off_harvest_prints_the_deterministic_table(tmp_project, tmp_path,
                                                                   run_hippo):
    manifest = _one_lane(tmp_project)
    env = {"PATH": _stub(tmp_path, "codex", HALF_STUB)}
    assert _batch(run_hippo, tmp_project, manifest, env=env).returncode == 0

    # Everything is done, so the rerun launches nothing and only harvests.
    proc = _batch(run_hippo, tmp_project, manifest, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "judge off — no TYPESAFE_API_KEY" in proc.stderr
    assert _table(proc)[0].split()[:6] == ["id", "rc", "check", "claim", "route", "verify"]
    _, row = _row(proc, "ok-lane")
    assert row[:4] == ["ok-lane", "0", "-", "-"], "rc and check are read off the journal"
    assert row[4:7] == ["-", "-", "-"], "route, verify and the numbers need a judge"
    assert row[-1] == "wave.out/ok-lane.out"
    assert _records(manifest, "triage") == []
    assert not (tmp_project / "wave.verdicts.jsonl").exists(), "nothing judged, nothing to pipe"
    summary = _summary(proc)
    assert summary["harvested"] == 1 and summary["verdicts"] is None
    assert summary["launched"] == 0
    assert "routes" not in summary


# --------------------------------------------------------------------------
# (d) the harvest: the table, the cluster and the verdicts file
# --------------------------------------------------------------------------

def test_harvest_tables_two_lanes_and_writes_only_the_accept_row(tmp_project, tmp_path,
                                                                 run_hippo):
    manifest = _two_lanes(tmp_project)
    path = _stub(tmp_path, "codex", HALF_STUB)
    mock = _mock(tmp_path, {
        "answers": {**ACCEPT, "cause": {"choice": "environment", "confidence": 0.81}},
        "default": DEFAULT})
    proc = _batch(run_hippo, tmp_project, manifest, env={"PATH": path, **_jev(mock)})
    assert proc.returncode == 1, "the rc is still the launches'"
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
    assert "hippo dispatch --batch wave.yaml  # a rerun relaunches the environment failures" \
        in footer
    assert "hippo log outcome --from-batch" in footer

    verdicts = (tmp_project / "wave.verdicts.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(ln) for ln in verdicts.splitlines() if ln.strip()]
    assert len(rows) == 1, "a failed lane needs a diagnosis, not a verdict"
    assert rows[0]["entry"] == "ok-lane" and rows[0]["result"] == "accepted"
    assert rows[0]["attempt"] == 1
    assert rows[0]["note"] == ("triage accept-candidate: done 0.95, check -; "
                               "confirmed by main")

    summary = _summary(proc)
    assert summary["routes"] == {"failed": 1, "accept-candidate": 1}
    assert summary["clusters"] == 1
    assert (summary["launched"], summary["ok"], summary["failed"]) == (2, 1, 1)


def test_a_finished_batch_reharvests_without_reading_a_lane_twice(tmp_project, tmp_path,
                                                                  run_hippo):
    manifest = _one_lane(tmp_project)
    env = {"PATH": _stub(tmp_path, "codex", HALF_STUB),
           **_jev(_mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}))}
    assert _batch(run_hippo, tmp_project, manifest, env=env).returncode == 0
    proc = _batch(run_hippo, tmp_project, manifest, env=env)
    assert proc.returncode == 0, proc.stderr

    assert _summary(proc)["launched"] == 0
    assert _row(proc, "ok-lane")[1][4] == "accept-candidate", "the lane-exit triage is reused"
    assert len(_records(manifest, "triage")) == 1
    rows = read_ledger(tmp_project)
    assert len([e for e in rows if e.get("ev") == "triage"]) == 1, "one lane, one reading"
    assert [e["name"] for e in rows if e.get("ev") == "clerk"] == ["jev-plan", "jev-harvest"]


def test_a_lane_triaged_with_the_judge_off_is_read_when_it_is_on(tmp_project, tmp_path,
                                                                  run_hippo):
    manifest = _one_lane(tmp_project)
    path = _stub(tmp_path, "codex", HALF_STUB)
    assert _batch(run_hippo, tmp_project, manifest, env={"PATH": path}).returncode == 0
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": path,
                       **_jev(_mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}))})
    assert proc.returncode == 0, proc.stderr
    assert _summary(proc)["launched"] == 0
    assert [r["route"] for r in _records(manifest, "triage")] == ["accept-candidate"]
    assert (tmp_project / "wave.verdicts.jsonl").exists()


@pytest.mark.parametrize("same, expected", [(0.9, ["c1"]), (0.1, ["c1", "c2"])])
def test_clustering_merges_failures_one_fix_would_clear(tmp_project, tmp_path, run_hippo,
                                                        same, expected):
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
          model: gpt-6-luna
        entries:
          - id: bad-one
            scope: "first failure"
            prompt: "FAIL one"
          - id: bad-two
            scope: "second failure"
            prompt: "FAIL two"
        """)
    path = _stub(tmp_path, "codex", HALF_STUB)
    capture = tmp_path / "sent.json"
    mock = _mock(tmp_path, {
        "answers": {**ACCEPT, "cause": {"choice": "environment", "confidence": 0.8},
                    "same_0": {"noul": same}},
        "default": DEFAULT})
    proc = _batch(run_hippo, tmp_project, manifest, env={"PATH": path, **_jev(mock, capture)})
    assert proc.returncode == 1, proc.stderr

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
# (e) a rerun relaunches what a relaunch could clear, and says what it skipped
# --------------------------------------------------------------------------

def _seed_triage(manifest, entry_id, cause):
    path = manifest.parent / f"{manifest.stem}.journal.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": "2026-09-23T00:00:00Z", "event": "triage", "id": entry_id,
                            "attempt": 1, "dispatch": "d" + entry_id, "route": "failed",
                            "verify": False,
                            "answers": {"cause": {"choice": cause, "confidence": 0.9}},
                            "trimmed": [], "jev": {"ok": True}}) + "\n")


def test_a_rerun_relaunches_only_what_a_relaunch_could_clear(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
          model: gpt-6-luna
        entries:
          - id: lane-tr
            scope: "timed out"
            prompt: "FAIL transiently"
          - id: lane-cap
            scope: "got it wrong"
            prompt: "FAIL at the work"
          - id: lane-spec
            scope: "asked the wrong thing"
            prompt: "FAIL on the brief"
          - id: lane-new
            scope: "never triaged"
            prompt: "FAIL unjudged"
        """)
    launched = tmp_path / "launched.txt"
    env = {"PATH": _stub(tmp_path, "codex", RECORD_STUB), "LAUNCHED": str(launched)}
    assert _batch(run_hippo, tmp_project, manifest, env=env).returncode == 1
    assert len(launched.read_text(encoding="utf-8").splitlines()) == 4
    launched.unlink()

    _seed_triage(manifest, "lane-tr", "transient")
    _seed_triage(manifest, "lane-cap", "capability")
    _seed_triage(manifest, "lane-spec", "spec")
    proc = _batch(run_hippo, tmp_project, manifest, env=env)
    assert proc.returncode == 1, "the relaunched lanes fail again"

    ran = launched.read_text(encoding="utf-8")
    assert "FAIL transiently" in ran
    assert "FAIL unjudged" in ran, "an entry with no triage is never dropped by its cause"
    assert "FAIL at the work" not in ran and "FAIL on the brief" not in ran
    lines = proc.stderr.splitlines()
    assert lines[0] == f"resuming {manifest}: 2 to relaunch, 2 skipped", "said first"
    assert "skipped 1 (cause capability): lane-cap — a different brief, then a new entry" \
        in lines
    assert "skipped 1 (cause spec): lane-spec — a different brief, then a new entry" in lines
    skips = [r for r in _records(manifest, "skip") if r.get("why")]
    assert sorted((r["id"], r["why"]) for r in skips) == [("lane-cap", "cause capability"),
                                                          ("lane-spec", "cause spec")]
    s = _summary(proc)
    assert (s["launched"], s["skipped"]) == (2, 2)


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


def test_changes_read_the_entry_cwd_when_the_args_name_no_worktree(tmp_project, tmp_path,
                                                                   run_hippo):
    lane = tmp_path / "lane"
    lane.mkdir()
    subprocess.run(["git", "-C", str(lane), "init", "-q"], check=True, capture_output=True)
    (lane / "notes.md").write_text("new\n", encoding="utf-8")
    manifest = _manifest(tmp_project, "wave.yaml", f"""\
        defaults:
          kind: impl
          model: gpt-6-luna
        entries:
          - id: ok-lane
            scope: "one lane"
            cwd: {lane}
            prompt: "do the thing"
        """)
    capture = tmp_path / "sent.json"
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", HALF_STUB),
                       **_jev(_mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}),
                              capture)})
    assert proc.returncode == 0, proc.stderr
    changes = json.loads(capture.read_text(encoding="utf-8"))["state"]["changes"]
    assert "?? notes.md" in changes, "no -C in args: the entry cwd is where the lane worked"


def test_the_outdir_files_are_where_the_state_comes_from(tmp_project, tmp_path, run_hippo):
    """A check that ran puts its output in the state, and its rc in the exit object."""
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
          model: gpt-6-luna
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


# --------------------------------------------------------------------------
# (h) verifier findings — a verification lane reports everything, the judge ranks it
# --------------------------------------------------------------------------

# A verification lane's report: a prose preamble that is not a finding, then four findings in
# three of the shapes a report uses (dash bullet, numbered item, star bullet).
VERIFY_STUB = """\
#!/bin/sh
for a in "$@"; do last="$a"; done
printf 'model: gpt-6-luna\\n' >&2
case "$last" in
  *PROSE*)
    printf 'I read the whole diff and it holds together.\\n'
    printf 'Nothing in it worried me.\\n' ;;
  *)
    printf 'I reviewed the retry path against the brief.\\n'
    printf -- '- the retry loop swallows the timeout\\n'
    printf '  so a hung call is reported as success\\n'
    printf -- '- naming: prefer spans over ranges\\n'
    printf '2. the cache key drops the tenant id\\n'
    printf -- '* nice test coverage on the parser\\n' ;;
esac
exit 0
"""

# Distinct severities, so the ranking is checkable: the cache key (k=2) outranks the retry
# loop (k=0), then the naming preference, then the praise.
RANK = {
    "severity_0": {"score": 2.9, "confidence": 0.8}, "real_0": {"noul": 0.95},
    "severity_1": {"score": 0.4, "confidence": 0.7}, "real_1": {"noul": 0.2},
    "severity_2": {"score": 3.0, "confidence": 0.9}, "real_2": {"noul": 0.9},
    "severity_3": {"score": 0.1, "confidence": 0.9}, "real_3": {"noul": 0.05},
}


def _verify_lane(project, prompt="review the retry path"):
    return _manifest(project, "wave.yaml", f"""\
        defaults:
          kind: verify
          model: gpt-6-luna
        entries:
          - id: verifier
            scope: "boundary check of the retry path"
            prompt: "{prompt}"
        """)


def _harvested(run_hippo, project, tmp_path, manifest, answers, capture=None):
    return _batch(run_hippo, project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", VERIFY_STUB),
                       **_jev(_mock(tmp_path, {"answers": answers, "default": DEFAULT}),
                              capture)})


def test_verifier_findings_are_split_ranked_and_recorded(tmp_project, tmp_path, run_hippo):
    manifest = _verify_lane(tmp_project)
    capture = tmp_path / "sent.json"
    proc = _harvested(run_hippo, tmp_project, tmp_path, manifest, {**ACCEPT, **RANK}, capture)
    assert proc.returncode == 0, proc.stderr

    # The findings request is the last one out: one severity and one real per finding.
    sent = json.loads(capture.read_text(encoding="utf-8"))
    assert list(sent["questions"]) == ["severity_0", "real_0", "severity_1", "real_1",
                                       "severity_2", "real_2", "severity_3", "real_3"]
    assert set(sent["state"]) == {"scope", "findings"}
    findings = sent["state"]["findings"]
    assert len(findings) == 4, "the prose preamble is not a finding"
    assert findings[0] == ("- the retry loop swallows the timeout\n"
                           "  so a hung call is reported as success"), "a finding runs on"
    assert findings[2].startswith("2. the cache key")

    rec = _records(manifest, "findings")
    assert len(rec) == 1 and rec[0]["id"] == "verifier" and rec[0]["attempt"] == 1
    assert [f["k"] for f in rec[0]["ranked"]] == [2, 0, 1, 3], "severity desc, then real desc"
    assert rec[0]["ranked"][0]["severity"] == 3.0 and rec[0]["ranked"][0]["real"] == 0.9
    assert rec[0]["ranked"][0]["head"] == "2. the cache key drops the tenant id"

    assert [ln for ln in proc.stdout.splitlines() if ln.startswith("  ▸")] == [
        "  ▸ 3.0 real .90  2. the cache key drops the tenant id",
        "  ▸ 2.9 real .95  - the retry loop swallows the timeout so a hung call is reported "
        "as success",
        "  ▸ 0.4 real .20  - naming: prefer spans over ranges",
        "  ▸ 0.1 real .05  * nice test coverage on the parser"]
    # The ranking rides directly under the row it is about.
    i, _ = _row(proc, "verifier")
    assert _table(proc)[i + 1].startswith("  ▸ 3.0")

    metered = [e for e in read_ledger(tmp_project) if e.get("name") == "jev-verify"]
    assert len(metered) == 1 and metered[0]["ok"] is True and metered[0]["src"] == "wrapper"


def test_a_prose_report_asks_nothing_and_records_nothing(tmp_project, tmp_path, run_hippo):
    manifest = _verify_lane(tmp_project, prompt="PROSE review")
    capture = tmp_path / "sent.json"
    proc = _harvested(run_hippo, tmp_project, tmp_path, manifest, {**ACCEPT, **RANK}, capture)
    assert proc.returncode == 0, proc.stderr

    assert _records(manifest, "findings") == []
    assert [ln for ln in proc.stdout.splitlines() if ln.startswith("  ▸")] == []
    assert [e for e in read_ledger(tmp_project) if e.get("name") == "jev-verify"] == []
    # The last request out is the triage one — no verify request was made at all.
    assert "findings" not in json.loads(capture.read_text(encoding="utf-8"))["state"]


def test_only_a_verify_entry_is_ranked(tmp_project, tmp_path, run_hippo):
    """The same report under kind impl asks nothing: ranking is what a verification lane's
    output is for, and every other lane's report is read by triage alone."""
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
          model: gpt-6-luna
        entries:
          - id: builder
            scope: "not a verification lane"
            prompt: "build the thing"
        """)
    proc = _harvested(run_hippo, tmp_project, tmp_path, manifest, {**ACCEPT, **RANK})
    assert proc.returncode == 0, proc.stderr
    assert _records(manifest, "findings") == []
    assert [e for e in read_ledger(tmp_project) if e.get("name") == "jev-verify"] == []


def test_with_the_judge_off_a_verify_lane_harvests_as_it_always_did(tmp_project, tmp_path,
                                                                    run_hippo):
    manifest = _verify_lane(tmp_project)
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", VERIFY_STUB)})
    assert proc.returncode == 0, proc.stderr
    assert "judge off — no TYPESAFE_API_KEY" in proc.stderr
    assert _records(manifest, "findings") == []
    assert [ln for ln in proc.stdout.splitlines() if ln.startswith("  ▸")] == []
