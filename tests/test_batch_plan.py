"""The plan over a batch manifest (DESIGN §3.6) — the judge measures each brief, code prices
it against prices.yaml and the ledger's kind × exec cells. `--dry-run` prints it; a launch runs
it first and routes the entries that left `model` unset.

Contract under test: what the request actually asks, the tier and effort the difficulty
demands, the evidence bump and drop, the check, kind, tier-disagreement and directive-conflict
notes, the `.plan.jsonl` records, that a dry run launches nothing and the manifest is never
rewritten, auto-routing at launch, and — the rule that outranks all of it — that with the
judge off the deterministic half still prints and an unrouted launch fails as it always did.

Nothing here may reach the network: conftest pins HIPPO_JEV_BACKEND=off and the tests that
want a judge pin `mock`, whose answers come from $HIPPO_JEV_MOCK_OUTPUT.
"""
import json
from datetime import datetime, timezone

import pytest

from conftest import read_ledger
from test_batch import _batch, _manifest, _stub
from test_batch_harvest import _jev, _mock

STUB_OK = "#!/bin/sh\nexit 0\n"

# One answer set per difficulty shape. kind_fit is confident and inside the vocabulary unless a
# test is about the kind note.
DEFAULT = {"noul": 0.5, "choice": "impl", "score": 0.0}
FIT = {"kind_fit": {"choice": "impl", "confidence": 0.95}}


def _answers(scope, novelty, spec, verifiable=0.9, **over):
    return {"scope": {"score": scope, "confidence": 0.8},
            "novelty": {"score": novelty, "confidence": 0.8},
            "spec": {"score": spec, "confidence": 0.8},
            "verifiable": {"noul": verifiable}, **FIT, **over}


HARD = _answers(1.0, 2.8, 1.0)     # design judgment → top
MIDDLING = _answers(1.0, 1.2, 0.5)  # some local decisions → mid
EASY = _answers(0.4, 0.2, 0.1)      # fully specified, textbook → cheap


def _wave(project, kind="impl", extra="", entry_id="solo"):
    """A manifest written to be planned: no model, because that is what it is asking for."""
    return _manifest(project, "wave.yaml", f"""\
        defaults:
          kind: {kind}
        entries:
          - id: {entry_id}
            scope: "one lane"
            prompt: "add the retry loop and run tests/run.sh"
        {extra}
        """)


def _plan(run_hippo, project, manifest, mock=None, capture=None, **env):
    env = dict(env)
    if mock is not None:
        env.update(_jev(mock, capture))
    return _batch(run_hippo, project, manifest, "--dry-run", env=env)


def _row(proc, eid):
    """One table row, split on whitespace. Only `evidence` (the last column) holds spaces, so
    the fixed columns are positional and the evidence is whatever is left."""
    for line in proc.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == eid:
            return parts[:7] + [" ".join(parts[7:])]
    raise AssertionError(f"no {eid} row in:\n{proc.stdout}")


def _notes(proc, eid):
    return [ln.split(": ", 1)[1] for ln in proc.stdout.splitlines()
            if ln.startswith(f"{eid}: ")]


def _plan_file(manifest):
    path = manifest.parent / f"{manifest.stem}.plan.jsonl"
    if not path.exists():
        return None
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _seed(project, kind, ex, n, accepted):
    """n judged dispatches on one kind × exec cell, `accepted` of them first-pass."""
    t = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with (project / ".hippo" / "ledger.jsonl").open("a", encoding="utf-8") as f:
        for i in range(n):
            did = f"dseed{i:03d}"
            f.write(json.dumps({"t": t, "ev": "dispatch", "id": did, "kind": kind, "exec": ex,
                                "scope": "seeded", "src": "wrapper"}) + "\n")
            f.write(json.dumps({"t": t, "ev": "outcome", "ref": did, "attr": "work",
                                "result": "accepted" if i < accepted else "refuted",
                                "note": "seeded"}) + "\n")


