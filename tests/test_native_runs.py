"""The scribe records native runs — Agent/Task subagents, forks, Workflow runs — like codex lanes
(DESIGN §3.5.3c).

Contract under test: in every mode, each run main's Claude Code transcript launched is listed
for the clerk until it has a row; the clerk gives the kind and nothing else of its lands; code
fills exec from the agent's own transcript, scope, task and parent, writes cumulative usage per
model at completions, and — judge on only — triages a run's answer to its brief once. A clerk
dispatch that restates a run is dumped without costing main's verdict, main's own `log
dispatch` for a run is its record, and a bug on this path never costs the window its clerk.

Nothing here may reach the network: conftest pins HIPPO_JEV_BACKEND=off and the tests that
want a judge pin `mock`.
"""
import json
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone

import yaml

from conftest import REPO_ROOT, read_ledger
from test_batch_harvest import ACCEPT, DEFAULT, _mock

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402

AGENT = "a0123456789abcdef"
DID = "ag-" + AGENT
TUID = "toolu_01ABCDEFGHJKLMNPQRSTUVWX"
BASH_TUID = "toolu_01BashBashBashBashBash1"
BRIEF = "Task feat/retry: add a retry loop around the fetch call; run tests/run.sh until green."
REPORT = "Done: the retry loop is in and tests/run.sh passes (412 passed)."


def _ts(minutes_ago=5):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# --------------------------------------------------------------------------
# transcript lines, in the shapes measured on Claude Code 2.1.281
# --------------------------------------------------------------------------

def _assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _user(text):
    return {"type": "user", "timestamp": _ts(), "message": {"role": "user", "content": text}}


def _call(tuid=TUID, desc="retry loop", prompt=BRIEF, subagent_type="general-purpose"):
    return _assistant({"type": "tool_use", "id": tuid, "name": "Agent", "input": {
        "description": desc, "prompt": prompt, "subagent_type": subagent_type, "model": "opus"}})


def _launched(tuid=TUID, agent=AGENT, desc="retry loop"):
    return {"type": "user", "timestamp": _ts(),
            "message": {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tuid,
                "content": [{"type": "text", "text": "Async agent launched successfully."}]}]},
            "toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": agent,
                              "description": desc, "resolvedModel": "claude-opus-5-5[1m]"}}


def _note(task=AGENT, tuid=TUID, status="completed", result=REPORT, interim=False):
    head = f"<task-notification>\n<task-id>{task}</task-id>\n"
    if tuid:
        head += f"<tool-use-id>{tuid}</tool-use-id>\n"
    note = ("This agent stopped with background work of its own still running." if interim
            else "A task-notification fires each time this agent stops.")
    body = f"<status>{status}</status>\n<summary>Agent finished</summary>\n<note>{note}</note>\n"
    if result is not None:
        body += f"<result>{result}</result>\n"
    return head + body + "<usage><subagent_tokens>99999</subagent_tokens></usage>\n" \
                         "</task-notification>"


def _queued(text):
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": text,
                                                 "commandMode": "task-notification"}}


def _bash():
    return [_assistant({"type": "tool_use", "id": BASH_TUID, "name": "Bash",
                        "input": {"command": "sleep 60", "run_in_background": True}}),
            {"type": "user", "message": {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": BASH_TUID, "content": "running: b77"}]},
             "toolUseResult": {"backgroundTaskId": "b77"}}]


def _write(path, lines, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as f:
        f.writelines((x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)) + "\n"
                     for x in lines)
    return path


def _msg(mid, model="claude-opus-5-5", effort="xhigh", inp=10, read=1000, made=100, out=50,
         edit=None):
    """One API message as the agent's transcript streams it: two lines, output growing."""
    content = [{"type": "text", "text": "working"}]
    if edit:
        content.append({"type": "tool_use", "id": f"toolu_{mid}", "name": "Edit",
                        "input": {"replace_all": False, "file_path": edit}})

    def line(o):
        return {"type": "assistant", "isSidechain": True, "effort": effort,
                "message": {"id": mid, "model": model, "role": "assistant", "content": content,
                            "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                                      "cache_creation_input_tokens": made, "output_tokens": o}}}
    return [line(1), line(out)]


def _agent(session, agent=AGENT, msgs=(), brief=BRIEF, fork=False, **meta):
    sub = session / "subagents"
    lines = []
    if fork:  # a fork opens with main's own launching message copied in (measured)
        lines += [{"type": "fork-context-ref", "agentId": agent},
                  *_msg("msg_main", inp=5, read=900000, made=0, out=700)]
    lines.append({"type": "user", "isSidechain": True, "agentId": agent,
                  "message": {"role": "user", "content": brief}})
    for m in msgs:
        lines += m
    _write(sub / f"agent-{agent}.jsonl", lines)
    meta = {"agentType": "fork" if fork else "general-purpose", "isFork": fork or None,
            "description": "retry loop", **meta}
    (sub / f"agent-{agent}.meta.json").write_text(
        json.dumps({k: v for k, v in meta.items() if v is not None}), encoding="utf-8")


# --------------------------------------------------------------------------
# running the scribe
# --------------------------------------------------------------------------

def _clerk(tmp_path, name, events=()):
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"worklog": f"{name} window", "events": list(events)}),
                 encoding="utf-8")
    return p


