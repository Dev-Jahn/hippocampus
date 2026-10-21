"""A codex lane's environment and khala identity (DESIGN §3.6, D18).

Contract under test: every lane launched by `hippo dispatch` — single, batch, a printed resume —
runs without the Claude session variables of the session that launched it (every CLAUDE_CODE_*,
CLAUDECODE, CLAUDE_PID, CLAUDE_EFFORT; other CLAUDE_* configuration passes). With `khala` on
PATH it also gets an identity of its own: KHALA_SESSION = the task's slug (sanitised to khala's
name rule, `lane-<8 hex>` without one, `-2`, `-3` while main or a running lane holds it, the
recorded name on a resume, an inherited value kept only when it is provably not main's own) and
KHALA_PARENT = main's `khala whoami`, omitted with the reason on record when that fails. The
name rides the launch line, the lane record and the ev:dispatch row, and a lane worktree gets a
`.khala-codex-session` that is never overwritten. With no khala, nothing else changes.

Fakes only: the real khala is hidden from every test process (conftest), the fake one answers
`whoami` alone, and the stub codex dumps its environment.
"""
import json
import os
import subprocess
import sys

import pytest

from conftest import HIPPO_BIN, REPO_ROOT, read_ledger
from test_batch import _batch, _manifest

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402

DID = "d" + "1a2b3c4d" + "0" * 24
SID = "01a0f82c-1211-7261-8897-3c26f9bdc737"
MAIN = ("main@mbp", None)
# What a Claude Code session's Bash calls carry (measured, Claude Code 2.1.304) — the session's
# own, every one. The first five were fff293c's list; the rest reached a real lane past it.
CLAUDE = {"CLAUDE_CODE_SESSION_ID": "c0ffee", "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc.sock",
          "CLAUDE_CODE_MESSAGING_TOKEN": "tok", "CLAUDECODE": "1", "CLAUDE_PID": "4242",
          "CLAUDE_CODE_BRIDGE_SESSION_ID": "session_01", "CLAUDE_CODE_ENTRYPOINT": "cli",
          "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_SESSION_ATTENDED": "1",
          "CLAUDE_CODE_EXECPATH": "/x/claude/versions/2.1.304",
          "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1", "CLAUDE_EFFORT": "xhigh"}

# A fake khala: `whoami` prints $FAKE_WHOAMI (main@mbp by default) and exits $FAKE_RC; every
# call is logged, so a test sees hippo asked nothing else.
FAKE_KHALA = """#!/bin/sh
echo "$*" >> "$FAKE_KHALA_CALLS"
[ "$1" = whoami ] || exit 64
printf '%s\\n' "${FAKE_WHOAMI-main@mbp}"
exit "${FAKE_RC-0}"
"""
# A khala from before `whoami` existed.
OLD_KHALA = """#!/bin/sh
echo "$*" >> "$FAKE_KHALA_CALLS"
echo "khala: unknown command: $1" >&2
exit 2
"""
# A codex that dumps its environment to $ENV_DIR/<its dispatch id>.json and, given
# $STUB_SESSION, prints the banner's session id as codex does (on stderr).
STUB_CODEX = """#!/usr/bin/env python3
import json, os, sys, time
env = dict(os.environ)
with open(os.path.join(env["ENV_DIR"], env["HIPPO_DISPATCH"] + ".json"), "w") as f:
    json.dump(env, f)
if env.get("STUB_SESSION"):
    sys.stderr.write("session id: " + env["STUB_SESSION"] + "\\n")
time.sleep(float(env.get("STUB_SLEEP", "0")))
print("done")
"""