# --------------------------------------------------------------------------
# (a) difficulty → tier and effort
# --------------------------------------------------------------------------

@pytest.mark.parametrize("answers, tier, model, effort", [
    (HARD, "top", "gpt-6-astra", "high"),
    (MIDDLING, "mid", "gpt-5.6-sol", "medium"),
    (EASY, "cheap", "gpt-5.6-luna", "medium"),
])
def test_difficulty_picks_the_tier_and_the_effort(tmp_project, tmp_path, run_hippo,
                                                  answers, tier, model, effort):
    manifest = _wave(tmp_project)
    proc = _plan(run_hippo, tmp_project, manifest,
                 _mock(tmp_path, {"answers": answers, "default": DEFAULT}))
    assert proc.returncode == 0, proc.stderr

    row = _row(proc, "solo")
    assert row[6] == f"codex/{model}/{effort}"
    assert row[5] == "-", "the manifest named no exec — that is what it is asking for"
    assert json.loads(proc.stdout.splitlines()[-1])["tiers"] == {tier: 1}


def test_the_ladder_is_printed_once_per_executor_from_the_price_sheet(tmp_project, tmp_path,
                                                                      run_hippo):
    """Read off prices.yaml at call time — the lowest, highest and second-highest *price
    level*, so a price refresh moves the tiers instead of a config file going stale (§4). Levels,
    not rows: fable-5 shares fable-5-1's price and must not become `mid`."""
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
        entries:
          - id: solo
            scope: "one lane"
            prompt: "do the thing"
          - id: other
            scope: "the other lane"
            executor: claude
            prompt: "do the other thing"
        """)
    proc = _plan(run_hippo, tmp_project, manifest,
                 _mock(tmp_path, {"answers": EASY, "default": DEFAULT}))
    ladders = [ln for ln in proc.stdout.splitlines() if ln.startswith("ladder ")]
    assert ladders == [
        "ladder codex: cheap gpt-5.6-luna · mid gpt-5.6-sol · top gpt-6-astra",
        "ladder claude: cheap claude-haiku-4-5 · mid claude-opus-5 · top claude-fable-5-1"]
    assert _row(proc, "other")[6] == "claude/claude-haiku-4-5/medium"


def test_the_request_asks_the_route_questions_over_the_whole_brief(tmp_project, tmp_path,
                                                                   run_hippo):
    capture = tmp_path / "sent.json"
    manifest = _wave(tmp_project)
    before = manifest.read_bytes()
    _plan(run_hippo, tmp_project, manifest,
          _mock(tmp_path, {"answers": EASY, "default": DEFAULT}), capture)

    sent = json.loads(capture.read_text(encoding="utf-8"))
    assert list(sent["questions"]) == ["scope", "novelty", "spec", "verifiable", "kind_fit"]
    assert sent["state"] == {"scope": "one lane", "kind": "impl",
                             "brief": "add the retry loop and run tests/run.sh"}
    # The wire shape a score wants (measured against the live API, 2026-09-23): criteria are an
    # ordered list and the answer is a 0-indexed float.
    assert sent["questions"]["scope"]["criteria"][3].startswith("cross-cutting")
    assert set(sent["questions"]["kind_fit"]["criteria"]) >= {"impl", "verify", "docs"}
    assert manifest.read_bytes() == before, "the plan suggests; main edits the manifest"


# --------------------------------------------------------------------------
# (b) the evidence adjustment — the ledger moves the suggestion at most one step
# --------------------------------------------------------------------------

def test_a_tier_this_kind_keeps_failing_at_is_bumped_one_step(tmp_project, tmp_path,
                                                              run_hippo):
    _seed(tmp_project, "impl", "codex/gpt-5.6-luna/medium", 5, accepted=1)
    proc = _plan(run_hippo, tmp_project, _wave(tmp_project),
                 _mock(tmp_path, {"answers": EASY, "default": DEFAULT}))
    assert proc.returncode == 0, proc.stderr

    row = _row(proc, "solo")
    assert row[6] == "codex/gpt-5.6-sol/medium", "cheap scored 1/5 — one tier up"
    assert row[7] == "no evidence", "the evidence column follows the suggestion"
    assert _notes(proc, "solo") == [
        "cheap → mid: priors impl×codex/gpt-5.6-luna/medium 1/5 is under 0.50 first-pass"]


def test_a_cheaper_tier_that_clears_the_bar_takes_the_work(tmp_project, tmp_path, run_hippo):
    _seed(tmp_project, "impl", "codex/gpt-5.6-luna/medium", 5, accepted=5)
    proc = _plan(run_hippo, tmp_project, _wave(tmp_project),
                 _mock(tmp_path, {"answers": MIDDLING, "default": DEFAULT}))
    assert proc.returncode == 0, proc.stderr

    row = _row(proc, "solo")
    assert row[6] == "codex/gpt-5.6-luna/medium", "5/5 at the cheap tier answers §9.6"
    assert row[7] == "priors impl×codex/gpt-5.6-luna/medium 5/5"
    assert _notes(proc, "solo") == [
        "mid → cheap: priors impl×codex/gpt-5.6-luna/medium 5/5 is at or over 0.80 first-pass"]


def test_a_cell_under_the_sample_threshold_moves_nothing_and_is_named(tmp_project, tmp_path,
                                                                      run_hippo):
    """n=3 at 1/3 is worse than the bump threshold and still not evidence (§3.6b). The reader
    is told the cell is thin rather than left unable to tell it from an absent one."""
    _seed(tmp_project, "impl", "codex/gpt-5.6-luna/medium", 3, accepted=1)
    proc = _plan(run_hippo, tmp_project, _wave(tmp_project),
                 _mock(tmp_path, {"answers": EASY, "default": DEFAULT}))
    row = _row(proc, "solo")
    assert row[6] == "codex/gpt-5.6-luna/medium"
    assert row[7] == "no evidence (n=3)"
    assert _notes(proc, "solo") == []


# --------------------------------------------------------------------------
# (c) and (d) the notes that are about the manifest rather than the routing
# --------------------------------------------------------------------------

def test_a_kind_outside_the_vocabulary_is_read_back(tmp_project, tmp_path, run_hippo):
    """PRIORS aggregates on kind, so a stray tag is a column of one — said while the manifest
    is still being edited."""
    mock = _mock(tmp_path, {"answers": {**EASY, "kind_fit": {"choice": "impl",
                                                             "confidence": 0.9}},
                            "default": DEFAULT})
    proc = _plan(run_hippo, tmp_project, _wave(tmp_project, kind="lane"), mock)
    assert 'kind "lane" reads as impl (0.90)' in _notes(proc, "solo")


def test_an_unconfident_kind_reading_says_nothing(tmp_project, tmp_path, run_hippo):
    mock = _mock(tmp_path, {"answers": {**EASY, "kind_fit": {"choice": "impl",
                                                             "confidence": 0.5}},
                            "default": DEFAULT})
    proc = _plan(run_hippo, tmp_project, _wave(tmp_project, kind="lane"), mock)
    assert _notes(proc, "solo") == []


@pytest.mark.parametrize("check, expected", [
    ("", ["add a check — the brief names no machine-verifiable completion"]),
    ("check: \"true\"", []),
])
def test_a_brief_naming_nothing_a_machine_can_run_earns_a_check_note(tmp_project, tmp_path,
                                                                     run_hippo, check,
                                                                     expected):
    manifest = _manifest(tmp_project, "wave.yaml", f"""\
        defaults:
          kind: impl
        entries:
          - id: solo
            scope: "one lane"
            prompt: "make it nicer"
            {check}
        """)
    mock = _mock(tmp_path, {"answers": _answers(0.4, 0.2, 0.1, verifiable=0.05),
                            "default": DEFAULT})
    proc = _plan(run_hippo, tmp_project, manifest, mock)
    assert _row(proc, "solo")[4] == ".05"
    assert _notes(proc, "solo") == expected


# --------------------------------------------------------------------------
# (e) the .plan.jsonl — so the wave's routing decision can be joined later
# --------------------------------------------------------------------------

def test_every_entry_lands_in_the_plan_file(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
        entries:
          - id: one
            scope: "first"
            prompt: "do the first thing"
          - id: two
            scope: "second"
            prompt: "do the second thing"
        """)
    proc = _plan(run_hippo, tmp_project, manifest,
                 _mock(tmp_path, {"answers": HARD, "default": DEFAULT}))
    assert proc.returncode == 0, proc.stderr

    recs = _plan_file(manifest)
    assert [r["id"] for r in recs] == ["one", "two"]
    r = recs[0]
    assert r["difficulty"]["novelty"] == {"score": 2.8, "confidence": 0.8}
    assert r["difficulty"]["verifiable"] == 0.9, "a noul compacts to its probability"
    assert r["kind_fit"] == {"choice": "impl", "confidence": 0.95}
    assert r["suggested"] == {"model": "gpt-6-astra", "effort": "high"}
    assert r["evidence"] == "no evidence"
    assert r["jev"]["ok"] is True

    # Self-metering, one row per request (§2 clerk guardrails, judge row).
    rows = [e for e in read_ledger(tmp_project) if e.get("ev") == "clerk"]
    assert [e["name"] for e in rows] == ["jev-plan", "jev-plan"]
    assert all(e["src"] == "wrapper" and e["ok"] is True for e in rows)

    summary = json.loads(proc.stdout.splitlines()[-1])
    assert summary["total"] == 2 and summary["suggested"] == 2
    assert summary["plan"].endswith("wave.plan.jsonl")


