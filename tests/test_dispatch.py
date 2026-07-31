import json
import os
import re
import subprocess

from conftest import SCRIPTS_DIR


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
    assert proc.stderr == "dispatch: .hippo/ 없음 — dispatch 기록을 생략합니다\n"
    assert proc.stdout.startswith("dispatch:d")
