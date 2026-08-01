"""clerk_run.sh backend resolution and flag construction, with fake CLIs on PATH.

The codex/claude branches used to run only where the real CLI happened to be installed, so
flag drift was caught by nothing but a comment. A fake bin that records its argv pins the
contract on any machine. PATH is rebuilt from scratch (stub + system dirs) so a codex or
claude actually installed on the test machine can never leak into `auto`."""

import os
import subprocess

from conftest import SCRIPTS_DIR

FAKE_BIN = '#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\necho ok\n'
SYSTEM_PATH = "/usr/bin:/bin"


def _run_clerk(tmp_path, bins, env=None):
    stub = tmp_path / "stub-bin"
    stub.mkdir(exist_ok=True)
    for name in bins:
        f = stub / name
        f.write_text(FAKE_BIN, encoding="utf-8")
        f.chmod(0o755)
    prompt = tmp_path / "p.md"
    prompt.write_text("prompt-part\n", encoding="utf-8")
    inp = tmp_path / "i.txt"
    inp.write_text("input-part\n", encoding="utf-8")
    capture = tmp_path / "argv.txt"
    full_env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("HIPPO_")},
        **(env or {}),
        "PATH": f"{stub}:{SYSTEM_PATH}",
        "CAPTURE": str(capture),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "clerk_run.sh"), str(prompt), str(inp)],
        capture_output=True, text=True, timeout=60, env=full_env,
    )
    argv = capture.read_text(encoding="utf-8").splitlines() if capture.exists() else []
    return proc, argv


def test_auto_prefers_codex_and_passes_the_contract_flags(tmp_path):
    proc, argv = _run_clerk(tmp_path, ["codex", "claude"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
    assert argv[0] == "exec"
    assert argv[argv.index("-m") + 1] == "gpt-5.6-luna"
    assert "--disable" in argv and argv[argv.index("--disable") + 1] == "hooks"
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "--skip-git-repo-check" in argv
    # The combined prompt is one argument: prompt file first, input file after.
    assert "prompt-part" in argv and "input-part" in argv


def test_auto_falls_back_to_claude(tmp_path):
    proc, argv = _run_clerk(tmp_path, ["claude"])
    assert proc.returncode == 0, proc.stderr
    assert argv[0] == "-p"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--strict-mcp-config" in argv
    assert "--setting-sources" in argv
    # --tools "" — the empty argument really is empty, not dropped.
    assert argv[argv.index("--tools") + 1] == ""


def test_model_override_lands_on_either_backend(tmp_path):
    _, argv = _run_clerk(tmp_path, ["codex"],
                         env={"HIPPO_CLERK_BACKEND": "codex",
                              "HIPPO_CLERK_MODEL": "pinned-model"})
    assert argv[argv.index("-m") + 1] == "pinned-model"

    _, argv = _run_clerk(tmp_path, ["claude"],
                         env={"HIPPO_CLERK_BACKEND": "claude",
                              "HIPPO_CLERK_MODEL": "pinned-model"})
    assert argv[argv.index("--model") + 1] == "pinned-model"


def test_auto_with_no_backend_exits_3(tmp_path):
    proc, _ = _run_clerk(tmp_path, [])
    assert proc.returncode == 3
