"""Codex CLI 호스트 지원 (DESIGN §3.8).

여기서 고정하는 것은 전부 codex-cli 0.144.6 실측에서 나온 계약이다:
훅 파일은 두 호스트가 공유하고, transcript만 포맷이 다르다.
"""
import json
import subprocess
import sys

from conftest import REPO_ROOT

CODEX_MANIFEST = REPO_ROOT / ".codex-plugin" / "plugin.json"
CLAUDE_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
DIGEST = REPO_ROOT / "scripts" / "digest_lite.py"


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifests_agree_on_identity_and_version():
    """두 매니페스트가 다른 버전을 주장하면 두 마켓플레이스의 pin이 갈라진다."""
    c, x = _json(CLAUDE_MANIFEST), _json(CODEX_MANIFEST)
    assert c["name"] == x["name"] == "hippo"
    assert c["version"] == x["version"]
    assert c["description"] == x["description"]


def test_codex_manifest_paths_are_plugin_relative():
    """codex는 `./`로 시작하고 플러그인 루트 안에 있는 경로만 받는다."""
    x = _json(CODEX_MANIFEST)
    for key in ("skills", "hooks"):
        assert x[key].startswith("./"), key
        assert ".." not in x[key], key
    assert x["hooks"] == "./hooks/hooks.json"
    assert (REPO_ROOT / x["hooks"][2:]).is_file()
    assert (REPO_ROOT / x["skills"][2:]).is_dir()


def test_hooks_json_is_shared_by_both_hosts():
    """codex 0.144.6은 PascalCase 이벤트 키에 `type: command` 핸들러만 실행한다.
    `async: true`는 파싱은 되지만 건너뛰므로, 비차단은 스크립트가 스스로 얻어야 한다."""
    h = _json(HOOKS_JSON)["hooks"]
    assert set(h) == {"SessionStart", "Stop"}
    for groups in h.values():
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
        {"type": "event_msg", "payload": {"type": "user_message", "message": "GPU 0,1만 써줘"}},
        {"type": "response_item", "payload": {"type": "reasoning", "summary": "숨겨야 함"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "알겠습니다"}},
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
    """clerk 프롬프트가 호스트를 몰라도 되도록 두 포맷이 같은 줄 어휘로 환원된다."""
    proc = _run_digest(_codex_rollout(tmp_path))
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "USER: GPU 0,1만 써줘" in out
    assert "ASSIST: 알겠습니다" in out
    assert "TOOL exec:" in out and "pytest -q" in out
    assert "RES: 3 passed" in out
    assert "숨겨야 함" not in out  # reasoning은 thinking과 같은 이유로 제외
    assert "token_count" not in out


def test_digest_prefilter_still_applies_to_codex(tmp_path):
    """TOOL도 USER도 없는 rollout이면 무출력·rc=0 (clerk 호출 자체를 생략하는 관문)."""
    p = tmp_path / "empty.jsonl"
    p.write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "token_count", "total": 1}}) + "\n",
        encoding="utf-8",
    )
    proc = _run_digest(p)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_digest_still_reads_claude_transcripts(fake_transcript_path):
    """포맷 판별이 기존 경로를 밀어내지 않는다."""
    proc = _run_digest(fake_transcript_path)
    assert proc.returncode == 0, proc.stderr
    assert "USER:" in proc.stdout and "TOOL Bash:" in proc.stdout
