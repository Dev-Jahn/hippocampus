"""A verdict needs an ended delegation (DESIGN §3.2, §3.5.6b, §3.6b).

Measured (steno, 2026-10-01): the scribe — gpt-6-luna at medium, 1.15.4's default — read a
window whose last assistant line was "No verdict yet. I just launched two branch-triage lanes"
and recorded both lanes `accepted`, with notes saying the lanes were started. They ended 16 and
30 minutes later; main recorded `revised` and `lost`. PRIORS reads the first verdict, so both
counted as first-pass accepts. The prompt already said "never attach an outcome to a delegation
still in flight". So the writer checks it, the roster shows it, and the derived views stop
reading such a row as a verdict — without rewriting the ledger.

Nothing here may reach the network: conftest pins HIPPO_JEV_BACKEND=off.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from conftest import REPO_ROOT, ledger_path, read_ledger
from test_native_runs import (DID, _agent, _call, _launched, _msg, _note, _queued, _session,
                              _user, _write)

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402

RUNNING = "[running — no verdict yet]"


def _ago(minutes):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(project, *rows):
    with ledger_path(project).open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _dispatch(did, minutes_ago=10, src="wrapper", ex="codex/gpt-6-sol/high", kind="audit"):
    return {"t": _ago(minutes_ago), "ev": "dispatch", "id": did, "kind": kind, "exec": ex,
            "scope": f"lane {did}", "src": src}


def _usage(did, minutes_ago=1, src="wrapper"):
    return {"t": _ago(minutes_ago), "ev": "usage", "ref": did, "tokens": 10, "tin": 8,
            "tcached": 0, "tout": 2, "model": "gpt-6-sol", "src": src}


def _triage(did, minutes_ago=1):
    return {"t": _ago(minutes_ago), "ev": "triage", "ref": did, "route": "accept-candidate",
            "verify": False, "p": {"done": 0.9}, "src": "wrapper"}


def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def _lane(project, did, **over):
    lanes = project / ".hippo" / "lanes"
    lanes.mkdir(exist_ok=True)
    rec = {"id": did, "scope": f"lane {did}", "exec": "codex/gpt-6-sol/high",
           "pid": os.getpid(), "pgid": None, "started": _ago(10), "codex_session": None,
           "cmds": 3, "last": "exec: make", "last_at": _ago(1), **over}
    (lanes / f"{did}.json").write_text(json.dumps(rec), encoding="utf-8")


def _accept(did, note="started the lane"):
    return {"ev": "outcome", "ref": did, "result": "accepted", "note": note}


def _scribe(run_hippo, project, transcript, events, tmp_path, name="out", session="s1",
            capture=None):
    mock = tmp_path / f"{name}.json"
    mock.write_text(json.dumps({"worklog": "", "events": events}), encoding="utf-8")
    env = {"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock)}
    if capture is not None:
        env["HIPPO_MOCK_CAPTURE"] = str(capture)
    proc = run_hippo(["scribe", "--transcript", str(transcript), "--session", session],
                     cwd=project, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc


def _verdicts(project, src="scribe"):
    return [e for e in read_ledger(project) if e.get("ev") == "outcome" and e.get("src") == src]


def _dumps(project):
    return "".join(p.read_text() for p in (project / ".hippo" / "failures").glob("*-scribe-*"))


# --------------------------------------------------------------------------
# 1. the writer: a scribe verdict on a delegation still in flight is dumped
# --------------------------------------------------------------------------

def test_a_scribe_verdict_on_a_running_lane_is_dumped(tmp_project, run_hippo, fake_transcript,
                                                      tmp_path):
    """The steno case: the lane's record has no end and its wrapper is alive."""
    _seed(tmp_project, _dispatch("dlane"))
    _lane(tmp_project, "dlane")
    _scribe(run_hippo, tmp_project, fake_transcript, [_accept("dlane")], tmp_path)

    assert not _verdicts(tmp_project)
    dump = _dumps(tmp_project)
    assert "'dlane' is still running" in dump and f"pid {os.getpid()}" in dump
    assert "started the lane" in dump  # the dump keeps what the clerk proposed


