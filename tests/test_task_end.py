"""Open tasks that look ended (DESIGN §3.5.9) and the capsule's `check:` line (§6).

The judge is the mock backend throughout (conftest keeps it off otherwise): stage 1 answers
`done_{i}` per open task, every recheck answers the one id `done`, and the scribe gate's five
questions ride along in the same scribe run as a request of their own.
"""
import json

import pytest
import yaml

from conftest import REPO_ROOT, cursors_path, read_ledger, worklog_path

GATE_IDS = list(yaml.safe_load(
    (REPO_ROOT / "clerks" / "jev" / "scribe-gate.yaml").read_text(encoding="utf-8"))["questions"])
LINE_ONE = "· check: {} looks finished or abandoned — close it, or note what is left"
LINE_MANY = "· check: {} look finished or abandoned — close them, or note what is left"


def _mock(tmp_path, answers=None, default=0.1, name="jev.json"):
    payload = {"answers": {q: {"noul": p} for q, p in (answers or {}).items()}}
    if default is not None:
        payload["default"] = {"noul": default}
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _scribe(run_hippo, project, transcript, session, clerk, jev=None, capture=None, **env):
    env = {"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(clerk),
           "HIPPO_JEV_BACKEND": "mock", **env}
    if jev is not None:
        env["HIPPO_JEV_MOCK_OUTPUT"] = str(jev)
    if capture is not None:
        env["HIPPO_JEV_MOCK_CAPTURE"] = str(capture)
    return run_hippo(["scribe", "--transcript", str(transcript), "--session", session],
                     cwd=project, env=env)


def _rows(project, name):
    return [e for e in read_ledger(project) if e.get("ev") == "clerk" and e.get("name") == name]


def _flags(project):
    p = project / ".hippo" / "task-flags.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _tasks(run_hippo, project, *ids):
    for tid in ids:
        proc = run_hippo(["task", "add", tid, "--title", f"build {tid}", "--notes", "stage 1 of 2"],
                         cwd=project)
        assert proc.returncode == 0, proc.stderr


def _capsule(run_hippo, project, **env):
    proc = run_hippo(["status", "--inject"], cwd=project, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def _checks(lines):
    return [ln for ln in lines if ln.startswith("· check:")]


def test_without_the_key_nothing_is_asked_and_the_capsule_is_unchanged(
    tmp_project, run_hippo, fake_transcript, valid_mock_output
):
    """The opt-in rule (§3.9): TYPESAFE_API_KEY's presence alone — no key, no request, no row,
    no file, and a capsule of the lines it always had."""
    _tasks(run_hippo, tmp_project, "feat/a")
    proc = _scribe(run_hippo, tmp_project, fake_transcript, "s-nokey", valid_mock_output,
                   HIPPO_JEV_BACKEND="", TYPESAFE_API_KEY="")
    assert proc.returncode == 0, proc.stderr

    assert [e["name"] for e in read_ledger(tmp_project) if e.get("ev") == "clerk"] == ["turn-scribe"]
    assert _flags(tmp_project) is None
    lines = _capsule(run_hippo, tmp_project)
    assert lines[0].startswith("[hippo] tasks 1 open"), lines
    assert [ln.split(":")[0] for ln in lines[1:]] == ["· last", "· cli"], lines


@pytest.mark.parametrize("recheck, flagged", [(0.7, ["feat/a", "feat/b"]), (0.69, None)])
def test_a_flag_rests_on_the_recheck_alone(
    tmp_project, run_hippo, fake_transcript, valid_mock_output, tmp_path, recheck, flagged
):
    """Stage 1 only picks what is asked again (at or over 0.5); the flag is the recheck at or
    over 0.7, asked with that one task alone beside the digest, in a request of its own."""
    _tasks(run_hippo, tmp_project, "feat/a", "feat/b", "feat/c")
    jev = _mock(tmp_path, {"done_0": 0.9, "done_1": 0.5, "done_2": 0.49, "done": recheck})
    capture = tmp_path / "capture.json"
    proc = _scribe(run_hippo, tmp_project, fake_transcript, "s-two", valid_mock_output, jev, capture)
    assert proc.returncode == 0, proc.stderr

    assert len(_rows(tmp_project, "jev-task-end")) == 3, "stage 1 and the two rechecks, no more"
    assert all(e["ok"] and e["src"] == "scribe" for e in _rows(tmp_project, "jev-task-end"))
    last = json.loads(capture.read_text(encoding="utf-8"))  # the recheck of feat/b
    assert list(last["questions"]) == ["done"]
    assert set(last["state"]) == {"digest", "task"}
    assert last["state"]["task"] == {"id": "feat/b", "title": "build feat/b",
                                     "notes": ["stage 1 of 2"]}
    flags = _flags(tmp_project)
    assert (sorted(flags) if flags is not None else None) == flagged
    if flagged:
        assert _checks(_capsule(run_hippo, tmp_project)) == [LINE_MANY.format("feat/a, feat/b")]


def test_a_flag_goes_quiet_once_the_task_is_touched_or_closed(tmp_project, run_hippo):
    """Derived at every read, never deleted: a write to the task after the flag (its `updated`
    stamp moves past it) or the task no longer open hides it, and the file is left alone."""
    hp = tmp_project / ".hippo"
    old = "2026-09-01T00:00:00Z"
    (hp / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": tid, "title": tid, "status": "active", "notes": [], "deps": [], "updated": old}
        for tid in ("feat/a", "feat/b", "feat/c")]}), encoding="utf-8")
    text = json.dumps({tid: {"t": "2026-09-02T00:00:00Z", "p": 0.9}
                       for tid in ("feat/a", "feat/b", "feat/c", "feat/gone")})
    (hp / "task-flags.json").write_text(text, encoding="utf-8")

    assert _checks(_capsule(run_hippo, tmp_project)) == [LINE_MANY.format("feat/a, feat/b, feat/c")]
    assert run_hippo(["task", "set", "feat/a", "notes", "stage 2 left"], cwd=tmp_project).returncode == 0
    assert _checks(_capsule(run_hippo, tmp_project)) == [LINE_MANY.format("feat/b, feat/c")]
    assert run_hippo(["task", "done", "feat/b"], cwd=tmp_project).returncode == 0
    assert _checks(_capsule(run_hippo, tmp_project)) == [LINE_ONE.format("feat/c")]
    assert (hp / "task-flags.json").read_text(encoding="utf-8") == text