def _scribe(run_hippo, project, clerk, jev=None, session="s1", capture=None, jev_capture=None):
    env = {"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(clerk),
           "HIPPO_JEV_BACKEND": "off" if jev is None else "mock"}
    if jev is not None:
        env["HIPPO_JEV_MOCK_OUTPUT"] = str(jev)
    if capture is not None:
        env["HIPPO_MOCK_CAPTURE"] = str(capture)
    if jev_capture is not None:
        env["HIPPO_JEV_MOCK_CAPTURE"] = str(jev_capture)
    proc = run_hippo(["scribe", "--transcript", str(project / "transcript.jsonl"),
                      "--session", session], cwd=project, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc


def _rows(project, ev, **match):
    return [e for e in read_ledger(project) if e.get("ev") == ev
            and all(e.get(k) == v for k, v in match.items())]


def _payload(capture):
    return "# live directives\n" + capture.read_text(encoding="utf-8").rsplit(
        "\n# live directives\n", 1)[1]


def _dumps(project):
    return "".join(p.read_text() for p in (project / ".hippo" / "failures").glob("*"))


def _session(project):
    return project / "transcript"


def _launch_window():
    return [_user("add the retry loop in a subagent"), _call(), _launched(), *_bash(),
            _assistant({"type": "text", "text": "Launched; waiting for the report."})]


# --------------------------------------------------------------------------
# the scribe step, end to end
# --------------------------------------------------------------------------

def test_a_run_is_listed_until_recorded_then_costed_once(tmp_project, run_hippo, tmp_path):
    """No key. The launch window lists the run; a kind outside the vocabulary is dumped and the
    run stays listed. The completion window records it — the clerk's kind on exec, scope and
    task that code observed (its exec and scope are ignored) — and an outcome in the same
    output lands on it. Usage is the agent's own messages, each counted once. Reading the
    same lines again writes nothing twice, and a Bash task is no run."""
    tasks = {"tasks": [{"id": "feat/retry", "title": "t", "status": "active"},
                       {"id": "feat/retry-v2", "title": "u", "status": "pending"}]}
    (tmp_project / ".hippo" / "tasks.yaml").write_text(yaml.safe_dump(tasks))
    _write(tmp_project / "transcript.jsonl", _launch_window())
    _agent(_session(tmp_project), msgs=[_msg("m1", edit="src/fetch.py")])
    capture = tmp_path / "clerk.txt"
    junk = [{"ev": "dispatch", "id": DID, "kind": "retry-impl"},
            {"ev": "outcome", "ref": DID, "result": "revised", "note": "said too early"}]
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w1", junk), capture=capture)

    listed = f"- {DID} · subagent · retry loop · brief: {BRIEF}"
    assert listed in _payload(capture)
    assert not _rows(tmp_project, "dispatch")
    assert "kind 'retry-impl'" in _dumps(tmp_project)
    # the verdict went down with its run's row, and says so — not check_ref's words to main
    assert "this dump is the only record of the verdict" in _dumps(tmp_project)
    assert "no call needed" not in _dumps(tmp_project)

    _agent(_session(tmp_project), msgs=[_msg("m1", edit="src/fetch.py"),
                                        _msg("m2", read=2000, out=70)])
    _write(tmp_project / "transcript.jsonl",
           [_queued(_note()), _user(_note(task="b77", tuid=BASH_TUID, result="exit 0")),
            _user("merged, thanks"),
            _assistant({"type": "text", "text": "Accepted the retry loop."})], mode="a")
    events = [{"ev": "outcome", "ref": DID, "result": "accepted", "note": "retry loop merged"},
              {"ev": "dispatch", "id": DID, "kind": "impl", "exec": "subagent/opus/inherit",
               "scope": "the clerk's own words"}]
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w2", events), capture=capture)

    assert listed in _payload(capture), "a run the clerk skipped stays listed"
    [d] = _rows(tmp_project, "dispatch")
    assert (d["id"], d["kind"], d["exec"], d["scope"], d["task"], d["src"]) == (
        DID, "impl", "subagent/claude-opus-5-5/xhigh", "retry loop", "feat/retry", "scribe")
    assert [e["ref"] for e in _rows(tmp_project, "outcome", result="accepted")] == [DID]
    [u] = _rows(tmp_project, "usage")
    assert (u["ref"], u["model"], u["src"]) == (DID, "claude-opus-5-5", "scribe")
    assert (u["tin"], u["tcached"], u["tout"]) == (10 + 1000 + 100 + 10 + 2000 + 100, 3000, 120)
    assert u["tokens"] == u["tin"] + u["tout"]
    assert not _rows(tmp_project, "triage") and not _rows(tmp_project, "clerk", name="jev-harvest")

    before = [e for e in read_ledger(tmp_project) if e["ev"] != "clerk"]
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "quiet"), session="s2", capture=capture)
    assert [e for e in read_ledger(tmp_project) if e["ev"] != "clerk"] == before
    assert "# native runs to record" not in _payload(capture)


def test_with_the_judge_the_first_completion_is_triaged_once(tmp_project, run_hippo, tmp_path):
    """The run's first completion is read like a wrapper lane at exit, src=scribe, with the
    agent's own edits as its changes and the route never shown to the clerk. A resumed agent's
    next notification — no tool-use-id after a SendMessage — gets a new cumulative usage row,
    and is never triage material."""
    jev = _mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT})
    _write(tmp_project / "transcript.jsonl", _launch_window() + [_user(_note())])
    _agent(_session(tmp_project), msgs=[_msg("m1", edit="src/fetch.py")])
    record = [{"ev": "dispatch", "id": DID, "kind": "impl"}]
    jev_capture = tmp_path / "jev-request.json"
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w1", record), jev, jev_capture=jev_capture)

    [t] = _rows(tmp_project, "triage")
    assert (t["ref"], t["route"], t["src"]) == (DID, "accept-candidate", "scribe")
    assert [e["src"] for e in _rows(tmp_project, "clerk", name="jev-harvest")] == ["scribe"]
    state = json.loads(jev_capture.read_text(encoding="utf-8"))["state"]
    assert (state["brief"], state["report"], state["exit"]["rc"]) == (BRIEF, REPORT, 0)
    assert state["changes"] == f"{hippo_cli.NATIVE_EDITS_HEAD}:\nsrc/fetch.py"

    _agent(_session(tmp_project), msgs=[_msg("m1", edit="src/fetch.py"), _msg("m2", out=900)])
    _write(tmp_project / "transcript.jsonl", [_user("also handle 429"),
                                              _user(_note(tuid=None, result="429 handled too"))],
           mode="a")
    capture = tmp_path / "clerk.txt"
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w2"), jev, capture=capture)
    assert len(_rows(tmp_project, "triage")) == 1
    assert [u["tout"] for u in _rows(tmp_project, "usage")] == [50, 950]
    assert "accept-candidate" not in _payload(capture), "the route never reaches the clerk"


def test_a_failed_first_reading_is_not_retried_on_a_resume(tmp_project, run_hippo, tmp_path):
    broken = _mock(tmp_path, {"default": {"noul": 0.5}}, "broken.json")  # harvest has a score
    _write(tmp_project / "transcript.jsonl", _launch_window() + [_user(_note())])
    _agent(_session(tmp_project), msgs=[_msg("m1")])
    _scribe(run_hippo, tmp_project,
            _clerk(tmp_path, "w1", [{"ev": "dispatch", "id": DID, "kind": "impl"}]), broken)
    assert [e["ok"] for e in _rows(tmp_project, "clerk", name="jev-harvest")] == [False]

    _write(tmp_project / "transcript.jsonl", [_user("go on"), _user(_note(tuid=None))], mode="a")
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w2"),
            _mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}))
    assert not _rows(tmp_project, "triage")
    assert len(_rows(tmp_project, "clerk", name="jev-harvest")) == 1


WF, WF_TASK, WF_TUID = "wf_1234abcd-567", "wxyz", "toolu_01WorkflowWorkflowWorkflo"
WF_ID = "ag-" + WF
SCRIPT = "export const meta = { name: 'port-parser' }; await agent('port the parser')"


def _wf_agent(project, aid, msgs, run=WF, **meta):
    """One agent of a Workflow run: its transcript and, when given, its meta.json."""
    d = _session(project) / "subagents" / "workflows" / run
    _write(d / f"agent-{aid}.jsonl", [{"type": "user", "message": {"content": "x"}}]
           + [ln for m in msgs for ln in m])
    if meta:
        (d / f"agent-{aid}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _wf_file(project, run=WF, **fields):
    """The run file the host writes at a launch's end."""
    d = _session(project) / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run}.json").write_text(json.dumps(fields), encoding="utf-8")


def _wf_launch(run=WF, task=WF_TASK, tuid=WF_TUID, name="port-parser", **inp):
    return [_assistant({"type": "tool_use", "id": tuid, "name": "Workflow",
                        "input": {"script": SCRIPT, **inp}}),
            {"type": "user", "timestamp": _ts(), "message": {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tuid, "content": "launched"}]},
             "toolUseResult": {"status": "async_launched", "taskId": task, "runId": run,
                               "workflowName": name}}]


