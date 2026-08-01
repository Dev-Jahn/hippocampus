"""Item (3): directive lifecycle — active -> withdraw -> gone from
`directive list --active`.

DESIGN.md §3.2: the last event for an id is its current state; derived views are never stored
(principles 4 and 5). 1.0 surface: `hippo directive add [flags]`,
`hippo directive withdraw <directive-id>`,
`hippo directive list [--active] [--json]`.
"""
import json


def test_directive_active_then_withdraw_disappears_from_active_list(
    tmp_project, run_hippo
):
    log_proc = run_hippo(
        [
            "directive",
            "add",
            "--id",
            "gpu-01",
            "--text",
            "Use GPUs 0 and 1 only",
            "--lifetime",
            "phase",
            "--state",
            "active",
        ],
        cwd=tmp_project,
    )
    assert log_proc.returncode == 0, log_proc.stderr

    active = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    assert active.returncode == 0, active.stderr
    ids = {d["id"] for d in json.loads(active.stdout)}
    assert "gpu-01" in ids

    withdraw = run_hippo(["directive", "withdraw", "gpu-01"], cwd=tmp_project)
    assert withdraw.returncode == 0, withdraw.stderr

    after = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    assert after.returncode == 0, after.stderr
    ids_after = {d["id"] for d in json.loads(after.stdout)}
    assert "gpu-01" not in ids_after


def test_directive_state_resolution_uses_last_event_not_history(
    tmp_project, run_hippo
):
    """Same id logged active -> withdrawed -> active again must resolve to
    active (last event wins; no derived/cached view)."""
    run_hippo(
        [
            "directive",
            "add",
            "--id",
            "dur-01",
            "--text",
            "Keep review replies in context; never save them to a file",
            "--lifetime",
            "durable",
            "--state",
            "active",
        ],
        cwd=tmp_project,
    )
    withdraw = run_hippo(["directive", "withdraw", "dur-01"], cwd=tmp_project)
    assert withdraw.returncode == 0, withdraw.stderr

    mid = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    mid_ids = {d["id"] for d in json.loads(mid.stdout)}
    assert "dur-01" not in mid_ids

    reactivate = run_hippo(
        [
            "directive",
            "add",
            "--id",
            "dur-01",
            "--text",
            "Keep review replies in context; never save them to a file",
            "--lifetime",
            "durable",
            "--state",
            "active",
        ],
        cwd=tmp_project,
    )
    assert reactivate.returncode == 0, reactivate.stderr

    final = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    final_ids = {d["id"] for d in json.loads(final.stdout)}
    assert "dur-01" in final_ids