@pytest.mark.parametrize("record", [
    pytest.param({"status": "exited", "rc": 0, "ended": "2026-10-01T15:32:43Z"}, id="ended"),
    pytest.param({"pid": "dead"}, id="lost"),
    pytest.param({"pid": "dead", "status": "killed", "rc": -15}, id="died-after-its-status"),
])
def test_an_ended_or_lost_lane_takes_the_verdict(tmp_project, run_hippo, fake_transcript,
                                                 tmp_path, record):
    """`--watch`'s reading of a record: a lane whose wrapper is gone is not in flight, so a lost
    lane never blocks a verdict forever."""
    if record.get("pid") == "dead":
        record = {**record, "pid": _dead_pid()}
    _seed(tmp_project, _dispatch("dlane"))
    _lane(tmp_project, "dlane", **record)
    _scribe(run_hippo, tmp_project, fake_transcript, [_accept("dlane", "merged")], tmp_path)
    assert [e["ref"] for e in _verdicts(tmp_project)] == ["dlane"]


@pytest.mark.parametrize("rows, in_flight", [
    pytest.param([_dispatch("dnorec")], True, id="fresh-launch-no-exit"),
    pytest.param([_dispatch("dnorec"), _usage("dnorec")], False, id="usage-written-at-exit"),
    pytest.param([_dispatch("dnorec"), _triage("dnorec")], False, id="triage-written-at-exit"),
    pytest.param([_dispatch("dnorec", minutes_ago=25 * 60)], False, id="older-than-a-day"),
    pytest.param([_dispatch("dnorec", src="cli", ex="claude/opus/high")], False,
                 id="main-s-own-row-has-no-exit-to-wait-for"),
])
def test_without_a_lane_record_the_ledger_decides(tmp_project, run_hippo, fake_transcript,
                                                  tmp_path, rows, in_flight):
    """No record (it could not be written, or was removed): a wrapper launch with no exit row
    yet, launched within the day the capsule calls in flight, may still run. Anything else is
    not blocked — no guess."""
    _seed(tmp_project, *rows)
    _scribe(run_hippo, tmp_project, fake_transcript, [_accept("dnorec")], tmp_path)
    assert bool(_verdicts(tmp_project)) is not in_flight
    assert ("is still running" in _dumps(tmp_project)) is in_flight


def test_a_native_run_takes_a_verdict_only_once_it_has_ended(tmp_project, run_hippo, tmp_path):
    """hippo's own index says when a run ended (a notification, a TaskStop, a run file); the
    roster says so too, and a verdict the clerk reads off the launch window is dumped."""
    _seed(tmp_project, _dispatch(DID, src="scribe", ex="subagent/claude-opus-5-5/xhigh",
                                 kind="impl"))
    transcript = tmp_project / "transcript.jsonl"
    _write(transcript, [_user("add the retry loop in a subagent"), _call(), _launched()])
    _agent(_session(tmp_project), msgs=[_msg("m1")])
    capture = tmp_path / "payload.txt"
    _scribe(run_hippo, tmp_project, transcript, [_accept(DID, "launched the retry loop")],
            tmp_path, name="w1", capture=capture)

    assert f"- {DID} (impl): lane {DID}  {RUNNING}" in capture.read_text()
    assert not _verdicts(tmp_project)
    assert f"'{DID}' is still running" in _dumps(tmp_project)

    _write(transcript, [_queued(_note()), _user("merged, thanks")], mode="a")
    _scribe(run_hippo, tmp_project, transcript, [_accept(DID, "retry loop merged")], tmp_path,
            name="w2", capture=capture)
    assert f"- {DID} (impl): lane {DID}  [no outcome yet]" in capture.read_text()
    assert [e["note"] for e in _verdicts(tmp_project)] == ["retry loop merged"]


def test_main_and_a_lane_are_never_gated(tmp_project, run_hippo):
    """Main's own verdict is main's call whenever it makes it, and a lane's claim is a claim."""
    _seed(tmp_project, _dispatch("dlane"))
    _lane(tmp_project, "dlane")
    main = run_hippo(["log", "outcome", "--ref", "dlane", "--result", "lost"], cwd=tmp_project)
    lane = run_hippo(["log", "outcome", "--result", "accepted"], cwd=tmp_project,
                     env={"HIPPO_DISPATCH": "dlane"})
    assert main.returncode == 0 and lane.returncode == 0, (main.stderr, lane.stderr)
    assert [(e["src"], e["result"]) for e in read_ledger(tmp_project)
            if e["ev"] == "outcome"] == [("cli", "lost"), ("executor", "accepted")]


# --------------------------------------------------------------------------
# 2. the roster: the clerk can see what is still running
# --------------------------------------------------------------------------