def test_a_workflow_run_is_one_row_over_all_its_agents(tmp_project, run_hippo, tmp_path):
    """exec is the most-used model and effort across the run's agents; usage is one row per
    model; the brief is the script and the report is the run's whole result, not the ~8k the
    notification carries."""
    result = {"impl": {"commit": "abc1234", "tests": "412 passed"}, "note": "π ≈ 3.14"}
    _wf_agent(tmp_project, "a1", [_msg("w1"), _msg("w2")])
    _wf_agent(tmp_project, "a2", [_msg("w3", model="h-4-5", effort="low")])
    _wf_file(tmp_project, status="completed", taskId=WF_TASK, result=json.dumps(result))
    _write(tmp_project / "transcript.jsonl", [
        _user("port it with a workflow"), *_wf_launch(),
        _user(_note(task=WF_TASK, tuid=WF_TUID, result="{\"impl\": … (truncated)"))])
    jev_capture = tmp_path / "jev-request.json"
    _scribe(run_hippo, tmp_project,
            _clerk(tmp_path, "w", [{"ev": "dispatch", "id": WF_ID, "kind": "impl"}]),
            _mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT}), jev_capture=jev_capture)

    [d] = _rows(tmp_project, "dispatch")
    assert (d["exec"], d["scope"]) == ("workflow/claude-opus-5-5/xhigh", "port-parser")
    assert sorted((u["model"], u["tout"]) for u in _rows(tmp_project, "usage")) == [
        ("claude-opus-5-5", 100), ("h-4-5", 50)]
    state = json.loads(jev_capture.read_text(encoding="utf-8"))["state"]
    assert state["brief"] == SCRIPT
    assert state["report"] == result, "a JSON result reaches the judge as the structure it is"
    [t] = _rows(tmp_project, "triage")
    assert t["ref"] == WF_ID and "trimmed" not in t


def test_a_workflow_row_waits_for_its_end_and_reads_the_whole_run(tmp_project, run_hippo,
                                                                  tmp_path):
    """Measured live: a Workflow recorded at the first Stop, a second after its launch, read
    `…/low` from the lines that existed then — a hippo:lane relay's — where the whole run read
    `…/high`, and before any agent's reply it dumped "no model readable" for a run that was
    only starting. The row waits for the run's end, quietly, and the clerk's kind is asked for
    again then; a hippo:lane agent's tokens and effort are not the run's (the lane's wrapper
    records the lane); and the task id may ride in the Workflow's `args`."""
    tasks = {"tasks": [{"id": "feat/parser", "title": "t", "status": "active"}]}
    (tmp_project / ".hippo" / "tasks.yaml").write_text(yaml.safe_dump(tasks))
    lane = [_msg("l1", model="claude-sonnet-5", effort="low", out=9000)]
    _wf_agent(tmp_project, "alane", lane, agentType="hippo:lane")
    _write(tmp_project / "transcript.jsonl", [
        _user("port it"), *_wf_launch(args={"task": "feat/parser"}),
        _assistant({"type": "text", "text": "Launched."})])
    kind = [{"ev": "dispatch", "id": WF_ID, "kind": "impl"}]
    capture = tmp_path / "clerk.txt"
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w1", kind), capture=capture)
    assert f"- {WF_ID} · workflow · port-parser" in _payload(capture)
    assert not _rows(tmp_project, "dispatch") and not _dumps(tmp_project)

    _wf_agent(tmp_project, "a1", [_msg("h1", effort="high"), _msg("h2", effort="high")],
              agentType="workflow-subagent")
    _wf_file(tmp_project, status="completed", taskId=WF_TASK, result="ported")
    _write(tmp_project / "transcript.jsonl",
           [_user(_note(task=WF_TASK, tuid=WF_TUID, result="ported"))], mode="a")
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w2", kind), capture=capture)

    [d] = _rows(tmp_project, "dispatch")
    assert (d["id"], d["kind"], d["exec"], d["task"]) == (
        WF_ID, "impl", "workflow/claude-opus-5-5/high", "feat/parser")
    assert [(u["model"], u["tout"]) for u in _rows(tmp_project, "usage")] == [
        ("claude-opus-5-5", 100)], "the lane relay's tokens are not the run's"
    assert not _dumps(tmp_project)


def test_a_workflow_of_hippo_lane_agents_only_gets_no_row(tmp_project, run_hippo, tmp_path):
    """Every lane it relays has its wrapper row: the run itself would count the work twice."""
    _wf_agent(tmp_project, "alane", [_msg("l1", effort="low")], agentType="hippo:lane")
    _wf_file(tmp_project, status="completed", taskId=WF_TASK, result="lane lines")
    _write(tmp_project / "transcript.jsonl",
           [_user("fan out lanes"), *_wf_launch(), _user(_note(task=WF_TASK, tuid=WF_TUID))])
    capture = tmp_path / "clerk.txt"
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w"), capture=capture)
    assert "# native runs to record" not in _payload(capture)
    assert not _rows(tmp_project, "dispatch") and not _rows(tmp_project, "usage")


def test_a_run_the_clerk_skips_gets_its_row_when_it_ends(tmp_project, run_hippo, tmp_path):
    """Measured: the cheap clerk recorded a listed run only when the window's digest was about
    it, and 10 finished runs aged out of the list unrecorded. A listed run that has ended with
    no row after the clerk gets one from code, kind `unclassified` — and main's verdict in the
    same output lands on it rather than in a dump."""
    _write(tmp_project / "transcript.jsonl", _launch_window())
    _agent(_session(tmp_project), msgs=[_msg("m1")])
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "skips"))
    assert not _rows(tmp_project, "dispatch"), "a run still working waits for the clerk"

    _write(tmp_project / "transcript.jsonl", [_user(_note()), _user("merged")], mode="a")
    verdict = [{"ev": "outcome", "ref": DID, "result": "accepted", "note": "merged"}]
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "verdict only", verdict))

    [d] = _rows(tmp_project, "dispatch")
    assert (d["id"], d["kind"], d["exec"]) == (DID, "unclassified",
                                               "subagent/claude-opus-5-5/xhigh")
    assert [o["ref"] for o in _rows(tmp_project, "outcome")] == [DID]
    assert [u["ref"] for u in _rows(tmp_project, "usage")] == [DID]
    assert not _dumps(tmp_project)


def test_a_stopped_workflow_ends_without_a_notification(tmp_project, run_hippo, tmp_path):
    """Measured (mlx-vlm): a Workflow stopped by TaskStop never notifies; its run file says
    `killed`, and one such run's 2.59M tokens reached no usage row. It ends at that file — or
    at main's TaskStop naming its task, the file not written yet — gets its row and its
    cumulative usage, and no triage: there is no answer to read. A run file naming an earlier
    launch's task does not end a resumed run."""
    jev = _mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT})
    _wf_agent(tmp_project, "a1", [_msg("k1")])
    _wf_file(tmp_project, status="killed", taskId=WF_TASK, result=None)
    b, b_task, b_tuid = "wf_bbbbbbbb-bbb", "wbbbb", "toolu_01BBBBBBBBBBBBBBBBBBBBBBBB"
    _wf_agent(tmp_project, "b1", [_msg("s1", out=70)], run=b)
    c, c_task, c_tuid = "wf_cccccccc-ccc", "wcccc", "toolu_01CCCCCCCCCCCCCCCCCCCCCCCC"
    _wf_agent(tmp_project, "c1", [_msg("r1")], run=c)
    _wf_file(tmp_project, run=c, status="killed", taskId="wolder", result=None)
    stop = _assistant({"type": "tool_use", "id": "toolu_01StopStopStopStopStopSt",
                       "name": "TaskStop", "input": {"task_id": b_task}})
    _write(tmp_project / "transcript.jsonl", [
        _user("run three"), *_wf_launch(), *_wf_launch(run=b, task=b_task, tuid=b_tuid),
        *_wf_launch(run=c, task=c_task, tuid=c_tuid), stop])
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w"), jev)

    assert sorted((d["id"], d["kind"]) for d in _rows(tmp_project, "dispatch")) == [
        (WF_ID, "unclassified"), ("ag-" + b, "unclassified")]
    assert sorted((u["ref"], u["tout"]) for u in _rows(tmp_project, "usage")) == [
        (WF_ID, 50), ("ag-" + b, 70)]
    assert not _rows(tmp_project, "triage")


