"""Regression tests for the adversarial-review findings (2026-07-31 repair).

Each test here fails if its repair is reverted — that was the acceptance
criterion for the repair round. Test names carry the finding id so a future
failure points straight back at what it protects.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import (
    REPO_ROOT,
    SCRIPTS_DIR,
    failures_dir_path,
    ledger_path,
    read_ledger,
    worklog_path,
)


def _mock_env(mock_output_path):
    return {
        "HIPPO_CLERK_BACKEND": "mock",
        "HIPPO_MOCK_OUTPUT": str(mock_output_path),
    }


def _seed(tmp_project, run_hippo):
    proc = run_hippo(
        [
            "log",
            "dispatch",
            "--id",
            "seed",
            "--kind",
            "docs",
            "--exec",
            "codex/gpt-5.6-luna/low",
            "--scope",
            "seed line",
        ],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr


def _run_hook(script_path, payload, cwd, env=None, timeout=30):
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


def _path_without(stub_root, names):
    """A PATH in which none of `names` resolves, with everything else intact.

    Any PATH directory holding a banned name is mirrored into a temp dir by
    symlink, minus that name — so the test can remove one tool without also
    removing the hundreds of unrelated tools that share its directory.
    """
    parts = [p for p in os.environ.get("PATH", "").split(":") if p]
    out = []
    for i, p in enumerate(parts):
        d = Path(p)
        try:
            entries = list(d.iterdir())
        except OSError:
            out.append(p)
            continue
        if not any(e.name in names for e in entries):
            out.append(p)
            continue
        mirror = stub_root / f"mirror{i}"
        mirror.mkdir(parents=True, exist_ok=True)
        for e in entries:
            if e.name in names:
                continue
            try:
                (mirror / e.name).symlink_to(e)
            except OSError:
                pass
        out.append(str(mirror))
    return ":".join(out)


def _resolves(path_value, tool):
    return (
        subprocess.run(
            ["bash", "-c", f"command -v {tool}"],
            env={**os.environ, "PATH": path_value},
            capture_output=True,
        ).returncode
        == 0
    )


# --------------------------------------------------------------------------
# B1 — the writer stamps t/src; a caller may not supply either
# --------------------------------------------------------------------------

def test_b1_caller_supplied_t_is_rejected(tmp_project, run_hippo):
    _seed(tmp_project, run_hippo)
    before = ledger_path(tmp_project).read_bytes()

    payload = json.dumps(
        {
            "ev": "directive",
            "id": "forged-t",
            "text": "x",
            "scope": "phase",
            "state": "active",
            "t": "1999-01-01T00:00:00Z",
        },
        ensure_ascii=False,
    )
    proc = run_hippo(["log", "raw", payload], cwd=tmp_project)
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""
    assert ledger_path(tmp_project).read_bytes() == before


def test_b1_caller_supplied_src_is_rejected(tmp_project, run_hippo):
    _seed(tmp_project, run_hippo)
    before = ledger_path(tmp_project).read_bytes()

    payload = json.dumps({"ev": "clerk", "name": "turn-scribe", "ok": True, "src": "cli"})
    proc = run_hippo(["log", "raw", payload], cwd=tmp_project)
    assert proc.returncode != 0
    assert ledger_path(tmp_project).read_bytes() == before


def test_b1_writer_stamps_fresh_t_and_src(tmp_project, run_hippo):
    proc = run_hippo(
        ["log", "raw", json.dumps({"ev": "clerk", "name": "manual", "ok": True})],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr
    entry = read_ledger(tmp_project)[-1]
    assert entry["src"] == "cli"
    assert re.match(r"^20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$", entry["t"]), entry["t"]


def test_b1_scribe_cannot_forge_t_or_src_through_clerk_output(
    tmp_project, run_hippo, fake_transcript, tmp_path
):
    """The clerk reply is untrusted input: an event carrying t/src must be
    treated exactly like any other schema violation (dumped, not appended)."""
    mock = tmp_path / "forged.json"
    mock.write_text(
        json.dumps(
            {
                "worklog": "위조 시도",
                "events": [
                    {
                        "ev": "dispatch",
                        "id": "d999",
                        "kind": "docs",
                        "exec": "codex/x/low",
                        "scope": "forged",
                        "src": "cli",
                        "t": "1999-01-01T00:00:00Z",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-forge"],
        cwd=tmp_project,
        env=_mock_env(mock),
    )
    ledger = read_ledger(tmp_project)
    assert [e for e in ledger if e.get("ev") != "clerk"] == []
    assert [e for e in ledger if e.get("ev") == "clerk"][0]["ok"] is False


def test_b1_unknown_hippo_src_env_is_rejected(tmp_project, run_hippo):
    _seed(tmp_project, run_hippo)
    before = ledger_path(tmp_project).read_bytes()

    proc = run_hippo(
        ["log", "raw", json.dumps({"ev": "clerk", "name": "manual", "ok": True})],
        cwd=tmp_project,
        env={"HIPPO_SRC": "root"},
    )
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""
    assert ledger_path(tmp_project).read_bytes() == before


def test_b1_wrapper_src_env_is_accepted(tmp_project, run_hippo):
    proc = run_hippo(
        [
            "log",
            "dispatch",
            "--id",
            "dwrap",
            "--kind",
            "docs",
            "--exec",
            "codex/x/low",
            "--scope",
            "wrapper path",
        ],
        cwd=tmp_project,
        env={"HIPPO_SRC": "wrapper"},
    )
    assert proc.returncode == 0, proc.stderr
    assert read_ledger(tmp_project)[-1]["src"] == "wrapper"


# --------------------------------------------------------------------------
# M1 — per-ev key whitelist
# --------------------------------------------------------------------------

def test_m1_unknown_key_rejected(tmp_project, run_hippo):
    _seed(tmp_project, run_hippo)
    before = ledger_path(tmp_project).read_bytes()

    payload = json.dumps(
        {
            "ev": "dispatch",
            "id": "d-extra",
            "kind": "docs",
            "exec": "codex/x/low",
            "scope": "s",
            "surprise": "payload",
        }
    )
    proc = run_hippo(["log", "raw", payload], cwd=tmp_project)
    assert proc.returncode != 0
    assert "surprise" in proc.stderr
    assert ledger_path(tmp_project).read_bytes() == before


def test_m1_key_allowed_on_another_ev_still_rejected(tmp_project, run_hippo):
    """`attr` is legal on outcome but not on dispatch — the whitelist is per-ev,
    not one global key set."""
    _seed(tmp_project, run_hippo)
    before = ledger_path(tmp_project).read_bytes()

    payload = json.dumps(
        {
            "ev": "dispatch",
            "id": "d-attr",
            "kind": "docs",
            "exec": "codex/x/low",
            "scope": "s",
            "attr": "work",
        }
    )
    proc = run_hippo(["log", "raw", payload], cwd=tmp_project)
    assert proc.returncode != 0
    assert ledger_path(tmp_project).read_bytes() == before


def test_m1_documented_optional_keys_still_accepted(tmp_project, run_hippo):
    """The whitelist must not be so tight that the schema's own optional
    fields stop working."""
    for payload in (
        {
            "ev": "dispatch",
            "id": "d-ok",
            "kind": "docs",
            "exec": "codex/x/low",
            "scope": "s",
            "task": "feat/x",
        },
        {
            "ev": "outcome",
            "ref": "d-ok",
            "result": "revised",
            "attr": "work",
            "rework": 2,
            "by": "verify/opus",
            "note": "n",
        },
        {"ev": "review-status", "ref": "r1", "addressed": "partial", "at": "abc1234"},
        {"ev": "clerk", "name": "turn-scribe", "ok": True, "ms": 10, "tokens": 5},
    ):
        proc = run_hippo(
            ["log", "raw", json.dumps(payload, ensure_ascii=False)], cwd=tmp_project
        )
        assert proc.returncode == 0, f"{payload} -> {proc.stderr}"


def test_m1_scribe_may_still_emit_directives(
    tmp_project, run_hippo, fake_transcript, tmp_path
):
    """Owner ruling: ev:directive from the scribe is the context-keeper path and
    stays allowed. The whitelist hardening must not have closed it."""
    mock = tmp_path / "directive.json"
    mock.write_text(
        json.dumps(
            {
                "worklog": "지시 포착",
                "events": [
                    {
                        "ev": "directive",
                        "id": "gpu-01",
                        "text": "GPU 0,1만 사용",
                        "scope": "phase",
                        "state": "active",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-dir"],
        cwd=tmp_project,
        env=_mock_env(mock),
    )
    assert proc.returncode == 0, proc.stderr
    directives = [e for e in read_ledger(tmp_project) if e.get("ev") == "directive"]
    assert len(directives) == 1
    assert directives[0]["src"] == "scribe"


def test_m1_inject_truncates_long_directive_text(tmp_project, run_hippo):
    """--inject is a resident surface fed by untrusted transcript text: a
    directive is folded to one capped line so it cannot blow the surface up."""
    long_text = "A" * 300 + "\n두 번째 줄\n세 번째 줄"
    proc = run_hippo(
        [
            "log",
            "raw",
            json.dumps(
                {
                    "ev": "directive",
                    "id": "long-01",
                    "text": long_text,
                    "scope": "phase",
                    "state": "active",
                },
                ensure_ascii=False,
            ),
        ],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr

    inject = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert inject.returncode == 0, inject.stderr
    lines = inject.stdout.splitlines()
    live = [ln for ln in lines if ln.startswith("· live(")]
    assert len(live) == 1
    body = live[0].split(": ", 1)[1]
    assert len(body) <= 80, body
    assert "두 번째 줄" not in inject.stdout


# --------------------------------------------------------------------------
# M2 — failures/ filenames must not collide inside the same second
# --------------------------------------------------------------------------

def test_m2_same_second_failures_do_not_overwrite_each_other(tmp_project):
    sys.path.insert(0, str(REPO_ROOT / "cli"))
    import hippo_cli  # deliberately not imported at collection time

    hp = tmp_project / ".hippo"
    first = hippo_cli.dump_failure(hp, "scribe", "첫 번째 실패")
    second = hippo_cli.dump_failure(hp, "scribe", "두 번째 실패")
    assert first != second
    assert first.exists() and second.exists()
    assert first.read_text(encoding="utf-8") == "첫 번째 실패"
    assert second.read_text(encoding="utf-8") == "두 번째 실패"


# --------------------------------------------------------------------------
# M3 — a failed clerk still advances the cursor (no infinite re-billing)
# --------------------------------------------------------------------------

def test_m3_cursor_advances_on_clerk_failure(
    tmp_project, run_hippo, fake_transcript, malformed_mock_output
):
    run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-fail"],
        cwd=tmp_project,
        env=_mock_env(malformed_mock_output),
    )
    assert len(read_ledger(tmp_project)) == 1
    assert len(list(failures_dir_path(tmp_project).iterdir())) == 1

    # Same transcript, nothing new: the deterministic prefilter must find an
    # already-advanced cursor and skip the model entirely.
    second = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-fail"],
        cwd=tmp_project,
        env=_mock_env(malformed_mock_output),
    )
    assert second.returncode == 0, second.stderr
    assert len(read_ledger(tmp_project)) == 1, "a failed turn must not be re-billed"
    assert len(list(failures_dir_path(tmp_project).iterdir())) == 1


# --------------------------------------------------------------------------
# M4 — the Stop hook detaches for real and closes stdin
# --------------------------------------------------------------------------

def test_m4_stop_hook_detaches_into_its_own_session(
    tmp_project, repo_root, fake_transcript, tmp_path
):
    """The scribe must outlive the hook in a session of its own. Probe: a stub
    `hippo` records os.getsid() and whether stdin is at EOF."""
    stub_root = tmp_path / "stub-plugin"
    (stub_root / "bin").mkdir(parents=True)
    probe_out = tmp_path / "probe.json"
    (stub_root / "bin" / "hippo").write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "try:\n"
        "    stdin_data = sys.stdin.read()\n"
        "except Exception:\n"
        "    stdin_data = '<unreadable>'\n"
        "json.dump({'sid': os.getsid(0), 'ppid_sid': None, 'stdin': stdin_data},\n"
        f"          open({str(probe_out)!r}, 'w'))\n",
        encoding="utf-8",
    )
    (stub_root / "bin" / "hippo").chmod(0o755)

    proc = _run_hook(
        repo_root / "hooks" / "stop.sh",
        {
            "session_id": "sess-detach2",
            "transcript_path": str(fake_transcript),
            "cwd": str(tmp_project),
        },
        cwd=tmp_project,
        env={"CLAUDE_PLUGIN_ROOT": str(stub_root)},
    )
    assert proc.returncode == 0

    for _ in range(100):
        if probe_out.exists():
            break
        subprocess.run(["sleep", "0.05"], check=False)
    assert probe_out.exists(), "the detached scribe never ran"
    data = json.loads(probe_out.read_text(encoding="utf-8"))
    assert data["stdin"] == "", "stdin must be closed (</dev/null) for the scribe"
    assert data["sid"] != os.getsid(0), (
        "the scribe must run in its own session (setsid or the python3 shim), "
        "not the hook's"
    )


# --------------------------------------------------------------------------
# M5 — the hooks keep working when jq is absent
# --------------------------------------------------------------------------

def test_m5_session_start_hook_works_without_jq(tmp_project, repo_root, tmp_path):
    path_value = _path_without(tmp_path / "nojq", {"jq"})
    if _resolves(path_value, "jq"):
        pytest.skip("could not construct a jq-free PATH on this machine")

    proc = _run_hook(
        repo_root / "hooks" / "session_start.sh",
        {"cwd": str(tmp_project), "source": "startup"},
        cwd=tmp_project,
        env={"PATH": path_value},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("[hippo]"), (
        f"without jq the hook must fall back to python3, not die mute; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_m5_stop_hook_parses_stdin_without_jq(tmp_project, repo_root, tmp_path):
    path_value = _path_without(tmp_path / "nojq2", {"jq"})
    if _resolves(path_value, "jq"):
        pytest.skip("could not construct a jq-free PATH on this machine")

    marker = tmp_path / "stop-ran.txt"
    stub_root = tmp_path / "stub-plugin"
    (stub_root / "bin").mkdir(parents=True)
    (stub_root / "bin" / "hippo").write_text(
        f'#!/bin/sh\nprintf "%s" "$*" > {str(marker)!r}\n', encoding="utf-8"
    )
    (stub_root / "bin" / "hippo").chmod(0o755)

    proc = _run_hook(
        repo_root / "hooks" / "stop.sh",
        {
            "session_id": "sess-nojq",
            "transcript_path": "/tmp/whatever.jsonl",
            "cwd": str(tmp_project),
        },
        cwd=tmp_project,
        env={"PATH": path_value, "CLAUDE_PLUGIN_ROOT": str(stub_root)},
    )
    assert proc.returncode == 0, proc.stderr
    for _ in range(100):
        if marker.exists():
            break
        subprocess.run(["sleep", "0.05"], check=False)
    assert marker.exists(), "without jq the Stop hook never launched the scribe"
    assert "sess-nojq" in marker.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# M6 — recursion guard: a clerk's own session must not re-enter the hooks
# --------------------------------------------------------------------------

def test_m6_hooks_are_noop_inside_a_clerk(tmp_project, repo_root, fake_transcript):
    env = {"HIPPO_CLERK": "1"}
    stop = _run_hook(
        repo_root / "hooks" / "stop.sh",
        {
            "session_id": "sess-rec",
            "transcript_path": str(fake_transcript),
            "cwd": str(tmp_project),
        },
        cwd=tmp_project,
        env=env,
    )
    assert (stop.returncode, stop.stdout, stop.stderr) == (0, "", "")

    start = _run_hook(
        repo_root / "hooks" / "session_start.sh",
        {"cwd": str(tmp_project), "source": "startup"},
        cwd=tmp_project,
        env=env,
    )
    assert (start.returncode, start.stdout, start.stderr) == (0, "", "")


def test_m6_clerk_run_exports_the_guard_to_the_backend(tmp_path):
    """The guard is only useful if the backend process actually inherits it."""
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "codex").write_text(
        '#!/bin/sh\nprintf "CLERK=%s" "${HIPPO_CLERK:-MISSING}"\n', encoding="utf-8"
    )
    (stub / "codex").chmod(0o755)

    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt\n", encoding="utf-8")
    inp = tmp_path / "input.txt"
    inp.write_text("input\n", encoding="utf-8")

    proc = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "clerk_run.sh"), str(prompt), str(inp)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PATH": f"{stub}:{os.environ['PATH']}",
            "HIPPO_CLERK_BACKEND": "codex",
        },
    )
    assert proc.stdout.strip() == "CLERK=1", proc


# --------------------------------------------------------------------------
# M7 — the claude backend really blocks tools (flags exist on this machine)
# --------------------------------------------------------------------------

def test_m7_claude_backend_uses_real_flags(repo_root):
    """`--tools ""` etc. must be spellings the installed claude actually knows.
    Skipped when claude is not installed."""
    if subprocess.run(["bash", "-c", "command -v claude"], capture_output=True).returncode:
        pytest.skip("claude CLI not installed")
    help_text = subprocess.run(
        ["claude", "-p", "--help"], capture_output=True, text=True, timeout=60
    ).stdout
    script = (repo_root / "scripts" / "clerk_run.sh").read_text(encoding="utf-8")
    claude_block = script.split("claude)", 1)[1]
    used = set(re.findall(r"^\s*--([a-z-]+)", claude_block, re.M))
    unknown = sorted(f for f in used if f"--{f}" not in help_text)
    assert unknown == [], f"clerk_run.sh passes flags claude does not know: {unknown}"


# --------------------------------------------------------------------------
# M8 — cursors.json survives corruption and is written atomically
# --------------------------------------------------------------------------

def test_m8_corrupt_cursors_file_is_survived_and_dumped(
    tmp_project, run_hippo, fake_transcript, valid_mock_output
):
    (tmp_project / ".hippo" / "cursors.json").write_text(
        "{ broken ###", encoding="utf-8"
    )
    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-corrupt"],
        cwd=tmp_project,
        env=_mock_env(valid_mock_output),
    )
    assert proc.returncode == 0, proc.stderr
    cursors = json.loads(
        (tmp_project / ".hippo" / "cursors.json").read_text(encoding="utf-8")
    )
    assert cursors["sess-corrupt"] > 0
    assert any(
        "cursors" in p.name for p in failures_dir_path(tmp_project).iterdir()
    ), "an unreadable cursors.json must leave a trace in failures/"


# --------------------------------------------------------------------------
# m1 — argparse errors are silent outside a project; -h always works
# --------------------------------------------------------------------------

def test_m1_argparse_error_is_silent_outside_a_project(uninitialized_dir, run_hippo):
    proc = run_hippo(["log", "dispatch"], cwd=uninitialized_dir)  # missing required
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_m1_bare_invocation_is_silent_outside_a_project(uninitialized_dir, run_hippo):
    proc = run_hippo([], cwd=uninitialized_dir)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_m1_unknown_subcommand_is_silent_outside_a_project(
    uninitialized_dir, run_hippo
):
    proc = run_hippo(["nonesuch"], cwd=uninitialized_dir)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_m1_help_still_prints_outside_a_project(uninitialized_dir, run_hippo):
    for args in (["--help"], ["task", "--help"], ["log", "dispatch", "--help"]):
        proc = run_hippo(args, cwd=uninitialized_dir)
        assert proc.returncode == 0, f"{args}: {proc.stderr}"
        assert "usage:" in proc.stdout, f"{args}: {proc.stdout!r}"


def test_m1_argparse_error_still_loud_inside_a_project(tmp_project, run_hippo):
    """The silencing is conditional on being outside a project — inside one a
    malformed command must still say what is wrong."""
    proc = run_hippo(["log", "dispatch"], cwd=tmp_project)
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""


# --------------------------------------------------------------------------
# m2 / m3 / m4 / m5 / m7 / m8 / m9 / m10 / m11
# --------------------------------------------------------------------------

def test_m2_clerk_event_records_token_estimate(
    tmp_project, run_hippo, fake_transcript, valid_mock_output
):
    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-tok"],
        cwd=tmp_project,
        env=_mock_env(valid_mock_output),
    )
    assert proc.returncode == 0, proc.stderr
    clerk = [e for e in read_ledger(tmp_project) if e.get("ev") == "clerk"][0]
    assert isinstance(clerk.get("tokens"), int)
    assert clerk["tokens"] > 0


def test_m3_review_base_must_be_a_sha(tmp_project, run_hippo):
    _seed(tmp_project, run_hippo)
    before = ledger_path(tmp_project).read_bytes()

    proc = run_hippo(
        [
            "log",
            "review",
            "--id",
            "r-unknown",
            "--base",
            "unknown",
            "--source",
            "chatgpt-web",
            "--findings",
            "3",
        ],
        cwd=tmp_project,
    )
    assert proc.returncode != 0
    assert ledger_path(tmp_project).read_bytes() == before

    ok = run_hippo(
        [
            "log",
            "review",
            "--id",
            "r-ok",
            "--base",
            "abc123f",
            "--source",
            "chatgpt-web",
            "--findings",
            "3",
        ],
        cwd=tmp_project,
    )
    assert ok.returncode == 0, ok.stderr


def test_m3_scribe_review_without_sha_is_rejected(
    tmp_project, run_hippo, fake_transcript, tmp_path
):
    mock = tmp_path / "review_unknown.json"
    mock.write_text(
        json.dumps(
            {
                "worklog": "리뷰 회신",
                "events": [
                    {
                        "ev": "review",
                        "id": "r1",
                        "base": "unknown",
                        "source": "chatgpt-web",
                        "findings": 2,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-rev"],
        cwd=tmp_project,
        env=_mock_env(mock),
    )
    assert [e for e in read_ledger(tmp_project) if e.get("ev") == "review"] == []


def test_m3_clerk_prompt_no_longer_teaches_unknown_base(repo_root):
    text = (repo_root / "clerks" / "turn-scribe.md").read_text(encoding="utf-8")
    assert 'base를 "unknown"으로' not in text


def test_m4_digest_lite_until_line_bounds_the_window(fake_transcript_path):
    full = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "digest_lite.py"), str(fake_transcript_path)],
        capture_output=True,
        text=True,
    )
    bounded = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "digest_lite.py"),
            str(fake_transcript_path),
            "--until-line",
            "2",
        ],
        capture_output=True,
        text=True,
    )
    assert bounded.returncode == 0, bounded.stderr
    assert bounded.stdout.strip() != ""
    assert len(bounded.stdout) < len(full.stdout)
    assert "[3]" not in bounded.stdout


def test_m5_second_scribe_waits_for_the_lock_instead_of_dropping_the_tail(
    tmp_project, run_hippo, fake_transcript, valid_mock_output
):
    """A scribe that finds the lock held must retry briefly: the loser of the
    session's last turn has no 'next run' to cover the gap."""
    lock = tmp_project / ".hippo" / "scribe.lock"
    lock.touch()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,time\n"
            "f=open(sys.argv[1],'w')\n"
            "fcntl.flock(f, fcntl.LOCK_EX)\n"
            "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
            "time.sleep(1.0)\n",
            str(lock),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        proc = run_hippo(
            ["scribe", "--transcript", str(fake_transcript), "--session", "sess-lock"],
            cwd=tmp_project,
            env=_mock_env(valid_mock_output),
        )
        assert proc.returncode == 0, proc.stderr
    finally:
        holder.wait(timeout=10)
    assert any(e.get("ev") == "clerk" for e in read_ledger(tmp_project)), (
        "the second scribe gave up instead of waiting out a 1s lock hold"
    )