def _bin(tmp_path, khala=None):
    """PATH with the stub codex — and a fake khala when given — first."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for name, body in (("codex", STUB_CODEX), ("khala", khala)):
        if body is not None:
            (d / name).write_text(body, encoding="utf-8")
            (d / name).chmod(0o755)
    return f"{d}{os.pathsep}{os.environ['PATH']}"


def _env(tmp_path, khala=None, **extra):
    (tmp_path / "envs").mkdir(exist_ok=True)
    return {**os.environ, "PATH": _bin(tmp_path, khala), "HIPPO_DISPATCH": "",
            "ENV_DIR": str(tmp_path / "envs"), "FAKE_KHALA_CALLS": str(tmp_path / "khala.calls"),
            **extra}


def _dispatch(project, env, *args, task="feat/khala-lane"):
    return subprocess.run([str(HIPPO_BIN), "dispatch", "--kind", "impl", "--scope", "s",
                           *(["--task", task] if task else []), *args],
                          cwd=project, env=env, capture_output=True, text=True, timeout=60)


def _child_env(tmp_path, did):
    return json.loads((tmp_path / "envs" / f"{did}.json").read_text(encoding="utf-8"))


def _did(proc):
    return proc.stdout.splitlines()[0].removeprefix("dispatch:")


def _record(project, did):
    return json.loads((project / ".hippo" / "lanes" / f"{did}.json").read_text("utf-8"))


def _row(project, did):
    return next(e for e in read_ledger(project) if e.get("ev") == "dispatch" and e["id"] == did)


def _lane(task="feat/x", base=None, main=MAIN, args=(), workdir="/", ld=None, did=DID):
    # The default workdir is no worktree, so no .khala-codex-session is written anywhere.
    return hippo_cli.khala_lane(main, base or {}, did, task, list(args), workdir, ld)


def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


# --- the name ---------------------------------------------------------------------------


@pytest.mark.parametrize("task, name", [
    ("design/d18-subagent-inbound", "d18-subagent-inbound"),
    ("feat/Khala Lane_Identity!!", "khala-lane-identity"),
    ("feat/--a..b--", "a-b"),
    ("fix/a/b", "a-b"),
    ("plain", "plain"),
    ("feat/", "lane-1a2b3c4d"),
    ("", "lane-1a2b3c4d"),
    ("feat/한글", "lane-1a2b3c4d"),
    ("feat/khala", "lane-1a2b3c4d"),
    ("feat/User", "lane-1a2b3c4d"),
])
def test_the_name_is_the_task_slug_under_khalas_name_rule(task, name):
    kh = _lane(task)
    assert kh["name"] == name and kh["khala"] == f"{name}@mbp"
    assert hippo_cli.khala_name_ok(name)


def test_a_name_main_or_a_running_lane_holds_gets_a_suffix_and_each_claim_counts(tmp_path):
    ld = tmp_path / "lanes"
    ld.mkdir()
    for did, khala, extra in (("d01", "x@mbp", {}), ("d02", "x-2@mbp", {}),
                              ("d03", "y@mbp", {"ended": "2026-10-21T00:00:00Z"}),
                              ("d04", "z@mbp", {"pid": _dead_pid()})):
        (ld / f"{did}.json").write_text(json.dumps(
            {"id": did, "pid": os.getpid(), "khala": khala, **extra}))
    assert _lane("feat/x", ld=ld)["name"] == "x-3"
    assert _lane("feat/y", ld=ld)["name"] == "y", "an ended lane holds nothing"
    assert _lane("feat/z", ld=ld)["name"] == "z", "nor a lost one (its wrapper is gone)"
    assert _lane("feat/main", ld=ld)["name"] == "main-2", "main's own name is never a lane's"
    # The record claims the name before the lock goes: the next lane starting sees it.
    first, second = (_lane("feat/w", ld=ld, did=d) for d in ("d" + "a" * 32, "d" + "b" * 32))
    assert (first["name"], second["name"]) == ("w", "w-2")
    assert json.loads((ld / f"d{'a' * 32}.json").read_text())["khala"] == "w@mbp"


def test_a_resume_reuses_the_name_its_lane_was_recorded_with(tmp_path):
    ld = tmp_path / "lanes"
    ld.mkdir()
    (ld / "d01.json").write_text(json.dumps({"id": "d01", "pid": _dead_pid(), "khala": "x-2@mbp",
                                             "codex_session": SID, "ended": "2026-10-21T01:00:00Z",
                                             "started": "2026-10-21T00:00:00Z"}))
    resume = ["-m", "gpt-6-luna", "-C", "/w", "resume", SID, "Continue the same task."]
    assert _lane("feat/x", args=resume, ld=ld)["name"] == "x-2"
    assert _lane("feat/x", args=["resume", "-c", "a=b", SID, "go on"], ld=ld)["name"] == "x-2"
    other = ["resume", "01a0f82c-0000-0000-0000-000000000000", "go on"]
    assert _lane("feat/x", args=other, ld=ld)["name"] == "x", "another session: a fresh name"


@pytest.mark.parametrize("held, main, name, note", [
    ("special", MAIN, "special", None),  # the caller's choice for this lane
    ("main", MAIN, "x", None),  # main's own name, inherited: never the lane's
    ("special", (None, "`khala whoami` exited 1 (no identity)"), "x",
     "KHALA_SESSION=special may be main's own name"),
    ("Bad Name", MAIN, "x", "is no khala name"),
    ("operator", MAIN, "x", "is no khala name"),
])
def test_an_inherited_khala_session_wins_only_when_provably_not_mains(held, main, name, note):
    kh = _lane(base={"KHALA_SESSION": held}, main=main)
    assert kh["name"] == name
    assert (note is None and "khala_note" not in kh) or note in kh["khala_note"]


def test_a_chosen_name_is_one_lane_s_only(tmp_path):
    """A batch's lanes share the environment that launched them: a chosen name goes to the
    first, the next gets `-2` — two running lanes never answer to one name."""
    ld = tmp_path / "lanes"
    ld.mkdir()
    base = {"KHALA_SESSION": "special"}
    names = [_lane(base=base, ld=ld, did=f"d{c * 32}")["name"] for c in "ab"]
    assert names == ["special", "special-2"]


# --- the parent -------------------------------------------------------------------------


def _main(tmp_path, khala, **extra):
    return hippo_cli.khala_main(_env(tmp_path, khala, **extra))


def test_the_parent_is_mains_whoami_or_nothing_with_the_reason(tmp_path):
    assert _main(tmp_path, FAKE_KHALA) == ("main@mbp", None)
    (tmp_path / "old").mkdir()
    old = _main(tmp_path / "old", OLD_KHALA)
    assert old[0] is None and "exited 2 (khala: unknown command: whoami)" in old[1]
    failed = _main(tmp_path, FAKE_KHALA, FAKE_RC="1", FAKE_WHOAMI="")
    assert failed[0] is None and "exited 1" in failed[1]
    odd = _main(tmp_path, FAKE_KHALA, FAKE_WHOAMI="main")
    assert odd[0] is None and "printed no <identity>@<node> ('main')" in odd[1]
    calls = (tmp_path / "khala.calls").read_text().split()
    assert set(calls) == {"whoami"}, "hippo asks khala one thing"
    assert hippo_cli.khala_main({"PATH": str(tmp_path / "nowhere")}) is None


def test_lane_env_scrubs_claude_and_sets_the_identity(tmp_path):
    base = {**CLAUDE, "HOME": "/h", "KHALA_SESSION": "main", "KHALA_PARENT": "boss@mbp"}
    assert hippo_cli.lane_env(base) == {"HOME": "/h", "KHALA_SESSION": "main",
                                        "KHALA_PARENT": "boss@mbp"}
    assert hippo_cli.lane_env(base, _lane(base=base)) == {
        "HOME": "/h", "KHALA_SESSION": "x", "KHALA_PARENT": "main@mbp"}
    unknown = _lane(base=base, main=(None, "`khala whoami` exited 2 (old)"))
    assert hippo_cli.lane_env(base, unknown) == {"HOME": "/h", "KHALA_SESSION": "x"}, \
        "an inherited KHALA_PARENT names main's own parent: dropped, never passed on"
    assert "no KHALA_PARENT: `khala whoami` exited 2 (old)" in unknown["khala_note"]


def test_every_claude_code_variable_leaves_by_its_prefix_and_configuration_stays():
    """The rule is the prefix, not a list: a CLAUDE_CODE_* name no list has yet leaves too,
    while CLAUDE_* configuration — and a name that only looks close — passes as it was."""
    kept = {"CLAUDE_CONFIG_DIR": "/c", "CLAUDE_PLUGIN_ROOT": "/p", "CLAUDE_PROJECT_DIR": "/d",
            "CLAUDE_CODEX": "x", "MY_CLAUDE_CODE_X": "y", "HOME": "/h"}
    base = {**CLAUDE, "CLAUDE_CODE_NOT_YET_SHIPPED": "1", **kept}
    assert hippo_cli.lane_env(base) == kept


# --- .khala-codex-session ---------------------------------------------------------------


def test_the_session_file_goes_only_into_a_lane_worktree_and_is_never_overwritten(tmp_path):
    main = tmp_path / "repo"
    wt = main / ".claude" / "worktrees" / "w"
    (wt / "sub").mkdir(parents=True)
    for d in (main, main / ".claude" / "worktrees"):
        assert _lane(workdir=d).get("khala_note") is None
        assert not (d / ".khala-codex-session").exists(), "never the main checkout's"
    _lane(workdir=wt)
    _lane(workdir=wt / "sub")  # codex's cwd is where khala-codex reads it
    assert (wt / ".khala-codex-session").read_text() == "x\n"
    assert (wt / "sub" / ".khala-codex-session").read_text() == "x\n"
    assert "khala_note" not in _lane(workdir=wt), "the same name: nothing to say"
    kh = _lane("feat/y", workdir=wt)
    assert (wt / ".khala-codex-session").read_text() == "x\n"
    assert "names 'x', not y — left as it is" in kh["khala_note"]


def _git(*args, cwd):
    """git for a throwaway repository, blind to the user's own config (signing, hooks)."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          env=env, check=True, capture_output=True, text=True).stdout


