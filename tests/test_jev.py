"""The judge layer (DESIGN §3.9) and the scribe gate (§3.5.3b).

Nothing here may reach the network. The unit tests pin `HIPPO_JEV_BACKEND` themselves and the
subprocess tests get `off` from conftest unless they ask for `mock` — this machine's
environment carries a live TYPESAFE_API_KEY, so an unpinned scribe run would be a real HTTP
call. The live path is smoked by hand, never from the suite.
"""
import json
import sys
from datetime import datetime, timezone

import pytest

from conftest import REPO_ROOT, cursors_path, read_ledger, worklog_path

GATE_IDS = {"user_instruction", "verdict", "external_review", "launch", "substantive_work"}


@pytest.fixture
def cli(monkeypatch):
    """hippo_cli imported in-process, with a spec cache of its own for this test."""
    path = str(REPO_ROOT / "cli")
    if path not in sys.path:
        sys.path.insert(0, path)
    import hippo_cli

    monkeypatch.setattr(hippo_cli, "JEV_SPECS", {})
    return hippo_cli


def _noul(text="whatever"):
    return {"type": "noul", "instructions": text,
            "criteria": {"true": "it does", "false": "it does not"}}


# --------------------------------------------------------------------------
# backend resolution (§3.9 — the clerk's precedence shape)
# --------------------------------------------------------------------------

