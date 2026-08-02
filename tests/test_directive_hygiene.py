"""Directive hygiene: the `turn` lifetime expiring on its own, ids that can actually supersede,
and the volume nudges that replaced the old hard caps (DESIGN §3.2, §3.5, §6)."""

import json
import sys

import pytest

from conftest import REPO_ROOT, read_ledger


def _mock_env(mock_output_path):
    return {
        "HIPPO_CLERK_BACKEND": "mock",
        "HIPPO_MOCK_OUTPUT": str(mock_output_path),
    }


def _add(run_hippo, cwd, text, lifetime, did=None):
    argv = ["directive", "add", "--text", text, "--lifetime", lifetime]
    if did:
        argv += ["--id", did]
    return run_hippo(argv, cwd=cwd)


def _active_ids(run_hippo, cwd):
    out = run_hippo(["directive", "list", "--active", "--json"], cwd=cwd)
    assert out.returncode == 0, out.stderr
    return {d["id"] for d in json.loads(out.stdout)}


# --------------------------------------------------------------------------
# turn lifetime — one turn of life, then expired by the scribe
# --------------------------------------------------------------------------

def test_turn_directive_expires_at_the_next_stop(
    tmp_project, run_hippo, fake_transcript, valid_mock_output
):
    """A turn directive main records mid-turn is live for the rest of that turn and expired by
    the Stop that ends it. phase and durable are untouched — only the clock-bound one expires."""
    _add(run_hippo, tmp_project, "skip the code in the next answer", "turn", "turn-01")
    _add(run_hippo, tmp_project, "use GPUs 0 and 1 only", "phase", "gpu-01")
    _add(run_hippo, tmp_project, "never save review replies", "durable", "dur-01")
    assert _active_ids(run_hippo, tmp_project) == {"turn-01", "gpu-01", "dur-01"}

    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "s1"],
        cwd=tmp_project,
        env=_mock_env(valid_mock_output),
    )
    assert proc.returncode == 0, proc.stderr

    assert _active_ids(run_hippo, tmp_project) == {"gpu-01", "dur-01"}
    expired = [
        e
        for e in read_ledger(tmp_project)
        if e.get("ev") == "directive" and e.get("state") == "expired"
    ]
    # expired, not withdrawn: nobody changed their mind, the clock ran out.
    assert [e["id"] for e in expired] == ["turn-01"]


