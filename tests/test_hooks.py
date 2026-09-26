"""Item (8): hooks/stop.sh and hooks/session_start.sh — silent no-op path.

DESIGN.md §3.4: both hooks parse the Claude Code hook JSON off stdin
(`transcript_path` / `session_id` / `cwd` for Stop; `cwd` for SessionStart)
and, when `.hippo/` is absent for that `cwd`, must be a complete silent
no-op: exit 0, 0 bytes on stdout and stderr (the same global rule as every
other surface).

A couple of light bonus checks (initialized-project wiring, Stop's detached
"return immediately" contract) are included since they are directly written
into §3.4's text and are cheap to verify — but the primary scope per the
assignment is the no-op path.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

from conftest import REPO_ROOT


def _run_hook(script_path, payload: dict, cwd, env=None, timeout=10):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    # Same rule as conftest's run_hippo: the Stop hook launches a real scribe, and this
    # machine's environment carries a live TYPESAFE_API_KEY (DESIGN §3.9).
    full_env.setdefault("HIPPO_JEV_BACKEND", "off")
    return subprocess.run(
        ["bash", str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_stop_hook_silent_noop_when_uninitialized(uninitialized_dir, repo_root):
    payload = {
        "session_id": "sess-x",
        "transcript_path": str(uninitialized_dir / "transcript.jsonl"),
        "cwd": str(uninitialized_dir),
        "hook_event_name": "Stop",
    }
    proc = _run_hook(repo_root / "hooks" / "stop.sh", payload, cwd=uninitialized_dir)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_session_start_hook_silent_noop_when_uninitialized(uninitialized_dir, repo_root):
    payload = {
        "session_id": "sess-y",
        "cwd": str(uninitialized_dir),
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    proc = _run_hook(
        repo_root / "hooks" / "session_start.sh", payload, cwd=uninitialized_dir
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


def _capsule(proc):
    """The capsule out of a SessionStart hook run.

    Both hosts read `hookSpecificOutput.additionalContext`; codex 0.146 additionally
    *requires* it (bare text is rejected as invalid JSON, measured on a host upgrade),
    so the JSON envelope is the contract this suite pins.
    """
    out = json.loads(proc.stdout)
    hs = out["hookSpecificOutput"]
    assert hs["hookEventName"] == "SessionStart"
    return hs["additionalContext"]


def test_session_start_hook_emits_capsule_when_initialized(tmp_project, repo_root):
    payload = {
        "session_id": "sess-z",
        "cwd": str(tmp_project),
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    proc = _run_hook(repo_root / "hooks" / "session_start.sh", payload, cwd=tmp_project)
    assert proc.returncode == 0
    assert _capsule(proc).strip().startswith("[hippo]")


def test_session_start_uses_stdin_cwd_not_process_cwd(tmp_project, repo_root):
    """§3.4 has the hook read `cwd` off the stdin JSON, so the capsule must be
    produced for *that* project even when the hook process itself was started
    somewhere else. Running the hook from the project dir (as the other tests
    do) cannot distinguish a correct implementation from one that silently
    relies on the inherited working directory.

    repo_root is used as the foreign process cwd: it is guaranteed to differ
    from tmp_project and, having a .git, terminates the upward walk without
    finding a .hippo of its own. (Note `uninitialized_dir` would NOT work
    here — it and `tmp_project` share the same underlying `tmp_path`.)
    """
    payload = {"cwd": str(tmp_project), "hook_event_name": "SessionStart"}
    proc = _run_hook(
        repo_root / "hooks" / "session_start.sh", payload, cwd=repo_root
    )
    assert proc.returncode == 0
    assert _capsule(proc).strip().startswith("[hippo]"), (
        f"hook must honor the stdin cwd, got stdout={proc.stdout!r}"
    )


def test_stop_hook_returns_immediately_detached(
    tmp_project, repo_root, fake_transcript, valid_mock_output
):
    payload = {
        "session_id": "sess-detach",
        "transcript_path": str(fake_transcript),
        "cwd": str(tmp_project),
        "hook_event_name": "Stop",
    }
    env = {
        "HIPPO_CLERK_BACKEND": "mock",
        "HIPPO_MOCK_OUTPUT": str(valid_mock_output),
    }
    start = time.monotonic()
    proc = _run_hook(
        repo_root / "hooks" / "stop.sh", payload, cwd=tmp_project, env=env
    )
    elapsed = time.monotonic() - start
    assert proc.returncode == 0
    # DESIGN.md §3.4 asks for <100ms (detached dispatch); a generous bound is
    # used here to avoid CI flakiness while still catching an implementation
    # that blocks Stop on the full scribe pipeline.
    assert elapsed < 2.0, f"stop.sh must not block on scribe; took {elapsed:.2f}s"


# --------------------------------------------------------------------------
# the HIPPO_DISPATCH gate (1.8.1): a lane gets the capsule, never the scribe
# --------------------------------------------------------------------------

def _worktree(tmp_project, name="pass2"):
    lane = tmp_project / ".claude" / "worktrees" / name
    lane.mkdir(parents=True)
    (lane / ".git").write_text("gitdir: ../../../.git/worktrees/pass2\n", encoding="utf-8")
    return lane


def test_session_start_walks_through_a_worktree_for_a_lane(tmp_project, repo_root):
    lane = _worktree(tmp_project)
    payload = {"cwd": str(lane), "hook_event_name": "SessionStart", "source": "compact"}
    proc = _run_hook(repo_root / "hooks" / "session_start.sh", payload, cwd=lane,
                     env={"HIPPO_DISPATCH": "dlane1"})
    assert proc.returncode == 0
    capsule = _capsule(proc)
    assert capsule.strip().startswith("[hippo]")
    assert "· report: hippo log outcome" in capsule


def test_session_start_stays_conservative_without_the_gate(tmp_project, repo_root):
    lane = _worktree(tmp_project, "plain")
    payload = {"cwd": str(lane), "hook_event_name": "SessionStart", "source": "startup"}
    proc = _run_hook(repo_root / "hooks" / "session_start.sh", payload, cwd=lane)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_session_start_follows_hippo_dir_from_an_unrelated_cwd(
    tmp_project, repo_root, uninitialized_dir
):
    payload = {"cwd": str(uninitialized_dir), "hook_event_name": "SessionStart",
               "source": "compact"}
    proc = _run_hook(repo_root / "hooks" / "session_start.sh", payload, cwd=uninitialized_dir,
                     env={"HIPPO_DISPATCH": "dlane1", "HIPPO_DIR": str(tmp_project / ".hippo")})
    assert proc.returncode == 0, proc.stderr
    assert "· report: hippo log outcome" in _capsule(proc)


def test_stop_follows_hippo_dir_from_an_unrelated_cwd(
    tmp_project, repo_root, uninitialized_dir, fake_transcript, valid_mock_output
):
    """No lane reaches this (HIPPO_DISPATCH exits first), but the hook decides whether to run
    from the same place the CLI resolves the ledger from."""
    payload = {"session_id": "sess-far", "transcript_path": str(fake_transcript),
               "cwd": str(uninitialized_dir), "hook_event_name": "Stop"}
    proc = _run_hook(repo_root / "hooks" / "stop.sh", payload, cwd=uninitialized_dir,
                     env={"HIPPO_DIR": str(tmp_project / ".hippo"),
                          "HIPPO_CLERK_BACKEND": "mock",
                          "HIPPO_MOCK_OUTPUT": str(valid_mock_output)})
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    cursors = tmp_project / ".hippo" / "cursors.json"
    deadline = time.monotonic() + 10
    while not cursors.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert "sess-far" in cursors.read_text(encoding="utf-8")


def test_stop_exits_at_once_for_a_lane_and_spawns_no_scribe(
    tmp_project, repo_root, fake_transcript, valid_mock_output
):
    payload = {
        "session_id": "sess-lane",
        "transcript_path": str(fake_transcript),
        "cwd": str(tmp_project),
        "hook_event_name": "Stop",
    }
    proc = _run_hook(repo_root / "hooks" / "stop.sh", payload, cwd=tmp_project,
                     env={"HIPPO_DISPATCH": "dlane1",
                          "HIPPO_CLERK_BACKEND": "mock",
                          "HIPPO_MOCK_OUTPUT": str(valid_mock_output)})
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    # A spawned scribe (even the mock) would advance a cursor and meter itself in the
    # ledger within moments; give it that moment, then assert nothing happened.
    time.sleep(1.2)
    assert not (tmp_project / ".hippo" / "cursors.json").exists()
    ledger = (tmp_project / ".hippo" / "ledger.jsonl").read_text(encoding="utf-8")
    assert '"ev": "clerk"' not in ledger and '"ev":"clerk"' not in ledger


# --------------------------------------------------------------------------
# SubagentStart and PreCompact (1.15.0, Claude Code only — §3.4)
# --------------------------------------------------------------------------

def _directives(project, run_hippo):
    for did, aud, text in (("gpu-pin", "all", "use GPUs 0 and 1 only"),
                           ("main-only", "main", "keep review replies in context"),
                           ("worker-rule", "executor", "never push; main merges")):
        proc = run_hippo(["directive", "add", "--id", did, "--audience", aud, "--text", text],
                         cwd=project)
        assert proc.returncode == 0, proc.stderr


def _subagent(project, repo_root, agent_type="general-purpose"):
    payload = {"session_id": "s", "transcript_path": str(project / "t.jsonl"),
               "cwd": str(project), "agent_id": "a1b2c3", "agent_type": agent_type,
               "hook_event_name": "SubagentStart"}
    return _run_hook(repo_root / "hooks" / "subagent_start.sh", payload, cwd=project)


WORKER_RULE = ("· hippo task, directive and outcome writes are main's — run one only when your "
               "brief asks for it, otherwise put what should change in your report")
SLICE = ["[hippo] directives 2 live — this project's recorded rules",
         "· live: use GPUs 0 and 1 only", "· live: never push; main merges", WORKER_RULE]


def test_a_subagent_gets_the_executor_directives_and_nothing_else(tmp_project, repo_root,
                                                                  run_hippo):
    """Only the envelope is injected into a subagent (plain text is dropped, measured), and the
    slice is the executor audience — no report line (a native worker's `log outcome` would land
    as main's verdict), no depth line, no tasks — under a header that names the directives as
    the project's record, not the user's voice (a Workflow agent's harness says the relayed
    request is the only one), then the one line that says hippo writes are main's."""
    _directives(tmp_project, run_hippo)
    proc = _subagent(tmp_project, repo_root)
    assert proc.returncode == 0, proc.stderr
    hs = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hs["hookEventName"] == "SubagentStart"
    assert hs["additionalContext"].splitlines() == SLICE


def test_a_worktree_isolated_subagent_gets_the_directives_too(tmp_project, repo_root,
                                                              run_hippo):
    """isolation: "worktree" starts the agent in <project>/.claude/worktrees/agent-<id>
    (measured, 2.1.281), past the linked worktree's .git file where an ordinary session's walk
    stops — and those are the agents that edit files, the ones "never push" is for."""
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_project), "-c", "user.name=t",
                        "-c", "user.email=t@t", *a], check=True, capture_output=True)

    (tmp_project / "README").write_text("x\n", encoding="utf-8")
    git("init", "-q")
    git("add", "README")  # .hippo/ stays untracked: the worktree must not carry a copy
    git("commit", "-qm", "init")
    wt = tmp_project / ".claude" / "worktrees" / "agent-a1b2c3"
    git("worktree", "add", "-q", str(wt))
    _directives(tmp_project, run_hippo)
    proc = _subagent(wt, repo_root)
    assert proc.returncode == 0, proc.stderr
    lines = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"].splitlines()
    assert lines == SLICE