def test_backend_is_the_key_alone_with_a_developer_override(cli, tmp_project, monkeypatch):
    hp = tmp_project / ".hippo"
    monkeypatch.delenv("HIPPO_JEV_BACKEND", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert cli.jev_backend(hp) == "off", "auto without a key: the judge does not exist"
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    assert cli.jev_backend(hp) == "off", "an empty key is no key"
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-test")
    assert cli.jev_backend(hp) == "live"
    monkeypatch.setenv("HIPPO_JEV_BACKEND", "off")
    assert cli.jev_backend(hp) == "off", "the developer knob overrides auto"
    # No user-facing switch exists: a config.yaml key is not read, on purpose (§3.9).
    (hp / "config.yaml").write_text("jev:\n  backend: mock\n", encoding="utf-8")
    assert cli.jev_backend(hp) == "off"
    monkeypatch.delenv("HIPPO_JEV_BACKEND")
    assert cli.jev_backend(hp) == "live", "config.yaml must not be able to turn it on or off"


def test_off_answers_without_touching_anything(cli, tmp_project, monkeypatch):
    monkeypatch.setenv("HIPPO_JEV_BACKEND", "off")
    answers, meta = cli.judge(tmp_project / ".hippo", "unit", "state", {"q": _noul()})
    assert answers is None
    assert (meta["ok"], meta["reason"], meta["tokens"]) == (False, "off", 0)


def test_live_without_a_key_fails_instead_of_calling(cli, tmp_project, monkeypatch):
    monkeypatch.setenv("HIPPO_JEV_BACKEND", "live")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    answers, meta = cli.judge(tmp_project / ".hippo", "unit", "state", {"q": _noul()})
    assert (answers, meta["ok"], meta["reason"]) == (None, False, "no TYPESAFE_API_KEY")


# --------------------------------------------------------------------------
# the mock backend (§3.9)
# --------------------------------------------------------------------------

def _mock_file(tmp_path, payload, name="jev-mock.json"):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_mock_answers_by_id_then_by_type_default(cli, tmp_project, tmp_path, monkeypatch):
    out = _mock_file(tmp_path, {
        "answers": {"named": {"noul": 0.91}},
        "default": {"noul": 0.05, "choice": "b", "score": 2.0},
    })
    capture = tmp_path / "capture.json"
    monkeypatch.setenv("HIPPO_JEV_BACKEND", "mock")
    monkeypatch.setenv("HIPPO_JEV_MOCK_OUTPUT", str(out))
    monkeypatch.setenv("HIPPO_JEV_MOCK_CAPTURE", str(capture))
    questions = {
        "named": _noul(),
        "fallback": _noul(),
        "pick": {"type": "choice", "instructions": "which", "criteria": {"a": "A", "b": "B"}},
        "level": {"type": "score", "instructions": "how far", "criteria": ["none", "some"]},
    }
    answers, meta = cli.judge(tmp_project / ".hippo", "unit", {"digest": "hi"}, questions)
    assert (meta["ok"], meta["reason"]) == (True, None)
    assert answers["named"] == {"noul": 0.91}
    assert answers["fallback"] == {"noul": 0.05}
    assert answers["pick"] == {"choice": "b", "confidence": 1.0, "probabilities": {"b": 1.0}}
    assert answers["level"] == {"score": 2.0, "confidence": 1.0}

    sent = json.loads(capture.read_text(encoding="utf-8"))
    assert sent["state"] == {"digest": "hi"}
    assert set(sent["questions"]) == set(questions)
    assert sent["model"] == "jev-latest"


def test_mock_fails_named_for_an_id_no_default_covers(cli, tmp_project, tmp_path, monkeypatch):
    out = _mock_file(tmp_path, {"default": {"choice": "b"}})
    monkeypatch.setenv("HIPPO_JEV_BACKEND", "mock")
    monkeypatch.setenv("HIPPO_JEV_MOCK_OUTPUT", str(out))
    answers, meta = cli.judge(tmp_project / ".hippo", "unit", "s", {"lonely": _noul()})
    assert answers is None
    assert (meta["ok"], meta["reason"]) == (False, "mock: no answer for lonely")


def test_mock_with_no_file_fails_with_a_reason(cli, tmp_project, tmp_path, monkeypatch):
    monkeypatch.setenv("HIPPO_JEV_BACKEND", "mock")
    monkeypatch.setenv("HIPPO_JEV_MOCK_OUTPUT", str(tmp_path / "absent.json"))
    answers, meta = cli.judge(tmp_project / ".hippo", "unit", "s", {"q": _noul()})
    assert answers is None and meta["ok"] is False
    assert meta["reason"].startswith("mock: "), meta["reason"]


def test_oversize_state_never_leaves_the_client(cli, tmp_project, tmp_path, monkeypatch):
    out = _mock_file(tmp_path, {"default": {"noul": 0.9}})
    capture = tmp_path / "capture.json"
    monkeypatch.setenv("HIPPO_JEV_BACKEND", "mock")
    monkeypatch.setenv("HIPPO_JEV_MOCK_OUTPUT", str(out))
    monkeypatch.setenv("HIPPO_JEV_MOCK_CAPTURE", str(capture))
    state = "x" * (cli.JEV_STATE_BUDGET_CHARS + 1)
    answers, meta = cli.judge(tmp_project / ".hippo", "unit", state, {"q": _noul()})
    assert answers is None and meta["ok"] is False
    assert meta["reason"].startswith("state exceeds jev budget ("), meta["reason"]
    assert not capture.exists(), "an oversize state must not be sent anywhere"


# --------------------------------------------------------------------------
# spec loading and templating (§3.9)
# --------------------------------------------------------------------------

def test_templating_renders_id_and_instructions_and_leaves_prose_alone(cli, tmp_path, monkeypatch):
    d = tmp_path / "jev"
    d.mkdir()
    (d / "fan.yaml").write_text(
        "questions:\n"
        "  lane_{i}_green:\n"
        "    type: noul\n"
        "    instructions: Did `lanes[{i}]` report a {word} result, unlike {unknown}?\n"
        "    criteria:\n"
        '      "true": it did\n'
        '      "false": it did not\n'
        "policy:\n"
        "  skip_when_all_below: 0.3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "JEV_DIR", d)

    merged = {}
    for i in (1, 2):
        merged.update(cli.jev_questions("fan", i=i, word="green"))
    assert set(merged) == {"lane_1_green", "lane_2_green"}
    assert merged["lane_2_green"]["instructions"] == (
        "Did `lanes[2]` report a green result, unlike {unknown}?"
    )
    assert merged["lane_1_green"]["criteria"] == {"true": "it did", "false": "it did not"}
    assert cli.jev_policy("fan") == {"skip_when_all_below": 0.3}


def test_a_brace_that_is_not_a_placeholder_is_a_malformed_spec(cli, tmp_path, monkeypatch):
    """The unknown-name case survives (above); a bare brace is a typo, and a typo in
    infrastructure is a hard error rather than a question quietly asked wrong."""
    d = tmp_path / "jev"
    d.mkdir()
    (d / "broken.yaml").write_text(
        "questions:\n"
        "  q:\n"
        "    type: noul\n"
        "    instructions: 'a stray { brace'\n"
        "    criteria:\n"
        '      "true": "it does"\n'
        '      "false": "it does not"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "JEV_DIR", d)
    with pytest.raises(SystemExit):
        cli.jev_questions("broken")


def test_a_missing_spec_is_a_hard_error(cli, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "JEV_DIR", tmp_path / "empty")
    with pytest.raises(SystemExit):
        cli.jev_questions("nope")


def test_the_shipped_gate_spec_asks_exactly_the_ids_the_hints_name(cli):
    questions = cli.jev_questions("scribe-gate")
    assert set(questions) == GATE_IDS
    for qid, q in questions.items():
        assert q["type"] == "noul", qid
        assert set(q["criteria"]) == {"true", "false"}, qid
    # The gate never decides whether the clerk runs (§3.5.3b) — no threshold to read.
    assert "skip_when_all_below" not in cli.jev_policy("scribe-gate")


# --------------------------------------------------------------------------
# the scribe gate (§3.5.3b) — end to end, both mock backends
# --------------------------------------------------------------------------

def _env(clerk_mock, jev_mock=None, clerk_capture=None, jev_capture=None, backend="mock"):
    env = {
        "HIPPO_CLERK_BACKEND": "mock",
        "HIPPO_MOCK_OUTPUT": str(clerk_mock),
        "HIPPO_JEV_BACKEND": backend,
    }
    if jev_mock is not None:
        env["HIPPO_JEV_MOCK_OUTPUT"] = str(jev_mock)
    if clerk_capture is not None:
        env["HIPPO_MOCK_CAPTURE"] = str(clerk_capture)
    if jev_capture is not None:
        env["HIPPO_JEV_MOCK_CAPTURE"] = str(jev_capture)
    return env


def _clerk_rows(project, name):
    return [e for e in read_ledger(project)
            if e.get("ev") == "clerk" and e.get("name") == name]


def _payload(capture):
    """The scribe's half of what the clerk was handed. HIPPO_MOCK_CAPTURE stores prompt++input,
    and the prompt itself talks about `# gate hints` — only the payload proves anything."""
    text = capture.read_text(encoding="utf-8")
    assert "\n# live directives\n" in text, text[:200]
    return "# live directives\n" + text.rsplit("\n# live directives\n", 1)[1]


def test_a_low_gate_still_runs_the_clerk_with_low_hints(
    tmp_project, run_hippo, fake_transcript, valid_mock_output, tmp_path
):
    """§3.5.3b: the gate never decides whether the clerk runs — measured, a skip rule would
    have dropped about one real event in seven to save one clerk call in thirty."""
    jev = _mock_file(tmp_path, {"default": {"noul": 0.01}})
    capture = tmp_path / "clerk.capture"
    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-low"],
        cwd=tmp_project,
        env=_env(valid_mock_output, jev, clerk_capture=capture),
    )
    assert proc.returncode == 0, proc.stderr

    gate = _clerk_rows(tmp_project, "jev-gate")
    assert len(gate) == 1 and gate[0]["ok"] is True
    assert gate[0]["src"] == "scribe"
    assert len(_clerk_rows(tmp_project, "turn-scribe")) == 1, "the clerk runs whatever the gate says"
    assert "- user_instruction: 0.01" in _payload(capture)
    assert json.loads(cursors_path(tmp_project).read_text(encoding="utf-8"))["sess-low"] > 0
    assert worklog_path(tmp_project).exists()


def test_gate_hints_ride_into_the_clerk_payload(
    tmp_project, run_hippo, fake_transcript, valid_mock_output, tmp_path
):
    jev = _mock_file(tmp_path, {
        "answers": {"user_instruction": {"noul": 0.87}},
        "default": {"noul": 0.42},
    })
    capture = tmp_path / "clerk-capture.txt"
    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-high"],
        cwd=tmp_project,
        env=_env(valid_mock_output, jev, clerk_capture=capture),
    )
    assert proc.returncode == 0, proc.stderr

    text = _payload(capture)
    assert "# gate hints" in text
    assert (text.index("# dispatches already recorded")
            < text.index("# gate hints")
            < text.index("# transcript digest")), "hints sit between the roster and the digest"
    assert "the digest is the only evidence" in text
    assert "- user_instruction: 0.87" in text
    for qid in GATE_IDS - {"user_instruction"}:
        assert f"- {qid}: 0.42" in text

    assert [e["ok"] for e in _clerk_rows(tmp_project, "jev-gate")] == [True]
    assert [e["ok"] for e in _clerk_rows(tmp_project, "turn-scribe")] == [True]


def test_a_failed_gate_is_recorded_and_the_clerk_runs_unchanged(
    tmp_project, run_hippo, fake_transcript, valid_mock_output, tmp_path
):
    capture = tmp_path / "clerk-capture.txt"
    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-broken"],
        cwd=tmp_project,
        env=_env(valid_mock_output, tmp_path / "absent.json", clerk_capture=capture),
    )
    assert proc.returncode == 0, proc.stderr

    assert [e["ok"] for e in _clerk_rows(tmp_project, "jev-gate")] == [False]
    assert [e["ok"] for e in _clerk_rows(tmp_project, "turn-scribe")] == [True]
    assert "# gate hints" not in _payload(capture)
    assert "jev-gate:" in proc.stderr, "a gate that failed says why"
    assert "test dummy work finished" in worklog_path(tmp_project).read_text(encoding="utf-8")


