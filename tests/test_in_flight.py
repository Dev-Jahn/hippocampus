"""The `in flight` capsule line (DESIGN §6): launched, unjudged, recent, launcher-written."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402


def _inject(run_hippo, cwd):
    out = run_hippo(["status", "--inject"], cwd=cwd)
    assert out.returncode == 0, out.stderr
    return [ln for ln in out.stdout.splitlines() if ln.startswith("· in flight:")]


def _launch(run_hippo, cwd, did, scope="tensorize pass2"):
    proc = run_hippo(["log", "dispatch", "--id", did, "--kind", "impl",
                      "--exec", "codex/gpt-6-sol/high", "--scope", scope], cwd=cwd)
    assert proc.returncode == 0, proc.stderr


def test_nothing_flying_costs_no_line(tmp_project, run_hippo):
    assert _inject(run_hippo, tmp_project) == []


def test_a_launched_dispatch_shows_with_its_age(tmp_project, run_hippo):
    _launch(run_hippo, tmp_project, "d1", "NVFP4 factor-rebasing 6-part")
    line = _inject(run_hippo, tmp_project)
    assert len(line) == 1
    assert "NVFP4 factor-rebasing 6-part" in line[0]
    assert "0h00m" in line[0]


def test_a_judged_dispatch_drops_off(tmp_project, run_hippo):
    _launch(run_hippo, tmp_project, "d1")
    _launch(run_hippo, tmp_project, "d2", "verify the kernel")
    run_hippo(["log", "outcome", "--ref", "d1", "--result", "accepted"], cwd=tmp_project)
    line = _inject(run_hippo, tmp_project)
    assert "verify the kernel" in line[0]
    assert "tensorize pass2" not in line[0]


def test_scribe_inferred_dispatches_are_not_counted(tmp_project, run_hippo, fake_transcript,
                                                    valid_mock_output):
    """Measured: over every writer this query returned 16 where three lanes were flying. The
    launcher's rows are the ones somebody chose to launch and will come back to."""
    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "s1"],
        cwd=tmp_project,
        env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(valid_mock_output)},
    )
    assert proc.returncode == 0, proc.stderr
    # the fixture's scribe dispatch (d100) landed, but must not appear here
    assert _inject(run_hippo, tmp_project) == []


def test_a_stale_dispatch_is_not_in_flight(tmp_project, run_hippo):
    """Older than a day is forgotten, not flying — `prior distill` reports those as open items."""
    hp = tmp_project / ".hippo"
    with (hp / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"t":"2020-01-01T00:00:00Z","ev":"dispatch","id":"dold","kind":"impl",'
                '"exec":"codex/gpt-6-sol/high","scope":"ancient","src":"wrapper"}\n')
    assert _inject(run_hippo, tmp_project) == []
    assert hippo_cli.in_flight(hp) == []


def test_a_lane_s_record_tells_running_from_ended(tmp_project, run_hippo):
    """Measured (1.17.1, 46 capsules): 194 of 285 in-flight entries were lanes that had already
    ended, shown with their age since launch, and lanes judged while their wrappers ran vanished.
    A lane record decides: running (judged or not) on the in-flight line, `running <age>`; ended
    or lost and unjudged on its own line, with how it ended and when; judged and ended, nowhere.
    A launch with no record keeps the bare age."""
    hp = tmp_project / ".hippo"
    now = datetime.now(timezone.utc)
    ago = lambda m: (now - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    for did in ("drun", "djudged", "dended", "dkilled", "dlost", "ddone", "dbare"):
        _launch(run_hippo, tmp_project, did, f"scope {did}")
    lanes = hp / "lanes"
    lanes.mkdir()
    gone = 2 ** 22 + 12345  # a pid no process has
    records = {
        "drun": {"pid": os.getpid(), "started": ago(42)},
        "djudged": {"pid": os.getpid(), "started": ago(42)},
        "dended": {"pid": gone, "status": "exited", "rc": 1, "ended": ago(125)},
        "dkilled": {"pid": gone, "status": "killed", "rc": None, "signal": "SIGTERM",
                    "ended": ago(5)},
        "dlost": {"pid": gone, "last_at": ago(30)},
        "ddone": {"pid": gone, "status": "exited", "rc": 0, "ended": ago(5)},
    }
    for did, rec in records.items():
        (lanes / f"{did}.json").write_text(json.dumps({"id": did, **rec}), encoding="utf-8")
    for did in ("djudged", "ddone"):
        assert run_hippo(["log", "outcome", "--ref", did, "--result", "accepted"],
                         cwd=tmp_project).returncode == 0
    out = run_hippo(["status", "--inject"], cwd=tmp_project).stdout.splitlines()
    assert [ln for ln in out if ln.startswith("· in flight:")] == [
        "· in flight: scope drun (running 0h00m), scope djudged (running 0h00m · judged "
        "accepted), scope dbare (0h00m)"]
    assert [ln for ln in out if ln.startswith("· ended,")] == [
        "· ended, awaiting your verdict: scope dended (rc=1 · ended 2h05m ago), scope dkilled "
        "(killed by SIGTERM · ended 0h05m ago), scope dlost (lost: wrapper gone, no end "
        "recorded · last seen 0h30m ago)"]
    lane = run_hippo(["status", "--inject"], cwd=tmp_project, env={"HIPPO_DISPATCH": "x"})
    assert "· ended," not in lane.stdout  # a verdict is main's to give
