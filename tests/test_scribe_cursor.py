"""Item (6): scribe cursor advancement — a second consecutive run with no new
transcript lines must exit without invoking the model at all.

DESIGN.md §3.5 step 3 (the deterministic prefilter) plus §7's own suggested verification
method: deleting the mock file proves it — if the second run tried to call the
(mock) backend again it would have nothing to read and would either error or
produce a new ev:clerk line; observing neither proves the model call was
skipped by the deterministic prefilter/cursor check before backend
resolution.
"""

from conftest import failures_dir_path, read_ledger


def test_second_run_with_no_new_lines_skips_model_call(
    tmp_project, run_hippo, fake_transcript, valid_mock_output
):
    env = {
        "HIPPO_CLERK_BACKEND": "mock",
        "HIPPO_MOCK_OUTPUT": str(valid_mock_output),
    }

    first = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-cursor"],
        cwd=tmp_project,
        env=env,
    )
    assert first.returncode == 0, first.stderr

    ledger_after_first = read_ledger(tmp_project)
    clerk_count_after_first = len(
        [e for e in ledger_after_first if e.get("ev") == "clerk"]
    )
    assert clerk_count_after_first >= 1

    failures_dir = failures_dir_path(tmp_project)
    failures_before = list(failures_dir.iterdir()) if failures_dir.is_dir() else []

    # Remove the mock output: if scribe attempted a second model call, it
    # would have nothing to read and the failure/self-metering side effects
    # below would change.
    valid_mock_output.unlink()

    second = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-cursor"],
        cwd=tmp_project,
        env=env,
    )
    assert second.returncode == 0, second.stderr

    ledger_after_second = read_ledger(tmp_project)
    clerk_count_after_second = len(
        [e for e in ledger_after_second if e.get("ev") == "clerk"]
    )
    assert clerk_count_after_second == clerk_count_after_first, (
        "no new lines since the last cursor -> no model invocation, "
        "hence no new ev:clerk self-metering line"
    )

    failures_after = list(failures_dir.iterdir()) if failures_dir.is_dir() else []
    assert len(failures_after) == len(failures_before)
