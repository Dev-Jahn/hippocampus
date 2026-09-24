"""Codex CLI host support (DESIGN §3.8).

Everything pinned here comes from measuring codex-cli 0.144.6 and reading 0.156.1's plugin
loader: the two hosts share the hook scripts and hooks/hooks.json, Claude Code adds its own
hooks on top, and only the transcript format differs.
"""
import json
import subprocess
import sys

from conftest import REPO_ROOT

CODEX_MANIFEST = REPO_ROOT / ".codex-plugin" / "plugin.json"
CLAUDE_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
CLAUDE_HOOKS_JSON = REPO_ROOT / "hooks" / "claude-hooks.json"
DIGEST = REPO_ROOT / "scripts" / "digest_lite.py"


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifests_agree_on_identity_and_version():
    """If the two manifests claim different versions, the two marketplace pins drift apart."""
    c, x = _json(CLAUDE_MANIFEST), _json(CODEX_MANIFEST)
    assert c["name"] == x["name"] == "hippo"
    assert c["version"] == x["version"]
    assert c["description"] == x["description"]


def test_codex_manifest_paths_are_plugin_relative():
    """codex only accepts paths that start with `./` and stay inside the plugin root."""
    x = _json(CODEX_MANIFEST)
    for key in ("skills", "hooks"):
        assert x[key].startswith("./"), key
        assert ".." not in x[key], key
    assert (REPO_ROOT / x["hooks"][2:]).is_file()
    assert (REPO_ROOT / x["skills"][2:]).is_dir()


def test_codex_gets_only_the_hooks_measured_on_it():
    """Codex keys a hook's trust by the manifest-named file's plugin-relative path
    (`hippo@<marketplace>:hooks/hooks.json:session_start:0:0`, 0.156.1), and an untrusted hook
    is skipped silently: moving codex's file would switch the capsule and the scribe off for
    every user who trusted them — lanes included, since `codex exec` cannot ask. So codex keeps
    hooks/hooks.json with SessionStart and Stop, and the Claude Code-only hooks live in a file
    only Claude Code's manifest names; Claude Code loads the default hooks/hooks.json and adds
    it (2.1.281, measured: each event fired once). No event is in both, so none runs twice."""
    assert _json(CODEX_MANIFEST)["hooks"] == "./hooks/hooks.json"
    assert _json(CLAUDE_MANIFEST)["hooks"] == "./hooks/claude-hooks.json"
    shared, claude_only = _json(HOOKS_JSON)["hooks"], _json(CLAUDE_HOOKS_JSON)["hooks"]
    assert set(shared) == {"SessionStart", "Stop"}
    assert set(claude_only) == {"SubagentStart", "PreCompact"}


def test_hook_handlers_are_plain_commands():
    """codex 0.144.6 runs only `type: command` handlers under PascalCase event keys.
    `async: true` parses but is skipped, so a hook must earn its non-blocking behavior itself."""
    for path in (HOOKS_JSON, CLAUDE_HOOKS_JSON):
        for groups in _json(path)["hooks"].values():
            for g in groups:
                for handler in g["hooks"]:
                    assert handler["type"] == "command"
                    assert handler.get("async") is not True


def _run_digest(path):
    return subprocess.run(
        [sys.executable, str(DIGEST), str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _codex_rollout(tmp_path):
    lines = [
        {"type": "session_meta", "payload": {"session_id": "s1", "cwd": "/tmp/x"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Use GPUs 0 and 1 only"}},
        {"type": "response_item", "payload": {"type": "reasoning", "summary": "must stay hidden"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Understood"}},
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": 'tools.exec_command({cmd:"pytest -q"})',
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "output": [{"type": "input_text", "text": "3 passed"}],
            },
        },
        {"type": "event_msg", "payload": {"type": "token_count", "total": 10}},
    ]
    p = tmp_path / "rollout.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln, ensure_ascii=False) + "\n")
    return p


def test_digest_reduces_codex_rollout_to_the_shared_line_vocabulary(tmp_path):
    """Both formats reduce to the same line vocabulary, so the clerk prompt never learns which
    host it is reading."""
    proc = _run_digest(_codex_rollout(tmp_path))
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "USER: Use GPUs 0 and 1 only" in out
    assert "ASSIST: Understood" in out
    assert "TOOL exec:" in out and "pytest -q" in out
    assert "RES: 3 passed" in out
    assert "must stay hidden" not in out  # reasoning is excluded for the same reason as thinking
    assert "token_count" not in out


def test_digest_prefilter_still_applies_to_codex(tmp_path):
    """A rollout with no TOOL and no USER line prints nothing and exits 0 (the gate that skips
    the clerk call entirely)."""
    p = tmp_path / "empty.jsonl"
    p.write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "token_count", "total": 1}}) + "\n",
        encoding="utf-8",
    )
    proc = _run_digest(p)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_digest_still_reads_claude_transcripts(fake_transcript_path):
    """Format detection must not displace the existing path."""
    proc = _run_digest(fake_transcript_path)
    assert proc.returncode == 0, proc.stderr
    assert "USER:" in proc.stdout and "TOOL Bash:" in proc.stdout
