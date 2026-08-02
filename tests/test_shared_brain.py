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


# --------------------------------------------------------------------------
# deterministic usage injection (1.8.1) — capsule report line + COMMON seed
# --------------------------------------------------------------------------

def test_lane_capsule_carries_the_report_line(tmp_project, run_hippo):
    lane = run_hippo(["status", "--inject"], cwd=tmp_project,
                     env={"HIPPO_DISPATCH": "dlane1"})
    assert "· report: hippo log outcome" in lane.stdout
    assert "no --ref needed" in lane.stdout
    main_view = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert "report:" not in main_view.stdout


def test_init_seeds_the_common_bootstrap(tmp_path, run_hippo):
    proc = run_hippo(["init"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    seed = (tmp_path / ".hippo" / "briefs" / "COMMON.md").read_text(encoding="utf-8")
    assert "hippo status --inject" in seed
    assert "compaction" in seed


# --------------------------------------------------------------------------
# depth-indexed capsule tail (§9.5, 1.9.0)
# --------------------------------------------------------------------------

def test_leaf_lane_capsule_forbids_re_delegation(tmp_project, run_hippo):
    out = run_hippo(["status", "--inject"], cwd=tmp_project,
                    env={"HIPPO_DISPATCH": "dlane1"})
    assert "· depth 0: do not re-delegate" in out.stdout
    assert "· discipline:" in out.stdout


def test_orchestrator_lane_capsule_grants_children(tmp_project, run_hippo):
    out = run_hippo(["status", "--inject"], cwd=tmp_project,
                    env={"HIPPO_DISPATCH": "dlane1", "HIPPO_DEPTH": "1"})
    assert "· depth 1: you may dispatch children" in out.stdout
    assert "do not re-delegate" not in out.stdout.split("· depth 1")[0]
    assert "each child starts at depth 0" in out.stdout


def test_main_capsule_has_no_lane_tail(tmp_project, run_hippo):
    out = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert "depth" not in out.stdout
    assert "discipline" not in out.stdout


def test_log_dispatch_takes_depth_and_parent(tmp_project, run_hippo):
    run_hippo(["log", "dispatch", "--id", "dp1", "--kind", "impl",
               "--exec", "codex/sol/high", "--scope", "parent lane", "--depth", "1"],
              cwd=tmp_project)
    proc = run_hippo(["log", "dispatch", "--id", "dc1", "--kind", "impl",
                      "--exec", "codex/sol/high", "--scope", "child lane",
                      "--parent", "dp1"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr
    rows = {e["id"]: e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"}
    assert rows["dp1"]["depth"] == 1
    assert rows["dc1"]["parent"] == "dp1"

    bad = run_hippo(["log", "raw", json.dumps(
        {"ev": "dispatch", "id": "dx", "kind": "impl", "exec": "codex/sol/high",
         "scope": "x", "depth": "one"})], cwd=tmp_project)
    assert bad.returncode != 0
    assert "depth must be an integer" in bad.stderr


# --------------------------------------------------------------------------
# cost axis (§9.6, 1.10.0)
# --------------------------------------------------------------------------

def test_usage_validation_is_fail_closed(tmp_project, run_hippo):
    run_hippo(["log", "dispatch", "--id", "du1", "--kind", "impl",
               "--exec", "codex/gpt-5.6-luna/low", "--scope", "x"], cwd=tmp_project)
    ok = run_hippo(["log", "raw", json.dumps(
        {"ev": "usage", "ref": "du1", "tokens": 500, "model": "gpt-5.6-luna"})],
        cwd=tmp_project)
    assert ok.returncode == 0, ok.stderr

    dangling = run_hippo(["log", "raw", json.dumps(
        {"ev": "usage", "ref": "dnope", "tokens": 500})], cwd=tmp_project)
    assert dangling.returncode != 0
    assert "not a known dispatch id" in dangling.stderr

    junk = run_hippo(["log", "raw", json.dumps(
        {"ev": "usage", "ref": "du1", "tokens": "many"})], cwd=tmp_project)
    assert junk.returncode != 0


def test_the_scribe_may_not_record_usage(tmp_project, run_hippo, fake_transcript, tmp_path):
    run_hippo(["log", "dispatch", "--id", "du1", "--kind", "impl",
               "--exec", "codex/gpt-5.6-luna/low", "--scope", "x"], cwd=tmp_project)
    mock = tmp_path / "usage.json"
    mock.write_text(json.dumps({"worklog": "w", "events": [
        {"ev": "usage", "ref": "du1", "tokens": 500}]}), encoding="utf-8")
    proc = run_hippo(["scribe", "--transcript", str(fake_transcript), "--session", "s1"],
                     cwd=tmp_project,
                     env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock)})
    assert proc.returncode == 0, proc.stderr
    assert not [e for e in read_ledger(tmp_project) if e.get("ev") == "usage"]
    dumps = (tmp_project / ".hippo" / "failures").glob("*")
    assert "the wrapper records usage" in "".join(p.read_text() for p in dumps)


def test_priors_price_the_cells(repo_root):
    import sys
    sys.path.insert(0, str(repo_root / "cli"))
    import hippo_cli
    from datetime import datetime, timezone
    now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    prices = {"as_of": "2026-08-02",
              "models": {"gpt-5.6-luna": {"input": 0.20, "cached": 0.02, "output": 1.20}}}
    t = "2026-08-02T11:00:00Z"

    def d(i):
        return {"t": t, "ev": "dispatch", "id": f"d{i}", "kind": "impl",
                "exec": "codex/gpt-5.6-luna/low", "scope": "x"}

    def o(i):
        return {"t": t, "ev": "outcome", "ref": f"d{i}", "result": "accepted", "src": "cli"}

    rows = [d(i) for i in range(4)] + [o(i) for i in range(4)] + [
        {"t": t, "ev": "usage", "ref": "d0", "tokens": 1100000, "tin": 1000000,
         "tcached": 0, "tout": 100000, "model": "gpt-5.6-luna", "src": "wrapper"},
        {"t": t, "ev": "usage", "ref": "d1", "tokens": 7, "model": "mystery-model",
         "src": "wrapper"},
    ]
    page = hippo_cli.prior_facts(rows, now, prices=prices)
    assert "prices as of 2026-08-02" in page
    # d0: (1M − 0)·$0.20/1M + 0.1M·$1.20/1M = $0.32; d1 unpriced (no breakdown) → star.
    assert "| 1,100,007 | $0.32* |" in page
    assert "$0.08" in page          # $0.32 over 4 accepted
    assert "mystery-model×1" in page