def test_main_s_bare_run_id_row_is_the_record(tmp_project, run_hippo, tmp_path):
    """Main logged the Workflow at launch under the Run ID the tool printed, its scope in its
    own words (measured, mlx-vlm: 15 of 17 runs had such a row beside the scribe's `ag-` twin,
    main's verdict on one and the cost on the other). That row is the run's record: nothing is
    listed, the cost lands on it, and a verdict main typed under the `ag-` id the skills teach
    lands on it too, so PRIORS joins that verdict to that cost."""
    assert run_hippo(["log", "dispatch", "--id", WF, "--kind", "impl", "--exec",
                      "workflow/opus/xhigh", "--scope", "parser port"],
                     cwd=tmp_project).returncode == 0
    _wf_agent(tmp_project, "a1", [_msg("w1")])
    _wf_file(tmp_project, status="completed", taskId=WF_TASK, result="ported")
    _write(tmp_project / "transcript.jsonl",
           [_user("port it"), *_wf_launch(), _user(_note(task=WF_TASK, tuid=WF_TUID)),
            _assistant({"type": "text", "text": f"{WF_ID} is accepted; merged."})])
    capture = tmp_path / "clerk.txt"
    verdict = {"ev": "outcome", "ref": WF_ID, "result": "accepted", "note": "merged"}
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w", [verdict]), capture=capture)

    assert "# native runs to record" not in _payload(capture)
    assert [d["id"] for d in _rows(tmp_project, "dispatch")] == [WF]
    assert [u["ref"] for u in _rows(tmp_project, "usage")] == [WF]
    assert [(o["ref"], o["src"]) for o in _rows(tmp_project, "outcome")] == [(WF, "scribe")]
    assert not _dumps(tmp_project)
    [cell] = hippo_cli.prior_cells(read_ledger(tmp_project)).values()
    assert cell["judged"] == 1 and cell["tokens"] == 1160


def test_the_roster_names_open_runs_by_what_a_digest_shows(tmp_project, run_hippo, tmp_path):
    """Measured: 0 of 21 `ag-wf_` rows ever got a verdict, while main merged the runs' work by
    branch names. Every unjudged row of this session's recent runs reaches the clerk's roster —
    past the last 12 dispatches — with its task and a Workflow's worktrees, and an outcome
    naming it joins its cost."""
    _wf_agent(tmp_project, "a1", [_msg("w1")], agentType="workflow-subagent",
              worktreePath=str(tmp_project / ".claude" / "worktrees" / f"{WF}-1"))
    _wf_file(tmp_project, status="completed", taskId=WF_TASK, result="ported")
    _write(tmp_project / "transcript.jsonl",
           [_user("port it"), *_wf_launch(), _user(_note(task=WF_TASK, tuid=WF_TUID))])
    _scribe(run_hippo, tmp_project,
            _clerk(tmp_path, "w1", [{"ev": "dispatch", "id": WF_ID, "kind": "impl"}]))
    for i in range(hippo_cli.DISPATCH_ROSTER_N):
        assert run_hippo(["log", "dispatch", "--id", f"d{i}", "--kind", "impl", "--exec",
                          "codex/gpt-6-sol/high", "--scope", "x"], cwd=tmp_project).returncode == 0

    _write(tmp_project / "transcript.jsonl", [_user("merged the parser branch")], mode="a")
    capture = tmp_path / "clerk.txt"
    verdict = [{"ev": "outcome", "ref": WF_ID, "result": "accepted", "note": "merged"}]
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w2", verdict), capture=capture)
    assert (f"- {WF_ID} (impl): port-parser · worktrees {WF}-1  [no outcome yet]"
            in _payload(capture))
    [cell] = [c for (k, ex), c in hippo_cli.prior_cells(read_ledger(tmp_project)).items()
              if ex.startswith("workflow/")]
    assert cell["judged"] == 1 and cell["tokens"] == 1160


def test_a_workflow_result_over_budget_is_trimmed_by_its_structure(tmp_project, monkeypatch,
                                                                   tmp_path, capsys):
    """Measured: 2 of 23 Workflow results were over the judge's budget (226,811 and 129,195
    chars). Every key and item stays, every long string keeps its head, the state fits, and the
    triage row names what was cut. A report no cut down to TRIAGE_LEAF_MIN rescues stays whole,
    the judge refuses it, and the reason is on stderr."""
    findings = [{"claim": f"finding {i}: " + "evidence " * 60, "confidence": "measured"}
                for i in range(300)]
    result = {"designs": findings, "verdict": "Recommend the minimal design. " + "why " * 9000}
    run = {"executor": "workflow", "answer": [hippo_cli.TaskNote(9, "w", None, "completed",
                                                                  "cut", False, None)],
           "brief": "script", "scope": "big", "agents": [], "stats": {"edits": {}},
           "summary": tmp_project / "wf.json"}
    run["summary"].write_text(json.dumps({"result": json.dumps(result)}), encoding="utf-8")
    jev_capture = tmp_path / "jev-request.json"
    monkeypatch.setenv("HIPPO_JEV_BACKEND", "mock")
    monkeypatch.setenv("HIPPO_JEV_MOCK_OUTPUT",
                       str(_mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT})))
    monkeypatch.setenv("HIPPO_JEV_MOCK_CAPTURE", str(jev_capture))
    (tmp_project / ".hippo" / "ledger.jsonl").write_text(json.dumps(
        {"t": _ts(), "ev": "dispatch", "id": "ag-wf", "kind": "impl",
         "exec": "workflow/claude-opus-5-5/high", "scope": "big"}) + "\n", encoding="utf-8")

    assert hippo_cli.native_triage(tmp_project / ".hippo", run, "ag-wf", "impl") is True
    state = json.loads(jev_capture.read_text(encoding="utf-8"))["state"]
    assert len(json.dumps(state, ensure_ascii=False)) <= hippo_cli.JEV_STATE_BUDGET_CHARS
    report = state["report"]
    assert list(report) == ["designs", "verdict"] and len(report["designs"]) == 300
    assert report["designs"][299]["confidence"] == "measured"
    assert report["designs"][299]["claim"].startswith("finding 299: evidence")
    assert report["verdict"].startswith("Recommend the minimal design.")
    assert report["verdict"].endswith("…") and len(report["verdict"]) < len(result["verdict"])
    [t] = _rows(tmp_project, "triage")
    assert (t["route"], t["trimmed"]) == ("accept-candidate", ["report"])

    items = {"items": [str(i) * 5 for i in range(30000)]}  # no string is long: nothing to cut
    state = {"brief": "b", "report": items}
    assert hippo_cli.fit_triage_state(state) == [] and state["report"] is items
    run["summary"].write_text(json.dumps({"result": json.dumps(items)}), encoding="utf-8")
    capsys.readouterr()
    assert hippo_cli.native_triage(tmp_project / ".hippo", run, "ag-wf", "impl") is True
    assert ("native: ag-wf no triage — the judge did not answer (state exceeds jev budget"
            in capsys.readouterr().err)
    assert len(_rows(tmp_project, "triage")) == 1


T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
WATCH_TUID = "toolu_01WatchWatchWatchWatchWa"


