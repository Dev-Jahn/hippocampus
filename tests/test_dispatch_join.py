"""Keeping outcomes joined to dispatches: the roster the scribe is handed instead of being asked
to find ids in a digest, and `--ref task:<id>` resolution (DESIGN §3.2, §3.5.5)."""

import json
import sys

from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402


def _dispatch(run_hippo, cwd, did, task=None, kind="impl", scope="do the thing"):
    argv = ["log", "dispatch", "--id", did, "--kind", kind,
            "--exec", "codex/gpt-5.6-sol/high", "--scope", scope]
    if task:
        argv += ["--task", task]
    proc = run_hippo(argv, cwd=cwd)
    assert proc.returncode == 0, proc.stderr
    return did


def _outcome(run_hippo, cwd, ref, result="accepted"):
    return run_hippo(["log", "outcome", "--ref", ref, "--result", result], cwd=cwd)


# --------------------------------------------------------------------------
# the roster
# --------------------------------------------------------------------------

def test_roster_lists_recent_dispatches_and_flags_the_unjudged(tmp_project, run_hippo):
    """The clerk was told not to record a launch twice but had no way to know what was already
    recorded. The roster is that list, and the flag marks the ones an outcome is actually about."""
    _dispatch(run_hippo, tmp_project, "d001", scope="tensorize pass2")
    _dispatch(run_hippo, tmp_project, "d002", scope="verify the kernel")
    _outcome(run_hippo, tmp_project, "d001")

    roster = hippo_cli.dispatch_roster(tmp_project / ".hippo")
    assert "d001 (impl): tensorize pass2" in roster
    assert "d002 (impl): verify the kernel  [no outcome yet]" in roster
    # the judged one is listed, but not offered as somewhere to hang a verdict
    assert "d001" in roster and "no outcome yet" not in roster.split("d002")[0]


def test_roster_is_bounded(tmp_project, run_hippo):
    """It rides on every scribe call, so it is a fixed cost, not one that grows with the ledger."""
    for i in range(hippo_cli.DISPATCH_ROSTER_N + 5):
        _dispatch(run_hippo, tmp_project, f"d{i:03d}")
    roster = hippo_cli.dispatch_roster(tmp_project / ".hippo")
    assert len(roster.splitlines()) == hippo_cli.DISPATCH_ROSTER_N
    assert "d016" in roster and "d000" not in roster  # the recent end is the useful end


def test_empty_roster_says_so(tmp_project):
    assert hippo_cli.dispatch_roster(tmp_project / ".hippo") == "(none yet)"


def test_scribe_payload_carries_both_rosters(tmp_project, run_hippo, fake_transcript,
                                             valid_mock_output, tmp_path):
    """A prompt cannot enforce what its inputs do not contain — the whole point is that the list
    reaches the clerk, so assert on what is actually sent."""
    _dispatch(run_hippo, tmp_project, "d001", scope="tensorize pass2")
    run_hippo(["directive", "add", "--text", "use GPUs 0 and 1 only",
               "--lifetime", "phase", "--id", "gpu-01"], cwd=tmp_project)

    captured = tmp_path / "payload.txt"
    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "s1"],
        cwd=tmp_project,
        env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(valid_mock_output),
             "HIPPO_MOCK_CAPTURE": str(captured)},
    )
    assert proc.returncode == 0, proc.stderr
    assert captured.exists(), "the mock backend did not record what it was handed"
    text = captured.read_text(encoding="utf-8")
    assert "# live directives" in text and "gpu-01" in text
    assert "# dispatches already recorded" in text and "d001" in text
    assert "# transcript digest" in text


# --------------------------------------------------------------------------
# --ref task:<id>
# --------------------------------------------------------------------------

def test_ref_task_resolves_to_the_open_dispatch_and_stores_the_dispatch_id(
    tmp_project, run_hippo
):
    """The ergonomic fix that does not loosen the contract: the caller names the task they
    remember, the ledger keeps the id that joins."""
    _dispatch(run_hippo, tmp_project, "d7f1a2", task="feat/x")
    proc = _outcome(run_hippo, tmp_project, "task:feat/x")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ref"] == "d7f1a2"


def test_ref_task_ignores_a_dispatch_that_already_has_a_verdict(tmp_project, run_hippo):
    _dispatch(run_hippo, tmp_project, "dold", task="feat/x")
    _outcome(run_hippo, tmp_project, "dold")
    _dispatch(run_hippo, tmp_project, "dnew", task="feat/x")

    proc = _outcome(run_hippo, tmp_project, "task:feat/x", result="refuted")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ref"] == "dnew"


def test_ref_task_with_two_open_dispatches_lists_them_and_fails(tmp_project, run_hippo):
    """Two lanes on one task is exactly when a guess would attach the verdict to the wrong one."""
    _dispatch(run_hippo, tmp_project, "dlane1", task="feat/x", scope="the first lane")
    _dispatch(run_hippo, tmp_project, "dlane2", task="feat/x", scope="the second lane")

    proc = _outcome(run_hippo, tmp_project, "task:feat/x")
    assert proc.returncode != 0
    assert "dlane1" in proc.stderr and "dlane2" in proc.stderr
    assert "the first lane" in proc.stderr


def test_ref_task_with_no_dispatch_at_all_fails(tmp_project, run_hippo):
    proc = _outcome(run_hippo, tmp_project, "task:feat/nothing")
    assert proc.returncode != 0
    assert "no dispatch recorded" in proc.stderr


def test_ref_task_when_everything_is_judged_says_so(tmp_project, run_hippo):
    _dispatch(run_hippo, tmp_project, "donly", task="feat/x")
    _outcome(run_hippo, tmp_project, "donly")
    proc = _outcome(run_hippo, tmp_project, "task:feat/x", result="revised")
    assert proc.returncode != 0
    assert "already has an outcome" in proc.stderr and "donly" in proc.stderr


def test_a_bare_task_id_is_still_rejected(tmp_project, run_hippo):
    """The old defect was a task id stored *as* the ref. The prefix is what makes the intent
    explicit; without it this must keep failing closed."""
    _dispatch(run_hippo, tmp_project, "d7f1a2", task="feat/x")
    proc = _outcome(run_hippo, tmp_project, "feat/x")
    assert proc.returncode != 0
    assert "not a known dispatch id" in proc.stderr
