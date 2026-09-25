"""The hippo writes a subagent made, recorded for main to confirm or undo (DESIGN §3.5.3d, §6).

A native worker's shell carries main's environment (every agent kind, measured on Claude Code
2.1.282), so its `hippo task done` lands as main's. The scribe reads each run's own transcript
afterwards, keeps the calls this project's state shows landed, and the capsule names them until
main writes to the same thing. Nothing is blocked and nothing is undone. The judge stays off.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from conftest import REPO_ROOT, read_ledger
from test_native_runs import (WF, WF_TASK, _assistant, _clerk, _note, _queued, _scribe, _user,
                              _wf_file, _wf_launch, _write)

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402

RUN = "wf_5a1e0001-abc"
BASE = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(microsecond=0)


def _at(seconds, ms=0):
    t = BASE + timedelta(seconds=seconds, milliseconds=ms)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"


def _stamp(seconds):
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bash(tuid, seconds, cmd, background=None):
    """A Bash call at `seconds` and its result 1.5s later, as an agent's transcript holds them."""
    call = {**_assistant({"type": "tool_use", "id": tuid, "name": "Bash",
                          "input": {"command": cmd, "run_in_background": bool(background)}}),
            "timestamp": _at(seconds, 200)}
    result = {"type": "user", "timestamp": _at(seconds + 1, 500),
              "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tuid,
                                                       "content": "ok"}]},
              "toolUseResult": {"backgroundTaskId": background} if background else {}}
    return [call, result]


def _state(project):
    """This project's state as the writes left it: the worker's task, directive and verdict
    writes, main's own directive, and feat/w, which main closed long before a scratch-project
    call closed a task of the same name."""
    hp = project / ".hippo"
    (hp / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "feat/w", "title": "w", "status": "done", "notes": [], "updated": _stamp(-600)},
        {"id": "feat/x", "title": "x", "status": "done", "notes": [], "updated": _stamp(1)},
        {"id": "feat/y", "title": "y", "status": "active", "notes": [], "updated": _stamp(-600)},
        {"id": "feat/z", "title": "z", "status": "done", "notes": [], "updated": _stamp(360)},
    ]}), encoding="utf-8")
    rows = [
        {"t": _stamp(-600), "ev": "dispatch", "id": "d1", "kind": "impl",
         "exec": "codex/gpt-6-sol/high", "scope": "lane y", "task": "feat/y", "src": "wrapper"},
        {"t": _stamp(30), "ev": "directive", "id": "main-rule", "state": "active",
         "text": "main typed this", "src": "cli"},
        {"t": _stamp(91), "ev": "outcome", "ref": "d1", "result": "accepted", "src": "cli"},
        {"t": _stamp(150), "ev": "directive", "id": "worker-rule", "state": "active",
         "text": "never push", "src": "cli"},
    ]
    with (hp / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)


def _session(project):
    wtuid = "toolu_01WorkflowWorkflowWorkflo"
    _write(project / "transcript.jsonl", [
        _user("run the workflow"),
        {**_assistant({"type": "tool_use", "id": wtuid, "name": "Workflow",
                       "input": {"script": "await agent('prune')"}}), "timestamp": _at(0)},
        {"type": "user", "timestamp": _at(0, 300), "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": wtuid, "content": "launched"}]},
         "toolUseResult": {"status": "async_launched", "taskId": "wt1", "runId": RUN,
                           "workflowName": "prune"}},
        # main's own call, beside the run: main's transcript is never read for worker writes
        *_bash("toolu_main", 29, "hippo directive add --id main-rule --text 'main typed this'"),
        _user(_note(task="wt1", tuid=wtuid, result="{}")),
    ])
    run = project / "transcript" / "subagents" / "workflows" / RUN
    brief = {"type": "user", "timestamp": _at(0, 500), "message": {"role": "user",
                                                                   "content": "prune"}}
    _write(run / "agent-a1.jsonl", [
        brief,
        # the real mlx-vlm shape: cd, pipes, the write last with its output cut to two lines
        *_bash("toolu_1", 0, f"cd {project}; du -sk * | sort -rn; "
                             "for t in feat/x; do hippo task done $t; done 2>&1 | tail -2"),
        # a brief written with a heredoc is text, not a call
        *_bash("toolu_2", 120, "cat > b.md <<EOF\nhippo task done feat/y once green\nEOF"),
        *_bash("toolu_3", 149, "H=hippo; $H directive add --id worker-rule --text 'never push'"),
        # sent to the background: the write lands after the launch result, before its notice
        *_bash("toolu_4", 299, "sleep 60; hippo task done feat/z", background="b9"),
        {**_queued("<task-notification>\n<task-id>b9</task-id>\n<status>completed</status>\n"
                   "</task-notification>"), "timestamp": _at(361)},
    ])
    _write(run / "agent-a2.jsonl", [
        brief,
        # a scratch project's feat/w: this project's feat/w is done too, but not since then
        *_bash("toolu_5", 59, "cd /tmp/scratch && hippo init && hippo task done feat/w"),
        # `task:` resolves to d1 at write time; the row names d1
        *_bash("toolu_6", 90, "hippo log outcome --ref task:feat/y --result accepted"),
    ])


