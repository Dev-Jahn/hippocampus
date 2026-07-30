"""Item (1): task add/list(--status multi-comma)/done/show/drop roundtrip.

DESIGN.md §3.3 locks the subcommand shapes:
    task add <id> --title T [--status pending] [--notes N] [--deps a,b]
    task set <id> <field> <value>
    task done <id> [--note N]
    task list [--status s1,s2] [--all] [--json]
    task show <id> | task drop <id>
states: pending|active|done|dropped

DESIGN.md does not lock what the *default* (no --status/--all) list view
filters to, so these tests only assert behavior via explicit --status/--all,
never the bare default view.
"""
import json


def test_task_add_show_roundtrip(tmp_project, run_waystone):
    proc = run_waystone(
        ["task", "add", "feat/roundtrip", "--title", "Round trip check"],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr

    show = run_waystone(["task", "show", "feat/roundtrip"], cwd=tmp_project)
    assert show.returncode == 0, show.stderr
    assert "feat/roundtrip" in show.stdout
    assert "Round trip check" in show.stdout


def test_task_list_json_contains_added_task(tmp_project, run_waystone):
    proc = run_waystone(
        ["task", "add", "feat/listed", "--title", "Listed task"], cwd=tmp_project
    )
    assert proc.returncode == 0, proc.stderr

    listing = run_waystone(["task", "list", "--all", "--json"], cwd=tmp_project)
    assert listing.returncode == 0, listing.stderr
    items = json.loads(listing.stdout)
    ids = {item["id"] for item in items}
    assert "feat/listed" in ids


def test_task_status_multi_comma_filter(tmp_project, run_waystone):
    for tid, title in (
        ("feat/a1", "A1"),
        ("feat/a2", "A2"),
        ("feat/a3", "A3"),
    ):
        proc = run_waystone(["task", "add", tid, "--title", title], cwd=tmp_project)
        assert proc.returncode == 0, proc.stderr

    set_proc = run_waystone(
        ["task", "set", "feat/a2", "status", "active"], cwd=tmp_project
    )
    assert set_proc.returncode == 0, set_proc.stderr

    done_proc = run_waystone(
        ["task", "done", "feat/a3", "--note", "finished"], cwd=tmp_project
    )
    assert done_proc.returncode == 0, done_proc.stderr

    open_view = run_waystone(
        ["task", "list", "--status", "pending,active", "--json"], cwd=tmp_project
    )
    assert open_view.returncode == 0, open_view.stderr
    open_ids = {item["id"] for item in json.loads(open_view.stdout)}
    assert "feat/a1" in open_ids
    assert "feat/a2" in open_ids
    assert "feat/a3" not in open_ids

    done_view = run_waystone(
        ["task", "list", "--status", "done", "--json"], cwd=tmp_project
    )
    assert done_view.returncode == 0, done_view.stderr
    done_ids = {item["id"] for item in json.loads(done_view.stdout)}
    assert done_ids == {"feat/a3"}


def test_task_done_marks_status_done(tmp_project, run_waystone):
    run_waystone(["task", "add", "feat/done-me", "--title", "Finish me"], cwd=tmp_project)

    proc = run_waystone(["task", "done", "feat/done-me"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr

    show = run_waystone(["task", "show", "feat/done-me"], cwd=tmp_project)
    assert show.returncode == 0, show.stderr
    assert "done" in show.stdout


def test_task_drop_visible_only_via_explicit_status_or_all(tmp_project, run_waystone):
    run_waystone(["task", "add", "feat/drop-me", "--title", "Drop me"], cwd=tmp_project)

    proc = run_waystone(["task", "drop", "feat/drop-me"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr

    dropped_view = run_waystone(
        ["task", "list", "--status", "dropped", "--json"], cwd=tmp_project
    )
    assert dropped_view.returncode == 0, dropped_view.stderr
    dropped_ids = {item["id"] for item in json.loads(dropped_view.stdout)}
    assert "feat/drop-me" in dropped_ids

    all_view = run_waystone(["task", "list", "--all", "--json"], cwd=tmp_project)
    assert all_view.returncode == 0, all_view.stderr
    all_ids = {item["id"] for item in json.loads(all_view.stdout)}
    assert "feat/drop-me" in all_ids


def test_task_show_missing_id_fails_with_stderr_usage(tmp_project, run_waystone):
    proc = run_waystone(["task", "show", "feat/does-not-exist"], cwd=tmp_project)
    assert proc.returncode != 0
    # DESIGN.md §3.3: "오류 시 usage를 stderr에 동봉"
    assert proc.stderr.strip() != ""