def test_a_subagent_hook_is_silent_for_forks_lanes_and_outside_a_project(
        tmp_project, repo_root, run_hippo, uninitialized_dir):
    """With no directive live a worker still gets the writes line — a hippo write from its shell
    lands as main's either way — while a fork (main's context) and hippo:lane get nothing."""
    proc = _subagent(tmp_project, repo_root)  # no directive at all yet
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"] == WORKER_RULE
    for agent_type in ("fork", "hippo:lane"):
        proc = _subagent(tmp_project, repo_root, agent_type)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", ""), agent_type
    _directives(tmp_project, run_hippo)
    for agent_type in ("fork", "hippo:lane"):
        proc = _subagent(tmp_project, repo_root, agent_type)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", ""), agent_type
    proc = _subagent(uninitialized_dir, repo_root)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def _pre_compact(project, repo_root, **extra):
    payload = {"session_id": "s", "transcript_path": str(project / "t.jsonl"),
               "cwd": str(project), "hook_event_name": "PreCompact", "trigger": "auto",
               "custom_instructions": None, **extra}
    return _run_hook(repo_root / "hooks" / "pre_compact.sh", payload, cwd=project)


def test_pre_compact_asks_for_hippo_deltas_over_main_s_lists(tmp_project, repo_root, run_hippo):
    """Plain text with exit 0 is what the host appends to the compaction instructions."""
    _directives(tmp_project, run_hippo)
    for tid, title in (("feat/alpha", "Alpha"), ("fix/beta", "Beta")):
        assert run_hippo(["task", "add", tid, "--title", title, "--notes", "parser written"],
                         cwd=tmp_project).returncode == 0
    assert run_hippo(["task", "done", "fix/beta"], cwd=tmp_project).returncode == 0
    proc = _pre_compact(tmp_project, repo_root)
    assert proc.returncode == 0, proc.stderr
    text = proc.stdout
    assert "`## hippo deltas`" in text and "hippo task done <id> --note" in text
    assert "- feat/alpha — Alpha — parser written" in text
    assert "fix/beta" not in text  # done is not open
    assert "- gpu-pin — use GPUs 0 and 1 only" in text and "- main-only —" in text
    assert "worker-rule" not in text  # main's audience: a manual /compact shows this to main