def _capsule(run_hippo, project):
    proc = run_hippo(["status", "--inject"], cwd=project)
    assert proc.returncode == 0, proc.stderr
    return [ln for ln in proc.stdout.splitlines() if ln.startswith("· worker wrote:")]


def test_a_worker_s_writes_show_until_main_makes_them_its_own(tmp_project, run_hippo, tmp_path):
    """A Workflow agent's task, verdict and directive writes are recorded under the run, the
    latest per thing written to; main's own call, a call against another project and a brief
    that quotes a command are not. Each shows in main's capsule until main writes to it."""
    _state(tmp_project)
    _session(tmp_project)
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w"))

    written = json.loads((tmp_project / ".hippo" / "worker-writes.json").read_text())
    ag = "ag-" + RUN
    assert written == {
        "task:feat/x": {"t": _stamp(1), "run": ag, "op": "task done feat/x"},
        "outcome:d1": {"t": _stamp(91), "run": ag, "op": "outcome accepted on d1"},
        "directive:worker-rule": {"t": _stamp(150), "run": ag, "op": "directive add worker-rule"},
        "task:feat/z": {"t": _stamp(360), "run": ag, "op": "task done feat/z"},
    }
    assert _capsule(run_hippo, tmp_project) == [
        f"· worker wrote: task done feat/x ({ag}), outcome accepted on d1 ({ag}), directive add "
        f"worker-rule ({ag}), task done feat/z ({ag}) — not yours yet: confirm it with a write of "
        "your own, or undo it"]
    assert read_ledger(tmp_project)[-1]["name"] == "turn-scribe", "no ledger event of its own"

    for args in (["task", "set", "feat/x", "notes", "checked"], ["task", "set", "feat/z", "status",
                 "active"], ["directive", "add", "--id", "worker-rule", "--text", "never push"],
                 ["log", "outcome", "--ref", "d1", "--result", "accepted"]):
        assert run_hippo(args, cwd=tmp_project).returncode == 0
    assert _capsule(run_hippo, tmp_project) == []
    # The next window touches no run and finds nothing; what no longer shows is dropped.
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w2"))
    assert json.loads((tmp_project / ".hippo" / "worker-writes.json").read_text()) == {}


def test_a_malformed_tasks_file_never_holds_the_scribe_back(tmp_project, run_hippo, tmp_path):
    """The step reads tasks.yaml, whose reader exits on a malformed file: that exit is dumped
    like any bug on the native path, and the window still advances the cursor."""
    _state(tmp_project)
    _session(tmp_project)
    (tmp_project / ".hippo" / "tasks.yaml").write_text("tasks: {not: a list}\n", encoding="utf-8")
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w"))

    cursors = json.loads((tmp_project / ".hippo" / "cursors.json").read_text())
    assert cursors["s1"] == len((tmp_project / "transcript.jsonl").read_text().splitlines())
    assert any("malformed tasks.yaml" in p.read_text()
               for p in (tmp_project / ".hippo" / "failures").glob("*-native-*"))


@pytest.mark.parametrize("end", ["TaskStop", "killed"])
def test_a_run_over_a_day_old_is_read_in_the_window_it_ends(tmp_project, run_hippo, tmp_path,
                                                            end):
    """A run launched over 24h ago is no longer read while it runs, so the window it ends in is
    the last to read what its agents wrote since — and main's TaskStop, or a run file saying
    `killed`, ends a Workflow with no notification (§3.5.3c)."""
    hp, transcript = tmp_project / ".hippo", tmp_project / "transcript.jsonl"
    (hp / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "feat/x", "title": "x", "status": "done", "notes": [], "updated": _stamp(1)},
    ]}), encoding="utf-8")
    launch = _wf_launch()
    launch[1]["timestamp"] = _at(-25 * 3600)
    _write(transcript, [_user("run it"), *launch])
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w1"))

    brief = {"type": "user", "timestamp": _at(0, 500), "message": {"role": "user",
                                                                   "content": "x"}}
    _write(tmp_project / "transcript" / "subagents" / "workflows" / WF / "agent-a1.jsonl",
           [brief, *_bash("toolu_1", 0, "hippo task done feat/x")])
    if end == "TaskStop":
        _write(transcript, [_user("stop it"), _assistant({
            "type": "tool_use", "id": "toolu_01StopStopStopStopStopSt", "name": "TaskStop",
            "input": {"task_id": WF_TASK}})], mode="a")
    else:
        _wf_file(tmp_project, status="killed", taskId=WF_TASK)
        _write(transcript, [_user("next")], mode="a")
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w2"))
    assert json.loads((hp / "worker-writes.json").read_text()) == {
        "task:feat/x": {"t": _stamp(1), "run": "ag-" + WF, "op": "task done feat/x"}}