def test_m6_find_ws_stops_at_home(tmp_path, run_hippo):
    """The upward walk must not climb past $HOME even when no .git caps it —
    a .hippo sitting above the user's home would otherwise be adopted by
    every unrelated directory below it."""
    (tmp_path / ".hippo").mkdir()  # above $HOME — must never be adopted
    (tmp_path / ".hippo" / "ledger.jsonl").touch()
    home = tmp_path / "home"
    (home / "projects" / "elsewhere").mkdir(parents=True)

    proc = run_hippo(
        ["status", "--inject"],
        cwd=home / "projects" / "elsewhere",
        env={"HOME": str(home)},
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")

    # …while a .hippo at $HOME itself is still perfectly findable.
    (home / ".hippo").mkdir()
    (home / ".hippo" / "ledger.jsonl").touch()
    ok = run_hippo(
        ["status", "--inject"],
        cwd=home / "projects" / "elsewhere",
        env={"HOME": str(home)},
    )
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.startswith("[hippo]"), ok.stdout


def test_m7_die_includes_usage_of_active_subcommand(tmp_project, run_hippo):
    proc = run_hippo(["task", "show", "no-such-task"], cwd=tmp_project)
    assert proc.returncode != 0
    assert "usage:" in proc.stderr
    assert "task show" in proc.stderr, proc.stderr


def test_m8_dispatch_flag_without_value_prints_usage(tmp_project):
    proc = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "dispatch.sh"), "--kind"],
        cwd=str(tmp_project),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2, proc
    assert "Usage:" in proc.stderr
    assert "unbound variable" not in proc.stderr


