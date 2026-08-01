"""The rules that apply to the clerk's output and to nothing else (DESIGN §3.5.6b).

The clerk infers `exec` from a transcript; the launcher reads its own argv. That is the whole
reason these live on one side only — main's writes stay as permissive as they were. The same
line holds for outcomes: the clerk re-judging a judged dispatch is rejected, main's deliberate
re-verdict is not."""

import json
import sys

import pytest

from conftest import REPO_ROOT, read_ledger

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402


def _scribe_output(tmp_path, name, events):
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"worklog": "a turn happened", "events": events},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _run_scribe(run_hippo, cwd, mock, session="s1"):
    return run_hippo(
        ["scribe", "--transcript", str(cwd / "transcript.jsonl"), "--session", session],
        cwd=cwd,
        env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock)},
    )


def _dispatch(exec_="fork/fable/inherit", did="d1"):
    return {"ev": "dispatch", "id": did, "kind": "impl", "exec": exec_, "scope": "x"}


# --------------------------------------------------------------------------
# the rules themselves
# --------------------------------------------------------------------------

def test_the_scribe_may_not_record_a_codex_launch(tmp_project, run_hippo, fake_transcript,
                                                  tmp_path):
    """The wrapper was there when it ran and wrote it from argv. A restatement is either a
    duplicate or a paraphrase of a launch that bypassed the wrapper — the gap is the record."""
    mock = _scribe_output(tmp_path, "codex", [_dispatch("codex/gpt-5.6-sol/high")])
    proc = run_hippo(["scribe", "--transcript", str(fake_transcript), "--session", "s1"],
                     cwd=tmp_project,
                     env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock)})
    assert proc.returncode == 0, proc.stderr

    assert not [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]
    dumps = list((tmp_project / ".hippo" / "failures").glob("*"))
    assert dumps, "a rejected event must survive in failures/"
    assert "does not record codex launches" in "".join(p.read_text() for p in dumps)


def test_what_only_the_scribe_can_see_still_lands(tmp_project, run_hippo, fake_transcript,
                                                  tmp_path):
    """A fork arm has no launcher record by construction — measured, one such arm of a design duo
    existed nowhere else. Killing those would cost more than the duplicates."""
    mock = _scribe_output(tmp_path, "fork", [
        _dispatch("fork/fable/inherit", "dfork"),
        _dispatch("subagent/sonnet/low", "dsub"),
        _dispatch("workflow/fable/xhigh", "dwf"),
    ])
    proc = run_hippo(["scribe", "--transcript", str(fake_transcript), "--session", "s1"],
                     cwd=tmp_project,
                     env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock)})
    assert proc.returncode == 0, proc.stderr
    got = {e["id"] for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"}
    assert got == {"dfork", "dsub", "dwf"}


@pytest.mark.parametrize("bad", [
    "background/CPU/sol-high",      # a launch mechanism in the executor slot
    "bash/unknown/unknown",
    "unknown/GPT-5.6/unknown",
    "fable/unknown/unknown",        # a model in the executor slot
    "fork/fable/unknown",           # effort outside its vocabulary
    "subagent/sonnet/inherited",
])
def test_inferred_junk_is_rejected(tmp_project, run_hippo, fake_transcript, tmp_path, bad):
    """Every one of these was written by a real scribe on a real ledger."""
    mock = _scribe_output(tmp_path, "junk", [_dispatch(bad)])
    proc = run_hippo(["scribe", "--transcript", str(fake_transcript), "--session", "s1"],
                     cwd=tmp_project,
                     env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock)})
    assert proc.returncode == 0, proc.stderr
    assert not [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]