def _repo_with_worktree(root):
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "base", cwd=repo)
    wt = repo / ".claude" / "worktrees" / "w"
    _git("worktree", "add", "-q", str(wt), "-b", "w", cwd=repo)
    return repo, wt


def test_the_session_file_is_kept_out_of_a_lane_s_commits(tmp_path):
    """The line goes into the repository's own info/exclude — shared by its worktrees, never
    tracked — created when missing, added once, on a line of its own; .gitignore is untouched."""
    repo, wt = _repo_with_worktree(tmp_path / "a")
    info = repo / ".git" / "info"
    for p in info.iterdir():
        p.unlink()
    info.rmdir()  # no info/ at all: it is created
    assert "khala_note" not in _lane(workdir=wt)
    assert "khala_note" not in _lane(workdir=wt)
    assert (info / "exclude").read_text() == ".khala-codex-session\n", "once, however often"
    assert (wt / ".khala-codex-session").read_text() == "x\n"
    assert _git("status", "--porcelain", "--untracked-files=all", cwd=wt) == ""
    _git("add", "-A", cwd=wt)
    assert _git("diff", "--cached", "--name-only", cwd=wt) == "", "git add -A takes nothing"
    assert not list(tmp_path.rglob(".gitignore"))

    repo, wt = _repo_with_worktree(tmp_path / "b")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("# mine\n.khala-codex-session-old")  # no final newline; a near miss
    _lane(workdir=wt)
    assert exclude.read_text() == "# mine\n.khala-codex-session-old\n.khala-codex-session\n"

    repo, wt = _repo_with_worktree(tmp_path / "c")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.unlink()
    exclude.mkdir()  # cannot be written: a note, never an error
    kh = _lane(workdir=wt)
    assert (wt / ".khala-codex-session").read_text() == "x\n"
    assert "exclude not updated" in kh["khala_note"]


