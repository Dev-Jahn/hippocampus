"""Item (5): scribe pipeline with the mock clerk backend.

DESIGN.md §3.5 (steps 4-7):
    backend resolution honors HIPPO_CLERK_BACKEND=mock (test-only) reading
    HIPPO_MOCK_OUTPUT as the clerk's raw stdout.
    Success -> events appended to ledger with src:"scribe", worklog.md gains
    a line, ev:clerk ok:true self-metering line appended.
    Failure (malformed output, or schema-invalid events) -> raw output dumped
    under failures/, ledger gets *only* an ev:clerk ok:false line — no
    fabricated events ("지어내서 메꾸지 않는다").

The exit code of `hippo scribe` itself on a *clerk validation* failure is
not specified by DESIGN.md (it is designed to run detached from the Stop
hook, where "the clerk died" must not surface as a crash) — so these tests
do not assert a specific returncode for the malformed-output cases, only the
ledger/failures side effects.
"""

from conftest import failures_dir_path, ledger_path, read_ledger, worklog_path


def _mock_env(mock_output_path):
    return {
        "HIPPO_CLERK_BACKEND": "mock",
        "HIPPO_MOCK_OUTPUT": str(mock_output_path),
    }


def test_scribe_valid_mock_output_updates_ledger_and_worklog(
    tmp_project, run_hippo, fake_transcript, valid_mock_output
):
    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-valid"],
        cwd=tmp_project,
        env=_mock_env(valid_mock_output),
    )
    assert proc.returncode == 0, proc.stderr

    ledger = read_ledger(tmp_project)

    dispatch_events = [
        e for e in ledger if e.get("ev") == "dispatch" and e.get("id") == "d100"
    ]
    assert len(dispatch_events) == 1
    assert dispatch_events[0].get("src") == "scribe"

    clerk_events = [e for e in ledger if e.get("ev") == "clerk"]
    assert any(
        e.get("name") == "turn-scribe" and e.get("ok") is True for e in clerk_events
    ), clerk_events

    worklog_text = worklog_path(tmp_project).read_text(encoding="utf-8")
    assert "테스트 더미 작업 완료" in worklog_text


def test_scribe_malformed_json_output_dumped_and_ledger_gets_only_clerk_failure(
    tmp_project, run_hippo, fake_transcript, malformed_mock_output
):
    run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-malformed"],
        cwd=tmp_project,
        env=_mock_env(malformed_mock_output),
    )

    ledger = read_ledger(tmp_project)
    clerk_events = [e for e in ledger if e.get("ev") == "clerk"]
    assert len(clerk_events) == 1
    assert clerk_events[0].get("ok") is False

    non_clerk_events = [e for e in ledger if e.get("ev") != "clerk"]
    assert non_clerk_events == [], "no fabricated/partial events may leak into the ledger"

    failures_dir = failures_dir_path(tmp_project)
    assert failures_dir.is_dir()
    dumped = list(failures_dir.iterdir())
    assert len(dumped) == 1


def test_scribe_schema_invalid_output_dumped_and_ledger_gets_only_clerk_failure(
    tmp_project, run_hippo, fake_transcript, schema_invalid_mock_output
):
    """Valid JSON envelope, but an event that fails `hippo log` validation
    (unknown ev) — must be treated identically to a malformed clerk reply."""
    run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-schema-bad"],
        cwd=tmp_project,
        env=_mock_env(schema_invalid_mock_output),
    )

    ledger = read_ledger(tmp_project)
    clerk_events = [e for e in ledger if e.get("ev") == "clerk"]
    assert len(clerk_events) == 1
    assert clerk_events[0].get("ok") is False

    non_clerk_events = [e for e in ledger if e.get("ev") != "clerk"]
    assert non_clerk_events == []

    failures_dir = failures_dir_path(tmp_project)
    assert failures_dir.is_dir()
    assert len(list(failures_dir.iterdir())) == 1
