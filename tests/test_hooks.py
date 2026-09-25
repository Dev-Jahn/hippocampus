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


def test_pre_compact_is_capped_and_counts_what_it_cut(tmp_project, repo_root, run_hippo):
    tasks = tmp_project / ".hippo" / "tasks.yaml"
    tasks.write_text("tasks:\n" + "".join(
        f"- {{id: feat/t{i:02d}, title: task number {i}, status: pending, notes: ['{'n' * 90}']}}\n"
        for i in range(40)), encoding="utf-8")
    proc = _pre_compact(tmp_project, repo_root)
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout) <= 3000
    shown = proc.stdout.count("- feat/t")
    assert 0 < shown < 40
    assert f"({40 - shown} more not shown" in proc.stdout


def test_pre_compact_is_silent_for_a_marked_subagent_and_outside_a_project(
    tmp_project, repo_root, uninitialized_dir
):
    for proc in (_pre_compact(tmp_project, repo_root, agent_id="a1b2c3"),
                 _pre_compact(uninitialized_dir, repo_root)):
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


# A Claude Code session as the compaction hooks find it on disk: main's transcript t.jsonl and,
# beside it, t/subagents/agent-<id>.meta.json and agent-<id>.jsonl (measured, 2.1.282).
def _rec(kind, content, **extra):
    return {"type": kind, "message": {"role": kind, "content": content}, **extra}


def _call(tuid, name="Agent"):
    return _rec("assistant", [{"type": "tool_use", "id": tuid, "name": name, "input": {}}])


def _answer(tuid):
    return _rec("user", [{"type": "tool_result", "tool_use_id": tuid, "content": "report"}])


def _lines(path, recs, mode="w"):
    with path.open(mode, encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in recs)


def _session(project, main, agents):
    """agents: {agent_id: (toolUseId, requestShape, agent_type, the agent's own records)}"""
    _lines(project / "t.jsonl", main)
    sub = project / "t" / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    for key, (tuid, shape, atype, recs) in agents.items():
        (sub / f"agent-{key}.meta.json").write_text(json.dumps(
            {"agentType": atype, "toolUseId": tuid, "requestShape": shape}), encoding="utf-8")
        _lines(sub / f"agent-{key}.jsonl", [{**r, "isSidechain": True} for r in recs])
    return sub


# An agent mid-work at its own compaction: its last tool call answered, no answer of its own yet.
WORKING = [_rec("user", "brief"), _call("toolu_r1", "Read"), _answer("toolu_r1")]
ANSWERED = [*WORKING, _rec("assistant", [{"type": "text", "text": "done"}])]


def _session_start(project, repo_root, source="compact"):
    payload = {"session_id": "s", "transcript_path": str(project / "t.jsonl"),
               "cwd": str(project), "hook_event_name": "SessionStart", "source": source}
    proc = _run_hook(repo_root / "hooks" / "session_start.sh", payload, cwd=project)
    assert proc.returncode == 0, proc.stderr
    return _capsule(proc) if proc.stdout else ""


def test_a_subagent_s_own_compaction_is_not_main_s(tmp_project, repo_root, run_hippo):
    """On 2.1.282 a subagent's own compaction fires PreCompact and SessionStart(compact) with
    main's session and transcript and no agent_id (measured, §3.4). Main cannot compact while a
    foreground call of its own runs, so the foreground agent main waits on, still working, is
    whose compaction it is: no deltas request (commands it ran would land as main's, src=cli),
    and once compacted what SubagentStart gave it — never main's capsule with its `compact:` line."""
    _directives(tmp_project, run_hippo)
    main = [_rec("user", "go"), _call("toolu_done"), _answer("toolu_done"), _call("toolu_w")]
    _session(tmp_project, main, {"a1": ("toolu_done", "foreground", "general-purpose", ANSWERED),
                                 "a2": ("toolu_w", "foreground", "general-purpose", WORKING)})
    proc = _pre_compact(tmp_project, repo_root)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert _session_start(tmp_project, repo_root).splitlines() == SLICE
    # A fork's compaction summarized away the capsule it carried from main, so it gets the slice
    # too; hippo:lane gets nothing, as at its start.
    for atype, first in (("fork", "[hippo] directives 2 live"), ("hippo:lane", "")):
        _session(tmp_project, main, {"a2": ("toolu_w", "foreground", atype, WORKING)})
        assert _session_start(tmp_project, repo_root).startswith(first), atype
        assert _pre_compact(tmp_project, repo_root).stdout == "", atype