def test_a_command_is_read_the_way_the_shell_and_the_cli_read_it():
    """The walker the T1 replay measured main's calls with, then hippo's own parser: variables
    and loops expand, a function can alias hippo, heredoc bodies and quoted text are not calls,
    and only the writes that are main's count — not a read, a dispatch or `-h`."""
    parser = hippo_cli.build_parser()

    def calls(cmd):
        return [c for argv in hippo_cli._commands(hippo_cli._shell_words(cmd) or [], {}, {})
                if (c := hippo_cli.worker_call(parser, hippo_cli.hippo_args(argv)))]

    def task(tid, op, **want):
        return {"task": tid, "want": want, "op": op}

    def row(**kw):
        return {"row": kw}

    cases = {
        'for id in fix/x feat/y; do H=hippo; $H task drop "${id%%/*}/${id#*/}"; done':
            [task("fix/x", "task drop fix/x", status="dropped"),
             task("feat/y", "task drop feat/y", status="dropped")],
        'h() { hippo directive "$@"; }; h withdraw gpu-pin':
            [row(ev="directive", id="gpu-pin", state="withdrawn")],
        "hippo task set feat/a status active && hippo task set feat/a notes 'n'":
            [task("feat/a", "task set feat/a status", status="active"),
             task("feat/a", "task set feat/a notes", notes=["n"])],
        'uv run --script cli/hippo_cli.py task add feat/n --title "T"':
            [task("feat/n", "task add feat/n", title="T", status="pending", notes=[], deps=[])],
        "env -u HIPPO_DIR /opt/p/bin/hippo directive add --text 'use GPUs 0 and 1 only'":
            [row(ev="directive", id=hippo_cli.directive_id("use GPUs 0 and 1 only"),
                 state="active", text="use GPUs 0 and 1 only")],
        """hippo log raw '{"ev": "outcome", "ref": "d1", "result": "accepted"}'""":
            [row(ev="outcome", ref="d1", result="accepted")],
        # its verdicts are named in a journal, not on the line: nothing to match them by
        "hippo log outcome --from-batch j.jsonl < verdicts.jsonl": [],
        'cat <<<"x"; hippo directive add --id x --state withdrawn':
            [row(ev="directive", id="x", state="withdrawn")],
        "cat > b.md <<'EOF'\nhippo task done feat/z\nEOF\nhippo task list": [],
        'echo "hippo task done feat/a"; hippo task done -h; ls hippo': [],
        "hippo log dispatch --id d1 --kind impl --exec codex/m/low --scope s": [],
        # refused by the CLI too: no ref for a native worker, no id and no text to derive one
        "hippo log outcome --result accepted; hippo directive add --state active": [],
    }
    for cmd, want in cases.items():
        assert calls(cmd) == want, cmd


def test_a_call_is_matched_on_every_value_it_wrote():
    """A call claims only a write that shows what it set: main's own close of a task, or its own
    verdict or directive, in the call's window is never the worker's for sharing the thing."""
    parser = hippo_cli.build_parser()
    lo = BASE
    hi = lo + timedelta(minutes=30)  # a call sent to the background: its window runs long
    tasks = {"feat/x": {"id": "feat/x", "title": "x", "status": "done", "notes": [],
                        "updated": _stamp(600)},  # main closed it, nothing else
             "feat/n": {"id": "feat/n", "title": "N", "status": "pending", "notes": ["why"],
                        "deps": ["feat/x"], "updated": _stamp(60)}}
    rows = [{"t": _stamp(600), "ev": "outcome", "ref": "d9", "result": "accepted", "src": "cli"},
            {"t": _stamp(600), "ev": "directive", "id": "gpu", "state": "active",
             "text": "GPUs 0 and 1", "src": "cli"}]

    def landed(cmd):
        return [k for argv in hippo_cli._commands(hippo_cli._shell_words(cmd), {}, {})
                if (c := hippo_cli.worker_call(parser, hippo_cli.hippo_args(argv)))
                for k, _, _ in hippo_cli.worker_landed(c, lo, hi, tasks, rows, {})]

    assert landed("hippo task set feat/x notes 'wip'") == []
    assert landed("hippo task set feat/x title y; hippo task add feat/x --title x") == []
    assert landed("hippo log outcome --from-batch j.jsonl < v.jsonl") == []
    assert landed("hippo directive add --id gpu --text 'GPU 2 only'") == []
    assert landed("hippo task set feat/x status done; hippo directive add --id gpu "
                  "--text 'GPUs 0 and 1'") == ["task:feat/x", "directive:gpu"]
    assert landed("hippo task add feat/n --title N --notes why --deps 'feat/x, '") == [
        "task:feat/n"]