def test_an_oversize_digest_sends_nothing_and_the_clerk_runs(
    tmp_project, run_hippo, valid_mock_output, tmp_path
):
    # ~60 user lines, each capped at 2500 chars by digest_lite — past the 110k budget.
    transcript = tmp_project / "big.jsonl"
    with transcript.open("w", encoding="utf-8") as fh:
        for i in range(60):
            fh.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": f"line {i} " + "padding " * 400},
            }) + "\n")
    jev = _mock_file(tmp_path, {"default": {"noul": 0.9}})
    jev_capture = tmp_path / "jev-capture.json"
    clerk_capture = tmp_path / "clerk-capture.txt"
    proc = run_hippo(
        ["scribe", "--transcript", str(transcript), "--session", "sess-big"],
        cwd=tmp_project,
        env=_env(valid_mock_output, jev, clerk_capture=clerk_capture, jev_capture=jev_capture),
    )
    assert proc.returncode == 0, proc.stderr

    assert not jev_capture.exists(), "an oversize digest must not be sent"
    assert [e["ok"] for e in _clerk_rows(tmp_project, "jev-gate")] == [False]
    assert "state exceeds jev budget" in proc.stderr
    assert [e["ok"] for e in _clerk_rows(tmp_project, "turn-scribe")] == [True]
    assert "# gate hints" not in _payload(clerk_capture)


