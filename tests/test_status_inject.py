"""Item (4): `hippo status --inject`.

DESIGN.md §3.3 + §6: in an initialized project it must print the resident
capsule (a `[hippo]`-prefixed block, at most 6 lines per §6's own example
and the "anything larger is a regression" ceiling). Outside any `.hippo/` project
the global common rule applies: complete silent no-op — 0 bytes on stdout
*and* stderr, exit 0.
"""
import json


def test_status_inject_initialized_project_emits_capsule(tmp_project, run_hippo):
    proc = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() != ""

    lines = proc.stdout.splitlines()
    assert lines[0].startswith("[hippo]")
    assert len(lines) <= 6, f"resident capsule must stay <=6 lines, got {len(lines)}"


def test_status_inject_uninitialized_dir_is_fully_silent(uninitialized_dir, run_hippo):
    proc = run_hippo(["status", "--inject"], cwd=uninitialized_dir)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_task_list_uninitialized_dir_is_fully_silent(uninitialized_dir, run_hippo):
    """The no-op rule is stated as universal (every surface), not specific to
    `status --inject` — spot-check one more entry point cheaply."""
    proc = run_hippo(["task", "list"], cwd=uninitialized_dir)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


def _scribe_runs(project, oks):
    with (project / ".hippo" / "ledger.jsonl").open("a", encoding="utf-8") as f:
        for ok in oks:
            f.write(json.dumps({"t": "2026-09-24T12:00:00Z", "ev": "clerk", "name": "turn-scribe",
                                "ok": ok, "ms": 10, "tokens": 1, "src": "scribe"}) + "\n")


def test_a_scribe_failing_three_times_in_a_row_says_so(tmp_project, run_hippo):
    """Measured on b200: seven failed scribe runs in a row and nothing said so (§6). The line
    names the streak and quotes the newest dump's reason; two failures, or a success after
    them, is no line at all."""
    failures = tmp_project / ".hippo" / "failures"
    failures.mkdir(exist_ok=True)
    (failures / "20260924T110000-scribe-1-aaaaaa.txt").write_text("clerk rc=124\n")
    (failures / "20260924T120000-scribe-2-bbbbbb.txt").write_text(
        "clerk rc=1: codex: ERROR: unexpected status 401 Unauthorized\nrc=1\n--- stderr ---\n")

    def capsule():
        proc = run_hippo(["status", "--inject"], cwd=tmp_project)
        assert proc.returncode == 0, proc.stderr
        return [ln for ln in proc.stdout.splitlines() if ln.startswith("· scribe:")]

    _scribe_runs(tmp_project, [True, False, False])
    assert capsule() == []
    _scribe_runs(tmp_project, [False])
    assert capsule() == [("· scribe: the last 3 runs failed — clerk rc=1: codex: ERROR: "
                          "unexpected status 401 Unauthorized (.hippo/failures/)")]
    _scribe_runs(tmp_project, [True])
    assert capsule() == []