def test_watch_s_final_lines_carry_the_lane_s_address_when_it_has_one():
    rec = {"id": "d1", "scope": "s", "status": "exited", "rc": 0, "cmds": 0, "log": "/l",
           "started": "2026-10-21T00:00:00Z", "ended": "2026-10-21T00:01:00Z"}
    lines = hippo_cli.watch_lines
    assert not [ln for ln in lines(rec, "ended") if ln.startswith("khala")]
    assert "khala: x@mbp" in lines({**rec, "khala": "x@mbp"}, "ended")
    assert "khala: x@mbp · parent main@mbp" in lines(
        {**rec, "khala": "x@mbp", "khala_parent": "main@mbp"}, "ended")


# --- end to end -------------------------------------------------------------------------


def test_a_single_lane_runs_as_itself_with_main_as_its_parent(tmp_project, tmp_path):
    wt = tmp_project / ".claude" / "worktrees" / "w"
    wt.mkdir(parents=True)
    env = _env(tmp_path, FAKE_KHALA, **CLAUDE, KHALA_SESSION="main")
    proc = _dispatch(tmp_project, env, "-C", str(wt), "go")
    assert proc.returncode == 0, proc.stderr
    did = _did(proc)
    child = _child_env(tmp_path, did)
    assert (child["KHALA_SESSION"], child["KHALA_PARENT"]) == ("khala-lane", "main@mbp")
    assert not set(CLAUDE) & set(child)
    assert "lane s · 0s · khala: khala-lane@mbp · parent main@mbp" in proc.stderr.splitlines()
    row, rec = _row(tmp_project, did), _record(tmp_project, did)
    assert (row["khala"], row["khala_parent"]) == ("khala-lane@mbp", "main@mbp")
    assert (rec["khala"], rec["khala_parent"]) == ("khala-lane@mbp", "main@mbp")
    assert (wt / ".khala-codex-session").read_text() == "khala-lane\n"
    watch = subprocess.run([str(HIPPO_BIN), "dispatch", "--watch", did, "--for", "0"],
                           cwd=tmp_project, env=env, capture_output=True, text=True, timeout=30)
    assert watch.returncode == 0, watch.stderr
    assert "khala: khala-lane@mbp · parent main@mbp" in watch.stdout.splitlines(), \
        "the relay's reply is these lines, verbatim"


