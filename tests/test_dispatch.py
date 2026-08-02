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


# --------------------------------------------------------------------------
# depth and parent (§9.5, 1.9.0) — indexed, recorded, never enforced
# --------------------------------------------------------------------------

def _wrapper(tmp_project, tmp_path, args, env=None, body='#!/bin/sh\nexit 0\n'):
    full = {**os.environ, **(env or {}), "PATH": _stub_codex(tmp_path, body)}
    return subprocess.run(
        ["bash", str(SCRIPTS_DIR / "dispatch.sh"), *args],
        cwd=tmp_project, env=full, capture_output=True, text=True, timeout=30,
    )


def test_depth_defaults_to_zero_and_is_recorded(tmp_project, tmp_path):
    proc = _wrapper(tmp_project, tmp_path, ["--kind", "impl", "--scope", "leaf"])
    assert proc.returncode == 0, proc.stderr
    (event,) = read_ledger(tmp_project)
    assert event["depth"] == 0
    assert "parent" not in event


def test_depth_flag_is_recorded_and_planted(tmp_project, tmp_path):
    body = '#!/bin/sh\nprintf "DEPTH=%s DISPATCH=%s" "$HIPPO_DEPTH" "$HIPPO_DISPATCH"\n'
    proc = _wrapper(tmp_project, tmp_path,
                    ["--kind", "impl", "--scope", "orchestrator", "--depth", "1"], body=body)
    assert proc.returncode == 0, proc.stderr
    (event,) = read_ledger(tmp_project)
    assert event["depth"] == 1
    did = proc.stdout.splitlines()[0].removeprefix("dispatch:")
    assert f"DEPTH=1 DISPATCH={did}" in proc.stdout


def test_a_launch_inside_a_lane_records_its_parent(tmp_project, tmp_path):
    proc = _wrapper(tmp_project, tmp_path, ["--kind", "impl", "--scope", "child"],
                    env={"HIPPO_DISPATCH": "dparent1", "HIPPO_DEPTH": "1"})
    assert proc.returncode == 0, proc.stderr
    (event,) = read_ledger(tmp_project)
    assert event["parent"] == "dparent1"
    assert event["depth"] == 0          # children start at depth 0 unless told otherwise
    assert event["src"] == "wrapper"    # the wrapper's own record is never mis-stamped executor


def test_depth_after_double_dash_belongs_to_codex(tmp_project, tmp_path):
    body = '#!/bin/sh\nprintf "%s\\n" "$@"\n'
    proc = _wrapper(tmp_project, tmp_path,
                    ["--kind", "impl", "--scope", "x", "--", "--depth", "9"], body=body)
    assert proc.returncode == 0, proc.stderr
    (event,) = read_ledger(tmp_project)
    assert event["depth"] == 0
    assert "--depth" in proc.stdout.splitlines()


def test_junk_depth_dies_with_usage(tmp_project, tmp_path):
    proc = _wrapper(tmp_project, tmp_path,
                    ["--kind", "impl", "--scope", "x", "--depth", "much"])
    assert proc.returncode == 2
    assert "--depth must be an integer" in proc.stderr


# --------------------------------------------------------------------------
# usage collection (§9.6, 1.10.0) — the wrapper observes what the lane cost
# --------------------------------------------------------------------------

UUID = "01234567-abcd-7000-8000-0123456789ab"
BANNER_BODY = (
    '#!/bin/sh\n'
    'printf "OpenAI Codex stub\\n--------\\n"\n'
    f'printf "model: gpt-5.6-luna\\nsession id: {UUID}\\n--------\\n"\n'
    'printf "codex\\nOK\\ntokens used\\n18,169\\n"\n'
)


def _fake_rollout(home, uuid=UUID):
    d = home / ".codex" / "sessions" / "2026" / "08" / "02"
    d.mkdir(parents=True)
    row = {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 1000000, "cached_input_tokens": 400000,
                              "output_tokens": 90000, "reasoning_output_tokens": 10000,
                              "total_tokens": 1100000}}}}
    (d / f"rollout-2026-08-02T00-00-00-{uuid}.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8")


