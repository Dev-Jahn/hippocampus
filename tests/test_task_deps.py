"""`deps` had two writers and no reader until 1.7.2 (DESIGN §3.3). These pin the reader down."""

import json

def _add(run_hippo, cwd, tid, deps=None, status=None):
    argv = ["task", "add", tid, "--title", tid]
    if deps:
        argv += ["--deps", ",".join(deps)]
    if status:
        argv += ["--status", status]
    proc = run_hippo(argv, cwd=cwd)
    assert proc.returncode == 0, proc.stderr


def _list(run_hippo, cwd):
    out = run_hippo(["task", "list"], cwd=cwd)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_an_unfinished_dep_is_named(tmp_project, run_hippo):
    _add(run_hippo, tmp_project, "fix/a")
    _add(run_hippo, tmp_project, "gate/b", deps=["fix/a"])
    assert "waiting on: fix/a" in _list(run_hippo, tmp_project)


def test_a_task_with_nothing_in_its_way_stays_one_line(tmp_project, run_hippo):
    _add(run_hippo, tmp_project, "fix/a")
    out = _list(run_hippo, tmp_project)
    assert "waiting on" not in out
    assert len(out.strip().splitlines()) == 1


def test_a_finished_dep_stops_blocking(tmp_project, run_hippo):
    _add(run_hippo, tmp_project, "fix/a")
    _add(run_hippo, tmp_project, "fix/b")
    _add(run_hippo, tmp_project, "gate/c", deps=["fix/a", "fix/b"])
    run_hippo(["task", "done", "fix/a"], cwd=tmp_project)
    out = _list(run_hippo, tmp_project)
    assert "waiting on: fix/b" in out
    assert "fix/a," not in out


def test_a_dropped_dep_stops_blocking(tmp_project, run_hippo):
    """Dropped is a decision, not an omission — it no longer stands in anything's way."""
    _add(run_hippo, tmp_project, "fix/a")
    _add(run_hippo, tmp_project, "gate/b", deps=["fix/a"])
    run_hippo(["task", "drop", "fix/a"], cwd=tmp_project)
    assert "waiting on" not in _list(run_hippo, tmp_project)


def test_a_dep_naming_no_task_is_marked_not_ignored(tmp_project, run_hippo):
    """Same silent shape as a dangling ref: it validates, it sits there, it means nothing."""
    _add(run_hippo, tmp_project, "gate/b", deps=["fix/never-existed"])
    assert "waiting on: fix/never-existed?" in _list(run_hippo, tmp_project)


def test_deps_survive_a_set_and_are_visible_in_json(tmp_project, run_hippo):
    _add(run_hippo, tmp_project, "fix/a")
    _add(run_hippo, tmp_project, "gate/b")
    run_hippo(["task", "set", "gate/b", "deps", "fix/a"], cwd=tmp_project)
    out = run_hippo(["task", "list", "--json"], cwd=tmp_project)
    got = {t["id"]: t.get("deps") for t in json.loads(out.stdout)}
    assert got["gate/b"] == ["fix/a"]
    assert "waiting on: fix/a" in _list(run_hippo, tmp_project)


def test_status_is_never_derived_from_deps(tmp_project, run_hippo):
    """Blocked-by-a-dep and blocked-on-something-outside-the-registry are different facts."""
    _add(run_hippo, tmp_project, "fix/a")
    _add(run_hippo, tmp_project, "gate/b", deps=["fix/a"])
    out = run_hippo(["task", "show", "gate/b", "--json"], cwd=tmp_project)
    assert json.loads(out.stdout)["status"] == "pending"