def test_with_whoami_missing_the_lane_has_no_parent_and_says_why(tmp_project, tmp_path):
    proc = _dispatch(tmp_project, _env(tmp_path, OLD_KHALA), "go", task="")
    assert proc.returncode == 0, proc.stderr
    did = _did(proc)
    child, rec, row = _child_env(tmp_path, did), _record(tmp_project, did), _row(tmp_project, did)
    assert child["KHALA_SESSION"] == f"lane-{did[1:9]}" and "KHALA_PARENT" not in child
    assert rec["khala"] == row["khala"] == f"lane-{did[1:9]}", "no node is known either"
    assert "khala_parent" not in rec and "khala_parent" not in row
    assert "no KHALA_PARENT: `khala whoami` exited 2" in rec["khala_note"]
    assert f"khala: lane-{did[1:9]} · no parent — `khala whoami` exited 2" in proc.stderr


def test_without_khala_only_the_claude_variables_leave(tmp_project, tmp_path):
    kept = {"KHALA_SESSION": "main", "KHALA_PARENT": "boss@mbp", "LANE_MARK": "a b\nc",
            "CLAUDE_PROJECT_DIR": "/p", "CLAUDE_CONFIG_DIR": "/c"}
    env = _env(tmp_path, None, **CLAUDE, **kept)
    wt = tmp_project / ".claude" / "worktrees" / "w"
    wt.mkdir(parents=True)
    proc = _dispatch(tmp_project, env, "-C", str(wt), "go")
    assert proc.returncode == 0, proc.stderr
    did = _did(proc)
    child = _child_env(tmp_path, did)
    assert {k: child.get(k) for k in kept} == kept, "the rest passes as it did"
    assert not set(CLAUDE) & set(child)
    assert {k for k in child if k.startswith("KHALA")} == {"KHALA_SESSION", "KHALA_PARENT"}
    assert "khala" not in _row(tmp_project, did) and "khala" not in _record(tmp_project, did)
    assert "khala:" not in proc.stderr and not (wt / ".khala-codex-session").exists()


def test_a_printed_resume_runs_under_the_lanes_recorded_name(tmp_project, tmp_path):
    lanes = tmp_project / ".hippo" / "lanes"
    lanes.mkdir(exist_ok=True)
    holder = lanes / "dholder.json"  # a running lane named x: the first lane becomes x-2
    holder.write_text(json.dumps({"id": "dholder", "pid": os.getpid(), "khala": "x@mbp"}))
    env = _env(tmp_path, FAKE_KHALA, STUB_SESSION=SID)
    first = _dispatch(tmp_project, env, "go", task="feat/x")
    assert first.returncode == 0, first.stderr
    assert _child_env(tmp_path, _did(first))["KHALA_SESSION"] == "x-2"
    holder.unlink()  # x is free now, and a fresh lane would take it
    again = _dispatch(tmp_project, env, "--", "-C", str(tmp_project), "resume", SID,
                      "Continue the same task.", task="feat/x")
    assert again.returncode == 0, again.stderr
    assert _child_env(tmp_path, _did(again))["KHALA_SESSION"] == "x-2"
    assert _record(tmp_project, _did(again))["khala"] == "x-2@mbp"


def test_batch_lanes_of_one_task_get_distinct_names_and_no_claude_variables(
        tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "m.yaml", """\
        concurrency: 2
        defaults: {kind: impl, executor: codex, model: gpt-6-luna, effort: low, task: feat/x}
        entries:
          - {id: a, scope: "lane a", prompt: "do a"}
          - {id: b, scope: "lane b", prompt: "do b"}
        """)
    env = _env(tmp_path, FAKE_KHALA, **CLAUDE, STUB_SLEEP="1")
    proc = _batch(run_hippo, tmp_project, manifest, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    children = [json.loads(p.read_text()) for p in (tmp_path / "envs").iterdir()]
    assert sorted(c["KHALA_SESSION"] for c in children) == ["x", "x-2"]
    assert all(c["KHALA_PARENT"] == "main@mbp" and not set(CLAUDE) & set(c) for c in children)
    rows = [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]
    assert sorted(r["khala"] for r in rows) == ["x-2@mbp", "x@mbp"]
    assert sum("· khala: x" in ln for ln in proc.stderr.splitlines()) == 2