def _write_tasks(project, n, notes="n" * 90):
    (project / ".hippo" / "tasks.yaml").write_text("tasks:\n" + "".join(
        f"- {{id: feat/t{i:03d}, title: task number {i}, status: pending, "
        f"updated: '2026-09-{1 + i % 28:02d}T00:00:{i % 60:02d}Z', notes: ['{notes}']}}\n"
        for i in range(n)), encoding="utf-8")


def _listed(text, head):
    """(ids on whole lines, ids on the `- also … (id only):` line) under one list header."""
    body = text.split(head + "\n", 1)[1].split("\n")
    whole, by_id = [], []
    for ln in body:
        if ln.startswith("- also ") and " (id only): " in ln:
            by_id = ln.split(" (id only): ", 1)[1].split(", ")
        elif ln.startswith("- "):
            whole.append(ln[2:].split(" — ", 1)[0])
        else:
            break
    return whole, by_id


def test_pre_compact_is_capped_and_lists_by_id_what_does_not_fit_whole(
        tmp_project, repo_root, run_hippo):
    _write_tasks(tmp_project, 40)
    proc = _pre_compact(tmp_project, repo_root)
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout) <= 3000
    whole, by_id = _listed(proc.stdout, "open tasks (id — title — notes):")
    assert 0 < len(whole) < 40 and len(whole) + len(by_id) == 40
    assert "more not shown" not in proc.stdout
    # Past even the ids, the least recently updated are cut and counted.
    _write_tasks(tmp_project, 400)
    proc = _pre_compact(tmp_project, repo_root)
    assert len(proc.stdout) <= 3000
    whole, by_id = _listed(proc.stdout, "open tasks (id — title — notes):")
    assert f"({400 - len(whole) - len(by_id)} more not shown" in proc.stdout