def test_a_failed_request_suggests_nothing(tmp_project, tmp_path, run_hippo):
    """A mock with no answer for a question is a judge failure. The row prints, the record
    keeps the gap, and no tier is invented out of it."""
    manifest = _wave(tmp_project)
    proc = _plan(run_hippo, tmp_project, manifest,
                 _mock(tmp_path, {"answers": {}}))  # and no default either
    assert proc.returncode == 0, proc.stderr
    assert _row(proc, "solo") == ["solo", "-", "-", "-", "-", "-", "-", "-"]

    rec = _plan_file(manifest)[0]
    assert rec["suggested"] == {"model": None, "effort": None}
    assert rec["difficulty"] == {"scope": None, "novelty": None, "spec": None,
                                 "verifiable": None}
    assert rec["jev"]["ok"] is False
    assert [e["ok"] for e in read_ledger(tmp_project) if e.get("ev") == "clerk"] == [False]


# --------------------------------------------------------------------------
# (f) opt-in by the key alone — with the judge off this is still a working command
# --------------------------------------------------------------------------

def test_with_the_judge_off_the_deterministic_half_still_prints(tmp_project, run_hippo):
    _seed(tmp_project, "impl", "codex/gpt-5.6-luna/medium", 5, accepted=4)
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
          model: gpt-5.6-luna
        entries:
          - id: solo
            scope: "one lane"
            prompt: "do the thing"
        """)
    proc = _plan(run_hippo, tmp_project, manifest)
    assert proc.returncode == 0, proc.stderr
    assert "judge off — no TYPESAFE_API_KEY" in proc.stderr
    assert "ladder codex: cheap gpt-5.6-luna" in proc.stdout

    row = _row(proc, "solo")
    assert row[1:5] == ["-", "-", "-", "-"], "no difficulty without the judge"
    assert row[5] == "codex/gpt-5.6-luna/medium", "what the manifest already routes to"
    assert row[6] == "-"
    assert row[7] == "priors impl×codex/gpt-5.6-luna/medium 4/5", "the priors need no key"

    assert _plan_file(manifest) is None, "no answers, no plan file"
    summary = json.loads(proc.stdout.splitlines()[-1])
    assert summary["plan"] is None and summary["suggested"] == 0
    assert [e for e in read_ledger(tmp_project) if e.get("ev") == "clerk"] == []


def test_with_the_judge_off_an_unrouted_manifest_still_does_not_refuse(tmp_project, run_hippo):
    proc = _plan(run_hippo, tmp_project, _wave(tmp_project))
    assert proc.returncode == 0, proc.stderr
    assert _row(proc, "solo")[5:] == ["-", "-", "-"]


# --------------------------------------------------------------------------
# a dry run is not a launch — and a launch routes what the manifest left unrouted
# --------------------------------------------------------------------------

def test_without_the_judge_an_unrouted_launch_fails_as_it_always_did(tmp_project, tmp_path,
                                                                      run_hippo):
    manifest = _wave(tmp_project)
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", STUB_OK)})
    assert proc.returncode == 2 and "model is required" in proc.stderr

    proc = _plan(run_hippo, tmp_project, manifest,
                 _mock(tmp_path, {"answers": EASY, "default": DEFAULT}))
    assert proc.returncode == 0, proc.stderr


def test_with_the_judge_an_unrouted_entry_launches_on_the_suggestion(tmp_project, tmp_path,
                                                                     run_hippo):
    manifest = _wave(tmp_project)
    argv = tmp_path / "argv.bin"
    stub = '#!/bin/sh\nfor a in "$@"; do printf \'%s\\0\' "$a" >> "$ARGV_FILE"; done\n'
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", stub), "ARGV_FILE": str(argv),
                       **_jev(_mock(tmp_path, {"answers": HARD, "default": DEFAULT}))})
    assert proc.returncode == 0, proc.stderr

    sent = argv.read_text(encoding="utf-8").split("\0")
    assert sent[:5] == ["exec", "-m", "gpt-6-astra", "-c", "model_reasoning_effort=high"]
    (d,) = [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]
    assert d["exec"] == "codex/gpt-6-astra/high"
    # The plan goes to stderr before the batch starts: stdout is the harvest's.
    assert "ladder codex: cheap gpt-5.6-luna" in proc.stderr
    assert proc.stderr.index("ladder codex") < proc.stderr.index("[1/1] solo")
    assert "ladder" not in proc.stdout
    assert _plan_file(manifest)[0]["suggested"] == {"model": "gpt-6-astra", "effort": "high"}


def test_an_unrouted_entry_the_judge_cannot_route_launches_nothing(tmp_project, tmp_path,
                                                                    run_hippo):
    capture = tmp_path / "launched.txt"
    proc = _batch(run_hippo, tmp_project, _wave(tmp_project),
                  env={"PATH": _stub(tmp_path, "codex", f'#!/bin/sh\ntouch "{capture}"\n'),
                       **_jev(_mock(tmp_path, {"answers": {}}))})
    assert proc.returncode == 2
    assert "model is required — the judge suggested none for solo" in proc.stderr
    assert not capture.exists()
    assert not (tmp_project / "wave.journal.jsonl").exists()


def test_a_routed_entry_two_tiers_from_its_brief_gets_a_note(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
          model: gpt-5.6-luna
        entries:
          - id: solo
            scope: "one lane"
            prompt: "redesign the capsule grammar"
        """)
    note = ("reads top-tier (scope 1.0, novelty 2.8, spec 1.0) — routed to "
            "gpt-5.6-luna/medium")
    mock = _mock(tmp_path, {"answers": HARD, "default": DEFAULT})
    assert note in _notes(_plan(run_hippo, tmp_project, manifest, mock), "solo")

    # At launch it is a stderr note beside the plan, never a gate: the entry runs as routed.
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", STUB_OK), **_jev(mock)})
    assert proc.returncode == 0, proc.stderr
    assert f"solo: {note}" in proc.stderr
    (d,) = [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]
    assert d["exec"] == "codex/gpt-5.6-luna/medium"

    # One tier apart is not worth a line.
    mid = _mock(tmp_path, {"answers": MIDDLING, "default": DEFAULT}, name="mid.json")
    assert not [n for n in _notes(_plan(run_hippo, tmp_project, manifest, mid), "solo")
                if "reads" in n]


