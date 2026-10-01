"""Item (7): digest_lite baseline — sample jsonl -> non-empty digest,
--since-line behaves.

DESIGN.md §3.5 step 2 only describes digest_lite.py's role (compress only the lines after the
cursor) without freezing an exact CLI surface. This test assumes the
simplest reading consistent with that description and with `scripts/` being
plain-Python entry points invoked positionally:

    python3 scripts/digest_lite.py <transcript.jsonl> [--since-line N]

writing the digest to stdout. If the real interface differs, this file is
the one to adjust — the two properties under test (non-empty digest; a
--since-line cutoff strictly shrinks the output) are the load-bearing
contract, not the exact flag spelling.
"""
import json
import re
import subprocess
import sys

from conftest import SCRIPTS_DIR

DIGEST_SCRIPT = SCRIPTS_DIR / "digest_lite.py"


def _run_digest(args, timeout=30):
    return subprocess.run(
        [sys.executable, str(DIGEST_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_digest_lite_produces_nonempty_digest(fake_transcript_path):
    proc = _run_digest([str(fake_transcript_path)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() != ""


def test_digest_lite_since_line_shrinks_output(fake_transcript_path):
    full = _run_digest([str(fake_transcript_path)])
    assert full.returncode == 0, full.stderr

    total_lines = len(
        fake_transcript_path.read_text(encoding="utf-8").splitlines()
    )

    since_all = _run_digest([str(fake_transcript_path), "--since-line", str(total_lines)])
    assert since_all.returncode == 0, since_all.stderr
    assert len(since_all.stdout) < len(full.stdout), (
        "digesting only the lines after the last one should yield strictly "
        "less content than digesting from the start"
    )


# --------------------------------------------------------------------------
# Kept by role (DESIGN §3.5 step 2): what the scribe records from is whole, the rest one line,
# one budget over the whole digest, the same vocabulary from both hosts.
# --------------------------------------------------------------------------

LINE = re.compile(r"^(?:\[\d+\] (?:USER|ASSIST|TOOL|RES|RES-ERR|COMPACTION)\b|OMITTED: )")
BIG = "x" * 4000  # past every per-line cap the digest used to have
MARKER = re.compile(r"OMITTED: .* the oldest (\d+) one-line entries \((\d+) chars\) and "
                    r"(\d+) whole entries \((\d+) chars\)")


def _write(path, records):
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _digest(path, *args):
    proc = _run_digest([str(path), *args])
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert all(LINE.match(ln) for ln in lines), [ln[:80] for ln in lines if not LINE.match(ln)]
    return lines


def _line(lines, n):
    found = [ln for ln in lines if ln.startswith(f"[{n}] ")]
    assert found, f"no line [{n}]"
    return found


def _user(content, **kw):
    return {"type": "user", "message": {"role": "user", "content": content}, **kw}


def _call(tid, name, inp, text=None):
    blocks = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def _result(tid, content, is_error=False):
    return _user([{"type": "tool_result", "tool_use_id": tid, "content": content,
                   "is_error": is_error}])


def _claude(tmp_path):
    return _write(tmp_path / "claude.jsonl", [
        _call("t0", "Agent", {"description": "early run", "prompt": "brief " + BIG}),  # 1
        _user("USER-OPENS " + BIG + " USER-ENDS"),  # 2
        _result("t0", "EARLY-REPORT " + BIG + " EARLY-END"),  # 3: answers a call before the window
        _user("Base directory for this skill: SKILL-BODY " + BIG, isMeta=True),  # 4
        _call("t1", "Bash",
              {"command": "cd /r && HIPPO_X=1 hippo log outcome --ref d1 --result accepted"},
              text="ASSIST-OPENS " + BIG + " ASSIST-ENDS"),  # 5
        _result("t1", "HIPPO-OUT " + BIG + " HIPPO-END"),  # 6
        _call("t2", "Bash", {"command": "git -C /r commit -m 'fix'\necho done"}),  # 7
        _result("t2", "[dev abc1234] fix " + BIG + " GIT-END"),  # 8
        _call("t3", "Bash", {"command": "cd ~/workspace/hippo && pytest -q\nsecond line\nthird",
                             "description": "run tests"}),  # 9
        _result("t3", "PYTEST-OUT " + BIG),  # 10
        _call("t4", "Bash", {"command": "make check"}),  # 11
        _result("t4", "Exit code 2\nnoise " + BIG + "\nFAILED: 1 test", is_error=True),  # 12
        _call("t5", "Read", {"file_path": "/r/a.py"}),  # 13
        _result("t5", "FILE-BODY " + BIG),  # 14
        _call("t6", "Agent", {"description": "kernel", "subagent_type": "general-purpose",
                              "prompt": "AGENT-BRIEF " + BIG + " BRIEF-END"}),  # 15
        _result("t6", [{"type": "text", "text": "AGENT-REPORT " + BIG + " REPORT-END"}]),  # 16
        _call("t7", "Workflow", {"script": "WF-SCRIPT " + BIG + " WF-END"}),  # 17
        _user("<task-notification>\n<task-id>w1</task-id>\nNOTIFY " + BIG + " NOTIFY-END"),  # 18
        {"type": "attachment", "attachment": {
            "type": "queued_command", "prompt": "QUEUED-RULE " + BIG + " QUEUED-END"}},  # 19
        _user("COMPACT-SUMMARY " + BIG + " COMPACT-END", isCompactSummary=True),  # 20
        _call("t8", "Bash", {"command": "git merge-base main dev"}),  # 21
        _result("t8", "MERGE-BASE " + BIG),  # 22
    ])


def test_what_the_scribe_records_from_is_kept_whole(tmp_path):
    lines = _digest(_claude(tmp_path), "--since-line", "1")
    text = "\n".join(lines)
    for opens, ends in [("USER-OPENS", "USER-ENDS"), ("ASSIST-OPENS", "ASSIST-ENDS"),
                        ("HIPPO-OUT", "HIPPO-END"), ("[dev abc1234] fix", "GIT-END"),
                        ("AGENT-BRIEF", "BRIEF-END"), ("AGENT-REPORT", "REPORT-END"),
                        ("WF-SCRIPT", "WF-END"), ("NOTIFY", "NOTIFY-END"),
                        ("QUEUED-RULE", "QUEUED-END"), ("COMPACT-SUMMARY", "COMPACT-END"),
                        ("EARLY-REPORT", "EARLY-END")]:
        assert re.search(re.escape(opens) + " x{4000} " + re.escape(ends), text), opens
    assert any("hippo log outcome --ref d1" in ln for ln in _line(lines, 5))
    assert any("echo done" in ln for ln in _line(lines, 7)), "a whole command keeps every line"
    assert _line(lines, 19)[0].startswith("[19] USER: QUEUED-RULE"), "a queued prompt is the user's"
    assert "SKILL-BODY" not in text, "a loaded skill's body is the host's, not the user's"


def test_other_tool_use_is_one_line_saying_what_was_done(tmp_path):
    lines = _digest(_claude(tmp_path), "--since-line", "1")
    assert _line(lines, 9) == [
        "[9] TOOL Bash: cd ~/workspace/hippo && pytest -q …(+2 lines)  // run tests"]
    assert _line(lines, 10) == ["[10] RES: ok"]
    assert _line(lines, 12) == ["[12] RES-ERR: exit 2: FAILED: 1 test"]
    assert _line(lines, 13) == ["[13] TOOL Read: /r/a.py"]
    assert _line(lines, 14) == ["[14] RES: ok"]
    assert _line(lines, 22) == ["[22] RES: ok"], "git merge-base is not an outcome signal"
    text = "\n".join(lines)
    assert "PYTEST-OUT" not in text and "FILE-BODY" not in text


def _codex(tmp_path):
    def item(payload, kind="response_item"):
        return {"type": kind, "payload": payload}

    def msg(role, text):
        return item({"type": "message", "role": role,
                     "content": [{"type": "input_text", "text": text}]})

    return _write(tmp_path / "rollout.jsonl", [
        {"type": "session_meta", "payload": {"id": "s"}},  # 1
        msg("user", "<environment_context>\n<cwd>/r</cwd>\n</environment_context>"),  # 2
        msg("user", "USER-OPENS " + BIG + " USER-ENDS"),  # 3
        item({"type": "user_message", "message": "USER-OPENS " + BIG + " USER-ENDS"},
             "event_msg"),  # 4: an older codex's twin of line 3
        msg("assistant", "ASSIST-OPENS " + BIG + " ASSIST-ENDS"),  # 5
        item({"type": "function_call", "name": "exec_command", "call_id": "c1",
              "arguments": json.dumps({"cmd": "cd /r && hippo task done t1"})}),  # 6
        item({"type": "function_call_output", "call_id": "c1",
              "output": "Process exited with code 0\nOutput:\nHIPPO-OUT " + BIG + " HIPPO-END"}),  # 7
        item({"type": "custom_tool_call", "name": "exec", "call_id": "c2",
              "input": 'tools.exec_command({cmd:"ls -la\\nwc -l a"})'}),  # 8
        item({"type": "custom_tool_call_output", "call_id": "c2",
              "output": [{"type": "input_text", "text": "LS-OUT " + BIG}]}),  # 9
        item({"type": "function_call", "name": "exec_command", "call_id": "c3",
              "arguments": json.dumps({"cmd": "cargo build"})}),  # 10
        item({"type": "function_call_output", "call_id": "c3",
              "output": "Process exited with code 101\nOutput:\nnoise\nerror: could not compile"}),  # 11
        item({"type": "function_call", "name": "spawn_agent", "namespace": "collaboration",
              "call_id": "c4", "arguments": json.dumps({"message": "AGENT-BRIEF " + BIG})}),  # 12
        item({"type": "function_call_output", "call_id": "c4",
              "output": "AGENT-REPORT " + BIG + " REPORT-END"}),  # 13
        item({"type": "custom_tool_call", "name": "exec", "call_id": "c5",
              "input": 'tools.exec_command({cmd:"git commit -m \\"fix\\""})'}),  # 14
        item({"type": "custom_tool_call_output", "call_id": "c5", "output": [
            {"type": "input_text", "text": "[main f00ba12] fix " + BIG + " GIT-END"}]}),  # 15
    ])


def test_codex_reads_to_the_same_vocabulary_by_the_same_rule(tmp_path):
    lines = _digest(_codex(tmp_path))
    text = "\n".join(lines)
    for opens, ends in [("USER-OPENS", "USER-ENDS"), ("ASSIST-OPENS", "ASSIST-ENDS"),
                        ("HIPPO-OUT", "HIPPO-END"), ("AGENT-REPORT", "REPORT-END"),
                        ("[main f00ba12] fix", "GIT-END")]:
        assert re.search(re.escape(opens) + " x{4000} " + re.escape(ends), text), opens
    assert "AGENT-BRIEF " + BIG in text
    assert "environment_context" not in text, "host-injected context is not the user's"
    assert text.count("USER-OPENS") == 1, "the event_msg twin is not repeated"
    assert _line(lines, 8) == ["[8] TOOL exec: ls -la …(+1 lines)"]
    assert _line(lines, 9) == ["[9] RES: ok"]
    assert _line(lines, 11) == ["[11] RES-ERR: exit 101: error: could not compile"]


def test_the_budget_drops_one_line_entries_first_and_says_so(tmp_path):
    path = _claude(tmp_path)
    whole = _digest(path, "--since-line", "1", "--budget", "0")
    size = sum(len(ln) + 1 for ln in whole)
    assert _digest(path, "--since-line", "1") == whole, "under the default budget: no change"

    # One char short: the marker's own line must fit too, which the one-line entries alone
    # pay for — they go, oldest first, and the marker accounts for each of them.
    lines = _digest(path, "--since-line", "1", "--budget", str(size - 1))
    assert sum(len(ln) + 1 for ln in lines) <= size - 1
    n_brief, c_brief, n_whole, c_whole = map(int, MARKER.match(lines[0]).groups())
    assert n_brief >= 1 and n_whole == c_whole == 0
    gone = [ln for ln in whole if ln not in lines]
    assert len(gone) == n_brief and sum(len(ln) + 1 for ln in gone) == c_brief
    assert gone[0].startswith("[9] "), "the oldest one-line entry goes first"

    # Room for a few whole entries only: every one-line entry goes, then the oldest whole
    # ones; nothing goes uncounted, and what stays keeps its order.
    lines = _digest(path, "--since-line", "1", "--budget", "15000")
    assert sum(len(ln) + 1 for ln in lines) <= 15000
    n_brief, c_brief, n_whole, c_whole = map(int, MARKER.match(lines[0]).groups())
    assert n_whole > 0 and len(lines) - 1 + n_brief + n_whole == len(whole)
    assert c_brief + c_whole + sum(len(ln) + 1 for ln in lines[1:]) == size
    assert not any(ln.endswith(" RES: ok") or " TOOL Read: " in ln for ln in lines)
    assert [ln for ln in whole if ln in lines] == lines[1:]
    assert lines[-1].startswith("[20] COMPACTION: "), "the newest whole entry stays"