def _at(minutes, line):
    """`line` stamped `minutes` after T0 — the interim rule reads time, so these tests set it."""
    stamp = (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {**line, "timestamp": stamp}


def _watcher(agent, report_at, resumes=(), tool="Monitor", task="m1", persistent=False):
    """An agent that armed a 20-minute watcher (a persistent one, on hosts 2.1.246-258: no
    deadline) or started a background command at minute 1 and reported at `report_at`;
    `resumes` are the lines it wrote when it was woken again."""
    inp = ({"command": "tail -f run.log | grep --line-buffered DONE", "timeout_ms": 1_200_000}
           if tool == "Monitor" else {"command": "sleep 3600", "run_in_background": True})
    tur = ({"taskId": task, "timeoutMs": 0 if persistent else 1_200_000, "persistent": persistent}
           if tool == "Monitor" else {"backgroundTaskId": task})
    return [_at(0, {"type": "user", "message": {"role": "user", "content": BRIEF}}),
            _at(1, _assistant({"type": "tool_use", "id": WATCH_TUID, "name": tool,
                               "input": inp})),
            _at(1, {"type": "user", "toolUseResult": tur, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": WATCH_TUID, "content": "armed"}]}}),
            _at(report_at - 0.1, _assistant({"type": "text", "text": REPORT})),
            *resumes]


def test_an_interim_only_run_is_read_once_its_work_has_ended(tmp_project, run_hippo, tmp_path):
    """Measured (mlx-vlm): an agent reported, stopped with a `tail -f` Monitor still armed,
    and the host never delivered the Monitor's expiry to it — no final notification came.
    Its interim reports are its answer, read once the watcher's deadline has passed with the
    agent still idle: not before, and a final notification arriving later is not read again,
    even though that one reading failed."""
    jev = _mock(tmp_path, {"answers": ACCEPT, "default": DEFAULT})
    broken = _mock(tmp_path, {"default": {"noul": 0.5}}, "broken.json")  # harvest has a score
    jev_capture = tmp_path / "jev-request.json"
    _write(_session(tmp_project) / "subagents" / f"agent-{AGENT}.jsonl", _watcher(AGENT, 15))
    _write(tmp_project / "transcript.jsonl", [
        _at(0, _user("run the e2e in a subagent")), _call(), _at(0, _launched()),
        _at(15, _user(_note(interim=True, result=REPORT))),
        _at(16, _user(_note(interim=True, tuid=None, result="The watcher timed out; the "
                                                             "report above stands.")))])
    _scribe(run_hippo, tmp_project,
            _clerk(tmp_path, "w1", [{"ev": "dispatch", "id": DID, "kind": "impl"}]), jev)
    assert _rows(tmp_project, "dispatch", id=DID) and not _rows(tmp_project, "triage"), (
        "the watcher's deadline (minute 21) has not passed: the agent may still report")

    _write(tmp_project / "transcript.jsonl", [_at(30, _user("anything else?"))], mode="a")
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w2"), broken, jev_capture=jev_capture)
    state = json.loads(jev_capture.read_text(encoding="utf-8"))["state"]
    assert state["report"] == f"{REPORT}\n\nThe watcher timed out; the report above stands."
    assert [e["ok"] for e in _rows(tmp_project, "clerk", name="jev-harvest")] == [False]

    late = _note(tuid=None, result="Late final report.")
    _write(_session(tmp_project) / "subagents" / f"agent-{AGENT}.jsonl", _watcher(AGENT, 15, [
        _at(39, _user(late)), _at(39.5, _assistant({"type": "text", "text": "Late final."}))]))
    _write(tmp_project / "transcript.jsonl", [_at(40, _user(late))], mode="a")
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w3"), jev)
    assert not _rows(tmp_project, "triage")
    assert len(_rows(tmp_project, "clerk", name="jev-harvest")) == 1


def test_an_answer_waits_for_work_without_a_deadline_and_ends_at_a_sendmessage(tmp_path):
    """A background command has no deadline: the interim report is not read however long the
    agent sits idle, and the final notification it resumed to send completes the answer. A
    report that answers a SendMessage is not the answer to the brief — main moved on from the
    interim one, which becomes the answer in that window."""
    bash, sent = "abash000000000000", "asent000000000000"
    bash_tuid, sent_tuid, msg_tuid = ("toolu_01BashRunBashRunBashRun", "toolu_01SentSentSentSent",
                                      "toolu_01SendMessageSendMessa")
    done = ("<task-notification>\n<task-id>b9</task-id>\n<status>completed</status>\n"
            "<summary>Background command finished</summary>\n</task-notification>")
    session = tmp_path / "t"
    _write(session / "subagents" / f"agent-{bash}.jsonl", _watcher(bash, 5, [
        _at(200, _user(done)), _at(201, _assistant({"type": "text", "text": "All done."}))],
        tool="Bash", task="b9"))
    _write(session / "subagents" / f"agent-{sent}.jsonl", _watcher(sent, 5, [
        _at(6, _user("also check the 429 path")),
        _at(7, _assistant({"type": "text", "text": "429 checked."}))]))
    lines = [_call(bash_tuid, desc="bench"), _at(0, _launched(bash_tuid, bash, "bench")),  # 1, 2
             _call(sent_tuid, desc="e2e"), _at(0, _launched(sent_tuid, sent, "e2e")),     # 3, 4
             _at(5, _user(_note(bash, bash_tuid, interim=True, result="started"))),        # 5
             _at(5, _user(_note(sent, sent_tuid, interim=True))),                          # 6
             _at(7, _user(_note(sent, msg_tuid, interim=True, result="429 checked."))),   # 7
             _at(190, _user("still there?")),                                              # 8
             _at(202, _user(_note(bash, None, result="bench: 3.1x faster")))]             # 9
    path = _write(tmp_path / "t.jsonl", lines)

    runs = hippo_cli.native_index(path, 6, 8)
    assert runs["ag-" + bash]["answer"] is None, "a command with no deadline may still end"
    assert [n.line for n in runs["ag-" + sent]["answer"]] == [6]
    assert runs["ag-" + sent]["first_now"] is True
    assert hippo_cli.native_index(path, 0, 6)["ag-" + sent]["answer"] is None
    runs = hippo_cli.native_index(path, 8, 9)
    assert [n.report for n in runs["ag-" + bash]["answer"]] == ["started", "bench: 3.1x faster"]
    assert (runs["ag-" + bash]["first_now"], runs["ag-" + sent]["first_now"]) == (True, False)


def test_an_interim_only_run_stays_in_flight_until_its_answer_is_complete(tmp_path):
    """After a compaction main's capsule names its background runs still out (§6). An interim
    notification promises a final one, so the run stays listed while the agent's own
    background work runs; once that work has ended with the agent idle — the Monitor's deadline
    passed — the interim report is the answer (the scribe's rule), and the run is back."""
    bench, e2e = "abench00000000000", "ae2e0000000000000"
    tb, te = "toolu_01BenchBenchBenchBench", "toolu_01E2eE2eE2eE2eE2eE2e"
    session = tmp_path / "t"
    _write(session / "subagents" / f"agent-{bench}.jsonl",
           _watcher(bench, 5, tool="Bash", task="b9"))
    _write(session / "subagents" / f"agent-{e2e}.jsonl", _watcher(e2e, 5))  # due at minute 21
    path = _write(tmp_path / "t.jsonl", [
        _call(tb, desc="bench"), _at(0, _launched(tb, bench, "bench")),
        _call(te, desc="e2e"), _at(0, _launched(te, e2e, "e2e")),
        _at(5, _user(_note(bench, tb, interim=True, result="started"))),
        _at(5, _user(_note(e2e, te, interim=True, result="armed")))])
    [flying] = hippo_cli.native_in_flight(path)
    assert flying.startswith("bench (subagent · 2h"), flying


def _event(task, text):
    """A Monitor's event: a notification with no status, its expiry included."""
    return (f"<task-notification>\n<task-id>{task}</task-id>\n<summary>Monitor event: "
            f"\"watch\"</summary>\n<event>{text}</event>\n</task-notification>")


def test_a_monitor_ends_when_its_notice_reaches_the_agent_else_at_its_deadline(tmp_path):
    """Measured: the host delivers a Monitor's expiry from 0.40s before its deadline to 0.27s
    after. An expiry that wakes the agent just after the deadline ends the Monitor there, so
    the run is not settled at the deadline, and its answer runs on to the final report it
    resumed to send, the interim one kept before it. Events before the agent's report are a
    live Monitor's, not its end; with no notice after it, the Monitor ends at its deadline. A
    persistent one has none, and an event of its own is not its end either."""
    late, evts, pers = "alate000000000000", "aevts000000000000", "apers000000000000"
    tl, te, tp = ("toolu_01LateLateLateLateLate", "toolu_01EvtsEvtsEvtsEvtsEvts",
                  "toolu_01PersPersPersPersPers")
    session = tmp_path / "t"
    expiry = "[Monitor expired after 20m with 0 events delivered.]"
    _write(session / "subagents" / f"agent-{late}.jsonl", _watcher(late, 15, [
        _at(21.02, _user(_event("m1", expiry))),
        _at(24.9, _assistant({"type": "text", "text": "FINAL: e2e actually failed."}))]))
    watched = _watcher(evts, 15)
    watched[3:3] = [_at(5, _queued(_event("m1", "step 1 ok"))),
                    _at(9, _queued(_event("m1", "step 2 ok")))]
    _write(session / "subagents" / f"agent-{evts}.jsonl", watched)
    _write(session / "subagents" / f"agent-{pers}.jsonl", _watcher(pers, 15, [
        _at(17, _queued(_event("m1", "step 3 ok")))], persistent=True))
    lines = [_call(tl, desc="late"), _at(0, _launched(tl, late, "late")),               # 1, 2
             _call(te, desc="evts"), _at(0, _launched(te, evts, "evts")),               # 3, 4
             _call(tp, desc="pers"), _at(0, _launched(tp, pers, "pers")),               # 5, 6
             *(_at(15, _user(_note(a, u, interim=True)))
               for a, u in ((late, tl), (evts, te), (pers, tp))),                       # 7-9
             _at(18, _user("main works on")),                                           # 10
             _at(22, _user("main keeps working")),                                      # 11
             _at(25, _user(_note(late, None, result="FINAL: e2e actually failed."))),   # 12
             _at(200, _user("much later"))]                                             # 13
    path = _write(tmp_path / "t.jsonl", lines)

    def answers(since, end):
        runs = hippo_cli.native_index(path, since, end)
        return {a: (runs["ag-" + a]["answer"] and [n.line for n in runs["ag-" + a]["answer"]],
                    runs["ag-" + a]["first_now"]) for a in (late, evts, pers)}

    none = (None, False)
    assert answers(9, 10) == {late: none, evts: none, pers: none}, "no deadline has passed"
    assert answers(10, 11) == {late: none, evts: ([8], True), pers: none}
    assert answers(11, 12)[late] == ([7, 12], True)
    assert answers(12, 13)[pers] == none


def test_an_event_written_late_never_ends_a_monitor_before_its_deadline(tmp_path):
    """Measured (mlx-vlm): the host can hold a notice for an idle agent and write it into the
    agent's transcript, under its own earlier time, only when a SendMessage resumes it. Such
    an event, stamped between the report and the deadline, does not settle the run back in a
    window that found it still watching: the interim report becomes the answer in the
    SendMessage's window, and the earlier window still reads as it did."""
    a, tl, ts = "astale00000000000", "toolu_01StaleStaleStaleStale", "toolu_01SendMessageSendMessa"
    agent = tmp_path / "t" / "subagents" / f"agent-{a}.jsonl"
    path = _write(tmp_path / "t.jsonl", [
        _call(tl, desc="e2e"), _at(0, _launched(tl, a, "e2e")),                   # 1, 2
        _at(15, _user(_note(a, tl, interim=True))),                               # 3
        _at(18, _user("main works on"))])                                         # 4
    _write(agent, _watcher(a, 15))
    assert hippo_cli.native_index(path, 2, 4)["ag-" + a]["answer"] is None, "deadline: minute 21"

    _write(agent, _watcher(a, 15, [
        _at(19, _user("also check the 429 path")),
        _at(16, _queued(_event("m1", "step 3 ok"))),
        _at(19.5, _assistant({"type": "text", "text": "429 checked."}))]))
    _write(path, [_at(19.6, _user(_note(a, ts, interim=True, result="429 checked.")))], "a")
    run = hippo_cli.native_index(path, 4, 5)["ag-" + a]
    assert ([n.line for n in run["answer"]], run["first_now"]) == ([3], True)
    assert hippo_cli.native_index(path, 2, 4)["ag-" + a]["answer"] is None


def test_a_restated_launch_keeps_main_s_verdict(tmp_project, run_hippo, tmp_path):
    """The clerk coined its own id for the Agent launch and put main's verdict on it. The
    restatement is dumped; the run is recorded under its own id with that kind, and the
    verdict lands there."""
    _write(tmp_project / "transcript.jsonl", _launch_window() + [_user(_note()),
                                                                 _user("looks right, merge")])
    _agent(_session(tmp_project), msgs=[_msg("m1")])
    events = [{"ev": "outcome", "ref": "d9", "result": "accepted", "note": "merged"},
              {"ev": "dispatch", "id": "d9", "kind": "fix", "exec": "subagent/opus/inherit",
               "scope": "add retries to fetch"}]
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "restates", events))

    [d] = _rows(tmp_project, "dispatch")
    assert (d["id"], d["kind"]) == (DID, "fix")
    assert [(o["ref"], o["result"]) for o in _rows(tmp_project, "outcome")] == [(DID, "accepted")]
    assert "record it there, under its listed id" in _dumps(tmp_project)


def test_a_restatement_names_its_run_not_the_only_one_in_sight(tmp_project, run_hippo,
                                                              tmp_path):
    """Window 2 touched only the retry-loop run, and main refuted a survey from window 1. The
    clerk restated the survey under coined ids: the one whose scope is the survey's
    description lands on the survey's row; the one naming no run is not moved onto the retry
    loop, which this output recorded under its own id — although the restatement comes first
    in the output (listed runs are read first). Its verdict stays in the dump."""
    q, tq = "a1111111111111111", "toolu_01QQQQQQQQQQQQQQQQQQQQQQQQ"
    _write(tmp_project / "transcript.jsonl", [
        _user("survey the api in a subagent"),
        _call(tuid=tq, desc="survey api", prompt="Survey the HTTP API."),
        _launched(tuid=tq, agent=q, desc="survey api"),
        _user(_note(task=q, tuid=tq, result="12 endpoints."))])
    _agent(_session(tmp_project), agent=q, msgs=[_msg("q1")], brief="Survey the HTTP API.")
    _scribe(run_hippo, tmp_project,
            _clerk(tmp_path, "w1", [{"ev": "dispatch", "id": "ag-" + q, "kind": "research"}]))

    _write(tmp_project / "transcript.jsonl", _launch_window(), mode="a")
    _agent(_session(tmp_project), msgs=[_msg("r1")])
    coined = {"kind": "research", "exec": "subagent/opus/inherit"}
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "w2", [
        {"ev": "dispatch", "id": "sv2", "scope": "the caching note", **coined},
        {"ev": "outcome", "ref": "sv2", "result": "refuted", "note": "caching note wrong"},
        {"ev": "dispatch", "id": "sv1", "scope": "Survey  API", **coined},
        {"ev": "outcome", "ref": "sv1", "result": "refuted", "note": "missed the v2 endpoints"},
        {"ev": "dispatch", "id": DID, "kind": "impl"}]))

    assert [(d["id"], d["kind"]) for d in _rows(tmp_project, "dispatch")] == [
        ("ag-" + q, "research"), (DID, "impl")]
    assert [(o["ref"], o["result"]) for o in _rows(tmp_project, "outcome")] == [
        ("ag-" + q, "refuted")]
    assert "caching note wrong" in _dumps(tmp_project)