def _clerk_run_with_stub_codex(tmp_path, body, timeout_s, extra_path_bans=()):
    tmp_path.mkdir(parents=True, exist_ok=True)
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "codex").write_text(body, encoding="utf-8")
    (stub / "codex").chmod(0o755)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt\n", encoding="utf-8")
    inp = tmp_path / "input.txt"
    inp.write_text("input\n", encoding="utf-8")

    path_value = os.environ["PATH"]
    if extra_path_bans:
        path_value = _path_without(tmp_path / "banned", set(extra_path_bans))
    return subprocess.run(
        ["bash", str(SCRIPTS_DIR / "clerk_run.sh"), str(prompt), str(inp)],
        capture_output=True,
        text=True,
        timeout=90,
        env={
            **os.environ,
            "PATH": f"{stub}:{path_value}",
            "HIPPO_CLERK_BACKEND": "codex",
            "HIPPO_CLERK_TIMEOUT": str(timeout_s),
        },
    )


def test_m9_timeout_is_124_but_a_signal_death_is_not(tmp_path):
    """The hand-rolled watchdog (used when no timeout(1) exists) must tell a
    real timeout apart from any other signal death."""
    no_timeout = ("timeout", "gtimeout")
    slow = _clerk_run_with_stub_codex(
        tmp_path / "slow", '#!/bin/sh\nsleep 30\n', 1, extra_path_bans=no_timeout
    )
    assert slow.returncode == 124, slow

    killed = _clerk_run_with_stub_codex(
        tmp_path / "killed",
        '#!/bin/sh\nkill -TERM $$\nsleep 5\n',
        30,
        extra_path_bans=no_timeout,
    )
    assert killed.returncode != 124, (
        f"a SIGTERM death is not a timeout; got rc={killed.returncode}"
    )
    assert killed.returncode >= 128, killed


