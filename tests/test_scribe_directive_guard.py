"""The scribe's directive writes against main's (DESIGN §3.5.5–6b, §6).

Measured on a consuming project (iislab-slurm, 2026-09-30): of 28 active scribe directive
writes, 20 reused an id main had just written with `hippo directive add`, most within the same
minute, and most shrank it — one replaced a 224-char allocation rule with a 101-char delta, and
the rule vanished from every session. The clerk had seen that directive only as a 100-char
preview. So: the clerk sees whole texts, an explicit write beats an inference, and a clerk that
repeats itself costs one failure file, not seventeen."""

import json
import sys
import types

from conftest import REPO_ROOT, ledger_path, read_ledger

sys.path.insert(0, str(REPO_ROOT / "cli"))
import hippo_cli  # noqa: E402

WINDOW_START = "2026-09-30T08:13:37.521Z"


def _transcript(project, stamps=True):
    """A Claude Code window whose first line is stamped WINDOW_START (or nothing at all)."""
    lines = [
        {"type": "user", "timestamp": WINDOW_START,
         "message": {"role": "user", "content": "spare jobs get a 120s GraceTime"}},
        {"type": "assistant", "timestamp": "2026-09-30T08:13:40.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "ls"}}]}},
    ]
    if not stamps:
        for ln in lines:
            ln.pop("timestamp")
    p = project / "transcript.jsonl"
    p.write_text("".join(json.dumps(ln) + "\n" for ln in lines), encoding="utf-8")
    return p


def _codex_transcript(project):
    lines = [
        {"timestamp": WINDOW_START, "type": "session_meta", "payload": {"id": "x"}},
        {"timestamp": "2026-09-30T08:13:40.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "spare GraceTime 120s"}},
    ]
    p = project / "rollout.jsonl"
    p.write_text("".join(json.dumps(ln) + "\n" for ln in lines), encoding="utf-8")
    return p


def _seed(project, *rows):
    with ledger_path(project).open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _main_write(t, text="the whole allocation rule, every clause of it", did="alloc"):
    return {"t": t, "ev": "directive", "id": did, "state": "active", "text": text, "src": "cli"}


def _clerk(tmp_path, events, name="out"):
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"worklog": "", "events": events}, ensure_ascii=False),
                 encoding="utf-8")
    return p


def _scribe(run_hippo, project, transcript, mock):
    proc = run_hippo(["scribe", "--transcript", str(transcript), "--session", "s1"],
                     cwd=project,
                     env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock)})
    assert proc.returncode == 0, proc.stderr
    return proc


def _live(project):
    return hippo_cli.directives(project / ".hippo")


def _dumps(project):
    return sorted((project / ".hippo" / "failures").glob("*-scribe-*"))


DELTA = {"ev": "directive", "id": "alloc", "state": "active", "text": "spare GraceTime 120s"}


# --------------------------------------------------------------------------
# A. the clerk sees whole directives
# --------------------------------------------------------------------------

def test_the_roster_carries_each_live_directive_whole(tmp_project):
    """A 100-char preview ending in "…" is all the clerk had to merge an update into — it could
    not have written the whole revised text even when told to."""
    long = "first clause; " * 20 + "the last clause the preview used to cut"
    _seed(tmp_project, _main_write("2026-09-29T13:55:03Z", long.replace("; ", ";\n", 3)))
    roster = hippo_cli.directive_roster(tmp_project / ".hippo")
    assert roster == f"- alloc: {' '.join(long.split())}"
    assert "…" not in roster


# --------------------------------------------------------------------------
# C. an explicit write beats a scribe inference
# --------------------------------------------------------------------------

def test_a_main_write_inside_the_window_wins(tmp_project, run_hippo, tmp_path):
    """Same second as the window's first line counts as inside: the ledger stamps whole
    seconds. The scribe's proposal survives in the dump, naming the id and the winning time."""
    _seed(tmp_project, _main_write("2026-09-30T08:13:37Z"))
    _scribe(run_hippo, tmp_project, _transcript(tmp_project), _clerk(tmp_path, [DELTA]))

    assert _live(tmp_project)["alloc"]["text"] == "the whole allocation rule, every clause of it"
    assert not [e for e in read_ledger(tmp_project)
                if e.get("ev") == "directive" and e.get("src") == "scribe"]
    [dump] = _dumps(tmp_project)
    text = dump.read_text()
    assert "alloc" in text and "2026-09-30T08:13:37Z" in text and "spare GraceTime 120s" in text


def test_a_withdrawal_is_held_to_the_same_rule(tmp_project, run_hippo, tmp_path):
    _seed(tmp_project, _main_write("2026-09-30T08:14:00Z"))
    _scribe(run_hippo, tmp_project, _transcript(tmp_project),
            _clerk(tmp_path, [{"ev": "directive", "id": "alloc", "state": "withdrawn"}]))
    assert _live(tmp_project)["alloc"]["state"] == "active"


