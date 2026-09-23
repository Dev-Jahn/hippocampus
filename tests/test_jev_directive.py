"""The judge's read of directive *content* — the notes after `directive add` and
`directive list` (DESIGN §6 fourth rule, §3.9) — automatic whenever there is a judge.

Nothing here reaches the network: every run pins `HIPPO_JEV_BACKEND` to `mock`, or takes `off`
from conftest. What most of these pin down is the two-stage rule — stage 1 reads the whole set
in one request and only decides what is worth asking again; stage 2 asks the pair alone, and it
is the only thing a conflict note is allowed to rest on.
"""
import json
import sys

import pytest

from conftest import REPO_ROOT, read_ledger


@pytest.fixture
def cli():
    path = str(REPO_ROOT / "cli")
    if path not in sys.path:
        sys.path.insert(0, path)
    import hippo_cli

    return hippo_cli


def _mock(tmp_path, payload, name="jev.json"):
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _choice(option, confidence):
    return {"choice": option, "confidence": confidence, "probabilities": {option: confidence}}


def _quiet(tmp_path, n=3, extra=None, name="jev.json"):
    """A mock that agrees with everything the tests store: `all` audience and no conflict
    anywhere. A test that is about one note answers that one id and inherits silence
    for the rest, instead of drowning in suggestions it did not ask about."""
    answers = {"audience": _choice("all", 1.0)}
    for i in range(n):
        answers[f"audience_{i}"] = _choice("all", 1.0)
    answers.update(extra or {})
    return _mock(tmp_path, {"answers": answers, "default": {"noul": 0.01}}, name)


def _env(mock_path, capture=None, backend="mock"):
    env = {"HIPPO_JEV_BACKEND": backend}
    if mock_path is not None:
        env["HIPPO_JEV_MOCK_OUTPUT"] = str(mock_path)
    if capture is not None:
        env["HIPPO_JEV_MOCK_CAPTURE"] = str(capture)
    return env


def _add(run_hippo, cwd, did, text, env=None, audience=None):
    argv = ["directive", "add", "--id", did, "--text", text]
    if audience:
        argv += ["--audience", audience]
    return run_hippo(argv, cwd=cwd, env=env)


def _meter_rows(project):
    return [e for e in read_ledger(project)
            if e.get("ev") == "clerk" and e.get("name") == "jev-directive"]


# --------------------------------------------------------------------------
# the shipped spec
# --------------------------------------------------------------------------

def test_the_shipped_directive_spec_carries_every_shape_both_surfaces_ask(cli):
    """Three conflict shapes because the state comes in three shapes, and one audience question
    per subject. The criteria are shared by anchor: stage 1 and stage 2 must ask the same question
    or the second stage is not a re-check of the first."""
    q = cli.jev_questions("directive", i=0, j=1)
    assert set(q) == {"conflict_0", "conflict_0_1", "conflict", "audience", "audience_0"}
    assert {k for k, v in q.items() if v["type"] == "noul"} == {
        "conflict_0", "conflict_0_1", "conflict"}
    assert q["conflict"]["criteria"] == q["conflict_0"]["criteria"] == q["conflict_0_1"]["criteria"]
    assert set(q["audience"]["criteria"]) == {"main", "executor", "all"}
    policy = cli.jev_policy("directive")
    assert (policy["recheck_at"], policy["report_at"]) == (0.5, 0.7)
    assert (policy["suggest_at"], policy["max_live"]) == (0.7, 40)


# --------------------------------------------------------------------------
# the conflict note, and the second stage that has to agree with the first
# --------------------------------------------------------------------------

def test_a_conflict_both_stages_agree_on_is_named_with_its_text(tmp_project, run_hippo, tmp_path):
    jev = _quiet(tmp_path, extra={"conflict_0": {"noul": 0.9}, "conflict": {"noul": 0.9}})
    _add(run_hippo, tmp_project, "english-only", "write every file in English", env=_env(jev))
    second = _add(run_hippo, tmp_project, "korean-comments", "write the comments in Korean",
                  env=_env(jev))
    assert second.returncode == 0, second.stderr
    assert ("note: may conflict with english-only (0.90): write every file in English"
            in second.stderr), second.stderr
    # The write landed all the same — a note is the whole of it (§6, fourth rule).
    listing = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    assert {d["id"] for d in json.loads(listing.stdout)} == {"english-only", "korean-comments"}