def test_turn_directive_written_by_the_scribe_survives_its_own_stop(
    tmp_project, run_hippo, fake_transcript, tmp_path
):
    """The whole point of expiring *before* the clerk writes: a turn directive the scribe records
    at this Stop has to survive to be injected into the next turn, and die at the Stop after."""
    mock = tmp_path / "turn_directive.json"
    mock.write_text(
        json.dumps(
            {
                "worklog": "recorded a turn-scoped instruction",
                "events": [
                    {
                        "ev": "directive",
                        "id": "one-turn-only",
                        "text": "answer the next question without code",
                        "lifetime": "turn",
                        "state": "active",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    first = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "s1"],
        cwd=tmp_project,
        env=_mock_env(mock),
    )
    assert first.returncode == 0, first.stderr
    assert "one-turn-only" in _active_ids(run_hippo, tmp_project)

    # A second Stop = the next turn ended. Now it goes.
    second = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "s2"],
        cwd=tmp_project,
        env=_mock_env(mock),
    )
    assert second.returncode == 0, second.stderr
    live = _active_ids(run_hippo, tmp_project)
    # The same mock records it again on the second run; what matters is that the first one was
    # expired rather than left standing forever.
    expired = [
        e
        for e in read_ledger(tmp_project)
        if e.get("ev") == "directive" and e.get("state") == "expired"
    ]
    assert [e["id"] for e in expired] == ["one-turn-only"]
    assert live == {"one-turn-only"}


def test_turn_directives_expire_even_when_the_prefilter_skips_the_model(
    tmp_project, run_hippo, tmp_path, valid_mock_output
):
    """A turn ended whether or not it was worth a model call, so expiry sits ahead of the
    deterministic prefilter."""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    _add(run_hippo, tmp_project, "skip the code in the next answer", "turn", "turn-01")

    proc = run_hippo(
        ["scribe", "--transcript", str(empty), "--session", "s1"],
        cwd=tmp_project,
        env=_mock_env(valid_mock_output),
    )
    assert proc.returncode == 0, proc.stderr
    assert "turn-01" not in _active_ids(run_hippo, tmp_project)


# --------------------------------------------------------------------------
# ids that can supersede
# --------------------------------------------------------------------------

def test_autoid_refuses_text_with_no_ascii_to_slug(tmp_project, run_hippo):
    """Korean text used to slug to "" and fall back to `directive-<hash>` — an id nobody can
    recall, so nobody can ever update the directive. Refuse and say what to pass instead."""
    proc = _add(run_hippo, tmp_project, "한글로만 쓴 지시", "phase")
    assert proc.returncode != 0
    assert "--id" in proc.stderr

    ok = _add(run_hippo, tmp_project, "한글로만 쓴 지시", "phase", "korean-rule")
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
                {"ev": "directive", "id": bad, "text": "t", "lifetime": "phase", "state": "active"},
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
    _add(run_hippo, tmp_project, "use GPUs 0 and 1 only", "phase", "gpu-01")
    _add(run_hippo, tmp_project, "use GPU 0 only", "phase", "gpu-01")
    out = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    live = json.loads(out.stdout)
    assert len(live) == 1
    assert live[0]["text"] == "use GPU 0 only"


def test_roster_hands_the_scribe_the_live_ids(tmp_project, run_hippo):
    """Without the roster the clerk coins a fresh id for a subject that already has one, and the
    update silently forks. Reading the ledger from inside the clerk is not worth the latency."""
    sys.path.insert(0, str(REPO_ROOT / "cli"))
    import hippo_cli

    _add(run_hippo, tmp_project, "use GPUs 0 and 1 only", "phase", "gpu-01")
    _add(run_hippo, tmp_project, "never save review replies", "durable", "dur-01")
    run_hippo(["directive", "withdraw", "dur-01"], cwd=tmp_project)

    roster = hippo_cli.directive_roster(tmp_project / ".hippo")
    assert "gpu-01 (phase): use GPUs 0 and 1 only" in roster
    assert "dur-01" not in roster  # withdrawn ones are not offered for reuse


# --------------------------------------------------------------------------
# volume: warned about, never enforced (principle 3)
# --------------------------------------------------------------------------

def test_long_directive_is_written_and_warned_about(tmp_project, run_hippo):
    proc = _add(run_hippo, tmp_project, "x" * 250, "phase", "long-01")
    assert proc.returncode == 0, proc.stderr
    assert "long-01 (250)" in proc.stderr
    assert "long-01" in _active_ids(run_hippo, tmp_project)


def test_an_already_registered_long_directive_is_named_when_listing(tmp_project, run_hippo):
    """The expensive directives are the ones already resident. Warning only at write time leaves
    them permanently unmentioned — every session pays and nobody is ever told."""
    _add(run_hippo, tmp_project, "x" * 250, "durable", "long-01")

    listing = run_hippo(["directive", "list"], cwd=tmp_project)
    assert listing.returncode == 0, listing.stderr
    assert "long-01 (250)" in listing.stderr
    # the listing itself stays a clean record — the note goes to stderr
    assert "note:" not in listing.stdout
    assert len(listing.stdout.splitlines()) == 1

    # and adding an unrelated short directive still surfaces the resident one
    later = _add(run_hippo, tmp_project, "short and fine", "phase", "short-01")
    assert "long-01 (250)" in later.stderr


def test_listing_a_tidy_directive_set_says_nothing(tmp_project, run_hippo):
    _add(run_hippo, tmp_project, "short and fine", "durable", "short-01")
    listing = run_hippo(["directive", "list"], cwd=tmp_project)
    assert listing.stderr.strip() == ""


def test_short_directive_warns_about_nothing(tmp_project, run_hippo):
    proc = _add(run_hippo, tmp_project, "keep it short", "phase", "short-01")
    assert proc.returncode == 0
    assert proc.stderr.strip() == ""


def test_crowded_directive_set_warns_but_still_records(tmp_project, run_hippo):
    """Eight live directives is a hygiene signal, not a limit — the eighth is still injected."""
    for i in range(7):
        proc = _add(run_hippo, tmp_project, f"directive number {i}", "phase", f"d-{i}")
        assert "live directives" not in proc.stderr, i
    eighth = _add(run_hippo, tmp_project, "directive number 7", "phase", "d-7")
    assert eighth.returncode == 0, eighth.stderr
    assert "8 live directives" in eighth.stderr
    assert "withdraw" in eighth.stderr

    inject = run_hippo(["status", "--inject"], cwd=tmp_project)
    live = [ln for ln in inject.stdout.splitlines() if ln.startswith("· live(")]
    assert len(live) == 8


# --------------------------------------------------------------------------
# phase staleness — shown, never resolved (DESIGN §6, third rule)
# --------------------------------------------------------------------------

def _backdate_directive(project_dir, did, text, lifetime, days_ago):
    """Append a directive event with an old writer timestamp. The CLI stamps t itself, so age
    can only be fabricated the way it really arises: as an old line already in the ledger."""
    from datetime import datetime, timedelta, timezone
    t = (datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    row = {"t": t, "ev": "directive", "id": did, "text": text,
           "lifetime": lifetime, "state": "active", "src": "cli"}
    with (project_dir / ".hippo" / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_phase_age_appears_in_the_capsule_from_seven_days(tmp_project, run_hippo):
    _backdate_directive(tmp_project, "old-phase", "hold the perf claims", "phase", 10)
    _add(run_hippo, tmp_project, "use GPUs 0 and 1 only", "phase", "fresh-phase")
    _backdate_directive(tmp_project, "old-durable", "answer tersely", "durable", 30)

    out = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert out.returncode == 0, out.stderr
    assert "live(phase·10d): hold the perf claims" in out.stdout
    # Fresh phase and durable lines are untouched — durable does not age by definition.
    assert "live(phase): use GPUs 0 and 1 only" in out.stdout
    assert "live(durable): answer tersely" in out.stdout


def test_stale_phase_note_names_the_id_on_list_and_add(tmp_project, run_hippo):
    _backdate_directive(tmp_project, "stale-hold", "hold off on speed claims", "phase", 15)

    listed = run_hippo(["directive", "list"], cwd=tmp_project)
    assert listed.returncode == 0
    assert "stale-hold (15d)" in listed.stderr
    assert "withdraw" in listed.stderr
    # The listing itself stays a clean record — the note rides stderr only.
    assert "15d" not in listed.stdout


def test_fresh_phase_directive_draws_no_staleness_note(tmp_project, run_hippo):
    _add(run_hippo, tmp_project, "use GPUs 0 and 1 only", "phase", "gpu-01")
    listed = run_hippo(["directive", "list"], cwd=tmp_project)
    assert listed.returncode == 0
    assert "phase directive(s)" not in listed.stderr
