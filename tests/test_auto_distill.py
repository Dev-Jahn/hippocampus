"""Auto-distill at Stop (DESIGN §3.5.8): the scribe regenerates PRIORS.md when the page is stale
and enough new verdicts landed since the last distiller run — both measured from the ledger."""

import json
import os
import time
from datetime import datetime, timezone

import pytest

from conftest import ledger_path, read_ledger


def _seed(project, n_verdicts, src="cli", distilled_before=False):
    """n dispatches each with one outcome; optionally a successful distiller row ahead of them."""
    t = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    if distilled_before:
        rows.append({"t": t, "ev": "clerk", "name": "distiller", "ok": True, "src": "cli"})
    for i in range(n_verdicts):
        rows.append({"t": t, "ev": "dispatch", "id": f"d{i}", "kind": "impl",
                     "exec": "codex/gpt-6-sol/high", "scope": f"lane {i}", "src": "wrapper"})
        rows.append({"t": t, "ev": "outcome", "ref": f"d{i}", "result": "accepted", "src": src})
    with ledger_path(project).open("a", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)


def _scribe(run_hippo, project, transcript, mock):
    proc = run_hippo(
        ["scribe", "--transcript", str(transcript), "--session", "s-auto"],
        cwd=project,
        env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock)},
    )
    assert proc.returncode == 0, proc.stderr
    return [e for e in read_ledger(project) if e.get("ev") == "clerk" and e.get("name") == "distiller"]


def test_due_runs_the_distiller_and_writes_priors(
    tmp_project, run_hippo, fake_transcript, valid_mock_output
):
    _seed(tmp_project, 5)
    rows = _scribe(run_hippo, tmp_project, fake_transcript, valid_mock_output)
    assert [(r["ok"], r["src"]) for r in rows] == [(True, "scribe")]
    # The mock backend hands every clerk the same file, so the page is the scribe's JSON here.
    assert "test dummy work finished" in (tmp_project / ".hippo" / "PRIORS.md").read_text()


@pytest.mark.parametrize("case", ["fresh-priors", "too-few", "claims-only", "since-last-run"])
def test_not_due_runs_no_distiller(tmp_project, run_hippo, fake_transcript, valid_mock_output, case):
    priors = tmp_project / ".hippo" / "PRIORS.md"
    if case == "fresh-priors":
        _seed(tmp_project, 6)
        priors.write_text("# PRIORS\n", encoding="utf-8")
    elif case == "too-few":
        _seed(tmp_project, 4)
    elif case == "claims-only":
        _seed(tmp_project, 6, src="executor")
    else:
        _seed(tmp_project, 2)
        _seed(tmp_project, 4, distilled_before=True)
        stale = time.time() - 8 * 86400
        priors.write_text("# PRIORS\n", encoding="utf-8")
        os.utime(priors, (stale, stale))
    before = [e for e in read_ledger(tmp_project) if e.get("name") == "distiller"]
    assert _scribe(run_hippo, tmp_project, fake_transcript, valid_mock_output) == before