DOCUMENTED_WS_ENTRIES = {  # DESIGN §3.1 + §3.5.1 (scribe.lock)
    "tasks.yaml",
    "ledger.jsonl",
    "worklog.md",
    "PRIORS.md",
    "cursors.json",
    "failures",
    "config.yaml",
    "scribe.lock",
}


def test_m10_no_scratch_files_inside_hippo_dir(
    tmp_project, run_hippo, fake_transcript, valid_mock_output, tmp_path
):
    """.hippo/ holds only what §3.1 documents — including *while* a clerk
    runs, which is the only moment the clerk's input file exists. The probe is
    a stub backend that lists the directory from inside the call."""
    stub = tmp_path / "probe-bin"
    stub.mkdir()
    snapshot = tmp_path / "hp-listing.txt"
    (stub / "codex").write_text(
        '#!/bin/sh\nls -A "$PROBE_WS" > "$PROBE_SNAPSHOT"\ncat "$PROBE_PAYLOAD"\n',
        encoding="utf-8",
    )
    (stub / "codex").chmod(0o755)

    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-scratch"],
        cwd=tmp_project,
        env={
            "PATH": f"{stub}:{os.environ['PATH']}",
            "HIPPO_CLERK_BACKEND": "codex",
            "PROBE_WS": str(tmp_project / ".hippo"),
            "PROBE_SNAPSHOT": str(snapshot),
            "PROBE_PAYLOAD": str(valid_mock_output),
        },
    )
    assert proc.returncode == 0, proc.stderr

    during = [
        n
        for n in snapshot.read_text(encoding="utf-8").split()
        if n not in DOCUMENTED_WS_ENTRIES
    ]
    assert during == [], f"scratch file inside .hippo/ during the clerk call: {during}"

    after = [
        p.name
        for p in (tmp_project / ".hippo").iterdir()
        if p.name not in DOCUMENTED_WS_ENTRIES
    ]
    assert after == [], f".hippo/ must hold only documented files, found {after}"


def test_m11_inject_last_ignores_nested_and_free_bullets(tmp_project, run_hippo):
    worklog_path(tmp_project).write_text(
        "## 2026-07-30\n\n- 09:00 어제 한 일\n\n## 2026-07-31\n\n"
        "- 10:00 진짜 마지막 항목\n  - 중첩 불릿은 항목이 아니다\n- 자유 불릿\n",
        encoding="utf-8",
    )
    proc = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr
    last = [ln for ln in proc.stdout.splitlines() if ln.startswith("· last:")]
    assert last == ["· last: 진짜 마지막 항목"], proc.stdout
