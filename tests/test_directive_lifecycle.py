"""Item (3): directive lifecycle — active -> retract -> gone from
`directive list --active`.

DESIGN.md §3.2: "같은 id의 마지막 이벤트가 현재 상태다(파생 뷰는 저장하지
않는다 — 원칙 4·5)." 1.0 surface: `hippo directive add [flags]`,
`hippo directive retract <directive-id>`,
`hippo directive list [--active] [--json]`.
"""
import json


def test_directive_active_then_retract_disappears_from_active_list(
    tmp_project, run_hippo
):
    log_proc = run_hippo(
        [
            "directive",
            "add",
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
    assert log_proc.returncode == 0, log_proc.stderr

    active = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    assert active.returncode == 0, active.stderr
    ids = {d["id"] for d in json.loads(active.stdout)}
    assert "gpu-01" in ids

    retract = run_hippo(["directive", "retract", "gpu-01"], cwd=tmp_project)
    assert retract.returncode == 0, retract.stderr

    after = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    assert after.returncode == 0, after.stderr
    ids_after = {d["id"] for d in json.loads(after.stdout)}
    assert "gpu-01" not in ids_after


def test_directive_state_resolution_uses_last_event_not_history(
    tmp_project, run_hippo
):
    """Same id logged active -> retracted -> active again must resolve to
    active (last event wins; no derived/cached view)."""
    run_hippo(
        [
            "directive",
            "add",
            "--id",
            "dur-01",
            "--text",
            "리뷰 회신은 컨텍스트 유지, 파일 저장 금지",
            "--scope",
            "durable",
            "--state",
            "active",
        ],
        cwd=tmp_project,
    )
    retract = run_hippo(["directive", "retract", "dur-01"], cwd=tmp_project)
    assert retract.returncode == 0, retract.stderr

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
            "리뷰 회신은 컨텍스트 유지, 파일 저장 금지",
            "--scope",
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
