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
import subprocess
import time


def _run_hook(script_path, payload: dict, cwd, env=None, timeout=10):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
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


def test_session_start_hook_emits_capsule_when_initialized(tmp_project, repo_root):
    payload = {
        "session_id": "sess-z",
        "cwd": str(tmp_project),
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    proc = _run_hook(repo_root / "hooks" / "session_start.sh", payload, cwd=tmp_project)
    assert proc.returncode == 0
    assert proc.stdout.strip().startswith("[hippo]")


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
    assert proc.stdout.strip().startswith("[hippo]"), (
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
