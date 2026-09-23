"""`hippo log outcome --from-batch <journal>` (DESIGN §3.6) — bulk verdict serialization.

Contract under test: stdin JSON-lines resolved through the journal's latest exited
attempts onto dispatches whose executor claim still awaits a verdict; fail-closed and
total (every problem with its line number, nothing written on any failure); the strict
row vocabulary (ref/t/src never accepted); --dry-run; the one-line summary; and the
scalar mode left untouched, including mutual exclusion with the bulk flag.
"""
import json

from conftest import read_ledger

T = "2026-08-13T00:00:00Z"


def _dispatch(did, scope="lane"):
    return {"t": T, "ev": "dispatch", "id": did, "kind": "impl",
            "exec": "codex/gpt-6-luna/low", "scope": scope, "src": "wrapper"}


def _claim(did, result="accepted"):
    return {"t": T, "ev": "outcome", "ref": did, "result": result, "src": "executor"}


def _verdict(did, result="accepted"):
    return {"t": T, "ev": "outcome", "ref": did, "result": result, "src": "cli"}


def _exit(entry, attempt, did, rc=0, check_rc=0):
    return {"t": T, "event": "exit", "id": entry, "attempt": attempt,
            "dispatch": did, "rc": rc, "check_rc": check_rc}


def _seed(project, journal_rows, ledger_rows):
    jp = project / "wave.journal.jsonl"
    jp.write_text("".join(json.dumps(r) + "\n" for r in journal_rows), encoding="utf-8")
    with (project / ".hippo" / "ledger.jsonl").open("a", encoding="utf-8") as f:
        for r in ledger_rows:
            f.write(json.dumps(r) + "\n")
    return jp


def _row(entry, attempt=1, result="accepted", note="patch and check output inspected", **kw):
    return json.dumps({"entry": entry, "attempt": attempt, "result": result,
                       "note": note, **kw})


def _bulk(run_hippo, project, jp, rows, *flags):
    # HIPPO_DISPATCH cleared: the suite itself may run inside a lane, and an inherited id
    # would turn every bulk write into an executor claim.
    return run_hippo(["log", "outcome", "--from-batch", str(jp), *flags],
                     cwd=project, env={"HIPPO_DISPATCH": ""},
                     input_text="\n".join(rows) + "\n")


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------

def test_bulk_writes_verdicts_and_summarizes(tmp_project, run_hippo):
    jp = _seed(tmp_project,
               [_exit("e1", 1, "d1"), _exit("e2", 1, "d2"), _exit("e3", 1, "d3")],
               [_dispatch("d1"), _dispatch("d2"), _dispatch("d3"),
                _claim("d1"), _claim("d2"), _claim("d3")])
    before = read_ledger(tmp_project)
    proc = _bulk(run_hippo, tmp_project, jp,
                 [_row("e1"),
                  _row("e2", result="revised", attr="work", rework=1, by="verify/opus")])
    assert proc.returncode == 0, proc.stderr
    after = read_ledger(tmp_project)
    assert len(after) == len(before) + 2
    by_ref = {e["ref"]: e for e in after[-2:]}
    assert by_ref["d1"]["ev"] == "outcome"
    assert by_ref["d1"]["result"] == "accepted"
    assert by_ref["d1"]["src"] == "cli"
    assert by_ref["d1"]["note"]
    assert by_ref["d2"]["result"] == "revised"
    assert by_ref["d2"]["attr"] == "work"
    assert by_ref["d2"]["rework"] == 1
    assert by_ref["d2"]["by"] == "verify/opus"
    summary = json.loads(proc.stdout)
    assert summary["validated"] == 2
    assert summary["written"] == 2
    assert summary["remaining_pending"] == 1  # d3's claim is still unjudged
    assert summary["results"] == {"accepted": 1, "revised": 1}


# --------------------------------------------------------------------------
# fail-closed and total: nothing written on any invalid row
# --------------------------------------------------------------------------