def test_the_roster_marks_a_running_delegation(tmp_project):
    hp = tmp_project / ".hippo"
    _seed(tmp_project, _dispatch("drun"), _dispatch("dended"), _dispatch("dclaim"),
          _dispatch("dopen", src="cli", ex="claude/opus/high"),
          {"t": _ago(2), "ev": "outcome", "ref": "dclaim", "result": "accepted",
           "src": "executor"})
    _lane(tmp_project, "drun")
    _lane(tmp_project, "dclaim")
    _lane(tmp_project, "dended", ended=_ago(1), status="exited", rc=0)
    roster = hippo_cli.dispatch_roster(hp).splitlines()
    assert roster == [f"- drun (audit): lane drun  {RUNNING}",
                      "- dended (audit): lane dended  [no outcome yet]",
                      f"- dclaim (audit): lane dclaim  {RUNNING}",
                      "- dopen (audit): lane dopen  [no outcome yet]"]


def test_the_prompt_reads_the_roster_s_marker():
    """Rule 2 names the marker exactly as the roster prints it."""
    prompt = (REPO_ROOT / "clerks" / "turn-scribe.md").read_text(encoding="utf-8")
    assert f"`{hippo_cli.ROSTER_RUNNING}`" in prompt


# --------------------------------------------------------------------------
# 3. the derived views: a scribe verdict stamped before the end is no verdict
# --------------------------------------------------------------------------

def _r(t, **e):
    return {"t": t, **e}


# The steno window, as the ledger has it (ids shortened, notes in English): a batch of two
# lanes, the scribe's two accepts 23s later, the remote lane's own claim, each lane's exit rows,
# a relaunch, and main's verdicts an hour later.
STENO = [
    _r("2026-10-01T15:15:39Z", ev="dispatch", id="dlocal", kind="audit",
       exec="codex/gpt-6.1-sol/high", scope="branch triage: local branches + tags", depth=0,
       src="wrapper"),
    _r("2026-10-01T15:15:39Z", ev="dispatch", id="dremote", kind="audit",
       exec="codex/gpt-6.1-sol/high", scope="branch triage: remote-only branches", depth=0,
       src="wrapper"),
    _r("2026-10-01T15:15:44Z", ev="clerk", name="jev-gate", ok=True, ms=302, tokens=3583,
       src="scribe"),
    _r("2026-10-01T15:16:02Z", ev="outcome", ref="dlocal", result="accepted",
       note="started the local branch and tag triage lane", src="scribe"),
    _r("2026-10-01T15:16:02Z", ev="outcome", ref="dremote", result="accepted",
       note="started the remote-only branch triage lane", src="scribe"),
    _r("2026-10-01T15:16:02Z", ev="clerk", name="turn-scribe", ms=18016, tokens=6128, ok=True,
       src="scribe"),
    _r("2026-10-01T15:32:21Z", ev="outcome", ref="dremote", result="accepted",
       note="L6 triage completed as an analysis claim for main review", src="executor"),
    _r("2026-10-01T15:32:43Z", ev="usage", ref="dremote", tokens=2172224, tin=2151467,
       tcached=2038016, tout=26403, model="gpt-6.1-sol", src="wrapper"),
    _r("2026-10-01T15:32:43Z", ev="triage", ref="dremote", route="accept-candidate",
       verify=False, p={"done": 0.95}, src="wrapper"),
    _r("2026-10-01T15:45:38Z", ev="usage", ref="dlocal", tokens=3649616, tin=3616353,
       tcached=3480064, tout=46558, model="gpt-6.1-sol", src="wrapper"),
    _r("2026-10-01T15:45:39Z", ev="triage", ref="dlocal", route="failed", verify=True,
       p={"done": 0.12}, src="wrapper"),
    _r("2026-10-01T15:46:16Z", ev="dispatch", id="drelaunch", kind="audit",
       exec="codex/gpt-6.1-sol/high", scope="branch triage: local branches + tags (relaunch)",
       depth=0, src="wrapper"),
    _r("2026-10-01T16:08:33Z", ev="usage", ref="drelaunch", tokens=3950776, tin=3925792,
       tcached=3792512, tout=37316, model="gpt-6.1-sol", src="wrapper"),
    _r("2026-10-01T16:42:09Z", ev="outcome", ref="dremote", result="revised", attr="work",
       rework=1, by="main/opus", src="cli"),
    _r("2026-10-01T16:42:10Z", ev="outcome", ref="dlocal", result="lost", attr="harness",
       by="main/opus", src="cli"),
    _r("2026-10-01T16:42:10Z", ev="outcome", ref="drelaunch", result="revised", attr="work",
       rework=1, by="main/opus", src="cli"),
]


