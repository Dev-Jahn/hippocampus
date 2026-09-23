"""The PRIORS arithmetic, which the clerk no longer does (DESIGN §3.2, §3.7).

Measured on a consuming project: all seven cells the clerk produced disagreed with the ledger, in
both directions and against its own stated formula. These tests pin the formula down."""

import sys
from datetime import datetime, timedelta, timezone

from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def _t(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _d(did, kind="impl", ex="codex/gpt-6-sol/xhigh", minutes_ago=60, task=None):
    e = {"t": _t(minutes_ago), "ev": "dispatch", "id": did, "kind": kind, "exec": ex,
         "scope": "x"}
    if task:
        e["task"] = task
    return e


def _o(ref, result="accepted", minutes_ago=30, **kw):
    return {"t": _t(minutes_ago), "ev": "outcome", "ref": ref, "result": result, **kw}


def facts(rows):
    return hippo_cli.prior_facts(rows, NOW)


def test_no_go_and_lost_leave_the_denominator():
    """The error that invented a worst-performing cell: 2 accepted and 2 no-go was reported as
    5/8 (62.5%) and read as the weakest routing in the project. It is 2 for 2."""
    rows = [_d("d1"), _d("d2"), _d("d3"), _d("d4"), _d("d5"), _d("d6"),
            _o("d1"), _o("d2"), _o("d3"), _o("d4"),
            _o("d5", "no-go"), _o("d6", "lost")]
    out = facts(rows)
    assert "| 4 | 4/4 (100.0%)" in out
    assert "no-go 1" in out and "lost 1" in out  # still visible in the verdict breakdown


def test_a_refutation_is_never_rounded_away():
    """Two verify cells read 100% while each hid one refutation, and the verification budget
    advice was derived from exactly that zero."""
    rows = [_d(f"d{i}", kind="verify") for i in range(5)]
    rows += [_o(f"d{i}") for i in range(4)] + [_o("d4", "refuted")]
    out = facts(rows)
    assert "4/5 (80.0%)" in out
    assert "| codex/gpt-6-sol/xhigh | 5 | 1 | 20.0% |" in out


def test_a_thin_cell_gets_no_rate_but_is_still_named():
    """A percentage over n=1 reads as evidence and is not one. Dropping it silently is the other
    failure — the reader cannot tell a missing cell from an absent one."""
    rows = [_d("d1", kind="research"), _o("d1")]
    out = facts(rows)
    assert "100.0%" not in out
    assert "below the n=4 threshold" in out
    assert "research×codex/gpt-6-sol/xhigh (n=1)" in out


def test_only_the_first_outcome_decides_first_pass():
    """Re-judging happens; first-pass means first."""
    rows = [_d(f"d{i}") for i in range(4)]
    rows += [_o(f"d{i}") for i in range(3)]
    rows += [_o("d3", "refuted", minutes_ago=30), _o("d3", "accepted", minutes_ago=10)]
    out = facts(rows)
    assert "3/4 (75.0%)" in out


def test_unjoined_outcomes_are_counted_and_excluded():
    rows = [_d("d1"), _o("d1"), _o("ghost")]
    out = facts(rows)
    assert "## unjoined outcomes\n\n1" in out


def test_unjudged_claims_render_the_pending_line():
    """222 claims-only outcomes rendered a fully empty page with no hint that data was waiting
    on main (measured, algo200). Refs, not claim events: one lane may claim twice."""
    rows = [_d("d1"), _d("d2"),
            _o("d1", src="executor"), _o("d1", "revised", src="executor"),
            _o("d2", src="executor")]
    assert "claims pending verdict: 2" in facts(rows)


def test_a_judged_claim_leaves_the_pending_count():
    rows = [_d("d1"), _o("d1", src="executor"), _o("d1")]
    assert "claims pending verdict" not in facts(rows)


def test_no_claims_no_pending_line():
    rows = [_d("d1"), _o("d1")]
    assert "claims pending verdict" not in facts(rows)


def test_open_items_are_only_the_stale_unjudged():
    rows = [_d("dold", minutes_ago=60 * 30), _d("dfresh", minutes_ago=60),
            _d("djudged", minutes_ago=60 * 30), _o("djudged")]
    out = facts(rows)
    section = out.split("## open items")[1].split("##")[0]
    assert "dold (30h00m)" in section
    assert "dfresh" not in section and "djudged" not in section


def test_attribution_counts_only_non_accepted():
    rows = [_d(f"d{i}") for i in range(4)]
    rows += [_o("d0"), _o("d1", "refuted", attr="work"),
             _o("d2", "no-go", attr="brief"), _o("d3", "lost", attr="harness")]
    assert "work 1 / brief 1 / harness 1 / unattributed 0" in facts(rows)


def test_reviews_are_listed_until_fully_addressed():
    rows = [{"t": _t(60), "ev": "review", "id": "r1", "base": "abc123f",
             "source": "chatgpt-web", "findings": 4},
            {"t": _t(50), "ev": "review", "id": "r2", "base": "def4567",
             "source": "chatgpt-web", "findings": 1},
            {"t": _t(40), "ev": "review-status", "ref": "r2", "addressed": "full"}]
    out = facts(rows)
    assert "r1 base=abc123f" in out
    assert "r2" not in out.split("## reviews")[1]


def test_the_page_and_plan_mode_read_the_same_cells():
    """`prior_cells` is the join, factored out so plan mode (§3.6) asks the ledger the same
    question the page does. A second implementation of it would be a second answer."""
    rows = [_d(f"d{i}") for i in range(5)] + [_d("v1", kind="verify")]
    rows += [_o(f"d{i}") for i in range(4)] + [_o("d4", "refuted"), _o("v1", "no-go")]
    rows.append({"t": _t(30), "ev": "outcome", "ref": "d0", "result": "refuted",
                 "src": "executor"})  # a claim is not a verdict and enters no cell

    cells = hippo_cli.prior_cells(rows)
    cell = cells[("impl", "codex/gpt-6-sol/xhigh")]
    assert (cell["judged"], cell["accepted"]) == (5, 4)
    assert hippo_cli.prior_n(cell) == 5
    assert hippo_cli.prior_n(cells[("verify", "codex/gpt-6-sol/xhigh")]) == 0

    # And the page renders from exactly those numbers.
    assert "| impl | codex/gpt-6-sol/xhigh | 5 | 4/5 (80.0%)" in facts(rows)


def test_the_clerk_is_not_handed_the_raw_ledger(tmp_project, run_hippo, tmp_path):
    """The point of computing the numbers here is that there is nothing left to compute from —
    sending the events anyway would put them back within reach."""
    run_hippo(["log", "dispatch", "--id", "d1", "--kind", "impl",
               "--exec", "codex/gpt-6-sol/high", "--scope", "a distinctive scope string"],
              cwd=tmp_project)
    captured = tmp_path / "payload.txt"
    mock = tmp_path / "priors.md"
    mock.write_text("# PRIORS\n", encoding="utf-8")
    proc = run_hippo(
        ["prior", "distill"],
        cwd=tmp_project,
        env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock),
             "HIPPO_MOCK_CAPTURE": str(captured)},
    )
    assert proc.returncode == 0, proc.stderr
    text = captured.read_text(encoding="utf-8")
    assert "# computed facts" in text
    assert "a distinctive scope string" not in text
    assert '"ev": "dispatch"' not in text and '"ev":"dispatch"' not in text
