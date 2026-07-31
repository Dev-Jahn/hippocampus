import json
import os
import re
import subprocess

from conftest import SCRIPTS_DIR


def _stub_codex(tmp_path):
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    codex = stub / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
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