def test_steno_s_premature_accepts_stop_counting_as_first_pass():
    [cell] = hippo_cli.prior_cells(STENO).values()
    assert {k: cell[k] for k in ("judged", "accepted", "revised", "lost")} == {
        "judged": 3, "accepted": 0, "revised": 2, "lost": 1}
    assert [e["result"] for e in hippo_cli.verdict_rows(STENO)] == ["revised", "lost", "revised"]
    assert hippo_cli.judged_refs(STENO) == {"dlocal", "dremote", "drelaunch"}
    page = hippo_cli.prior_facts(STENO, datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert "audit×codex/gpt-6.1-sol/high (n=2)" in page  # lost leaves the denominator


def test_the_judge_is_measured_against_main_s_verdict_not_the_premature_one():
    """With the scribe's 15:16 accept as the first verdict, both wrapper routes came after it
    and were dropped from the table; against main's verdicts they count."""
    section = "\n".join(hippo_cli.triage_agreement(STENO))
    assert "accept-candidate (n=1)" in section and "failed (n=1)" in section
    assert "accept-candidate precision 1/1" in section  # dremote: revised


def _native(*tail):
    """An Agent run: its row, and what one scribe run (or two) wrote after it."""
    return [_r("2026-10-01T10:00:00Z", ev="dispatch", id=DID, kind="impl",
               exec="subagent/claude-opus-5-5/xhigh", scope="retry loop", src="scribe"),
            *tail]


_OUT = _r("2026-10-01T10:05:00Z", ev="outcome", ref=DID, result="accepted", src="scribe")
_USE = _r("2026-10-01T10:05:01Z", ev="usage", ref=DID, tokens=10, tin=8, tcached=0, tout=2,
          model="claude-opus-5-5", src="scribe")
_METER = _r("2026-10-01T10:05:01Z", ev="clerk", name="turn-scribe", ok=True, ms=1, tokens=1,
            src="scribe")
_LANE_OUT = {**_OUT, "ref": "dlane", "t": _ago(5)}


@pytest.mark.parametrize("rows, counted", [
    # The run ended in the window its verdict was read from: the scribe writes the clerk's
    # events, then the run's usage, then its own meter — one run, so the verdict stands.
    pytest.param(_native(_OUT, _USE, _METER), True, id="native-ended-in-the-same-window"),
    pytest.param(_native(_OUT, _METER, _USE, _METER), False, id="native-ended-a-run-later"),
    pytest.param([_dispatch("dlane"), _LANE_OUT, _usage("dlane")], False,
                 id="wrapper-exit-after-the-verdict"),
    pytest.param([_dispatch("dlane"), _triage("dlane", minutes_ago=6), _LANE_OUT,
                  _usage("dlane")], True, id="wrapper-exit-before"),
    pytest.param([_dispatch("dlane"), _LANE_OUT], True, id="unknown-end"),
    pytest.param([_dispatch("dlane"), {**_LANE_OUT, "src": "cli"}, _usage("dlane")], True,
                 id="main-s-early-verdict-is-main-s-call"),
])
def test_a_scribe_verdict_counts_only_once_its_delegation_ended(rows, counted):
    assert bool(hippo_cli.verdict_rows(rows)) is counted
    assert bool(hippo_cli.prior_cells(rows)) is counted
    assert (_OUT["ref"] in hippo_cli.judged_refs(rows)
            or "dlane" in hippo_cli.judged_refs(rows)) is counted


def test_main_s_verdict_after_a_premature_one_is_the_first(tmp_project, run_hippo, tmp_path):
    """No second-verdict note: PRIORS reads main's. And nothing reading the ledger rewrites it —
    the distiller included."""
    _seed(tmp_project, *STENO[:11])
    before = ledger_path(tmp_project).read_bytes()
    proc = run_hippo(["log", "outcome", "--ref", "dremote", "--result", "revised"],
                     cwd=tmp_project)
    assert proc.returncode == 0 and "already had a verdict" not in proc.stderr, proc.stderr

    page = tmp_path / "priors.md"
    page.write_text("# PRIORS\n", encoding="utf-8")
    proc = run_hippo(["prior", "distill", "--days", "3650"], cwd=tmp_project,
                     env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(page)})
    assert proc.returncode == 0, proc.stderr
    assert ledger_path(tmp_project).read_bytes().startswith(before)
    added = ledger_path(tmp_project).read_bytes()[len(before):].decode().splitlines()
    assert [json.loads(x)["ev"] for x in added] == ["outcome", "clerk"]