def test_a_brief_that_contradicts_a_lane_directive_gets_a_note(tmp_project, tmp_path,
                                                               run_hippo):
    for did, audience, text in (("gpu-pin", "executor", "use GPUs 0 and 1 only"),
                                ("tone", "main", "answer the user in Korean")):
        proc = run_hippo(["directive", "add", "--id", did, "--text", text,
                          "--lifetime", "durable", "--audience", audience], cwd=tmp_project)
        assert proc.returncode == 0, proc.stderr
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
          model: gpt-5.6-luna
        entries:
          - id: solo
            scope: "one lane"
            prompt: "run the sweep on all eight GPUs"
        """)
    capture = tmp_path / "sent.json"
    mock = _mock(tmp_path, {"answers": {**EASY, "conflict_0": {"noul": 0.83}},
                            "default": DEFAULT})
    proc = _plan(run_hippo, tmp_project, manifest, mock, capture)
    assert proc.returncode == 0, proc.stderr
    assert ("brief may conflict with directive gpu-pin (0.83): use GPUs 0 and 1 only"
            in _notes(proc, "solo"))

    # The brief beside only what the lane's capsule will carry: a main-only rule is not asked.
    sent = json.loads(capture.read_text(encoding="utf-8"))
    assert list(sent["questions"]) == ["conflict_0"]
    assert sent["state"]["brief"] == "run the sweep on all eight GPUs"
    assert [d["id"] for d in sent["state"]["directives"]] == ["gpu-pin"]
    names = [e["name"] for e in read_ledger(tmp_project) if e.get("ev") == "clerk"]
    assert names.count("jev-brief") == 1

    quiet = _mock(tmp_path, {"answers": {**EASY, "conflict_0": {"noul": 0.4}},
                             "default": DEFAULT}, name="quiet.json")
    assert _notes(_plan(run_hippo, tmp_project, manifest, quiet), "solo") == []


def test_an_empty_model_is_still_a_problem_in_a_dry_run(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave.yaml", """\
        defaults:
          kind: impl
          model: ""
        entries:
          - id: solo
            scope: "one lane"
            prompt: "do the thing"
        """)
    proc = _plan(run_hippo, tmp_project, manifest,
                 _mock(tmp_path, {"answers": EASY, "default": DEFAULT}))
    assert proc.returncode == 2 and "model is required" in proc.stderr


def test_a_dry_run_launches_nothing(tmp_project, tmp_path, run_hippo):
    manifest = _wave(tmp_project)
    capture = tmp_path / "launched.txt"
    proc = _plan(run_hippo, tmp_project, manifest,
                 _mock(tmp_path, {"answers": EASY, "default": DEFAULT}),
                 PATH=_stub(tmp_path, "codex", f'#!/bin/sh\ntouch "{capture}"\n'))
    assert proc.returncode == 0, proc.stderr
    assert not capture.exists()
    assert not (tmp_project / "wave.journal.jsonl").exists()
    assert not (tmp_project / "wave.out").exists(), "no journal, no outdir — nothing ran"
    assert [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"] == []


def test_with_no_hippo_it_says_so_and_still_plans(uninitialized_dir, tmp_path, run_hippo):
    """The silent-no-op rule: no project memory means no priors and no metering rows, not a
    command that refuses."""
    manifest = _wave(uninitialized_dir)
    proc = _plan(run_hippo, uninitialized_dir, manifest,
                 _mock(tmp_path, {"answers": HARD, "default": DEFAULT}))
    assert proc.returncode == 0, proc.stderr
    assert "no .hippo/" in proc.stderr
    assert _row(proc, "solo")[6] == "codex/gpt-6-astra/high"
    assert _row(proc, "solo")[7] == "no evidence"
