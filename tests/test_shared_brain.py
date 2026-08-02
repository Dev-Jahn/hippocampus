"""The shared-brain data plane (DESIGN §9.2–§9.4, built in 1.8.0).

Two keys, no enforcement: HIPPO_DISPATCH makes a lane's writes src=executor and supplies the
default outcome ref; audience decides which directives reach which reader. The invariant under
test everywhere: a self-report is a claim, never a verdict."""

import json

from conftest import read_ledger


def _launch(run_hippo, cwd, did="dlane1"):
    proc = run_hippo(["log", "dispatch", "--id", did, "--kind", "impl",
                      "--exec", "codex/gpt-5.6-sol/high", "--scope", "pass2 tensorize"],
                     cwd=cwd)
    assert proc.returncode == 0, proc.stderr
    return did


# --------------------------------------------------------------------------
# src=executor via HIPPO_DISPATCH
# --------------------------------------------------------------------------

def test_lane_outcome_defaults_ref_and_stamps_executor(tmp_project, run_hippo):
    _launch(run_hippo, tmp_project)
    proc = run_hippo(["log", "outcome", "--result", "accepted", "--note", "it works"],
                     cwd=tmp_project, env={"HIPPO_DISPATCH": "dlane1"})
    assert proc.returncode == 0, proc.stderr
    row = json.loads(proc.stdout)
    assert (row["src"], row["ref"]) == ("executor", "dlane1")


def test_outcome_without_ref_outside_a_lane_dies_with_usage(tmp_project, run_hippo):
    proc = run_hippo(["log", "outcome", "--result", "accepted"], cwd=tmp_project)
    assert proc.returncode != 0
    assert "HIPPO_DISPATCH" in proc.stderr


def test_a_lane_may_propose_a_directive_but_not_rule(tmp_project, run_hippo):
    """The write lands (src=executor, visible to checkup and grep) — and never folds: a
    directive is a standing rule for every reader, and a lane changing one for the whole
    network is the belief propagation §9.3 forbids."""
    env = {"HIPPO_DISPATCH": "dlane1"}
    _launch(run_hippo, tmp_project)
    run_hippo(["directive", "add", "--id", "lane-note", "--text", "premise broken",
               "--lifetime", "turn"], cwd=tmp_project, env=env)
    rows = [e for e in read_ledger(tmp_project) if e.get("id") == "lane-note"]
    assert rows and rows[0]["src"] == "executor"
    listed = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    assert json.loads(listed.stdout) == []
    assert "premise broken" not in run_hippo(["status", "--inject"], cwd=tmp_project).stdout


def test_a_lane_cannot_withdraw_mains_directive(tmp_project, run_hippo):
    run_hippo(["directive", "add", "--id", "gpu-pinning", "--text", "GPUs 0 and 1 only",
               "--lifetime", "phase"], cwd=tmp_project)
    run_hippo(["directive", "withdraw", "gpu-pinning"], cwd=tmp_project,
              env={"HIPPO_DISPATCH": "dlane1"})
    listed = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    assert [d["id"] for d in json.loads(listed.stdout)] == ["gpu-pinning"]
    # The attempt itself is on the record.
    assert any(e.get("id") == "gpu-pinning" and e.get("src") == "executor"
               for e in read_ledger(tmp_project))


def test_a_lane_revising_its_claim_shows_the_latest(tmp_project, run_hippo):
    _launch(run_hippo, tmp_project)
    _claim(run_hippo, tmp_project, result="accepted")
    _claim(run_hippo, tmp_project, result="no-go")
    out = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert "claims no-go" in out.stdout
    assert "claims accepted" not in out.stdout


# --------------------------------------------------------------------------
# a claim is not a verdict
# --------------------------------------------------------------------------

def _claim(run_hippo, cwd, did="dlane1", result="accepted"):
    proc = run_hippo(["log", "outcome", "--result", result],
                     cwd=cwd, env={"HIPPO_DISPATCH": did})
    assert proc.returncode == 0, proc.stderr


def test_claimed_dispatch_stays_in_flight_with_the_claim_visible(tmp_project, run_hippo):
    _launch(run_hippo, tmp_project)
    _claim(run_hippo, tmp_project)
    out = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert "in flight" in out.stdout
    assert "claims accepted" in out.stdout

    # Main's verdict is what lands it.
    run_hippo(["log", "outcome", "--ref", "dlane1", "--result", "accepted"], cwd=tmp_project)
    out = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert "in flight" not in out.stdout


def test_mains_verdict_after_a_claim_draws_no_second_verdict_note(tmp_project, run_hippo):
    _launch(run_hippo, tmp_project)
    _claim(run_hippo, tmp_project)
    proc = run_hippo(["log", "outcome", "--ref", "dlane1", "--result", "refuted"],
                     cwd=tmp_project)
    assert proc.returncode == 0
    assert "already had a verdict" not in proc.stderr