def test_one_bad_row_writes_nothing_and_names_the_line(tmp_project, run_hippo):
    jp = _seed(tmp_project,
               [_exit("e1", 1, "d1"), _exit("e2", 1, "d2")],
               [_dispatch("d1"), _dispatch("d2"), _claim("d1"), _claim("d2")])
    before = read_ledger(tmp_project)
    proc = _bulk(run_hippo, tmp_project, jp,
                 [_row("e1"), _row("e2", result="maybe-ish")])
    assert proc.returncode == 2
    assert read_ledger(tmp_project) == before
    assert "line 2" in proc.stderr
    assert "nothing written" in proc.stderr


def test_unknown_key_rejected(tmp_project, run_hippo):
    jp = _seed(tmp_project, [_exit("e1", 1, "d1")], [_dispatch("d1"), _claim("d1")])
    before = read_ledger(tmp_project)
    row = json.dumps({"entry": "e1", "attempt": 1, "result": "accepted",
                      "note": "n", "ref": "d1"})
    proc = _bulk(run_hippo, tmp_project, jp, [row])
    assert proc.returncode == 2
    assert read_ledger(tmp_project) == before
    assert "ref" in proc.stderr
    assert "not allowed" in proc.stderr


def test_note_required_in_bulk_mode(tmp_project, run_hippo):
    jp = _seed(tmp_project, [_exit("e1", 1, "d1")], [_dispatch("d1"), _claim("d1")])
    row = json.dumps({"entry": "e1", "attempt": 1, "result": "accepted"})
    proc = _bulk(run_hippo, tmp_project, jp, [row])
    assert proc.returncode == 2
    assert "note" in proc.stderr


def test_duplicate_entry_rows_rejected(tmp_project, run_hippo):
    jp = _seed(tmp_project, [_exit("e1", 1, "d1")], [_dispatch("d1"), _claim("d1")])
    before = read_ledger(tmp_project)
    proc = _bulk(run_hippo, tmp_project, jp, [_row("e1"), _row("e1", result="refuted")])
    assert proc.returncode == 2
    assert read_ledger(tmp_project) == before
    assert "duplicate" in proc.stderr


def test_empty_stdin_rejected(tmp_project, run_hippo):
    jp = _seed(tmp_project, [_exit("e1", 1, "d1")], [_dispatch("d1"), _claim("d1")])
    proc = _bulk(run_hippo, tmp_project, jp, [""])
    assert proc.returncode == 2
    assert "no verdict rows" in proc.stderr


# --------------------------------------------------------------------------
# journal resolution: latest exited attempt only, unambiguous, journal-scoped
# --------------------------------------------------------------------------

def test_non_latest_attempt_rejected(tmp_project, run_hippo):
    jp = _seed(tmp_project,
               [_exit("e1", 1, "d1", rc=1), _exit("e1", 2, "d1b")],
               [_dispatch("d1"), _dispatch("d1b"), _claim("d1"), _claim("d1b")])
    before = read_ledger(tmp_project)
    proc = _bulk(run_hippo, tmp_project, jp, [_row("e1", attempt=1)])
    assert proc.returncode == 2
    assert read_ledger(tmp_project) == before
    assert "latest" in proc.stderr
    # The latest attempt for the same entry goes through.
    proc = _bulk(run_hippo, tmp_project, jp, [_row("e1", attempt=2)])
    assert proc.returncode == 0, proc.stderr
    assert read_ledger(tmp_project)[-1]["ref"] == "d1b"


def test_unknown_entry_rejected(tmp_project, run_hippo):
    jp = _seed(tmp_project, [_exit("e1", 1, "d1")], [_dispatch("d1"), _claim("d1")])
    proc = _bulk(run_hippo, tmp_project, jp, [_row("ghost")])
    assert proc.returncode == 2
    assert "ghost" in proc.stderr


# --------------------------------------------------------------------------
# ledger gate: recorded dispatch, still-pending claim
# --------------------------------------------------------------------------

def test_dispatch_missing_from_ledger_rejected(tmp_project, run_hippo):
    # The journal exited d1, but its ledger record failed at launch time (§3.6
    # record_failed): the gap stays a gap — bulk never backfills an identity.
    jp = _seed(tmp_project, [_exit("e1", 1, "d1")], [])
    proc = _bulk(run_hippo, tmp_project, jp, [_row("e1")])
    assert proc.returncode == 2
    assert "d1" in proc.stderr