def test_a_pair_the_second_stage_clears_draws_no_note(tmp_project, run_hippo, tmp_path):
    """The measured reason the second stage exists: with the whole set in the state, two
    unrelated directives scored 0.66-0.74. Stage 1 only decides what to ask again."""
    jev = _quiet(tmp_path, extra={"conflict_0": {"noul": 0.9}, "conflict": {"noul": 0.3}})
    _add(run_hippo, tmp_project, "english-only", "write every file in English", env=_env(jev))
    second = _add(run_hippo, tmp_project, "korean-comments", "write the comments in Korean",
                  env=_env(jev))
    assert second.returncode == 0, second.stderr
    assert "may conflict" not in second.stderr, second.stderr
    assert len(_meter_rows(tmp_project)) == 3, "first add, then stage 1 and the one re-check"


def test_a_low_stage_one_never_reaches_the_second_stage(tmp_project, run_hippo, tmp_path):
    jev = _quiet(tmp_path, extra={"conflict_0": {"noul": 0.4}, "conflict": {"noul": 0.99}})
    _add(run_hippo, tmp_project, "english-only", "write every file in English", env=_env(jev))
    second = _add(run_hippo, tmp_project, "korean-comments", "write the comments in Korean",
                  env=_env(jev))
    assert "may conflict" not in second.stderr, second.stderr
    assert len(_meter_rows(tmp_project)) == 2, "below recheck_at nothing is asked again"


# --------------------------------------------------------------------------
# the axis notes — only when the reading disagrees, and only when it is sure
# --------------------------------------------------------------------------

def test_audience_note_appears_when_the_reading_differs_from_the_stored_value(
    tmp_project, run_hippo, tmp_path
):
    jev = _quiet(tmp_path, extra={"audience": _choice("executor", 0.9)})
    proc = _add(run_hippo, tmp_project, "repo-english", "write every file in English",
                env=_env(jev))
    assert proc.returncode == 0, proc.stderr
    assert ("note: audience reads as executor (0.90) — stored as all; re-add with "
            "--audience executor if that is what was meant") in proc.stderr


def test_a_reading_that_agrees_with_the_stored_value_says_nothing(
    tmp_project, run_hippo, tmp_path
):
    jev = _quiet(tmp_path, extra={"audience": _choice("executor", 0.99)})
    proc = _add(run_hippo, tmp_project, "repo-english", "write every file in English",
                env=_env(jev), audience="executor")
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.strip() == "", proc.stderr


def test_an_unsure_reading_says_nothing(tmp_project, run_hippo, tmp_path):
    """A choice under the threshold is a coin toss between three options, not a finding."""
    jev = _quiet(tmp_path, extra={"audience": _choice("executor", 0.5)})
    proc = _add(run_hippo, tmp_project, "repo-english", "write every file in English",
                env=_env(jev))
    assert proc.stderr.strip() == "", proc.stderr


# --------------------------------------------------------------------------
# with the judge off, the command is what it always was
# --------------------------------------------------------------------------

def test_with_the_judge_off_add_prints_exactly_the_volume_notes(tmp_project, run_hippo):
    """conftest pins the backend off, so this is what every machine without a key sees."""
    quiet = _add(run_hippo, tmp_project, "short-01", "keep it short")
    assert quiet.stderr.strip() == ""
    loud = _add(run_hippo, tmp_project, "long-01", "x" * 250)
    assert loud.returncode == 0, loud.stderr
    assert loud.stderr.splitlines() == [
        "note: 1 directive(s) over 200 chars — long-01 (250). Compress and re-add under the "
        "same --id."
    ]
    assert _meter_rows(tmp_project) == [], "no key, no judge, no row"