def test_task_ref_skips_an_unjudged_native_row(tmp_project, run_hippo):
    """A subagent whose brief names the task gets tagged with it and rarely a verdict; it must
    not make `--ref task:<id>` ambiguous for the lane main is judging. Alone, it is the one."""
    for e in ({"ev": "dispatch", "id": "lane1", "kind": "impl", "exec": "codex/gpt-6-sol/high",
               "scope": "retry loop", "task": "feat/retry"},
              {"ev": "dispatch", "id": DID, "kind": "research", "scope": "survey",
               "exec": "subagent/claude-opus-5-5/inherit", "task": "feat/retry"}):
        assert run_hippo(["log", "raw", json.dumps(e)], cwd=tmp_project).returncode == 0
    for _ in range(2):
        proc = run_hippo(["log", "outcome", "--ref", "task:feat/retry", "--result", "accepted"],
                         cwd=tmp_project)
        assert proc.returncode == 0, proc.stderr
    assert [o["ref"] for o in _rows(tmp_project, "outcome")] == ["lane1", DID]


def test_main_s_own_log_dispatch_is_the_record(tmp_project, run_hippo, tmp_path):
    """Main logged the run itself under its own id, the scope its description in other case
    and spacing: nothing is listed and the run's cost lands on main's row."""
    assert run_hippo(["log", "dispatch", "--id", "d1", "--kind", "impl", "--exec",
                      "subagent/opus/inherit", "--scope", "Retry  LOOP"],
                     cwd=tmp_project).returncode == 0
    _write(tmp_project / "transcript.jsonl", _launch_window() + [_user(_note())])
    _agent(_session(tmp_project), msgs=[_msg("m1")])
    capture = tmp_path / "clerk.txt"
    _scribe(run_hippo, tmp_project, _clerk(tmp_path, "quiet"), capture=capture)

    assert "# native runs to record" not in _payload(capture)
    assert [d["id"] for d in _rows(tmp_project, "dispatch")] == ["d1"]
    assert [u["ref"] for u in _rows(tmp_project, "usage")] == ["d1"]