def test_the_check_line_is_main_s_at_every_session_start(tmp_project, run_hippo):
    """Every SessionStart source carries it, before the grammar; a subagent's slice and a lane's
    capsule never do — closing a task is main's call."""
    _tasks(run_hippo, tmp_project, "feat/a")
    assert run_hippo(["directive", "add", "--id", "worker-rule", "--audience", "executor",
                      "--text", "never push"], cwd=tmp_project).returncode == 0
    (tmp_project / ".hippo" / "task-flags.json").write_text(
        json.dumps({"feat/a": {"t": "2999-01-01T00:00:00Z", "p": 0.9}}), encoding="utf-8")

    for source in ("startup", "resume", "clear", "compact"):
        lines = _capsule(run_hippo, tmp_project, HIPPO_INJECT=source)
        assert _checks(lines) == [LINE_ONE.format("feat/a")], source
        assert lines.index(_checks(lines)[0]) < next(
            i for i, ln in enumerate(lines) if ln.startswith("· cli:")), source
    subagent = _capsule(run_hippo, tmp_project, HIPPO_INJECT="subagent")
    assert subagent and _checks(subagent) == [], subagent
    lane = _capsule(run_hippo, tmp_project, HIPPO_DISPATCH="d1")
    assert lane and _checks(lane) == [], lane


def test_two_sessions_scribes_keep_each_other_s_flags(
    tmp_project, run_hippo, fake_transcript, valid_mock_output, tmp_path
):
    """Every writer is a scribe holding scribe.lock, and each one merges into what the last one
    wrote: a session whose window reads a task low does not erase another session's flag."""
    _tasks(run_hippo, tmp_project, "feat/a", "feat/b")
    other = tmp_path / "other.jsonl"
    other.write_text(fake_transcript.read_text(encoding="utf-8"), encoding="utf-8")
    first = _mock(tmp_path, {"done_0": 0.9, "done": 0.9}, name="first.json")
    second = _mock(tmp_path, {"done_1": 0.9, "done": 0.9}, name="second.json")
    assert _scribe(run_hippo, tmp_project, fake_transcript, "s-1", valid_mock_output,
                   first).returncode == 0
    assert _scribe(run_hippo, tmp_project, other, "s-2", valid_mock_output,
                   second).returncode == 0

    assert sorted(_flags(tmp_project)) == ["feat/a", "feat/b"]


def test_a_failed_judge_leaves_the_clerk_path_untouched(
    tmp_project, run_hippo, fake_transcript, valid_mock_output, tmp_path
):
    """A request that fails is metered, named on stderr and flags nothing; the gate, the clerk,
    the worklog and the cursor are what they would have been."""
    _tasks(run_hippo, tmp_project, "feat/a")
    capture = tmp_path / "capture.json"
    gate_only = _mock(tmp_path, {q: 0.3 for q in GATE_IDS}, default=None)
    proc = _scribe(run_hippo, tmp_project, fake_transcript, "s-fail", valid_mock_output,
                   gate_only, capture)
    assert proc.returncode == 0, proc.stderr

    assert "jev-task-end: mock: no answer for done_0" in proc.stderr
    assert [e["ok"] for e in _rows(tmp_project, "jev-task-end")] == [False]
    assert [e["ok"] for e in _rows(tmp_project, "jev-gate")] == [True]
    assert [e["ok"] for e in _rows(tmp_project, "turn-scribe")] == [True]
    assert [e["id"] for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"] == ["d100"]
    assert "test dummy work finished" in worklog_path(tmp_project).read_text(encoding="utf-8")
    assert json.loads(cursors_path(tmp_project).read_text(encoding="utf-8"))["s-fail"] > 0
    stage1 = json.loads(capture.read_text(encoding="utf-8"))
    assert list(stage1["questions"]) == ["done_0"], "its own request, never the gate's"
    assert set(stage1["state"]) == {"digest", "tasks"}
    assert _flags(tmp_project) is None

    # a recheck that fails flags nothing either
    no_recheck = _mock(tmp_path, {**{q: 0.3 for q in GATE_IDS}, "done_0": 0.9}, default=None,
                       name="no-recheck.json")
    other = tmp_path / "other.jsonl"
    other.write_text(fake_transcript.read_text(encoding="utf-8"), encoding="utf-8")
    proc = _scribe(run_hippo, tmp_project, other, "s-fail-2", valid_mock_output, no_recheck)
    assert proc.returncode == 0, proc.stderr
    assert [e["ok"] for e in _rows(tmp_project, "jev-task-end")] == [False, True, False]
    assert _flags(tmp_project) is None
