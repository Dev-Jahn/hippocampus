"""The judge on the single dispatch, and the loop that measures it (DESIGN §3.2, §3.6).

Contract under test: `hippo dispatch` with the judge on reads the brief before the launch (a
routed tier two steps off, a clash with a lane directive — notes, never gates), captures the
lane's final message through `--output-last-message` without touching stdout, triages the lane
at exit into one stderr line and one `ev:triage` row (src=wrapper); with the judge off none of
it exists. Then the row's schema — the scribe may not write it — the in-flight line that shows
it, and the PRIORS section that measures the judge's routes against main's first verdicts.

Nothing here may reach the network: conftest pins HIPPO_JEV_BACKEND=off and the tests that
want a judge pin `mock`.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from conftest import REPO_ROOT, read_ledger
from test_batch_harvest import ACCEPT, _jev, _mock
from test_dispatch import _stub_codex

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402

# Writes its final message where codex would (-o / --output-last-message), records its argv,
# and prints the measured banner on stderr and the agent's own output on stdout.
LAST_MESSAGE_STUB = """\
#!/bin/sh
out=""
prev=""
for a in "$@"; do
  printf '%s\\0' "$a" >> "$ARGV_FILE"
  case "$prev" in -o|--output-last-message) out="$a" ;; esac
  prev="$a"