def test_with_the_backend_off_the_gate_leaves_no_trace(
    tmp_project, run_hippo, fake_transcript, valid_mock_output, tmp_path
):
    capture = tmp_path / "clerk-capture.txt"
    proc = run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-off"],
        cwd=tmp_project,
        env=_env(valid_mock_output, clerk_capture=capture, backend="off"),
    )
    assert proc.returncode == 0, proc.stderr

    assert _clerk_rows(tmp_project, "jev-gate") == [], "no key, no gate, no row"
    assert [e["ok"] for e in _clerk_rows(tmp_project, "turn-scribe")] == [True]
    assert "# gate hints" not in _payload(capture)
    dispatches = [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]
    assert [e["id"] for e in dispatches] == ["d100"]
    assert "test dummy work finished" in worklog_path(tmp_project).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# the metering row the gate writes (§3.6b fact sheet)
# --------------------------------------------------------------------------

def test_prior_facts_breaks_clerk_overhead_down_by_name(
    cli, tmp_project, run_hippo, fake_transcript, valid_mock_output, tmp_path
):
    jev = _mock_file(tmp_path, {"default": {"noul": 0.9}})
    run_hippo(
        ["scribe", "--transcript", str(fake_transcript), "--session", "sess-facts"],
        cwd=tmp_project,
        env=_env(valid_mock_output, jev),
    )
    sheet = cli.prior_facts(read_ledger(tmp_project), datetime.now(timezone.utc))
    line = sheet.split("## clerk overhead")[1].strip().splitlines()[0]
    assert "turn-scribe 1" in line and "jev-gate 1" in line, line
    assert line.startswith("2 runs ("), line
