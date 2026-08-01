"""Shared fixtures for the hippo 1.0 contract test suite.

Everything here treats DESIGN.md as the sole source of truth for interface
shape. Where DESIGN.md leaves a detail unspecified (e.g. the exact typed-flag
names for `hippo log <ev>`, or task JSON key names), the fixtures/tests pick
the simplest interpretation consistent with the ledger schema (§3.2) and note
it in the test that relies on it.

Implementation (bin/hippo, hooks/*, scripts/*) may not exist yet while this
suite is authored in parallel — tests are expected to fail at *run* time in
that case, not at *collection* time. Nothing here imports application code at
module scope.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HIPPO_BIN = REPO_ROOT / "bin" / "hippo"
HOOKS_DIR = REPO_ROOT / "hooks"
SCRIPTS_DIR = REPO_ROOT / "scripts"


# --------------------------------------------------------------------------
# Path fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


# --------------------------------------------------------------------------
# Process runner
# --------------------------------------------------------------------------

@pytest.fixture
def run_hippo():
    """Return a callable that invokes bin/hippo as a subprocess.

    run(args, cwd, env=None, input_text=None, timeout=30) -> CompletedProcess
    """

    def _run(args, cwd, env=None, input_text=None, timeout=30):
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        return subprocess.run(
            [str(HIPPO_BIN), *args],
            cwd=str(cwd),
            env=full_env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    return _run


# --------------------------------------------------------------------------
# Project fixtures (tmpdir isolation — every test gets its own throwaway dir)
# --------------------------------------------------------------------------

@pytest.fixture
def uninitialized_dir(tmp_path_factory) -> Path:
    """A fresh directory with no .hippo/ — the global silent-no-op case.

    Deliberately NOT derived from `tmp_path`: `tmp_project` is, so sharing it
    would make any test requesting both fixtures silently compare a directory
    against itself.
    """
    return tmp_path_factory.mktemp("uninitialized")


@pytest.fixture
def tmp_project(tmp_path, run_hippo) -> Path:
    """A directory initialized via `hippo init`."""
    proc = run_hippo(["init"], cwd=tmp_path)
    assert proc.returncode == 0, (
        f"hippo init failed (stdout={proc.stdout!r} stderr={proc.stderr!r})"
    )
    assert (tmp_path / ".hippo").is_dir(), "hippo init must create .hippo/"
    return tmp_path


# --------------------------------------------------------------------------
# Ledger / worklog / failures helpers (plain functions, not fixtures — used
# directly by test modules via `from conftest import ...`)
# --------------------------------------------------------------------------

def ledger_path(project_dir) -> Path:
    return Path(project_dir) / ".hippo" / "ledger.jsonl"


def read_ledger(project_dir) -> list[dict]:
    path = ledger_path(project_dir)
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def worklog_path(project_dir) -> Path:
    return Path(project_dir) / ".hippo" / "worklog.md"


def failures_dir_path(project_dir) -> Path:
    return Path(project_dir) / ".hippo" / "failures"


def cursors_path(project_dir) -> Path:
    return Path(project_dir) / ".hippo" / "cursors.json"


# --------------------------------------------------------------------------
# Fake transcript (Claude Code session JSONL) — assistant tool_use lines +
# user lines, per assignment item (5): a fake transcript jsonl with a few assistant
# tool_use lines and a few user lines.
# --------------------------------------------------------------------------

def _write_transcript(directory) -> Path:
    lines = [
        {
            "type": "user",
            "message": {"role": "user", "content": "Use GPUs 0 and 1 only"},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Understood, starting the dispatch."},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Bash",
                        "input": {
                            "command": "codex exec -m gpt-5.6-sol --effort high 'kernel impl'"
                        },
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "ok, dispatch id d100 launched",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
            },
        },
    ]
    path = Path(directory) / "transcript.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return path


@pytest.fixture
def fake_transcript(tmp_project) -> Path:
    """Fake transcript living inside the initialized project (for scribe tests)."""
    return _write_transcript(tmp_project)


@pytest.fixture
def fake_transcript_path(tmp_path) -> Path:
    """Fake transcript standalone (no .hippo/ needed — for digest_lite tests)."""
    return _write_transcript(tmp_path)


# --------------------------------------------------------------------------
# Mock clerk output files (HIPPO_MOCK_OUTPUT target)
# --------------------------------------------------------------------------

@pytest.fixture
def valid_mock_output(tmp_path) -> Path:
    payload = {
        "worklog": "test dummy work finished",
        "events": [
            {
                "ev": "dispatch",
                "id": "d100",
                "kind": "docs",
                # A fork arm: the kind of launch only the scribe can see, and so the kind it is
                # still allowed to record. A codex dispatch here would be rejected by §3.5.6b.
                "exec": "fork/fable/inherit",
                "scope": "scribe pipeline test dispatch",
            }
        ],
    }
    path = tmp_path / "mock_output_valid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def malformed_mock_output(tmp_path) -> Path:
    """Syntactically broken JSON — not even parseable."""
    path = tmp_path / "mock_output_malformed.json"
    path.write_text("{worklog: not even json ####", encoding="utf-8")
    return path


@pytest.fixture
def schema_invalid_mock_output(tmp_path) -> Path:
    """Valid JSON, but the event fails ledger schema validation (unknown ev)."""
    payload = {
        "worklog": "schema violation test",
        "events": [{"ev": "not-a-real-event", "id": "z1"}],
    }
    path = tmp_path / "mock_output_schema_invalid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path