def test_ultra_is_a_real_effort(tmp_project, run_hippo, fake_transcript, tmp_path):
    mock = _scribe_output(tmp_path, "ultra", [_dispatch("claude/opus/ultra", "dultra")])
    proc = run_hippo(["scribe", "--transcript", str(fake_transcript), "--session", "s1"],
                     cwd=tmp_project,
                     env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock)})
    assert proc.returncode == 0, proc.stderr
    assert [e["id"] for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"] == ["dultra"]


def test_one_rejected_event_does_not_erase_the_turn(tmp_project, run_hippo, fake_transcript,
                                                    tmp_path):
    """Per-event isolation (§3.5.6) has to keep holding for the new rule too."""
    mock = _scribe_output(tmp_path, "mixed", [
        _dispatch("codex/gpt-5.6-sol/high", "dbad"),
        _dispatch("fork/fable/inherit", "dgood"),
    ])
    proc = run_hippo(["scribe", "--transcript", str(fake_transcript), "--session", "s1"],
                     cwd=tmp_project,
                     env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock)})
    assert proc.returncode == 0, proc.stderr
    rows = read_ledger(tmp_project)
    assert [e["id"] for e in rows if e.get("ev") == "dispatch"] == ["dgood"]
    assert any("a turn happened" in (tmp_project / ".hippo" / "worklog.md").read_text()
               for _ in [0])


def test_the_scribe_does_not_rejudge(tmp_project, run_hippo, fake_transcript, tmp_path):
    """Measured on this repo's own ledger: a dispatch judged `revised` was re-judged `accepted`
    by two later scribe runs, roster marker notwithstanding. The writer is where the rule holds."""
    run_hippo(["log", "dispatch", "--id", "d9", "--kind", "impl",
               "--exec", "fork/fable/inherit", "--scope", "x"], cwd=tmp_project)
    run_hippo(["log", "outcome", "--ref", "d9", "--result", "revised"], cwd=tmp_project)

    mock = _scribe_output(tmp_path, "rejudge",
                          [{"ev": "outcome", "ref": "d9", "result": "accepted"}])
    proc = _run_scribe(run_hippo, tmp_project, mock)
    assert proc.returncode == 0, proc.stderr

    outs = [e for e in read_ledger(tmp_project)
            if e.get("ev") == "outcome" and e.get("ref") == "d9"]
    assert [e["result"] for e in outs] == ["revised"]
    dumps = list((tmp_project / ".hippo" / "failures").glob("*"))
    assert "does not re-judge" in "".join(p.read_text() for p in dumps)


def test_one_verdict_per_dispatch_holds_within_a_batch(tmp_project, run_hippo, fake_transcript,
                                                       tmp_path):
    """Two verdicts for one ref in a single clerk reply: the first lands, the second is the
    same re-judgment with less distance."""
    mock = _scribe_output(tmp_path, "batch", [
        _dispatch("fork/fable/inherit", "dtwice"),
        {"ev": "outcome", "ref": "dtwice", "result": "accepted"},
        {"ev": "outcome", "ref": "dtwice", "result": "refuted"},
    ])
    proc = _run_scribe(run_hippo, tmp_project, mock)
    assert proc.returncode == 0, proc.stderr
    outs = [e for e in read_ledger(tmp_project)
            if e.get("ev") == "outcome" and e.get("ref") == "dtwice"]
    assert [e["result"] for e in outs] == ["accepted"]


# --------------------------------------------------------------------------
# and none of it applies to main
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ex", [
    "codex/gpt-5.6-sol/high",       # the exact thing the scribe may not write
    "background/CPU/sol-high",      # main is trusted with its own spellings
    "fork/fable/unknown",
])
def test_main_writes_are_untouched(tmp_project, run_hippo, ex):
    """A vocabulary holds where the writer has the value. Main and the wrapper do; measured, the
    wrapper produced 0 malformed exec in 110 dispatches, and `kind` has held with no rule at all.
    Tightening main here would buy nothing and would be enforcement in the sense §4 rejects."""
    proc = run_hippo(["log", "dispatch", "--id", "d1", "--kind", "impl",
                      "--exec", ex, "--scope", "x"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["exec"] == ex


def test_the_rule_is_not_wired_into_the_shared_validator():
    """If it leaked into validate_event it would silently start applying to main."""
    assert hippo_cli.validate_event(_dispatch("codex/gpt-5.6-sol/high")) is None
    assert hippo_cli.validate_scribe_event(_dispatch("codex/gpt-5.6-sol/high")) is not None