def test_task_ref_still_resolves_to_a_claimed_dispatch(tmp_project, run_hippo):
    run_hippo(["log", "dispatch", "--id", "dt1", "--kind", "impl",
               "--exec", "codex/gpt-5.6-sol/high", "--scope", "x", "--task", "feat/x"],
              cwd=tmp_project)
    _claim(run_hippo, tmp_project, "dt1")
    proc = run_hippo(["log", "outcome", "--ref", "task:feat/x", "--result", "accepted"],
                     cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ref"] == "dt1"


def test_priors_fold_only_judgments(repo_root):
    import sys
    sys.path.insert(0, str(repo_root / "cli"))
    import hippo_cli
    from datetime import datetime, timezone
    now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    disp = {"t": "2026-08-01T10:00:00Z", "ev": "dispatch", "id": "d1", "kind": "impl",
            "exec": "codex/sol/high", "scope": "x"}  # launched 26h ago
    claim = {"t": "2026-08-01T11:00:00Z", "ev": "outcome", "ref": "d1",
             "result": "accepted", "src": "executor"}
    page = hippo_cli.prior_facts([disp, claim], now)
    # The claim joins no cell and pays no attribution; the dispatch stays an open item.
    assert "impl×codex/sol/high" not in page
    assert "- d1 (" in page

    verdict = {"t": "2026-08-01T11:30:00Z", "ev": "outcome", "ref": "d1",
               "result": "refuted", "src": "cli"}
    page = hippo_cli.prior_facts([disp, claim, verdict], now)
    assert "impl×codex/sol/high (n=1)" in page
    assert "- d1 (" not in page


def test_scribe_may_record_the_verdict_after_a_claim(tmp_project, run_hippo, fake_transcript,
                                                     tmp_path):
    _launch(run_hippo, tmp_project)
    _claim(run_hippo, tmp_project)
    mock = tmp_path / "verdict.json"
    mock.write_text(json.dumps({"worklog": "verdict landed", "events": [
        {"ev": "outcome", "ref": "dlane1", "result": "accepted"}]}), encoding="utf-8")
    proc = run_hippo(["scribe", "--transcript", str(fake_transcript), "--session", "s1"],
                     cwd=tmp_project,
                     env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock)})
    assert proc.returncode == 0, proc.stderr
    srcs = [e["src"] for e in read_ledger(tmp_project)
            if e.get("ev") == "outcome" and e.get("ref") == "dlane1"]
    assert srcs == ["executor", "scribe"]


# --------------------------------------------------------------------------
# audience — who a directive binds (§9.4)
# --------------------------------------------------------------------------

def test_audience_filters_the_capsule_by_reader(tmp_project, run_hippo):
    run_hippo(["directive", "add", "--id", "korean-replies", "--text", "answer in Korean",
               "--lifetime", "durable", "--audience", "main"], cwd=tmp_project)
    run_hippo(["directive", "add", "--id", "english-files", "--text", "files in English",
               "--lifetime", "durable", "--audience", "executor"], cwd=tmp_project)
    run_hippo(["directive", "add", "--id", "gpu-pinning", "--text", "GPUs 0 and 1 only",
               "--lifetime", "phase"], cwd=tmp_project)

    main_view = run_hippo(["status", "--inject"], cwd=tmp_project).stdout
    assert "answer in Korean" in main_view
    assert "files in English" not in main_view
    assert "GPUs 0 and 1 only" in main_view  # absent audience = all

    lane_view = run_hippo(["status", "--inject"], cwd=tmp_project,
                          env={"HIPPO_DISPATCH": "dlane1"}).stdout
    assert "answer in Korean" not in lane_view
    assert "files in English" in lane_view
    assert "GPUs 0 and 1 only" in lane_view
    assert "directives 2 live" in lane_view


def test_audience_survives_the_ledger_and_shows_in_list(tmp_project, run_hippo):
    run_hippo(["directive", "add", "--id", "english-files", "--text", "files in English",
               "--lifetime", "durable", "--audience", "executor"], cwd=tmp_project)
    listed = run_hippo(["directive", "list", "--json"], cwd=tmp_project)
    (d,) = json.loads(listed.stdout)
    assert d["audience"] == "executor"
    plain = run_hippo(["directive", "list"], cwd=tmp_project)
    assert "[active/durable/executor]" in plain.stdout

    bad = run_hippo(["log", "raw", json.dumps(
        {"ev": "directive", "id": "x-1", "text": "x", "lifetime": "phase",
         "state": "active", "audience": "everyone"})], cwd=tmp_project)
    assert bad.returncode != 0
    assert "audience" in bad.stderr


# --------------------------------------------------------------------------
# worktree resolution (§9.1) — the plumbing the data plane stands on
# --------------------------------------------------------------------------

def test_a_worktree_git_file_is_walked_through(tmp_project, run_hippo):
    lane = tmp_project / ".claude" / "worktrees" / "pass2"
    lane.mkdir(parents=True)
    (lane / ".git").write_text("gitdir: ../../../.git/worktrees/pass2\n", encoding="utf-8")
    proc = run_hippo(["log", "dispatch", "--id", "dwt1", "--kind", "impl",
                      "--exec", "codex/sol/high", "--scope", "from the lane"], cwd=lane)
    assert proc.returncode == 0, proc.stderr
    assert any(e.get("id") == "dwt1" for e in read_ledger(tmp_project))


def test_a_git_directory_is_still_a_hard_boundary(tmp_project, run_hippo):
    nested = tmp_project / "vendor" / "otherrepo"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()
    proc = run_hippo(["log", "dispatch", "--id", "dnope", "--kind", "impl",
                      "--exec", "codex/sol/high", "--scope", "x"], cwd=nested)
    assert proc.returncode == 0
    assert "nothing was recorded" in proc.stderr
    assert not any(e.get("id") == "dnope" for e in read_ledger(tmp_project))
