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


def test_task_add_show_roundtrip(tmp_project, run_hippo):
    proc = run_hippo(
        ["task", "add", "feat/roundtrip", "--title", "Round trip check"],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr

    show = run_hippo(["task", "show", "feat/roundtrip"], cwd=tmp_project)
    assert show.returncode == 0, show.stderr
    assert "feat/roundtrip" in show.stdout
    assert "Round trip check" in show.stdout


def test_task_list_json_contains_added_task(tmp_project, run_hippo):
    proc = run_hippo(
        ["task", "add", "feat/listed", "--title", "Listed task"], cwd=tmp_project
    )
    assert proc.returncode == 0, proc.stderr

    listing = run_hippo(["task", "list", "--all", "--json"], cwd=tmp_project)
    assert listing.returncode == 0, listing.stderr
    items = json.loads(listing.stdout)
    ids = {item["id"] for item in items}
    assert "feat/listed" in ids


def test_task_status_multi_comma_filter(tmp_project, run_hippo):
    for tid, title in (
        ("feat/a1", "A1"),
        ("feat/a2", "A2"),
        ("feat/a3", "A3"),
    ):
        proc = run_hippo(["task", "add", tid, "--title", title], cwd=tmp_project)
        assert proc.returncode == 0, proc.stderr

    set_proc = run_hippo(
        ["task", "set", "feat/a2", "status", "active"], cwd=tmp_project
    )
    assert set_proc.returncode == 0, set_proc.stderr

    done_proc = run_hippo(
        ["task", "done", "feat/a3", "--note", "finished"], cwd=tmp_project
    )
    assert done_proc.returncode == 0, done_proc.stderr

    open_view = run_hippo(
        ["task", "list", "--status", "pending,active", "--json"], cwd=tmp_project
    )
    assert open_view.returncode == 0, open_view.stderr
    open_ids = {item["id"] for item in json.loads(open_view.stdout)}
    assert "feat/a1" in open_ids
    assert "feat/a2" in open_ids
    assert "feat/a3" not in open_ids

    done_view = run_hippo(
        ["task", "list", "--status", "done", "--json"], cwd=tmp_project
    )
    assert done_view.returncode == 0, done_view.stderr
    done_ids = {item["id"] for item in json.loads(done_view.stdout)}
    assert done_ids == {"feat/a3"}


def test_task_done_marks_status_done(tmp_project, run_hippo):
    run_hippo(["task", "add", "feat/done-me", "--title", "Finish me"], cwd=tmp_project)

    proc = run_hippo(["task", "done", "feat/done-me"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr

    show = run_hippo(["task", "show", "feat/done-me"], cwd=tmp_project)
    assert show.returncode == 0, show.stderr
    assert "done" in show.stdout


def test_task_drop_visible_only_via_explicit_status_or_all(tmp_project, run_hippo):
    run_hippo(["task", "add", "feat/drop-me", "--title", "Drop me"], cwd=tmp_project)

    proc = run_hippo(["task", "drop", "feat/drop-me"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr

    dropped_view = run_hippo(
        ["task", "list", "--status", "dropped", "--json"], cwd=tmp_project
    )
    assert dropped_view.returncode == 0, dropped_view.stderr
    dropped_ids = {item["id"] for item in json.loads(dropped_view.stdout)}
    assert "feat/drop-me" in dropped_ids

    all_view = run_hippo(["task", "list", "--all", "--json"], cwd=tmp_project)
    assert all_view.returncode == 0, all_view.stderr
    all_ids = {item["id"] for item in json.loads(all_view.stdout)}
    assert "feat/drop-me" in all_ids


def test_task_show_missing_id_fails_with_stderr_usage(tmp_project, run_hippo):
    proc = run_hippo(["task", "show", "feat/does-not-exist"], cwd=tmp_project)
    assert proc.returncode != 0
    # DESIGN.md §3.3: attach the usage to stderr on error
    assert proc.stderr.strip() != ""


def test_task_note_appends_and_set_notes_replaces(tmp_project, run_hippo):
    """`task note` appends one timestamped note and never replaces (measured: 225 of 524
    `task set … notes` calls re-sent over 1,500 chars to add a line, and one lost notes);
    `task set … notes` stays the replace. A hand-edited scalar is kept as the first note."""
    import re

    import yaml

    assert run_hippo(["task", "add", "feat/n", "--title", "N", "--notes", "first"],
                     cwd=tmp_project).returncode == 0
    for text in ("second", "third: `quoted` $HOME"):
        proc = run_hippo(["task", "note", "feat/n", text], cwd=tmp_project)
        assert proc.returncode == 0, proc.stderr

    def notes(tid):
        tasks = yaml.safe_load((tmp_project / ".hippo" / "tasks.yaml").read_text())["tasks"]
        return next(t for t in tasks if t["id"] == tid)["notes"]

    got = notes("feat/n")
    assert got[0] == "first" and len(got) == 3
    stamp = r"\d{4}-\d\d-\d\dT\d\d:\d\dZ "
    assert re.fullmatch(stamp + "second", got[1])
    assert re.fullmatch(stamp + re.escape("third: `quoted` $HOME"), got[2])

    p = tmp_project / ".hippo" / "tasks.yaml"
    data = yaml.safe_load(p.read_text())
    data["tasks"][0]["notes"] = "a hand-written scalar"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert run_hippo(["task", "note", "feat/n", "more"], cwd=tmp_project).returncode == 0
    assert notes("feat/n")[0] == "a hand-written scalar" and len(notes("feat/n")) == 2

    assert run_hippo(["task", "set", "feat/n", "notes", "only"], cwd=tmp_project).returncode == 0
    assert notes("feat/n") == ["only"]
    empty = run_hippo(["task", "note", "feat/n", "  "], cwd=tmp_project)
    assert empty.returncode != 0 and notes("feat/n") == ["only"]


def test_an_unparseable_tasks_yaml_is_one_line_and_status_survives(tmp_project, run_hippo):
    """Measured (steno, 1.17.1): a hand edit left tasks.yaml unparseable, it was committed and
    pushed, and every command printed a raw traceback. Each task command now fails with one
    line naming the file, the place and the parser's reason, and writes nothing; status prints
    what it can — the capsule a session starts with included — plus that line."""
    p = tmp_project / ".hippo" / "tasks.yaml"
    broken = "tasks:\n- id: feat/a\n  title: 'unclosed\n  status: active\n"
    p.write_text(broken, encoding="utf-8")
    assert run_hippo(["directive", "add", "--id", "keep-it", "--text", "a live rule"],
                     cwd=tmp_project).returncode == 0

    for args in (["task", "list"], ["task", "add", "feat/b", "--title", "B"],
                 ["task", "note", "feat/a", "x"]):
        proc = run_hippo(args, cwd=tmp_project)
        assert proc.returncode == 1, args
        assert "Traceback" not in proc.stderr
        (line,) = proc.stderr.strip().splitlines()
        assert str(p) in line and "does not parse at line" in line and "column" in line
    assert p.read_text(encoding="utf-8") == broken, "nothing was saved over it"

    status = run_hippo(["status"], cwd=tmp_project)
    assert status.returncode == 1 and "Traceback" not in status.stderr
    assert status.stdout.startswith("[hippo] tasks 0 open · directives 1 live")
    assert "does not parse at line" in status.stderr

    capsule = run_hippo(["status", "--inject"], cwd=tmp_project, env={"HIPPO_INJECT": "startup"})
    assert "a live rule" in capsule.stdout
    assert capsule.stdout.strip().splitlines()[-1].startswith(f"hippo: {p} does not parse")
