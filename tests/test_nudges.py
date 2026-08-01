"""Write-time information surfaces: stderr notes that inform without refusing (principle 3).

Each of these exists because an audit found a working feature whose one consumer was never
told about it — the note is placed at the exact moment the information is actionable."""

import json

from conftest import read_ledger


def test_second_verdict_from_main_is_recorded_with_a_note(tmp_project, run_hippo):
    run_hippo(["log", "dispatch", "--id", "d1", "--kind", "impl",
               "--exec", "codex/gpt-5.6-sol/high", "--scope", "x"], cwd=tmp_project)
    first = run_hippo(["log", "outcome", "--ref", "d1", "--result", "revised"], cwd=tmp_project)
    assert first.returncode == 0, first.stderr
    assert "already had a verdict" not in first.stderr

    second = run_hippo(["log", "outcome", "--ref", "d1", "--result", "accepted"],
                       cwd=tmp_project)
    assert second.returncode == 0, second.stderr
    assert "already had a verdict" in second.stderr
    assert "first-pass" in second.stderr
    outs = [e for e in read_ledger(tmp_project) if e.get("ev") == "outcome"]
    assert [e["result"] for e in outs] == ["revised", "accepted"]


def test_review_write_names_the_closing_review_status(tmp_project, run_hippo):
    proc = run_hippo(["log", "review", "--id", "r1", "--base", "abc1234",
                      "--source", "chatgpt-web", "--findings", "3"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr
    assert "review-status" in proc.stderr
    assert "--ref r1" in proc.stderr


def test_addressed_is_fail_closed(tmp_project, run_hippo):
    run_hippo(["log", "review", "--id", "r1", "--base", "abc1234",
               "--source", "chatgpt-web", "--findings", "3"], cwd=tmp_project)
    bad = run_hippo(["log", "raw",
                     json.dumps({"ev": "review-status", "ref": "r1", "addressed": "fully"})],
                    cwd=tmp_project)
    assert bad.returncode != 0
    assert "addressed" in bad.stderr

    good = run_hippo(["log", "review-status", "--ref", "r1", "--addressed", "partial"],
                     cwd=tmp_project)
    assert good.returncode == 0, good.stderr


def test_init_names_the_commit_or_ignore_choice(tmp_path, run_hippo):
    proc = run_hippo(["init"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "gitignore" in proc.stdout
