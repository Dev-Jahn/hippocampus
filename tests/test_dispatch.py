import json
import os
import re
import subprocess

from conftest import SCRIPTS_DIR, read_ledger


def _stub_codex(tmp_path, body="#!/bin/sh\nexit 0\n"):
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    codex = stub / "codex"
    codex.write_text(body, encoding="utf-8")
    codex.chmod(0o755)
    return f"{stub}:{os.environ['PATH']}"


def test_dispatch_id_is_128_bit_hex(tmp_project, tmp_path):
    proc = subprocess.run(
        [
            "bash",
            str(SCRIPTS_DIR / "dispatch.sh"),
            "--kind",
            "test",
            "--scope",
            "id-format",
        ],
        cwd=tmp_project,
        env={**os.environ, "PATH": _stub_codex(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    dispatch_id = proc.stdout.removeprefix("dispatch:").strip()
    assert re.fullmatch(r"d[0-9a-f]{32}", dispatch_id)
    event = json.loads((tmp_project / ".hippo" / "ledger.jsonl").read_text())
    assert event["id"] == dispatch_id


def test_double_dash_forwards_wrapper_shaped_flags(tmp_project, tmp_path):
    path = _stub_codex(tmp_path, '#!/bin/sh\nprintf "%s\\n" "$@"\n')
    proc = subprocess.run(
        [
            "bash",
            str(SCRIPTS_DIR / "dispatch.sh"),
            "--kind",
            "wrapper-kind",
            "--scope",
            "wrapper-scope",
            "--",
            "--kind",
            "codex-kind",
            "--scope=codex-scope",
            "--task",
            "codex-task",
        ],
        cwd=tmp_project,
        env={**os.environ, "PATH": path},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[1:] == [
        "exec",
        "--kind",
        "codex-kind",
        "--scope=codex-scope",
        "--task",
        "codex-task",
    ]
    event = json.loads((tmp_project / ".hippo" / "ledger.jsonl").read_text())
    assert event["kind"] == "wrapper-kind"
    assert event["scope"] == "wrapper-scope"


def test_uninitialized_project_warns_when_dispatch_is_not_recorded(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").touch()
    path = _stub_codex(tmp_path)
    proc = subprocess.run(
        [
            "bash",
            str(SCRIPTS_DIR / "dispatch.sh"),
            "--kind",
            "test",
            "--scope",
            "missing-hippo",
        ],
        cwd=project,
        env={**os.environ, "PATH": path},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stderr == "dispatch: no .hippo/ — skipping the dispatch record\n"
    assert proc.stdout.startswith("dispatch:d")


# --------------------------------------------------------------------------
# `hippo dispatch` — the CLI subcommand is the real surface (scripts/dispatch.sh is a shim)
# --------------------------------------------------------------------------

def test_cli_dispatch_records_and_launches(tmp_project, tmp_path, run_hippo):
    proc = run_hippo(
        ["dispatch", "--kind", "impl", "--scope", "cli entry point", "--task", "feat/x",
         "-m", "gpt-5.6-sol", "-c", 'model_reasoning_effort="high"'],
        cwd=tmp_project,
        env={"PATH": _stub_codex(tmp_path, '#!/bin/sh\nprintf "%s\\n" "$@"\n')},
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert re.fullmatch(r"d[0-9a-f]{32}", lines[0].removeprefix("dispatch:"))
    assert lines[1] == "exec"  # codex was launched untouched
    event = json.loads((tmp_project / ".hippo" / "ledger.jsonl").read_text())
    assert event["exec"] == "codex/gpt-5.6-sol/high"
    assert event["task"] == "feat/x" and event["src"] == "wrapper"


def test_cli_dispatch_launches_even_without_a_project(tmp_path, run_hippo):
    """The CLI is a silent no-op outside a .hippo project, but dispatch is the exception:
    swallowing the launch because the record failed would make it a trap, not a wrapper."""
    project = tmp_path / "bare"
    project.mkdir()
    (project / ".git").touch()
    proc = run_hippo(
        ["dispatch", "--kind", "impl", "--scope", "launch without a record"],
        cwd=project,
        env={"PATH": _stub_codex(tmp_path, '#!/bin/sh\necho launched\n')},
    )
    assert proc.returncode == 0, proc.stderr
    assert "dispatch: no .hippo/" in proc.stderr
    assert proc.stdout.splitlines()[1] == "launched"


def test_cli_dispatch_help_does_not_launch(tmp_project, tmp_path, run_hippo):
    proc = run_hippo(
        ["dispatch", "-h"],
        cwd=tmp_project,
        env={"PATH": _stub_codex(tmp_path, '#!/bin/sh\necho LAUNCHED\n')},
    )
    assert proc.returncode == 0
    assert "LAUNCHED" not in proc.stdout
    assert "--kind" in proc.stdout
    assert read_ledger(tmp_project) == []


def test_cli_dispatch_honors_double_dash(tmp_project, tmp_path, run_hippo):
    proc = run_hippo(
        ["dispatch", "--kind", "k", "--scope", "s", "--", "--kind", "codex-kind"],
        cwd=tmp_project,
        env={"PATH": _stub_codex(tmp_path, '#!/bin/sh\nprintf "%s\\n" "$@"\n')},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[1:] == ["exec", "--kind", "codex-kind"]
    assert json.loads((tmp_project / ".hippo" / "ledger.jsonl").read_text())["kind"] == "k"
