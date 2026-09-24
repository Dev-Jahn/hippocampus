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
    assert argv[argv.index("-m") + 1] == "gpt-6-luna"
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
    assert argv[argv.index("--effort") + 1] == "low"
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


FAILING_CODEX = """#!/bin/sh
echo "OpenAI Codex v0.156.1" >&2
echo "ERROR: Reconnecting... 5/5" >&2
echo "ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication" >&2
exit 1
"""


def test_a_failing_backend_says_why_in_the_scribe_dump(tmp_project, run_hippo, fake_transcript,
                                                       tmp_path):
    """Measured on b200: 7 failed scribe runs whose dumps all read `clerk rc=1` with an empty
    stderr — clerk_run.sh threw codex's stderr away. Its last error line is the cause, and it
    now leads the dump (the line the capsule quotes)."""
    stub = tmp_path / "stub-bin"
    stub.mkdir()
    codex = stub / "codex"
    codex.write_text(FAILING_CODEX, encoding="utf-8")
    codex.chmod(0o755)
    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "s-fail"],
        cwd=tmp_project,
        env={"HIPPO_CLERK_BACKEND": "codex", "PATH": f"{stub}:{os.environ['PATH']}"},
    )
    assert proc.returncode != 0
    [dump] = (tmp_project / ".hippo" / "failures").glob("*-scribe-*")
    text = dump.read_text(encoding="utf-8")
    assert text.splitlines()[0] == (
        "clerk rc=1: codex: ERROR: unexpected status 401 Unauthorized: Missing bearer or basic "
        "authentication")
    assert "ERROR: Reconnecting... 5/5" in text.split("--- stderr ---", 1)[1]


ECHOING_CODEX = """#!/bin/sh
# codex echoes its whole prompt to stderr: here a digest line that mentions an error.
echo "OpenAI Codex v0.156.1" >&2
echo "user" >&2
echo "[3] RES: E   ImportError: cannot import name foo" >&2
echo "[4] USER: fix the error please" >&2
%s
"""


def _run_stub(tmp_path, body, env=None):
    stub = tmp_path / "stub-bin"
    stub.mkdir(exist_ok=True)
    codex = stub / "codex"
    codex.write_text(body, encoding="utf-8")
    codex.chmod(0o755)
    (tmp_path / "p.md").write_text("prompt-part\n", encoding="utf-8")
    (tmp_path / "i.txt").write_text("input-part\n", encoding="utf-8")
    full_env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("HIPPO_")},
        **(env or {}),
        "PATH": f"{stub}:{SYSTEM_PATH}",
        "HIPPO_CLERK_BACKEND": "codex",
    }
    return subprocess.run(
        ["bash", str(SCRIPTS_DIR / "clerk_run.sh"), str(tmp_path / "p.md"), str(tmp_path / "i.txt")],
        capture_output=True, text=True, timeout=60, env=full_env,
    )


def test_the_echoed_prompt_is_never_the_cause(tmp_path):
    """A digest line that mentions an error is transcript text, not codex's words: with no error
    line of its own, the failure names no cause rather than quoting the transcript."""
    proc = _run_stub(tmp_path, ECHOING_CODEX % "exit 1")
    assert proc.returncode == 1
    assert "ImportError" not in proc.stderr and "fix the error" not in proc.stderr


def test_a_timeout_is_named_from_the_exit_code(tmp_path):
    proc = _run_stub(tmp_path, ECHOING_CODEX % "sleep 10",
                     env={"HIPPO_CLERK_TIMEOUT": "1"})
    assert proc.returncode == 124
    assert proc.stderr.splitlines()[0] == "codex: timed out after 1s"
    assert "ImportError" not in proc.stderr


def test_a_broken_tmpdir_does_not_stop_the_backend(tmp_path):
    proc = _run_stub(tmp_path, "#!/bin/sh\necho ok\n",
                     env={"TMPDIR": str(tmp_path / "does-not-exist")})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
