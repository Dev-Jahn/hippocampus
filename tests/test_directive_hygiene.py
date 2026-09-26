"""Directive hygiene: one lifetime (until withdrawn), ids that can actually supersede, and the
volume and age nudges that replaced the old hard caps (DESIGN §3.2, §6)."""

import json
import sys

import pytest

from conftest import REPO_ROOT, read_ledger


def _mock_env(mock_output_path):
    return {
        "HIPPO_CLERK_BACKEND": "mock",
        "HIPPO_MOCK_OUTPUT": str(mock_output_path),
    }


def _add(run_hippo, cwd, text, did=None):
    argv = ["directive", "add", "--text", text]
    if did:
        argv += ["--id", did]
    return run_hippo(argv, cwd=cwd)


def _active_ids(run_hippo, cwd):
    out = run_hippo(["directive", "list", "--active", "--json"], cwd=cwd)
    assert out.returncode == 0, out.stderr
    return {d["id"] for d in json.loads(out.stdout)}


# --------------------------------------------------------------------------
# one lifetime — a directive lives until it is withdrawn (§3.2)
# --------------------------------------------------------------------------

def test_lifetime_flag_is_accepted_ignored_and_said_so(tmp_project, run_hippo):
    """Old callers keep working; nothing stored carries the retired field, and the caller is
    told once what replaced it."""
    proc = run_hippo(["directive", "add", "--id", "gpu-01", "--text", "use GPUs 0 and 1 only",
                      "--lifetime", "phase"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr
    assert ("note: lifetime is no longer recorded — a directive lives until "
            "'hippo directive withdraw <id>'") in proc.stderr
    row = [e for e in read_ledger(tmp_project) if e.get("ev") == "directive"][-1]
    assert "lifetime" not in row
    assert _active_ids(run_hippo, tmp_project) == {"gpu-01"}


def _old_row(project_dir, **row):
    with (project_dir / ".hippo" / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": "2026-08-01T00:00:00Z", "ev": "directive", "src": "cli", **row},
                           ensure_ascii=False) + "\n")


def test_an_old_active_turn_row_is_not_live(tmp_project, run_hippo):
    """Nothing sweeps `turn` any more, so the view reads it as the old rule would have left it.
    Other old lifetimes stay live, and a re-add of a turn id without one is live again."""
    _old_row(tmp_project, id="turn-01", text="skip the code", lifetime="turn", state="active")
    _old_row(tmp_project, id="phase-01", text="use GPU 0", lifetime="phase", state="active")
    _old_row(tmp_project, id="turn-02", text="be brief", lifetime="turn", state="active")
    assert _active_ids(run_hippo, tmp_project) == {"phase-01"}
    inject = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert "skip the code" not in inject.stdout

    _add(run_hippo, tmp_project, "be brief from now on", "turn-02")
    assert _active_ids(run_hippo, tmp_project) == {"phase-01", "turn-02"}


def test_a_scribe_directive_that_still_says_turn_stays_live(
    tmp_project, run_hippo, fake_transcript, tmp_path
):
    """The clerk is no longer asked for a lifetime; if it gives one anyway, the field is dropped
    rather than letting a `turn` make its directive invisible to the view."""
    mock = tmp_path / "turn_directive.json"
    mock.write_text(json.dumps({"worklog": "recorded an instruction", "events": [
        {"ev": "directive", "id": "no-code", "text": "answer without code",
         "lifetime": "turn", "state": "active"}]}), encoding="utf-8")
    proc = run_hippo(["scribe", "--transcript", str(fake_transcript), "--session", "s1"],
                     cwd=tmp_project, env=_mock_env(mock))
    assert proc.returncode == 0, proc.stderr
    assert "no-code" in _active_ids(run_hippo, tmp_project)
    row = [e for e in read_ledger(tmp_project) if e.get("ev") == "directive"][-1]
    assert "lifetime" not in row


# --------------------------------------------------------------------------
# ids that can supersede
# --------------------------------------------------------------------------

def test_autoid_refuses_text_with_no_ascii_to_slug(tmp_project, run_hippo):
    """Korean text used to slug to "" and fall back to `directive-<hash>` — an id nobody can
    recall, so nobody can ever update the directive. Refuse and say what to pass instead."""
    proc = _add(run_hippo, tmp_project, "한글로만 쓴 지시")
    assert proc.returncode != 0
    assert "--id" in proc.stderr

    ok = _add(run_hippo, tmp_project, "한글로만 쓴 지시", "korean-rule")
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["id"] == "korean-rule"


@pytest.mark.parametrize("bad", ["한글-id", "Upper-Case", "has space", "trailing-", "under_score"])
def test_non_kebab_directive_id_is_rejected(tmp_project, run_hippo, bad):
    """The scribe picks its own ids, so the validator is the only thing standing between a
    creative clerk and an unreusable handle."""
    proc = run_hippo(
        [
            "log",
            "raw",
            json.dumps(
                {"ev": "directive", "id": bad, "text": "t", "state": "active"},
                ensure_ascii=False,
            ),
        ],
        cwd=tmp_project,
    )
    assert proc.returncode != 0, proc.stdout
    assert "kebab" in proc.stderr


def test_same_id_supersedes_instead_of_forking(tmp_project, run_hippo):
    """Re-adding under the same --id is the update path; the derived id is a fingerprint of the
    text and would produce a second, unrelated directive."""
    _add(run_hippo, tmp_project, "use GPUs 0 and 1 only", "gpu-01")
    _add(run_hippo, tmp_project, "use GPU 0 only", "gpu-01")
    out = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    live = json.loads(out.stdout)
    assert len(live) == 1
    assert live[0]["text"] == "use GPU 0 only"


def test_roster_hands_the_scribe_the_live_ids(tmp_project, run_hippo):
    """Without the roster the clerk coins a fresh id for a subject that already has one, and the
    update silently forks. Reading the ledger from inside the clerk is not worth the latency."""
    sys.path.insert(0, str(REPO_ROOT / "cli"))
    import hippo_cli

    _add(run_hippo, tmp_project, "use GPUs 0 and 1 only", "gpu-01")
    _add(run_hippo, tmp_project, "never save review replies", "dur-01")
    run_hippo(["directive", "withdraw", "dur-01"], cwd=tmp_project)

    roster = hippo_cli.directive_roster(tmp_project / ".hippo")
    assert "- gpu-01: use GPUs 0 and 1 only" in roster
    assert "dur-01" not in roster  # withdrawn ones are not offered for reuse


# --------------------------------------------------------------------------
# volume: warned about, never enforced (principle 3)
# --------------------------------------------------------------------------

def test_long_directive_is_written_and_warned_about(tmp_project, run_hippo):
    proc = _add(run_hippo, tmp_project, "x" * 250, "long-01")
    assert proc.returncode == 0, proc.stderr
    assert "long-01 (250)" in proc.stderr
    assert "long-01" in _active_ids(run_hippo, tmp_project)


def test_an_already_registered_long_directive_is_named_when_listing(tmp_project, run_hippo):
    """The expensive directives are the ones already resident. Warning only at write time leaves
    them permanently unmentioned — every session pays and nobody is ever told."""
    _add(run_hippo, tmp_project, "x" * 250, "long-01")

    listing = run_hippo(["directive", "list"], cwd=tmp_project)
    assert listing.returncode == 0, listing.stderr
    assert "long-01 (250)" in listing.stderr
    # the listing itself stays a clean record — the note goes to stderr
    assert "note:" not in listing.stdout
    assert len(listing.stdout.splitlines()) == 1

    # and adding an unrelated short directive still surfaces the resident one
    later = _add(run_hippo, tmp_project, "short and fine", "short-01")
    assert "long-01 (250)" in later.stderr


def test_listing_a_tidy_directive_set_says_nothing(tmp_project, run_hippo):
    _add(run_hippo, tmp_project, "short and fine", "short-01")
    listing = run_hippo(["directive", "list"], cwd=tmp_project)
    assert listing.stderr.strip() == ""


def test_short_directive_warns_about_nothing(tmp_project, run_hippo):
    proc = _add(run_hippo, tmp_project, "keep it short", "short-01")
    assert proc.returncode == 0
    assert proc.stderr.strip() == ""


def test_crowded_directive_set_warns_but_still_records(tmp_project, run_hippo):
    """Eight live directives is a hygiene signal, not a limit — the eighth is still injected."""
    for i in range(7):
        proc = _add(run_hippo, tmp_project, f"directive number {i}", f"d-{i}")
        assert "live directives" not in proc.stderr, i
    eighth = _add(run_hippo, tmp_project, "directive number 7", "d-7")
    assert eighth.returncode == 0, eighth.stderr
    assert "8 live directives" in eighth.stderr
    assert "withdraw" in eighth.stderr

    inject = run_hippo(["status", "--inject"], cwd=tmp_project)
    live = [ln for ln in inject.stdout.splitlines() if ln.startswith("· live")]
    assert len(live) == 8


def test_main_s_capsule_names_the_volume_past_the_total_mark(tmp_project, run_hippo):
    """The stderr notes were discarded (`>/dev/null 2>&1` on every add, measured) while two sets
    grew past 7.8k chars. Once one reader carries the total mark main's capsule says so; a count
    alone is not the cost, and neither a lane's capsule nor a subagent's slice carries it."""
    def volume(**env):
        out = run_hippo(["status", "--inject"], cwd=tmp_project, env=env).stdout.splitlines()
        return [ln for ln in out if ln.startswith("· directives:")]

    def line(session, worker):
        return [f"· directives: {session} chars ride into every session, {worker} into every "
                "subagent — /hippo:checkup's directive pass tidies them"]

    def scoped(did, text, audience):
        run_hippo(["directive", "add", "--id", did, "--text", text, "--audience", audience],
                  cwd=tmp_project)

    for i in range(9):
        _add(run_hippo, tmp_project, f"directive number {i}", f"d-{i}")
    assert volume() == []  # nine live, 162 chars
    _add(run_hippo, tmp_project, "x" * 1500, "long-01")
    assert volume() == line(1662, 1662)
    assert volume(HIPPO_DISPATCH="d1") == [] and volume(HIPPO_INJECT="subagent") == []
    # Per reader, since that is what an audience moves: re-scoped, one figure drops.
    scoped("long-01", "x" * 1500, "main")
    assert volume() == line(1662, 162)
    scoped("long-01", "x" * 1500, "executor")
    assert volume() == line(162, 1662)
    # 1762 in all, but 962 to each reader: under the mark.
    scoped("long-01", "x" * 800, "executor")
    scoped("long-02", "y" * 800, "main")
    assert volume() == []


# --------------------------------------------------------------------------
# staleness — shown, never resolved (DESIGN §6)
# --------------------------------------------------------------------------

def _backdate_directive(project_dir, did, text, days_ago):
    """Append a directive event with an old writer timestamp. The CLI stamps t itself, so age
    can only be fabricated the way it really arises: as an old line already in the ledger."""
    from datetime import datetime, timedelta, timezone
    t = (datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    row = {"t": t, "ev": "directive", "id": did, "text": text, "state": "active", "src": "cli"}
    with (project_dir / ".hippo" / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_age_appears_in_the_capsule_from_fourteen_days(tmp_project, run_hippo):
    _backdate_directive(tmp_project, "old-hold", "hold the perf claims", 23)
    _backdate_directive(tmp_project, "young-hold", "answer tersely", 13)
    _add(run_hippo, tmp_project, "use GPUs 0 and 1 only", "fresh-01")

    out = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert out.returncode == 0, out.stderr
    live = [ln for ln in out.stdout.splitlines() if ln.startswith("· live")]
    # Ledger order, and the age only once it is worth a glance.
    assert live == ["· live(23d): hold the perf claims", "· live: answer tersely",
                    "· live: use GPUs 0 and 1 only"]


def test_stale_note_names_every_directive_30d_or_older(tmp_project, run_hippo):
    _backdate_directive(tmp_project, "stale-hold", "hold off on speed claims", 41)
    _backdate_directive(tmp_project, "older-hold", "never touch the waystone", 52)
    _backdate_directive(tmp_project, "aging-hold", "use GPU 0", 29)

    listed = run_hippo(["directive", "list"], cwd=tmp_project)
    assert listed.returncode == 0
    assert ("note: directives 30d or older — older-hold (52d), stale-hold (41d). Still true? "
            "Withdraw the ones that are not (`hippo directive withdraw <id>`).") in listed.stderr
    assert "aging-hold" not in listed.stderr
    # The listing itself stays a clean record — the note rides stderr only.
    assert "41d" not in listed.stdout

    added = _add(run_hippo, tmp_project, "another rule", "new-01")
    assert "stale-hold (41d)" in added.stderr


def test_fresh_directive_draws_no_staleness_note(tmp_project, run_hippo):
    _add(run_hippo, tmp_project, "use GPUs 0 and 1 only", "gpu-01")
    listed = run_hippo(["directive", "list"], cwd=tmp_project)
    assert listed.returncode == 0
    assert "30d or older" not in listed.stderr