def test_a_main_write_made_while_the_clerk_ran_wins(tmp_project, tmp_path, monkeypatch):
    """The scribe runs detached; main may write the same directive while the clerk is still
    thinking. The check reads the ledger as it is when the event is written."""
    transcript = _transcript(tmp_project)
    hp = tmp_project / ".hippo"
    out = json.dumps({"worklog": "", "events": [DELTA]})

    def clerk_meanwhile(*a, **k):
        _seed(tmp_project, {**_main_write(hippo_cli.now_iso()), "text": "main's fresh text"})
        return out, "", 0, 10, 100

    monkeypatch.setattr(hippo_cli, "run_clerk", clerk_meanwhile)
    hippo_cli.cmd_scribe(types.SimpleNamespace(hp=hp, transcript=str(transcript), session="s1"))
    assert _live(tmp_project)["alloc"]["text"] == "main's fresh text"
    assert len(_dumps(tmp_project)) == 1


def test_a_main_write_before_the_window_does_not_block_an_update(tmp_project, run_hippo,
                                                                tmp_path):
    """The 08:13 case: main had written the rule the day before, the user changed one clause in
    this window, and main recorded nothing. The scribe's whole merged text is the update."""
    _seed(tmp_project, _main_write("2026-09-29T13:55:03Z"))
    merged = {**DELTA, "text": "the whole allocation rule, every clause of it; spare GraceTime 120s"}
    _scribe(run_hippo, tmp_project, _transcript(tmp_project), _clerk(tmp_path, [merged]))
    assert _live(tmp_project)["alloc"]["text"] == merged["text"]
    assert not _dumps(tmp_project)


def test_a_codex_rollout_window_is_read_the_same(tmp_project, run_hippo, tmp_path):
    _seed(tmp_project, _main_write("2026-09-30T08:13:37Z"))
    _scribe(run_hippo, tmp_project, _codex_transcript(tmp_project), _clerk(tmp_path, [DELTA]))
    assert _live(tmp_project)["alloc"]["text"] == "the whole allocation rule, every clause of it"
    assert len(_dumps(tmp_project)) == 1


def test_with_no_readable_window_start_any_main_write_wins(tmp_project, run_hippo, tmp_path):
    """No guessing: with no timestamp in the window, a directive main ever wrote is main's, and
    the reason says why. An id main never wrote still lands."""
    _seed(tmp_project, _main_write("2026-01-01T00:00:00Z"))
    fresh = {"ev": "directive", "id": "new-rule", "state": "active", "text": "a new rule"}
    _scribe(run_hippo, tmp_project, _transcript(tmp_project, stamps=False),
            _clerk(tmp_path, [DELTA, fresh]))
    live = _live(tmp_project)
    assert live["alloc"]["text"] == "the whole allocation rule, every clause of it"
    assert live["new-rule"]["text"] == "a new rule"
    [dump] = _dumps(tmp_project)
    assert "no timestamp" in dump.read_text()


def test_a_lane_write_does_not_count_as_main(tmp_project, run_hippo, tmp_path):
    """src=executor never folds (§3.2), so it cannot be the write that wins."""
    _seed(tmp_project, _main_write("2026-09-29T13:55:03Z"),
          {**_main_write("2026-09-30T08:14:00Z", "a lane's idea"), "src": "executor"})
    merged = {**DELTA, "text": "the whole rule; spare GraceTime 120s"}
    _scribe(run_hippo, tmp_project, _transcript(tmp_project), _clerk(tmp_path, [merged]))
    assert _live(tmp_project)["alloc"]["text"] == merged["text"]


# --------------------------------------------------------------------------
# D. duplicates collapse; one run's rejections share one file
# --------------------------------------------------------------------------

def test_identical_events_in_one_output_are_written_once(tmp_project, run_hippo, tmp_path):
    """Measured: asked to record 7 runs, a luna-low clerk emitted the list ~5 times (33 events)."""
    fresh = {"ev": "directive", "id": "new-rule", "state": "active", "text": "a new rule"}
    reordered = {"text": "a new rule", "state": "active", "id": "new-rule", "ev": "directive"}
    _scribe(run_hippo, tmp_project, _transcript(tmp_project),
            _clerk(tmp_path, [fresh, reordered, fresh]))
    assert len([e for e in read_ledger(tmp_project) if e.get("ev") == "directive"]) == 1


def test_one_run_rejections_land_in_one_file(tmp_project, run_hippo, tmp_path):
    """17 rejections once wrote 17 files, and a later checkup read the pile as lost records."""
    bad = [{"ev": "ag-wf_1", "kind": "audit"}, {"ev": "ag-wf_2", "kind": "audit"},
           {"ev": "ag-wf_1", "kind": "audit"},
           {"ev": "outcome", "ref": "nope", "result": "accepted"}]
    _scribe(run_hippo, tmp_project, _transcript(tmp_project), _clerk(tmp_path, bad))
    [dump] = _dumps(tmp_project)
    text = dump.read_text()
    assert text.splitlines()[0].startswith("3 of 3 events rejected")
    assert "ag-wf_1" in text and "ag-wf_2" in text and "nope" in text