done
printf 'model: gpt-5.6-luna\\nsession id: none\\n' >&2
printf 'ModuleNotFoundError: nothing, just noise on stderr\\n' >&2
printf 'agent stdout\\n'
[ -n "$out" ] && printf 'Done: the retry loop is in and tests/run.sh passes.\\n' > "$out"
exit 0
"""
BRIEF = "Add a retry loop around the fetch call and run tests/run.sh until it is green."
DEFAULT = {"noul": 0.5, "choice": "none", "score": 1.0}


def _dispatch(run_hippo, project, tmp_path, *codex_args, env=None):
    argv = tmp_path / "argv.bin"
    full = {"HIPPO_DISPATCH": "", "PATH": _stub_codex(tmp_path, LAST_MESSAGE_STUB),
            "ARGV_FILE": str(argv), **(env or {})}
    proc = run_hippo(["dispatch", "--kind", "impl", "--scope", "retry loop",
                      "-m", "gpt-5.6-luna", "-c", "model_reasoning_effort=medium",
                      *codex_args], cwd=project, env=full)
    sent = argv.read_text(encoding="utf-8").split("\0")[:-1] if argv.exists() else []
    return proc, sent


def _rows(project, ev):
    return [e for e in read_ledger(project) if e.get("ev") == ev]


# --------------------------------------------------------------------------
# exit triage
# --------------------------------------------------------------------------

def test_the_lane_is_triaged_at_exit_into_one_line_and_one_row(tmp_project, tmp_path,
                                                                run_hippo):
    capture = tmp_path / "sent.json"
    proc, argv = _dispatch(run_hippo, tmp_project, tmp_path, BRIEF,
                           env=_jev(_mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}),
                                    capture))
    assert proc.returncode == 0, proc.stderr

    # The final message is captured to a file of the wrapper's own: stdout is untouched.
    assert argv[:2] == ["exec", "--output-last-message"]
    assert not os.path.exists(argv[2]), "the wrapper's own file is cleaned up"
    lines = proc.stdout.splitlines()
    assert lines[0].startswith("dispatch:d") and lines[1:] == ["agent stdout"]

    assert ("dispatch: triage accept-candidate (done .95 · blocked .02 · ask .03 · creep .04 "
            "· verify no)") in proc.stderr.splitlines()
    (d,) = _rows(tmp_project, "dispatch")
    (t,) = _rows(tmp_project, "triage")
    assert t["ref"] == d["id"] and t["src"] == "wrapper"
    assert t["route"] == "accept-candidate" and t["verify"] is False
    assert t["p"] == {"done": 0.95, "blocked": 0.02, "ask": 0.03, "creep": 0.04,
                      "evidence": 0.91, "risk": 1.0}

    # The last request out is the triage, over the same state a batch lane gets.
    sent = json.loads(capture.read_text(encoding="utf-8"))
    state = sent["state"]
    assert state["report"] == "Done: the retry loop is in and tests/run.sh passes.\n"
    assert state["brief"] == BRIEF and state["scope"] == "retry loop" and state["kind"] == "impl"
    assert state["exit"] == {"rc": 0, "check_rc": None, "timed_out": False}
    assert state["check_output"] is None and state["claim"] is None
    assert "ModuleNotFoundError" in state["stderr_tail"]
    assert "model: gpt-5.6-luna" not in state["stderr_tail"], "banner noise is filtered"
    names = [e["name"] for e in _rows(tmp_project, "clerk")]
    assert names == ["jev-plan", "jev-harvest"]


def test_a_callers_own_last_message_file_is_read_and_kept(tmp_project, tmp_path, run_hippo):
    mine = tmp_path / "last.txt"
    capture = tmp_path / "sent.json"
    proc, argv = _dispatch(run_hippo, tmp_project, tmp_path, "-o", str(mine), BRIEF,
                           env=_jev(_mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}),
                                    capture))
    assert proc.returncode == 0, proc.stderr
    assert argv.count("--output-last-message") == 0 and argv.count("-o") == 1
    assert mine.read_text(encoding="utf-8").startswith("Done:"), "the caller's file stays"
    assert json.loads(capture.read_text(encoding="utf-8"))["state"]["report"].startswith("Done:")


def test_a_dead_judge_writes_no_triage_row(tmp_project, tmp_path, run_hippo):
    proc, _ = _dispatch(run_hippo, tmp_project, tmp_path, BRIEF,
                        env=_jev(tmp_path / "not-there.json"))
    assert proc.returncode == 0, "the judge is an addition — its failure is not the lane's"
    assert "dispatch: no triage — the judge did not answer" in proc.stderr
    assert _rows(tmp_project, "triage") == []
    assert [(e["name"], e["ok"]) for e in _rows(tmp_project, "clerk")] == [
        ("jev-plan", False), ("jev-harvest", False)], "the clerk rows are the gap"


def test_with_the_judge_off_the_dispatch_is_what_it_was(tmp_project, tmp_path, run_hippo):
    proc, argv = _dispatch(run_hippo, tmp_project, tmp_path, BRIEF)
    assert proc.returncode == 0, proc.stderr
    assert "--output-last-message" not in argv, "no judge, no capture"
    assert argv == ["exec", "-m", "gpt-5.6-luna", "-c", "model_reasoning_effort=medium", BRIEF]
    assert "triage" not in proc.stderr and "note" not in proc.stderr
    assert _rows(tmp_project, "triage") == [] and _rows(tmp_project, "clerk") == []


def test_the_lane_dir_is_the_C_worktree(tmp_project, tmp_path, run_hippo):
    lane = tmp_path / "lane"
    lane.mkdir()
    subprocess.run(["git", "-C", str(lane), "init", "-q"], check=True, capture_output=True)
    (lane / "fetch.py").write_text("retry = True\n", encoding="utf-8")
    capture = tmp_path / "sent.json"
    proc, _ = _dispatch(run_hippo, tmp_project, tmp_path, "-C", str(lane), BRIEF,
                        env=_jev(_mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}),
                                 capture))
    assert proc.returncode == 0, proc.stderr
    assert "?? fetch.py" in json.loads(capture.read_text(encoding="utf-8"))["state"]["changes"]


# --------------------------------------------------------------------------
# launch-time notes
# --------------------------------------------------------------------------

def test_a_short_last_argument_is_not_read_as_a_brief(tmp_project, tmp_path, run_hippo):
    capture = tmp_path / "sent.json"
    proc, _ = _dispatch(run_hippo, tmp_project, tmp_path, "do it",
                        env=_jev(_mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}),
                                 capture))
    assert proc.returncode == 0, proc.stderr
    assert [e["name"] for e in _rows(tmp_project, "clerk")] == ["jev-harvest"], \
        "no brief, no launch-time requests — the exit triage still runs"
    assert json.loads(capture.read_text(encoding="utf-8"))["state"]["brief"] is None


def test_a_brief_two_tiers_above_its_model_gets_a_note(tmp_project, tmp_path, run_hippo):
    hard = {**ACCEPT, "scope": {"score": 1.0, "confidence": 0.8},
            "novelty": {"score": 2.8, "confidence": 0.8},
            "spec": {"score": 2.1, "confidence": 0.8}}
    proc, _ = _dispatch(run_hippo, tmp_project, tmp_path, BRIEF,
                        env=_jev(_mock(tmp_path, {"answers": hard, "default": DEFAULT})))
    assert proc.returncode == 0, "a note, never a gate"
    assert ("dispatch: note — this brief reads top-tier (scope 1.0, novelty 2.8, spec 2.1) — "
            "launched on gpt-5.6-luna/medium") in proc.stderr.splitlines()


def test_a_brief_that_contradicts_a_lane_directive_gets_a_note(tmp_project, tmp_path,
                                                               run_hippo):
    for did, audience, text in (("gpu-pin", "all", "use GPUs 0 and 1 only"),
                                ("tone", "main", "answer the user in Korean")):
        assert run_hippo(["directive", "add", "--id", did, "--text", text, "--lifetime",
                          "durable", "--audience", audience], cwd=tmp_project).returncode == 0
    mock = _mock(tmp_path, {"answers": {**ACCEPT, "conflict_0": {"noul": 0.83}},
                            "default": DEFAULT})
    proc, _ = _dispatch(run_hippo, tmp_project, tmp_path, BRIEF, env=_jev(mock))
    assert proc.returncode == 0
    assert ("dispatch: note — brief may conflict with directive gpu-pin (0.83): "
            "use GPUs 0 and 1 only") in proc.stderr.splitlines()
    brief_rows = [e for e in _rows(tmp_project, "clerk") if e["name"] == "jev-brief"]
    assert len(brief_rows) == 1 and brief_rows[0]["src"] == "wrapper"


# --------------------------------------------------------------------------
# the row: schema, who may write it
# --------------------------------------------------------------------------

def test_the_triage_row_is_schema_checked_and_joins_fail_closed(tmp_project, run_hippo):
    assert run_hippo(["log", "dispatch", "--id", "d1", "--kind", "impl", "--exec",
                      "codex/gpt-5.6-luna/medium", "--scope", "x"],
                     cwd=tmp_project).returncode == 0
    ok = {"ev": "triage", "ref": "d1", "route": "escalate", "verify": True,
          "cause": "spec", "p": {"done": 0.4, "risk": 2}}
    assert hippo_cli.validate_event(ok) is None
    for bad in ({**ok, "route": "accepted"}, {**ok, "cause": "none"}, {**ok, "verify": "yes"},
                {**ok, "p": {"done": True}}, {**ok, "p": {"mood": 0.5}},
                {**ok, "p": {"done": {"noul": 0.4}}}, {**ok, "answers": {}}):
        assert hippo_cli.validate_event(bad), bad
    proc = run_hippo(["log", "raw", json.dumps({**ok, "ref": "d-nope"})], cwd=tmp_project)
    assert proc.returncode != 0 and "not a known dispatch id" in proc.stderr


def test_the_scribe_may_not_write_a_triage():
    e = {"ev": "triage", "ref": "d1", "route": "accept-candidate"}
    assert "wrapper records triage" in hippo_cli.validate_scribe_event(e)


# --------------------------------------------------------------------------
# the in-flight line
# --------------------------------------------------------------------------

def test_the_in_flight_line_carries_the_latest_route(tmp_project):
    t = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{"t": t, "ev": "dispatch", "id": "d1", "kind": "impl",
             "exec": "codex/gpt-5.6-luna/medium", "scope": "retry loop", "src": "wrapper"},
            {"t": t, "ev": "outcome", "ref": "d1", "result": "accepted", "src": "executor"},
            {"t": t, "ev": "triage", "ref": "d1", "route": "accept-candidate", "verify": False,
             "src": "wrapper"},
            {"t": t, "ev": "triage", "ref": "d1", "route": "escalate", "verify": True,
             "src": "wrapper"}]
    with (tmp_project / ".hippo" / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    assert hippo_cli.in_flight(tmp_project / ".hippo") == [
        "retry loop (0h00m · claims accepted · triage escalate · verify)"]


# --------------------------------------------------------------------------
# the loop: PRIORS measures the judge the way it measures executors
# --------------------------------------------------------------------------

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def _t(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _judged(did, route, result, retriaged=None):
    rows = [{"t": _t(60), "ev": "dispatch", "id": did, "kind": "impl",
             "exec": "codex/gpt-5.6-luna/medium", "scope": "x", "src": "wrapper"},
            {"t": _t(50), "ev": "triage", "ref": did, "route": route, "src": "wrapper"}]
    if result:
        rows.append({"t": _t(40), "ev": "outcome", "ref": did, "result": result})
    if retriaged:  # a triage recorded after the verdict is not what main judged against
        rows.append({"t": _t(30), "ev": "triage", "ref": did, "route": retriaged,
                     "src": "wrapper"})
    return rows


def _section(text):
    head = "## triage agreement — the judge's route against main's first verdict"
    assert head in text
    return text.split(head, 1)[1].split("\n## ", 1)[0]


def test_the_agreement_table_counts_routes_against_first_verdicts():
    rows = []
    for i, result in enumerate(["accepted", "accepted", "accepted", "revised", "refuted"]):
        rows += _judged(f"da{i}", "accept-candidate", result,
                        retriaged="escalate" if i == 0 else None)
    rows += _judged("de0", "escalate", "refuted") + _judged("de1", "escalate", "accepted")
    rows += _judged("dn0", "no-go-candidate", None)  # no verdict yet: in no cell
    out = _section(hippo_cli.prior_facts(rows, NOW))
    assert "| route | n | accepted | revised | refuted | no-go | lost |" in out
    assert "| accept-candidate | 5 | 3 | 1 | 1 | 0 | 0 |" in out
    assert "below the n=4 threshold, no row reported (1): escalate (n=2)" in out
    assert ("accept-candidate precision 4/5 (80.0%) — accepted or revised of "
            "accept-candidates that got a verdict") in out
    assert "no-go-candidate" not in out


def test_a_thin_precision_is_named_not_rated():
    rows = _judged("d0", "accept-candidate", "accepted") + _judged("d1", "failed", "no-go")
    out = _section(hippo_cli.prior_facts(rows, NOW))
    assert "| route |" not in out
    assert "accept-candidate (n=1), failed (n=1)" in out
    assert "accept-candidate precision 1/1 (n=1, no rate)" in out


def test_no_triage_rows_no_section():
    rows = [{"t": _t(60), "ev": "dispatch", "id": "d0", "kind": "impl",
             "exec": "codex/gpt-5.6-luna/medium", "scope": "x"},
            {"t": _t(40), "ev": "outcome", "ref": "d0", "result": "accepted"}]
    assert "triage agreement" not in hippo_cli.prior_facts(rows, NOW)