def test_already_judged_dispatch_rejected(tmp_project, run_hippo):
    jp = _seed(tmp_project, [_exit("e1", 1, "d1")],
               [_dispatch("d1"), _claim("d1"), _verdict("d1")])
    before = read_ledger(tmp_project)
    proc = _bulk(run_hippo, tmp_project, jp, [_row("e1", result="refuted")])
    assert proc.returncode == 2
    assert read_ledger(tmp_project) == before
    assert "verdict" in proc.stderr
    assert "scalar" in proc.stderr


def test_unclaimed_dispatch_rejected(tmp_project, run_hippo):
    jp = _seed(tmp_project, [_exit("e1", 1, "d1")], [_dispatch("d1")])
    proc = _bulk(run_hippo, tmp_project, jp, [_row("e1")])
    assert proc.returncode == 2
    assert "claim" in proc.stderr


# --------------------------------------------------------------------------
# --dry-run
# --------------------------------------------------------------------------

def test_dry_run_validates_everything_and_writes_nothing(tmp_project, run_hippo):
    jp = _seed(tmp_project,
               [_exit("e1", 1, "d1"), _exit("e2", 1, "d2")],
               [_dispatch("d1"), _dispatch("d2"), _claim("d1"), _claim("d2")])
    before = read_ledger(tmp_project)
    proc = _bulk(run_hippo, tmp_project, jp, [_row("e1"), _row("e2")], "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert read_ledger(tmp_project) == before
    summary = json.loads(proc.stdout)
    assert summary["dry_run"] is True
    assert summary["validated"] == 2
    assert summary["written"] == 0
    assert summary["remaining_pending"] == 2  # nothing was judged


# --------------------------------------------------------------------------
# the scalar mode is untouched
# --------------------------------------------------------------------------

def test_scalar_flags_and_bulk_flag_are_mutually_exclusive(tmp_project, run_hippo):
    jp = _seed(tmp_project, [_exit("e1", 1, "d1")], [_dispatch("d1"), _claim("d1")])
    before = read_ledger(tmp_project)
    proc = run_hippo(
        ["log", "outcome", "--from-batch", str(jp), "--result", "accepted"],
        cwd=tmp_project, env={"HIPPO_DISPATCH": ""}, input_text=_row("e1") + "\n")
    assert proc.returncode == 2
    assert "--result" in proc.stderr
    assert read_ledger(tmp_project) == before


def test_scalar_outcome_still_works(tmp_project, run_hippo):
    _seed(tmp_project, [_exit("e1", 1, "d1")], [_dispatch("d1"), _claim("d1")])
    proc = run_hippo(
        ["log", "outcome", "--ref", "d1", "--result", "accepted"],
        cwd=tmp_project, env={"HIPPO_DISPATCH": ""})
    assert proc.returncode == 0, proc.stderr
    entry = read_ledger(tmp_project)[-1]
    assert entry["ev"] == "outcome"
    assert entry["ref"] == "d1"


def test_scalar_outcome_without_result_fails_closed(tmp_project, run_hippo):
    _seed(tmp_project, [_exit("e1", 1, "d1")], [_dispatch("d1")])
    before = read_ledger(tmp_project)
    proc = run_hippo(["log", "outcome", "--ref", "d1"],
                     cwd=tmp_project, env={"HIPPO_DISPATCH": ""})
    assert proc.returncode == 2
    assert "--result" in proc.stderr
    assert read_ledger(tmp_project) == before


def test_dry_run_without_from_batch_rejected(tmp_project, run_hippo):
    _seed(tmp_project, [_exit("e1", 1, "d1")], [_dispatch("d1")])
    proc = run_hippo(
        ["log", "outcome", "--ref", "d1", "--result", "accepted", "--dry-run"],
        cwd=tmp_project, env={"HIPPO_DISPATCH": ""})
    assert proc.returncode == 2
    assert "--from-batch" in proc.stderr