def test_pre_compact_keeps_tasks_in_view_past_many_directives(tmp_project, repo_root):
    """Measured (mlx-vlm): 32 live directives filled the whole budget, the summarizer saw none of
    18 open tasks and proposed re-adding the newest directive, which the oldest-first cut had
    dropped. Tasks keep their share; directives run newest first, the rest by id."""
    _write_tasks(tmp_project, 18, notes="stage 2 merged, stage 3 profiling on the worker")
    with (tmp_project / ".hippo" / "ledger.jsonl").open("a", encoding="utf-8") as f:
        for i in range(34):
            f.write(json.dumps({
                "t": f"2026-09-2{i // 10}T00:00:{i % 10:02d}Z", "ev": "directive",
                "id": f"rule-{i:02d}", "state": "active", "src": "cli",
                "text": f"rule {i}: " + "a durable user ruling in a full sentence " * 5}) + "\n")
    proc = _pre_compact(tmp_project, repo_root)
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout) <= 3000
    t_whole, t_ids = _listed(proc.stdout, "open tasks (id — title — notes):")
    assert len(t_whole) >= 3 and len(t_whole) + len(t_ids) == 18  # every open task in view
    d_whole, d_ids = _listed(proc.stdout, "live directives (id — text):")
    shown = d_whole + d_ids
    assert shown[:3] == ["rule-33", "rule-32", "rule-31"]  # newest first
    assert shown == [f"rule-{i:02d}" for i in range(33, 33 - len(shown), -1)]
    cut = 34 - len(shown)
    assert (f"({cut} more not shown" in proc.stdout) == (cut > 0)