def test_a_native_ref_not_in_the_ledger_says_what_to_do(tmp_project, run_hippo):
    """A run id with no row yet needs no call. A ref built from a Workflow's Task ID (printed
    first at launch) is no run id, and must not be told "no call needed" — that drops the
    verdict. A run main logged itself under its bare id points at that row."""
    def refused(ref):
        proc = run_hippo(["log", "outcome", "--ref", ref, "--result", "accepted"],
                         cwd=tmp_project)
        assert proc.returncode != 0
        return proc.stderr

    assert "no call needed" in refused(DID)
    task_id = refused("ag-wc5gduida")
    assert "is not a run id" in task_id and "no call needed" not in task_id
    assert run_hippo(["log", "dispatch", "--id", "wf_1234abcd-567", "--kind", "impl", "--exec",
                      "workflow/opus/high", "--scope", "port it"], cwd=tmp_project).returncode == 0
    assert "--ref wf_1234abcd-567" in refused("ag-wf_1234abcd-567")


def test_a_bug_on_the_native_path_never_costs_the_clerk(tmp_project, tmp_path, monkeypatch):
    _write(tmp_project / "transcript.jsonl", _launch_window())
    monkeypatch.setenv("HIPPO_CLERK_BACKEND", "mock")
    monkeypatch.setenv("HIPPO_MOCK_OUTPUT", str(_clerk(tmp_path, "quiet")))
    monkeypatch.setattr(hippo_cli, "native_index", lambda *a: 1 / 0)
    hp = tmp_project / ".hippo"
    hippo_cli.cmd_scribe(types.SimpleNamespace(
        hp=hp, transcript=str(tmp_project / "transcript.jsonl"), session="s1"))
    assert [e["ok"] for e in read_ledger(tmp_project) if e.get("name") == "turn-scribe"] == [True]
    [dump] = (hp / "failures").glob("*-native-*")
    assert "ZeroDivisionError" in dump.read_text()


# --------------------------------------------------------------------------
# the index, over the delivery shapes measured on real transcripts
# --------------------------------------------------------------------------

def test_the_index_reads_every_measured_delivery_shape(tmp_path):
    """A notification arrives on a user line or a `queued_command` attachment; after a
    SendMessage it names only the task-id; an interim one is no completion. Bookkeeping lines,
    a tag quoted in a tool's output, a Bash task and a failed launch are not runs; a foreground
    call is a launch and a completion at once. A fork's executor comes from its meta.json, a hippo:lane agent is indexed but never listed, and a
    nested run carries its parent."""
    fork, failed, lane = "toolu_01ForkForkForkForkFork2", "toolu_01FailFailFailFailFail3", \
        "toolu_01LaneLaneLaneLaneLane4"
    fg = "toolu_01ForeForeForeForeFore6"  # a foreground call: its report comes back inline
    lines = [
        _call(), _launched(),                                                        # 1, 2
        _call(fork, desc="fork arm", subagent_type="fork"),                          # 3
        _launched(fork, agent="afork", desc="fork arm"),                             # 4
        _call(failed),                                                               # 5
        {"type": "user", "message": {"role": "user", "content": [{                  # 6
            "type": "tool_result", "tool_use_id": failed, "is_error": True,
            "content": "Error: Cannot create agent worktree"}]},
         "toolUseResult": "Error: Cannot create agent worktree"},
        *_bash(),                                                                    # 7, 8
        {"type": "queue-operation", "operation": "enqueue", "content": _note()},    # 9
        _queued(_note(interim=True, result="waiting on a benchmark")),              # 10
        {"type": "user", "message": {"role": "user", "content": [{                  # 11
            "type": "tool_result", "tool_use_id": "toolu_01Grep", "content": _note()}]}},
        _user(_note(task="b77", tuid=BASH_TUID, result="exit 0")),                   # 12
        _user(_note(task="afork", tuid=None, status="killed", result=None)),        # 13
        "not json at all",                                                           # 14
        _user(_note()),                                                              # 15
        _call(lane, desc="watch lane", subagent_type="hippo:lane"),                  # 16
        _launched(lane, agent="alane", desc="watch lane"),                           # 17
        _call(fg, desc="quick check"),                                               # 18
        {"type": "user", "message": {"role": "user", "content": [{                  # 19
            "type": "tool_result", "tool_use_id": fg, "content": "[hand-back] ok"}]},
         "toolUseResult": {"status": "completed", "agentId": "afg", "content": [
             {"type": "text", "text": "fine as it is"}]}},
    ]
    path = _write(tmp_path / "t.jsonl", lines)
    session = tmp_path / "t"
    _agent(session, "afork", fork=True, msgs=[_msg("f1", out=40)])
    _agent(session, "alane", agentType="hippo:lane")
    kid, kid_tuid = "akid0000000000000", "toolu_01KidKidKidKidKidKidKid5"
    _agent(session, AGENT, msgs=[[_call(kid_tuid, desc="nested")], [_launched(kid_tuid, kid)],
                                 [_user(_note(task=kid, tuid=kid_tuid, result="kid done"))]])
    _agent(session, kid, parentAgentId=AGENT, msgs=[_msg("k1")])

    runs = hippo_cli.native_index(path, 0, 19)
    assert set(runs) == {DID, "ag-afork", "ag-alane", "ag-" + kid, "ag-afg"}
    assert [(n.line, n.report) for n in runs["ag-afg"]["notes"]] == [(19, "fine as it is")]
    main, fk, ln, nested = runs[DID], runs["ag-afork"], runs["ag-alane"], runs["ag-" + kid]
    assert [(n.line, n.status, n.interim) for n in main["notes"]] == [
        (10, "completed", True), (15, "completed", False)]
    assert [(n.line, n.status, n.report) for n in fk["notes"]] == [(13, "killed", None)]
    assert (fk["executor"], ln["skip"], nested["parent"]) == ("fork", True, DID)
    assert [n.report for n in nested["notes"]] == ["kid done"]
    assert hippo_cli.native_stats(fk)["usage"]["claude-opus-5-5"]["tout"] == 40, (
        "a fork is not billed for main's launching message copied into its transcript")

    runs = hippo_cli.native_index(path, 10, 17)
    assert (runs[DID]["seen"], runs[DID]["first_now"], runs[DID]["line"]) == (True, True, 2)
    runs = hippo_cli.native_index(path, 0, 12)
    assert runs[DID]["first_now"] is False, "an interim notification is not a completion"
    assert "ag-alane" not in hippo_cli.native_index(path, 0, 15), "nothing past `end` is read"