def test_main_s_own_compactions_keep_the_request(tmp_project, repo_root):
    """Main compacts before its last tool result reaches the file (a 100ms flush, measured), so
    the file can end in any unanswered call of main's own — a Read, or a foreground Agent call
    whose agent already answered (10 of 11 such, measured on 2.1.282). Neither is a subagent
    compacting, and neither is a background launch in that same window or a call a new prompt
    abandoned."""
    earlier = {"a0": ("toolu_old", "foreground", "general-purpose", ANSWERED)}
    cases = {
        "a Read": ([_rec("user", "go"), _call("toolu_1", "Read")], earlier),
        "an agent that answered": ([_rec("user", "go"), _call("toolu_1")],
                                   {"a1": ("toolu_1", "foreground", "general-purpose", ANSWERED)}),
        "a background launch": ([_rec("user", "go"), _call("toolu_1")],
                                {"a1": ("toolu_1", "background", "general-purpose", WORKING)}),
        "an abandoned call": ([_rec("user", "go"), _call("toolu_1"), _rec("user", "resumed")],
                              {"a1": ("toolu_1", "foreground", "general-purpose", WORKING)}),
    }
    for case, (main, agents) in cases.items():
        shutil.rmtree(tmp_project / "t", ignore_errors=True)
        _session(tmp_project, main, agents)
        proc = _pre_compact(tmp_project, repo_root)
        assert proc.returncode == 0, proc.stderr
        assert "`## hippo deltas`" in proc.stdout, case
        assert _session_start(tmp_project, repo_root).startswith("[hippo] tasks"), case


def test_a_call_main_got_back_in_the_flush_is_main_s_whatever_its_agent_shows(tmp_path,
                                                                               monkeypatch):
    """A foreground call can return to main while its agent's own transcript does not end in an
    answer (measured, 2.1.282): an agent stopped at its maxTurns ends in a tool result, and one
    the host moved to the background is still working — main got `async_launched`, the
    meta.json still says foreground. Main compacting right after still shows the call
    unanswered at the hook's start; its result lands within the flush, so main is read again
    after the wait and the compaction is main's. Had it not landed, it would be the agent's."""
    sys.path.insert(0, str(REPO_ROOT / "cli"))
    import hippo_cli

    capped = WORKING
    backgrounded = [_rec("user", "brief"), _call("toolu_b1", "Bash")]
    launched = {"toolUseResult": {"status": "async_launched", "agentId": "a1"}}
    for case, recs, result in (("capped", capped, _answer("toolu_1")),
                               ("backgrounded", backgrounded, {**_answer("toolu_1"), **launched})):
        _session(tmp_path, [_rec("user", "go"), _call("toolu_1")],
                 {"a1": ("toolu_1", "foreground", "general-purpose", recs)})
        monkeypatch.setattr(hippo_cli.time, "sleep", lambda s: None)
        assert hippo_cli.awaited_agent(str(tmp_path / "t.jsonl"))["toolUseId"] == "toolu_1", case
        monkeypatch.setattr(hippo_cli.time, "sleep",
                            lambda s: _lines(tmp_path / "t.jsonl", [result], "a"))
        assert hippo_cli.awaited_agent(str(tmp_path / "t.jsonl")) is None, case


def test_the_agent_is_read_after_the_host_s_flush(tmp_path, monkeypatch):
    """Main compacting right after a foreground agent answered: the answer was queued before main
    got it, but a read tens of ms after PreCompact fired still missed it (measured) — so the
    agent's transcript is read once the host's 100ms flush has passed, not before."""
    sys.path.insert(0, str(REPO_ROOT / "cli"))
    import hippo_cli

    sub = _session(tmp_path, [_rec("user", "go"), _call("toolu_1")],
                   {"a1": ("toolu_1", "foreground", "general-purpose", WORKING)})
    waits = []

    def flush(s):  # the host's write lands while the hook waits
        waits.append(s)
        _lines(sub / "agent-a1.jsonl", [{**ANSWERED[-1], "isSidechain": True}], "a")

    monkeypatch.setattr(hippo_cli.time, "sleep", flush)
    assert hippo_cli.awaited_agent(str(tmp_path / "t.jsonl")) is None
    assert waits == [hippo_cli.TRANSCRIPT_FLUSH_WAIT] and waits[0] > 0.1
    monkeypatch.setattr(hippo_cli.time, "sleep", lambda s: None)  # still working: it is the agent's
    _session(tmp_path, [_rec("user", "go"), _call("toolu_1")],
             {"a1": ("toolu_1", "foreground", "general-purpose", WORKING)})
    assert hippo_cli.awaited_agent(str(tmp_path / "t.jsonl"))["toolUseId"] == "toolu_1"


def test_the_capsule_after_a_compaction_points_at_the_deltas(tmp_project, repo_root):
    def capsule(source):
        payload = {"cwd": str(tmp_project), "hook_event_name": "SessionStart", "source": source}
        proc = _run_hook(repo_root / "hooks" / "session_start.sh", payload, cwd=tmp_project)
        assert proc.returncode == 0, proc.stderr
        return _capsule(proc)

    assert "## hippo deltas" in capsule("compact").splitlines()[-1]
    assert "hippo deltas" not in capsule("startup")