def test_pre_compact_is_silent_for_a_marked_subagent_and_outside_a_project(
    tmp_project, repo_root, uninitialized_dir
):
    for proc in (_pre_compact(tmp_project, repo_root, agent_id="a1b2c3"),
                 _pre_compact(uninitialized_dir, repo_root)):
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


# A Claude Code session as the compaction hooks find it on disk (measured, 2.1.282): main's
# transcript t.jsonl and, beside it, t/subagents/agent-<id>.jsonl (an Agent run) or
# t/subagents/workflows/<runId>/agent-<id>.jsonl (a Workflow's agent), each with its .meta.json.
# Every user line carries the promptId of the prompt main is at when it is written — one per
# process, whoever writes — and the compaction hooks carry it too.
def _rec(kind, content, **extra):
    return {"type": kind, "message": {"role": kind, "content": content}, **extra}


def _user(content, prompt="p1", **extra):
    return _rec("user", content, promptId=prompt, **extra)


def _call(tuid, name="Agent", **inp):
    return _rec("assistant", [{"type": "tool_use", "id": tuid, "name": name, "input": inp}])


def _answer(tuid, prompt="p1", **result):
    extra = {"toolUseResult": result} if result else {}
    return _user([{"type": "tool_result", "tool_use_id": tuid, "content": "report"}], prompt,
                 **extra)


def _said(text):
    return _rec("assistant", [{"type": "text", "text": text}])


def _response(mid, *recs):
    """The lines of one response: the host writes each block as a line of its own, all with the
    response's message id."""
    return [{**r, "message": {**r["message"], "id": mid}} for r in recs]