def test_a_mark_keeps_the_last_time_a_line_carries_itself(tmp_path):
    """A file-history snapshot names a timestamp only inside its snapshot: a window that ends
    on one keeps the time of the last line before it."""
    path = _write(tmp_path / "t.jsonl", [
        _at(0, _user("go")),
        {"type": "file-history-snapshot", "messageId": "m", "isSnapshotUpdate": False,
         "snapshot": {"messageId": "m", "trackedFileBackups": {}, "timestamp": _ts(0)}}])
    assert hippo_cli.native_scan(path, marks=(1, 2))[2] == {1: T0, 2: T0}


def test_an_isolated_agent_s_changes_are_read_against_an_honest_base(tmp_path):
    """HEAD never moved: the working tree is all of it. Committed and not yet in main's branch:
    against the merge-base. Already merged: no base is honest, so no git facts at all."""
    def git(d, *a):
        subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)

    repo, wt = tmp_path / "repo", tmp_path / "repo" / "wt"
    repo.mkdir()
    (repo / ".hippo").mkdir()
    git(repo, "init", "-q", "-b", "dev")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty",
        "-m", "base")
    git(repo, "worktree", "add", "-q", "-b", "agent", str(wt))
    (wt / "new.py").write_text("x = 1\n")
    facts = hippo_cli.worktree_changes(repo / ".hippo", wt)
    assert facts.splitlines()[1:] == ["?? new.py"]

    git(wt, "add", "new.py")
    git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "work")
    facts = hippo_cli.worktree_changes(repo / ".hippo", wt)
    assert "new.py | 1 +" in facts

    git(repo, "merge", "-q", "--ff-only", "agent")
    assert hippo_cli.worktree_changes(repo / ".hippo", wt) is None


def test_a_workflow_s_changes_are_read_agent_by_agent(tmp_path):
    """Measured on wf_cf850115-50b: 5 of the 12 paths its agents edited were scratch files, and
    its fix agent's commit in its own worktree — no edit-tool call — was in no list. An
    isolated agent is read by git in its worktree (its meta.json names it); an agent with none
    adds the project files it edited, never a scratch file; a hippo:lane agent adds nothing."""
    def git(d, *a):
        subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)

    repo = tmp_path / "repo"
    (repo / ".hippo").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "dev")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty",
        "-m", "base")
    wt = repo / ".claude" / "worktrees" / f"{WF}-1"
    git(repo, "worktree", "add", "-q", "-b", f"worktree-{WF}-1", str(wt))
    (wt / "shell_made.py").write_text("x = 1\n")
    runs = repo / "t" / "subagents" / "workflows" / WF
    agents = {"a1": ([_msg("i1")], {"worktreePath": str(wt)}),
              "a2": ([_msg("v1", edit=str(repo / "src" / "x.py")),
                      _msg("v2", edit=str(tmp_path / "scratch" / "probe.py"))], {}),
              "a3": ([_msg("l1", edit=str(repo / "lane.py"))], {"agentType": "hippo:lane"})}
    for aid, (msgs, meta) in agents.items():
        _write(runs / f"agent-{aid}.jsonl", [{"type": "user", "message": {"content": "x"}}]
               + [ln for m in msgs for ln in m])
        (runs / f"agent-{aid}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    path = _write(repo / "t.jsonl", _wf_launch())

    changes = hippo_cli.native_changes(repo / ".hippo", hippo_cli.native_index(path, 0, 2)[WF_ID])
    facts, listed = changes.split(f"\n\n{hippo_cli.NATIVE_RUN_EDITS_HEAD}:\n")
    assert facts.startswith(f"git in the agent's worktree {wt}") and "?? shell_made.py" in facts
    assert listed.splitlines() == [str(repo / "src" / "x.py")]


def test_the_model_is_the_price_sheet_key():
    assert hippo_cli.native_model("claude-opus-5-5[1m]") == "claude-opus-5-5"
    assert hippo_cli.native_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert "claude-opus-5-5" in hippo_cli.load_prices()["models"]


# --------------------------------------------------------------------------
# PRIORS
# --------------------------------------------------------------------------

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def _t(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_priors_read_the_last_row_per_model_and_leave_native_runs_out_of_open_items():
    rows = [{"t": _t(30), "ev": "dispatch", "id": DID, "kind": "impl",
             "exec": "subagent/claude-opus-5-5/xhigh", "scope": "retry loop", "src": "scribe"},
            {"t": _t(30), "ev": "dispatch", "id": "d7", "kind": "impl",
             "exec": "codex/gpt-6-sol/high", "scope": "lane", "src": "wrapper"},
            {"t": _t(29), "ev": "usage", "ref": DID, "model": "claude-opus-5-5", "tokens": 10,
             "tin": 8, "tcached": 0, "tout": 2, "src": "scribe"},
            {"t": _t(28), "ev": "usage", "ref": DID, "model": "claude-opus-5-5", "tokens": 1000,
             "tin": 1000000, "tcached": 0, "tout": 0, "src": "scribe"},
            {"t": _t(28), "ev": "usage", "ref": DID, "model": "claude-haiku-4-5", "tokens": 5,
             "tin": 1000000, "tcached": 0, "tout": 0, "src": "scribe"},
            {"t": _t(27), "ev": "outcome", "ref": DID, "result": "accepted", "src": "scribe"}]
    [cell] = hippo_cli.prior_cells(rows).values()
    assert (cell["tokens"], round(cell["usd"], 2), cell["priced"]) == (1005, 5.0, 1)

    page = hippo_cli.prior_facts(rows[:2], NOW)
    assert "- d7 (30h00m)" in page and f"- {DID}" not in page
    assert "native runs (`ag-`) with no verdict, left out of this list: 1" in page


def test_a_scribe_triage_after_the_verdict_still_counts():
    """Main typed its verdict during the turn; the scribe read the report at Stop, after it —
    main never saw that route, so it measures the judge."""
    rows = [{"t": _t(3), "ev": "dispatch", "id": DID, "kind": "impl",
             "exec": "subagent/claude-opus-5-5/xhigh", "scope": "retry loop", "src": "scribe"},
            {"t": _t(2), "ev": "outcome", "ref": DID, "result": "refuted", "src": "cli"},
            {"t": _t(1), "ev": "triage", "ref": DID, "route": "accept-candidate",
             "src": "scribe"}]
    section = "\n".join(hippo_cli.triage_agreement(rows))
    assert "accept-candidate precision 0/1" in section