def test_a_withdrawal_is_never_judged(tmp_project, run_hippo, tmp_path):
    """A withdrawal carries no text to read, and the directive it names is on its way out."""
    jev = _quiet(tmp_path)
    _add(run_hippo, tmp_project, "gpu-01", "use GPUs 0 and 1 only", env=_env(jev))
    before = len(_meter_rows(tmp_project))
    out = run_hippo(["directive", "withdraw", "gpu-01"], cwd=tmp_project, env=_env(jev))
    assert out.returncode == 0, out.stderr
    assert out.stderr.strip() == ""
    assert len(_meter_rows(tmp_project)) == before


# --------------------------------------------------------------------------
# the metering row — a failed judge is a gap in the ledger, not silence
# --------------------------------------------------------------------------

def test_a_failed_judge_leaves_a_row_and_the_write_still_landed(tmp_project, run_hippo, tmp_path):
    proc = _add(run_hippo, tmp_project, "gpu-01", "use GPUs 0 and 1 only",
                env=_env(tmp_path / "absent.json"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.strip() == "", "a judge that failed adds nothing to the terminal"
    rows = _meter_rows(tmp_project)
    assert [r["ok"] for r in rows] == [False]
    assert rows[0]["src"] == "cli"
    live = json.loads(run_hippo(["directive", "list", "--active", "--json"],
                                cwd=tmp_project).stdout)
    assert [d["id"] for d in live] == ["gpu-01"]


def test_every_request_gets_its_own_row(tmp_project, run_hippo, tmp_path):
    jev = _quiet(tmp_path, extra={"conflict_0": {"noul": 0.9}, "conflict_1": {"noul": 0.9},
                                  "conflict": {"noul": 0.9}})
    _add(run_hippo, tmp_project, "d-0", "write every file in English", env=_env(jev))
    _add(run_hippo, tmp_project, "d-1", "write the comments in Korean", env=_env(jev))
    third = _add(run_hippo, tmp_project, "d-2", "write the docs in Korean", env=_env(jev))
    assert third.returncode == 0, third.stderr
    rows = _meter_rows(tmp_project)
    # 1 + (stage 1 + one re-check) + (stage 1 + two re-checks)
    assert len(rows) == 6
    assert all(r["ok"] is True and r["src"] == "cli" for r in rows)
    assert all(isinstance(r["ms"], int) and isinstance(r["tokens"], int) for r in rows)
    assert third.stderr.count("may conflict with") == 2


# --------------------------------------------------------------------------
# what is actually sent
# --------------------------------------------------------------------------

def test_the_state_carries_the_whole_live_set_and_never_the_re_added_id(
    tmp_project, run_hippo, tmp_path
):
    """A re-add under the same --id is an update. Comparing a directive with its own previous
    text would report every edit as a conflict with itself."""
    jev = _quiet(tmp_path)
    capture = tmp_path / "capture.json"
    _add(run_hippo, tmp_project, "gpu-01", "use GPUs 0 and 1 only", env=_env(jev))
    _add(run_hippo, tmp_project, "dur-01", "never save review replies",
         env=_env(jev), audience="main")
    _add(run_hippo, tmp_project, "gpu-01", "use GPU 0 only", env=_env(jev, capture))

    sent = json.loads(capture.read_text(encoding="utf-8"))
    assert sent["state"]["new"] == {"id": "gpu-01", "audience": "all",
                                    "text": "use GPU 0 only"}
    assert sent["state"]["directives"] == [
        {"id": "dur-01", "audience": "main",
         "text": "never save review replies"}
    ], "the id being re-added is not compared with its own old text"
    assert set(sent["questions"]) == {"conflict_0", "audience"}


# --------------------------------------------------------------------------
# `directive list` — the judge reads the set by itself
# --------------------------------------------------------------------------

def _seed_three(run_hippo, project, env):
    _add(run_hippo, project, "english-only", "write every file in English", env=env)
    _add(run_hippo, project, "gpu-01", "use GPUs 0 and 1 only", env=env)
    _add(run_hippo, project, "korean-comments", "write the comments in Korean", env=env)


def test_hygiene_prints_pair_notes_and_per_directive_notes(tmp_project, run_hippo, tmp_path):
    _seed_three(run_hippo, tmp_project, _env(_quiet(tmp_path, name="seed.json")))
    jev = _quiet(tmp_path, extra={
        "conflict_0_2": {"noul": 0.9},           # english-only vs korean-comments
        "conflict": {"noul": 0.83},              # and it survives the second stage
        "audience_1": _choice("executor", 0.9),  # gpu-01, stored as all
    }, name="hygiene.json")
    capture = tmp_path / "capture.json"
    out = run_hippo(["directive", "list"], cwd=tmp_project, env=_env(jev, capture))
    assert out.returncode == 0, out.stderr
    assert len(out.stdout.splitlines()) == 3, out.stdout
    assert "note: english-only may conflict with korean-comments (0.83)" in out.stderr
    assert ("note: gpu-01: audience reads as executor (0.90) — stored as all; re-add with "
            "--audience executor if that is what was meant") in out.stderr
    assert "english-only: audience" not in out.stderr, "an agreeing reading says nothing"

    sent = json.loads(capture.read_text(encoding="utf-8"))  # the last request is the re-check
    assert set(sent["state"]) == {"a", "b"}
    assert (sent["state"]["a"]["id"], sent["state"]["b"]["id"]) == (
        "english-only", "korean-comments")
    assert set(sent["questions"]) == {"conflict"}


def test_hygiene_stage_one_asks_every_pair_and_every_audience_in_one_request(
    tmp_project, run_hippo, tmp_path
):
    jev = _quiet(tmp_path)
    _seed_three(run_hippo, tmp_project, _env(jev))
    capture = tmp_path / "capture.json"
    out = run_hippo(["directive", "list"], cwd=tmp_project, env=_env(jev, capture))
    assert out.returncode == 0, out.stderr
    sent = json.loads(capture.read_text(encoding="utf-8"))
    assert set(sent["questions"]) == {
        "conflict_0_1", "conflict_0_2", "conflict_1_2",
        "audience_0", "audience_1", "audience_2",
    }
    assert [d["id"] for d in sent["state"]["directives"]] == [
        "english-only", "gpu-01", "korean-comments"]
    assert out.stderr.strip() == "", "a set the judge reads as clean says nothing"


def test_with_the_judge_off_the_listing_is_byte_identical_to_before(tmp_project, run_hippo):
    """No key, no judge, no note: the read command is what it always was (§3.9)."""
    _add(run_hippo, tmp_project, "long-01", "x" * 250)
    out = run_hippo(["directive", "list"], cwd=tmp_project)
    assert out.returncode == 0, out.stderr
    assert not [ln for ln in out.stderr.splitlines() if not ln.startswith("note: ")], out.stderr
    assert "may conflict" not in out.stderr and "hygiene" not in out.stderr
    assert _meter_rows(tmp_project) == []


def test_hygiene_on_an_empty_set_asks_nothing(tmp_project, run_hippo, tmp_path):
    capture = tmp_path / "capture.json"
    out = run_hippo(["directive", "list"], cwd=tmp_project,
                    env=_env(_quiet(tmp_path), capture))
    assert out.returncode == 0, out.stderr
    assert (out.stdout, out.stderr) == ("", "")
    assert not capture.exists()


def test_a_hygiene_run_the_judge_could_not_answer_says_why(tmp_project, run_hippo, tmp_path):
    """The one mode that is mostly the judge: silence here would read as a clean set."""
    _add(run_hippo, tmp_project, "gpu-01", "use GPUs 0 and 1 only")
    out = run_hippo(["directive", "list"], cwd=tmp_project,
                    env=_env(tmp_path / "absent.json"))
    assert out.returncode == 0, out.stderr
    assert "hygiene: the judge did not answer (mock: " in out.stderr
    assert [r["ok"] for r in _meter_rows(tmp_project)] == [False]


def test_a_json_listing_never_asks_the_judge(tmp_project, run_hippo, tmp_path):
    """A --json read is a machine's: no request, no note, whatever the backend is."""
    jev = _quiet(tmp_path)
    _add(run_hippo, tmp_project, "gpu-01", "use GPUs 0 and 1 only", env=_env(jev))
    before = len(_meter_rows(tmp_project))
    out = run_hippo(["directive", "list", "--json"], cwd=tmp_project, env=_env(jev))
    assert out.returncode == 0, out.stderr
    assert out.stderr.strip() == ""
    assert len(_meter_rows(tmp_project)) == before