def test_wrapper_records_usage_from_the_rollout(tmp_project, tmp_path):
    home = tmp_path / "home"
    _fake_rollout(home)
    proc = _wrapper(tmp_project, tmp_path, ["--kind", "impl", "--scope", "cost"],
                    env={"HOME": str(home)}, body=BANNER_BODY)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout  # pass-through is intact
    (u,) = [e for e in read_ledger(tmp_project) if e.get("ev") == "usage"]
    d = [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"][0]
    assert u["ref"] == d["id"]
    assert (u["tokens"], u["tin"], u["tcached"], u["tout"]) == (1100000, 1000000, 400000, 100000)
    assert u["model"] == "gpt-5.6-luna"
    assert u["src"] == "wrapper"


def test_wrapper_falls_back_to_the_footer_total(tmp_project, tmp_path):
    home = tmp_path / "home"          # exists but holds no rollout
    home.mkdir()
    proc = _wrapper(tmp_project, tmp_path, ["--kind", "impl", "--scope", "cost"],
                    env={"HOME": str(home)}, body=BANNER_BODY)
    assert proc.returncode == 0, proc.stderr
    (u,) = [e for e in read_ledger(tmp_project) if e.get("ev") == "usage"]
    assert u["tokens"] == 18169
    assert "tin" not in u


def test_no_usage_report_leaves_a_gap_not_a_guess(tmp_project, tmp_path):
    proc = _wrapper(tmp_project, tmp_path, ["--kind", "impl", "--scope", "quiet"],
                    body='#!/bin/sh\nexit 0\n')
    assert proc.returncode == 0, proc.stderr
    assert not [e for e in read_ledger(tmp_project) if e.get("ev") == "usage"]


# --------------------------------------------------------------------------
# fan-out circuit breaker (§3.6, 1.11.0) — dollars, not lanes; main never gated
# --------------------------------------------------------------------------
# Reservation arithmetic under the shipped prices.yaml (1 Mtok in + 0.2 Mtok out):
# sol-class ≈ $11/child, luna-class ≈ $0.44/child; default budget $500, warn from $250.

def _seed_children(tmp_project, n, model="gpt-5.6-sol", parent="dorch", with_usage=None):
    import datetime as _dt
    t = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with (tmp_project / ".hippo" / "ledger.jsonl").open("a", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"t": t, "ev": "dispatch", "id": f"dc{i:04d}", "kind": "impl",
                                "exec": f"codex/{model}/high", "scope": f"child {i}",
                                "parent": parent, "src": "wrapper"}) + "\n")
            if with_usage:
                f.write(json.dumps({"t": t, "ev": "usage", "ref": f"dc{i:04d}",
                                    "model": model, "src": "wrapper", **with_usage}) + "\n")


def test_expensive_wave_trips_the_budget(tmp_project, tmp_path):
    _seed_children(tmp_project, 45)          # 45 × $11 reserved ≈ $495; +$11 breaks $500
    body = '#!/bin/sh\necho LAUNCHED > "$CAPTURE"\n'
    capture = tmp_path / "launched.txt"
    proc = _wrapper(tmp_project, tmp_path,
                    ["--kind", "impl", "--scope", "one more", "-m", "gpt-5.6-sol"],
                    env={"HIPPO_DISPATCH": "dorch", "CAPTURE": str(capture)}, body=body)
    assert proc.returncode == 2
    assert "$500 budget" in proc.stderr
    assert "no-go" in proc.stderr
    assert not capture.exists(), "codex must not have been launched"
    assert not [e for e in read_ledger(tmp_project) if e.get("scope") == "one more"]


def test_a_thousand_cheap_lanes_clear_the_same_budget(tmp_project, tmp_path):
    _seed_children(tmp_project, 1000, model="gpt-5.6-luna")   # ≈ $440 reserved
    proc = _wrapper(tmp_project, tmp_path,
                    ["--kind", "impl", "--scope", "and another", "-m", "gpt-5.6-luna"],
                    env={"HIPPO_DISPATCH": "dorch"})
    assert proc.returncode == 0, proc.stderr
    assert "$500 budget" in proc.stderr      # past half: the warning names the arithmetic
    assert any(e.get("scope") == "and another" for e in read_ledger(tmp_project))


def test_measured_usage_replaces_the_reservation(tmp_project, tmp_path):
    # The same 45 sol children, but finished and measured tiny (≈$0.80 each): the wave is
    # really ≈$36, so the next launch passes without a word.
    _seed_children(tmp_project, 45,
                   with_usage={"tokens": 110000, "tin": 100000, "tcached": 0, "tout": 10000})
    proc = _wrapper(tmp_project, tmp_path,
                    ["--kind", "impl", "--scope", "cheap in fact", "-m", "gpt-5.6-sol"],
                    env={"HIPPO_DISPATCH": "dorch"})
    assert proc.returncode == 0, proc.stderr
    assert "budget" not in proc.stderr
    assert any(e.get("scope") == "cheap in fact" for e in read_ledger(tmp_project))


def test_main_is_never_gated(tmp_project, tmp_path):
    _seed_children(tmp_project, 100)         # main's own wave, however expensive
    proc = _wrapper(tmp_project, tmp_path,
                    ["--kind", "impl", "--scope", "mains own", "-m", "gpt-5.6-sol"])
    assert proc.returncode == 0, proc.stderr
    assert any(e.get("scope") == "mains own" for e in read_ledger(tmp_project))


def test_budget_is_configurable_and_unknown_models_reserve_high(tmp_project, tmp_path):
    (tmp_project / ".hippo" / "config.yaml").write_text(
        "dispatch:\n  max_wave_usd: 5\n", encoding="utf-8")
    sol = _wrapper(tmp_project, tmp_path,
                   ["--kind", "impl", "--scope", "sol", "-m", "gpt-5.6-sol"],
                   env={"HIPPO_DISPATCH": "dorch"})
    assert sol.returncode == 2               # $11 reservation alone breaks a $5 budget
    assert "$5 budget" in sol.stderr

    luna = _wrapper(tmp_project, tmp_path,
                    ["--kind", "impl", "--scope", "luna", "-m", "gpt-5.6-luna"],
                    env={"HIPPO_DISPATCH": "dorch"})
    assert luna.returncode == 0, luna.stderr  # $0.44 fits

    mystery = _wrapper(tmp_project, tmp_path,
                       ["--kind", "impl", "--scope", "who", "-m", "mystery-9000"],
                       env={"HIPPO_DISPATCH": "dorch"})
    assert mystery.returncode == 2           # unknown reserves at the sheet's top tier