def _lines(path, recs, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as f:
        f.writelines(json.dumps(r, separators=(",", ":")) + "\n" for r in recs)


def _session(project, main, agents=None):
    """agents: {path under subagents/, no suffix: (agent_type, the agent's own records)}"""
    shutil.rmtree(project / "t", ignore_errors=True)
    _lines(project / "t.jsonl", main)
    for rel, (atype, recs) in (agents or {}).items():
        path = project / "t" / "subagents" / f"{rel}.jsonl"
        _lines(path, [{**r, "isSidechain": True} for r in recs])
        path.with_name(path.name.replace(".jsonl", ".meta.json")).write_text(
            json.dumps({"agentType": atype}), encoding="utf-8")


# An agent at a compaction point: its tool's result just appended. Nothing the compaction
# writes is on disk before SessionStart(compact) returns (measured), so this is also how the
# agent's file reads there. One running a tool has no user line to compact on.
WORKING = [_user("brief"), _call("toolu_r1", "Read"), _answer("toolu_r1")]
RUNNING = [_user("brief"), _call("toolu_r1", "Bash")]
# Agents that are done: a Workflow agent ends in its StructuredOutput call's result, and one
# stopped by Esc in the interrupt line (measured); no model turn follows either.
FINISHED = [_user("brief"), _call("toolu_s1", "StructuredOutput", verdict="ok"),
            _user([{"type": "tool_result", "tool_use_id": "toolu_s1",
                    "content": "Structured output provided successfully"}])]
STOPPED = [*RUNNING, _answer("toolu_r1"),
           _user([{"type": "text", "text": "[Request interrupted by user for tool use]"}])]

# Main, fork mode on: it launched in the background and ended its turn — or went on working.
LAUNCHED = [_user("go"), _call("toolu_1"), _answer("toolu_1", status="async_launched",
                                                    agentId="a1")]
IDLE = [*LAUNCHED, _said("launched")]
# A manual /compact takes a prompt id of its own and ends in its own output lines, after which
# main sits idle; the agents' next lines carry that id (measured).
AFTER_COMPACT = [*IDLE, _user("This session is being continued…", "p0", isCompactSummary=True),
                 _user("<local-command-caveat>…", "p0", isMeta=True),
                 _user("<command-name>/compact</command-name>", "p0"),
                 _user("<local-command-stdout>Compacted</local-command-stdout>", "p0")]


def _pre_compact_at(project, repo_root, prompt="p1", **extra):
    return _pre_compact(project, repo_root, prompt_id=prompt, **extra)


def _session_start(project, repo_root, source="compact", prompt="p1", cwd=None):
    payload = {"session_id": "s", "transcript_path": str(project / "t.jsonl"),
               "cwd": str(cwd or project), "hook_event_name": "SessionStart", "source": source,
               "prompt_id": prompt}
    proc = _run_hook(repo_root / "hooks" / "session_start.sh", payload, cwd=project)
    assert proc.returncode == 0, proc.stderr
    return _capsule(proc) if proc.stdout else ""


def test_an_agent_s_own_compaction_is_not_main_s(tmp_project, repo_root, run_hippo):
    """On 2.1.282 an agent's own compaction fires PreCompact and SessionStart(compact) with
    main's session, transcript and prompt_id and no agent_id (measured, §3.4). Main is not
    compacting while it sits idle (after its reply, a local command, a `!` command or an Esc),
    has a call of its response still out (a tool it runs, a foreground agent beside a call
    already back) or has moved past the hook's prompt, so then the agent at a compaction point
    is whose compaction it is: no deltas request (commands it ran would land as main's,
    src=cli), and once compacted what SubagentStart gave it — never main's capsule with its
    `compact:` line. Background agents and Workflow agents too: with
    CLAUDE_CODE_FORK_SUBAGENT=1 every Agent call is one of those."""
    _directives(tmp_project, run_hippo)
    cases = {
        "a background agent, main idle": (IDLE, "agent-a1"),
        "a Workflow agent, main idle": (IDLE, "workflows/wf_x-1/agent-a2"),
        "a foreground agent main waits on": ([_user("go"), _call("toolu_1")], "agent-a1"),
        "main running a tool beside it": ([*LAUNCHED, _call("toolu_b", "Bash")], "agent-a1"),
        "main already past the hook's prompt": ([*IDLE, _user("<task-notification>…", "p2")],
                                                "agent-a1"),
        "main idle after a manual /compact": (AFTER_COMPACT, "agent-a1"),
        # Main sends no request until every call of its response is back, so a call already
        # answered beside the foreground agent it still waits on is no compaction point.
        "a foreground agent beside a call already back": (
            [_user("go"), *_response("m1", _call("toolu_1"), _call("toolu_b", "Read")),
             _answer("toolu_b")], "agent-a1"),
        "main idle after an Esc": (
            [*LAUNCHED, _call("toolu_b", "Bash"), _answer("toolu_b"),
             _user([{"type": "text", "text": "[Request interrupted by user]"}])], "agent-a1"),
        "main idle after a `!` command": (
            [*IDLE, _user("<bash-input>ls</bash-input>", "p0"),
             _user("<bash-stdout>t.jsonl</bash-stdout><bash-stderr></bash-stderr>", "p0")],
            "agent-a1"),
    }
    for case, (main, rel) in cases.items():
        prompt = "p0" if main[-1].get("promptId") == "p0" else "p1"
        _session(tmp_project, main, {rel: ("general-purpose", WORKING)})
        proc = _pre_compact_at(tmp_project, repo_root, prompt)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", ""), case
        assert _session_start(tmp_project, repo_root, prompt=prompt).splitlines() == SLICE, case
    # A fork's compaction summarized away the capsule it carried from main, so it gets the slice
    # too; hippo:lane gets nothing, as at its start.
    for atype, first in (("fork", "[hippo] directives 2 live"), ("hippo:lane", "")):
        _session(tmp_project, IDLE, {"agent-a1": (atype, WORKING)})
        assert _session_start(tmp_project, repo_root).startswith(first), atype
        assert _pre_compact_at(tmp_project, repo_root).stdout == "", atype


def test_a_worktree_agent_s_own_compaction_gets_its_slice(tmp_project, repo_root, run_hippo):
    """An isolation:"worktree" agent compacts in <project>/.claude/worktrees/agent-<id>, and both
    compaction hooks carry that cwd (measured live, 2.1.282). SessionStart(compact) walks past
    the worktree's .git file, as SubagentStart does, and hands the agent its slice. A main
    session run inside a worktree has no capsule at startup and gets none after its own
    compaction either; PreCompact keeps the conservative walk and is silent for both."""
    _directives(tmp_project, run_hippo)
    wt = _worktree(tmp_project, "agent-a1")
    _session(tmp_project, IDLE, {"agent-a1": ("general-purpose", WORKING)})
    assert _session_start(tmp_project, repo_root, cwd=wt).splitlines() == SLICE
    assert _pre_compact_at(tmp_project, repo_root, cwd=str(wt)).stdout == ""
    main_s = {"mid-turn beside its own agent": (
                  [*LAUNCHED, _call("toolu_b", "Read"), _answer("toolu_b")],
                  {"agent-a1": ("general-purpose", WORKING)}),
              "no agent ever launched": (IDLE, None)}
    for case, (main, agents) in main_s.items():
        _session(tmp_project, main, agents)
        assert _session_start(tmp_project, repo_root, cwd=wt) == "", case
        assert _pre_compact_at(tmp_project, repo_root, cwd=str(wt)).stdout == "", case
        assert _session_start(tmp_project, repo_root, "startup", cwd=wt) == "", case


def test_main_s_own_compactions_keep_the_request(tmp_project, repo_root):
    """Main losing its request is the worse error, so anything short of main shown not to be
    compacting is main's: main at the hook's prompt ending in a user line with every call of its
    response back (mid-turn, or waiting on its reply beside an agent) — a call a dead process
    left unanswered before a new prompt included — a prompt main just took that is not on disk
    yet, a manual /compact (only the user types it, into main), no agent at a compaction point
    (finished agents stay at a user line for good), and a host that sends no prompt_id."""
    cases = {
        "mid-turn beside its own agent": ([*LAUNCHED, _call("toolu_b", "Read"),
                                           _answer("toolu_b")], WORKING, "p1", None),
        "waiting on its reply at a later prompt": ([*IDLE, _user("next", "p2")],
                                                   [_user("brief"), _call("toolu_r1", "Read"),
                                                    _answer("toolu_r1", "p2")], "p2", None),
        "a new prompt not on disk yet": (IDLE, WORKING, "p2", None),
        "a turn a host message started": ([*IDLE, _user("Another session sent a message", "p1",
                                                         isMeta=True)], WORKING, "p1", None),
        "a manual /compact": (IDLE, WORKING, "p1", "manual"),
        "an agent running a tool": (IDLE, RUNNING, "p1", None),
        "an agent that answered": ([_user("go"), _call("toolu_1"), _answer("toolu_1")],
                                   [*WORKING, _said("done")], "p1", None),
        # Finished agents stay at a user line for good; they are no compaction's owner.
        "a finished Workflow agent": (IDLE, FINISHED, "p1", None),
        "an agent stopped by Esc": (IDLE, STOPPED, "p1", None),
        "back from every call of its response": (
            [_user("go"), *_response("m1", _call("toolu_1"), _call("toolu_b", "Read")),
             _answer("toolu_b"), _answer("toolu_1", status="async_launched", agentId="a1")],
            WORKING, "p1", None),
        "a call a dead process left, then a prompt": (
            [*IDLE, _call("toolu_x", "Bash"), _user("go on", "p2")], WORKING, "p2", None),
        "a host without prompt_id": (IDLE, WORKING, None, None),
    }
    for case, (main, agent, prompt, trigger) in cases.items():
        _session(tmp_project, main, {"agent-a1": ("general-purpose", agent)})
        extra = {"trigger": trigger} if trigger else {}
        proc = _pre_compact_at(tmp_project, repo_root, prompt, **extra)
        assert proc.returncode == 0, proc.stderr
        assert "`## hippo deltas`" in proc.stdout, case
        if trigger != "manual":  # SessionStart carries no trigger
            assert _session_start(tmp_project, repo_root, prompt=prompt).startswith(
                "[hippo] tasks"), case


def test_main_s_line_that_lands_in_the_flush_keeps_the_request(tmp_path, monkeypatch):
    """The host writes each transcript on a 100ms flush, so main compacting right after its tool
    returned can still end, on disk, in the call. Both sides are read once that flush has passed:
    a result that lands in the wait makes the compaction main's."""
    sys.path.insert(0, str(REPO_ROOT / "cli"))
    import hippo_cli

    main = [*LAUNCHED, _call("toolu_b", "Read")]
    _session(tmp_path, main, {"agent-a1": ("general-purpose", WORKING)})
    waits = []

    def flush(s):  # main's tool result lands while the hook waits
        waits.append(s)
        _lines(tmp_path / "t.jsonl", [_answer("toolu_b")], "a")

    monkeypatch.setattr(hippo_cli.time, "sleep", flush)
    assert hippo_cli.compaction_agent(str(tmp_path / "t.jsonl"), "p1") is None
    assert waits == [hippo_cli.TRANSCRIPT_FLUSH_WAIT] and waits[0] > 0.1
    monkeypatch.setattr(hippo_cli.time, "sleep", lambda s: None)  # still running: the agent's
    _session(tmp_path, main, {"agent-a1": ("general-purpose", WORKING)})
    assert hippo_cli.compaction_agent(str(tmp_path / "t.jsonl"), "p1") == {
        "agentType": "general-purpose"}


def _launch(tuid, prompt="p1", workflow=None, **result):
    if workflow:
        call = _call(tuid, "Workflow", script="phase('x')")
        result = {"status": "async_launched", "runId": workflow[0], "taskId": workflow[1],
                  "workflowName": workflow[2], **result}
    else:
        call = _call(tuid, "Agent", description=result.pop("description"), prompt="brief")
        result = {"status": "async_launched", **result}
    return [call, {**_answer(tuid, prompt, **result),
                   "timestamp": datetime.now(timezone.utc).isoformat()}]


def _note(task, status="completed"):
    return _user(f"<task-notification>\n<task-id>{task}</task-id>\n<status>{status}</status>\n"
                 "<summary>done</summary>\n<result>report</result>\n</task-notification>")


def test_the_capsule_after_a_compaction_names_native_runs_still_out(tmp_project, repo_root):
    """A summary can drop a launch; main's capsule after a compaction names each background run
    whose answer is not back yet, beside the ledger's in-flight entries (§6). Not a run that
    notified — one that died with an earlier process is notified `stopped` on resume (measured)
    — nor one main stopped (a stopped run never notifies, measured), nor a hippo:lane relay
    (its lane is the ledger's). An interim notification is test_native_runs'."""
    main = [_user("go"), *_launch("toolu_0", description="old process run", agentId="a0"),
            _note("a0", "stopped"), _user("go on", "p2"),
            *_launch("toolu_1", "p2", description="scan the parser", agentId="a1"),
            *_launch("toolu_2", "p2", workflow=("wf_1-1", "w1", "wf-build")),
            *_launch("toolu_3", "p2", description="done one", agentId="a3"), _note("a3"),
            *_launch("toolu_4", "p2", workflow=("wf_2-2", "w2", "stopped one")),
            _call("toolu_5", "TaskStop", task_id="w2"), _answer("toolu_5", "p2"),
            *_launch("toolu_6", "p2", description="relay lane", agentId="a6"),
            _said("waiting")]
    _session(tmp_project, main, {"agent-a6": ("hippo:lane", WORKING)})
    capsule = _session_start(tmp_project, repo_root, prompt="p3")
    flying = [ln for ln in capsule.splitlines() if ln.startswith("· in flight:")]
    assert flying == ["· in flight: scan the parser (subagent · 0h00m), "
                      "wf-build (workflow · 0h00m)"], capsule
    assert "in flight" not in _session_start(tmp_project, repo_root, source="startup")


def test_the_capsule_after_a_compaction_points_at_the_deltas(tmp_project, repo_root):
    def capsule(source):
        payload = {"cwd": str(tmp_project), "hook_event_name": "SessionStart", "source": source}
        proc = _run_hook(repo_root / "hooks" / "session_start.sh", payload, cwd=tmp_project)
        assert proc.returncode == 0, proc.stderr
        return _capsule(proc)

    assert "## hippo deltas" in capsule("compact").splitlines()[-1]
    assert "hippo deltas" not in capsule("startup")
