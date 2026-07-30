"""Item (2): the 5 ledger event kinds + fail-closed validation.

DESIGN.md §3.2 (schema, verbatim contract) and §3.3 (`waystone log <ev>
[typed flags...]` / `waystone log raw '<json>'`).

Typed-flag names are not spelled out character-for-character in DESIGN.md
beyond the schema's field names; these tests assume `--<field> <value>`
mirrors the schema field name 1:1 (the simplest reading, and the only one
consistent with `waystone log raw` accepting the same field names literally).

Fail-closed contract (§3.2): "waystone log는 ev별 필수 필드를 fail-closed로
검사한다. 미지의 ev는 거부." — nonzero exit, and the ledger file must be
byte-identical before/after a rejected call (no partial/corrupt append).
"""
import json
import re

from conftest import ledger_path, read_ledger

ISO_T_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def _seed_one_valid_line(tmp_project, run_waystone):
    """Ensure ledger.jsonl exists and has at least one line, so a
    byte-for-byte before/after comparison around a failing call is possible
    regardless of whether ledger.jsonl is created eagerly at init."""
    proc = run_waystone(
        [
            "log",
            "dispatch",
            "--id",
            "seed",
            "--kind",
            "docs",
            "--exec",
            "codex/gpt-5.6-luna/low",
            "--scope",
            "seed line",
        ],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------
# Valid appends for each of the 5 event kinds
# --------------------------------------------------------------------------

def test_log_dispatch_valid_appends(tmp_project, run_waystone):
    before = read_ledger(tmp_project)
    proc = run_waystone(
        [
            "log",
            "dispatch",
            "--id",
            "d041",
            "--kind",
            "kernel-impl",
            "--exec",
            "codex/gpt-5.6-sol/high",
            "--scope",
            "pass2 SS-UMMA tensorize",
        ],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr

    after = read_ledger(tmp_project)
    assert len(after) == len(before) + 1
    entry = after[-1]
    assert entry["ev"] == "dispatch"
    assert entry["id"] == "d041"
    assert entry["kind"] == "kernel-impl"
    assert entry["exec"] == "codex/gpt-5.6-sol/high"
    assert entry["scope"] == "pass2 SS-UMMA tensorize"
    assert ISO_T_PREFIX.match(entry["t"]), entry["t"]


def test_log_outcome_valid_appends(tmp_project, run_waystone):
    _seed_dispatch = run_waystone(
        [
            "log",
            "dispatch",
            "--id",
            "d042",
            "--kind",
            "verify",
            "--exec",
            "codex/gpt-5.6-sol/high",
            "--scope",
            "cross-check",
        ],
        cwd=tmp_project,
    )
    assert _seed_dispatch.returncode == 0, _seed_dispatch.stderr

    before = read_ledger(tmp_project)
    proc = run_waystone(
        [
            "log",
            "outcome",
            "--ref",
            "d042",
            "--result",
            "refuted",
            "--attr",
            "work",
            "--rework",
            "2",
            "--note",
            "oracle 순환참조",
        ],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr

    after = read_ledger(tmp_project)
    assert len(after) == len(before) + 1
    entry = after[-1]
    assert entry["ev"] == "outcome"
    assert entry["ref"] == "d042"
    assert entry["result"] == "refuted"


def test_log_review_valid_appends(tmp_project, run_waystone):
    before = read_ledger(tmp_project)
    proc = run_waystone(
        [
            "log",
            "review",
            "--id",
            "r007",
            "--base",
            "abc123f",
            "--source",
            "chatgpt-web",
            "--findings",
            "4",
        ],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr

    after = read_ledger(tmp_project)
    assert len(after) == len(before) + 1
    entry = after[-1]
    assert entry["ev"] == "review"
    assert entry["id"] == "r007"
    assert entry["base"] == "abc123f"


def test_log_review_status_valid_appends(tmp_project, run_waystone):
    seed = run_waystone(
        [
            "log",
            "review",
            "--id",
            "r008",
            "--base",
            "def4567",
            "--source",
            "chatgpt-web",
            "--findings",
            "2",
        ],
        cwd=tmp_project,
    )
    assert seed.returncode == 0, seed.stderr

    before = read_ledger(tmp_project)
    proc = run_waystone(
        [
            "log",
            "review-status",
            "--ref",
            "r008",
            "--addressed",
            "partial",
            "--at",
            "def4567",
        ],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr

    after = read_ledger(tmp_project)
    assert len(after) == len(before) + 1
    entry = after[-1]
    assert entry["ev"] == "review-status"
    assert entry["ref"] == "r008"
    assert entry["addressed"] == "partial"


def test_log_directive_valid_appends(tmp_project, run_waystone):
    before = read_ledger(tmp_project)
    proc = run_waystone(
        [
            "log",
            "directive",
            "--id",
            "gpu-01",
            "--text",
            "GPU 0,1만 사용",
            "--scope",
            "phase",
            "--state",
            "active",
        ],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr

    after = read_ledger(tmp_project)
    assert len(after) == len(before) + 1
    entry = after[-1]
    assert entry["ev"] == "directive"
    assert entry["id"] == "gpu-01"
    assert entry["scope"] == "phase"
    assert entry["state"] == "active"


# --------------------------------------------------------------------------
# Fail-closed: missing required fields
# --------------------------------------------------------------------------

def test_log_dispatch_missing_id_fails_closed_and_ledger_unmodified(
    tmp_project, run_waystone
):
    _seed_one_valid_line(tmp_project, run_waystone)
    lp = ledger_path(tmp_project)
    before_bytes = lp.read_bytes()

    proc = run_waystone(
        [
            "log",
            "dispatch",
            "--kind",
            "docs",
            "--exec",
            "codex/gpt-5.6-luna/low",
            "--scope",
            "missing id field",
        ],
        cwd=tmp_project,
    )
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""
    assert lp.read_bytes() == before_bytes


def test_log_outcome_missing_ref_fails_closed_and_ledger_unmodified(
    tmp_project, run_waystone
):
    _seed_one_valid_line(tmp_project, run_waystone)
    lp = ledger_path(tmp_project)
    before_bytes = lp.read_bytes()

    proc = run_waystone(
        ["log", "outcome", "--result", "accepted"],
        cwd=tmp_project,
    )
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""
    assert lp.read_bytes() == before_bytes


def test_log_review_missing_base_fails_closed_and_ledger_unmodified(
    tmp_project, run_waystone
):
    _seed_one_valid_line(tmp_project, run_waystone)
    lp = ledger_path(tmp_project)
    before_bytes = lp.read_bytes()

    proc = run_waystone(
        ["log", "review", "--id", "r-bad", "--source", "chatgpt-web", "--findings", "1"],
        cwd=tmp_project,
    )
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""
    assert lp.read_bytes() == before_bytes


# --------------------------------------------------------------------------
# Fail-closed: invalid enum values
# --------------------------------------------------------------------------

def test_log_outcome_invalid_result_enum_fails_closed(tmp_project, run_waystone):
    seed = run_waystone(
        [
            "log",
            "dispatch",
            "--id",
            "d-enum",
            "--kind",
            "docs",
            "--exec",
            "codex/gpt-5.6-luna/low",
            "--scope",
            "enum test",
        ],
        cwd=tmp_project,
    )
    assert seed.returncode == 0, seed.stderr

    lp = ledger_path(tmp_project)
    before_bytes = lp.read_bytes()

    proc = run_waystone(
        ["log", "outcome", "--ref", "d-enum", "--result", "maybe-ish"],
        cwd=tmp_project,
    )
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""
    assert lp.read_bytes() == before_bytes


def test_log_directive_invalid_scope_enum_fails_closed(tmp_project, run_waystone):
    _seed_one_valid_line(tmp_project, run_waystone)
    lp = ledger_path(tmp_project)
    before_bytes = lp.read_bytes()

    proc = run_waystone(
        [
            "log",
            "directive",
            "--id",
            "bad-scope",
            "--text",
            "x",
            "--scope",
            "eternal",
            "--state",
            "active",
        ],
        cwd=tmp_project,
    )
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""
    assert lp.read_bytes() == before_bytes


def test_log_directive_invalid_state_enum_fails_closed(tmp_project, run_waystone):
    _seed_one_valid_line(tmp_project, run_waystone)
    lp = ledger_path(tmp_project)
    before_bytes = lp.read_bytes()

    proc = run_waystone(
        [
            "log",
            "directive",
            "--id",
            "bad-state",
            "--text",
            "x",
            "--scope",
            "phase",
            "--state",
            "sleeping",
        ],
        cwd=tmp_project,
    )
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""
    assert lp.read_bytes() == before_bytes


# --------------------------------------------------------------------------
# Fail-closed: unknown event type
# --------------------------------------------------------------------------

def test_log_unknown_event_type_rejected(tmp_project, run_waystone):
    _seed_one_valid_line(tmp_project, run_waystone)
    lp = ledger_path(tmp_project)
    before_bytes = lp.read_bytes()

    proc = run_waystone(
        ["log", "made-up-event", "--id", "x"],
        cwd=tmp_project,
    )
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""
    assert lp.read_bytes() == before_bytes


# --------------------------------------------------------------------------
# `waystone log raw '<json>'`
# --------------------------------------------------------------------------

def test_log_raw_valid_json_appends(tmp_project, run_waystone):
    before = read_ledger(tmp_project)
    payload = json.dumps(
        {
            "ev": "directive",
            "id": "raw-01",
            "text": "raw 경로 확인",
            "scope": "phase",
            "state": "active",
        },
        ensure_ascii=False,
    )
    proc = run_waystone(["log", "raw", payload], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr

    after = read_ledger(tmp_project)
    assert len(after) == len(before) + 1
    assert after[-1]["ev"] == "directive"
    assert after[-1]["id"] == "raw-01"


def test_log_raw_malformed_json_text_rejected(tmp_project, run_waystone):
    _seed_one_valid_line(tmp_project, run_waystone)
    lp = ledger_path(tmp_project)
    before_bytes = lp.read_bytes()

    proc = run_waystone(["log", "raw", "{not valid json ####"], cwd=tmp_project)
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""
    assert lp.read_bytes() == before_bytes


def test_log_raw_unknown_ev_rejected(tmp_project, run_waystone):
    _seed_one_valid_line(tmp_project, run_waystone)
    lp = ledger_path(tmp_project)
    before_bytes = lp.read_bytes()

    payload = json.dumps({"ev": "not-a-real-event", "id": "z1"})
    proc = run_waystone(["log", "raw", payload], cwd=tmp_project)
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""
    assert lp.read_bytes() == before_bytes


def test_log_raw_missing_required_field_rejected(tmp_project, run_waystone):
    _seed_one_valid_line(tmp_project, run_waystone)
    lp = ledger_path(tmp_project)
    before_bytes = lp.read_bytes()

    # directive without id — required per schema
    payload = json.dumps({"ev": "directive", "state": "active"})
    proc = run_waystone(["log", "raw", payload], cwd=tmp_project)
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""
    assert lp.read_bytes() == before_bytes
