"""`hippo dispatch --batch` (DESIGN §3.6, 1.12.0) — manifest fan-out in the wrapper.

Contract under test: fail-closed manifest validation, the journal (launch/skip/
exit/stopped lines) and resume, the concurrency cap, per-child ledger recording
(dispatch + usage — never ev:outcome), the fan-out breaker consulted before each
launch, both adapters (codex banner/footer on stderr, claude single-JSON stdout),
the per-child timeout, and {var} substitution. Where the spec fixes only what a
message must name (validation, breaker), tests assert the named tokens, not the
phrasing.
"""
import json
import os
import re
import textwrap
from datetime import datetime, timezone

from conftest import read_ledger


# --------------------------------------------------------------------------
# Stubs on PATH (test_dispatch._stub_codex pattern) and manifest helpers
# --------------------------------------------------------------------------

def _stub(tmp_path, name, body):
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    exe = stub / name
    exe.write_text(body, encoding="utf-8")
    exe.chmod(0o755)
    return f"{stub}:{os.environ['PATH']}"


STUB_OK = "#!/bin/sh\nexit 0\n"

# NUL-separated argv capture: prompts span lines, newline separation would shear them.
STUB_ARGV = (
    "#!/bin/sh\n"
    'for a in "$@"; do printf \'%s\\0\' "$a" >> "$ARGV_FILE"; done\n'
)

# codex 0.144.6 (measured): banner and "tokens used" footer ride stderr. The footer
# total is planted in each prompt (tokens=NNNN) so every lane's usage row is
# distinguishable without a fake rollout.
FOOTER_STUB = """\
#!/bin/sh
for a in "$@"; do last="$a"; done
n=$(printf '%s' "$last" | sed -n 's/.*tokens=\\([0-9][0-9]*\\).*/\\1/p')
printf 'model: gpt-5.6-luna\\n' >&2
printf 'agent output\\n'
printf 'tokens used\\n%s\\n' "$n" >&2
"""


def _manifest(project, name, text):
    path = project / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _journal_path(manifest):
    return manifest.parent / f"{manifest.stem}.journal.jsonl"


def _outdir(manifest):
    return manifest.parent / f"{manifest.stem}.out"


def _journal(manifest):
    path = _journal_path(manifest)
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def _batch(run_hippo, project, manifest, *flags, env, timeout=30):
    # HIPPO_DISPATCH is cleared unless the test sets it: the suite itself may run
    # inside a lane, and an inherited parent would arm the breaker mid-test.
    full = {"HIPPO_DISPATCH": "", **env}
    return run_hippo(["dispatch", "--batch", str(manifest), *flags],
                     cwd=project, env=full, timeout=timeout)


def _summary(proc):
    # The batch contract puts exactly ONE json line on stdout — anything more fails here.
    return json.loads(proc.stdout)


def _seed_children(tmp_project, n, model="gpt-5.6-sol", parent="dorch"):
    t = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with (tmp_project / ".hippo" / "ledger.jsonl").open("a", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"t": t, "ev": "dispatch", "id": f"dc{i:04d}", "kind": "impl",
                                "exec": f"codex/{model}/high", "scope": f"child {i}",
                                "parent": parent, "src": "wrapper"}) + "\n")


# --------------------------------------------------------------------------
# 1. validation — fail-closed and total: every problem in one death
# --------------------------------------------------------------------------

def test_validation_collects_every_problem_before_dying(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "bad1.yaml", """\
        concurrency: many
        bogus: true
        defaults:
          executor: gemini
        entries:
          - id: dup-id
            scope: "one"
            prompt: hi
          - id: dup-id
            scope: "two"
            prompt: hi
          - prompt: orphan
        """)
    capture = tmp_path / "launched.txt"
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", f'#!/bin/sh\ntouch "{capture}"\n')})
    assert proc.returncode == 2
    for token in ("bogus", "gemini", "dup-id", "concurrency", "kind", "model", "scope"):
        assert token in proc.stderr, f"validation must name {token!r}"
    assert not capture.exists(), "an invalid manifest must launch nothing"
    assert read_ledger(tmp_project) == []
    assert not _journal_path(manifest).exists()


# --------------------------------------------------------------------------
# 2. dry-run — resolve everything, write nothing
# --------------------------------------------------------------------------

def test_dry_run_resolves_and_writes_nothing(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave2.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: gpt-5.6-luna
          effort: medium
        entries:
          - id: plain
            scope: "plain lane"
            prompt: do the thing
          - id: checked
            scope: "checked lane"
            effort: high
            prompt: do the other thing
            check: "true"
        """)
    capture = tmp_path / "launched.txt"
    proc = _batch(run_hippo, tmp_project, manifest, "--dry-run",
                  env={"PATH": _stub(tmp_path, "codex", f'#!/bin/sh\ntouch "{capture}"\n')})
    assert proc.returncode == 0, proc.stderr
    s = _summary(proc)
    assert (s["total"], s["launched"]) == (2, 0)
    assert s["stopped"] is False
    assert re.search(r"plain exec=codex/gpt-5\.6-luna/medium prompt=\d+B check=no", proc.stderr)
    assert re.search(r"checked exec=codex/gpt-5\.6-luna/high prompt=\d+B check=yes", proc.stderr)
    assert not capture.exists()
    assert read_ledger(tmp_project) == []
    assert not _journal_path(manifest).exists()


# --------------------------------------------------------------------------
# 3. fan-out — one dispatch + one usage row per entry, one exit per entry
# --------------------------------------------------------------------------

def test_fanout_records_dispatch_usage_journal_per_entry(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave3.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: gpt-5.6-luna
          effort: medium
        entries:
          - scope: "algo: fenwick tree"
            prompt: build it tokens=1111
          - id: two
            scope: "second lane"
            prompt: work tokens=2222
          - id: three
            scope: "third lane"
            prompt: work tokens=3333
        """)
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", FOOTER_STUB)})
    assert proc.returncode == 0, proc.stderr
    s = _summary(proc)
    assert (s["total"], s["launched"], s["ok"], s["failed"], s["skipped"]) == (3, 3, 3, 0, 0)
    assert s["stopped"] is False
    assert s["journal"].endswith("wave3.journal.jsonl")
    assert "[3/3]" in proc.stderr

    rows = read_ledger(tmp_project)
    dispatches = [e for e in rows if e.get("ev") == "dispatch"]
    usages = [e for e in rows if e.get("ev") == "usage"]
    assert len(dispatches) == 3 and len(usages) == 3
    for e in dispatches:
        assert re.fullmatch(r"d[0-9a-f]{32}", e["id"])
        assert e["exec"] == "codex/gpt-5.6-luna/medium"
        assert e["src"] == "wrapper" and e["depth"] == 0
        assert "parent" not in e and "task" not in e
    by_scope = {e["scope"]: e for e in dispatches}
    tokens_by_ref = {u["ref"]: u["tokens"] for u in usages}
    assert tokens_by_ref[by_scope["algo: fenwick tree"]["id"]] == 1111
    assert tokens_by_ref[by_scope["second lane"]["id"]] == 2222
    assert tokens_by_ref[by_scope["third lane"]["id"]] == 3333
    assert all(u["model"] == "gpt-5.6-luna" and u["src"] == "wrapper" for u in usages)

    journal = _journal(manifest)
    assert len([l for l in journal if l["event"] == "launch"]) == 3
    exits = [l for l in journal if l["event"] == "exit"]
    assert len(exits) == 3
    assert {l["id"] for l in exits} == {"algo-fenwick-tree", "two", "three"}
    for l in exits:
        assert l["rc"] == 0 and l["attempt"] == 1 and l["check_rc"] is None
        assert "timed_out" not in l  # journaled only when true
        assert l["tokens"] == tokens_by_ref[l["dispatch"]]
    out = _outdir(manifest)
    assert (out / "algo-fenwick-tree.out").read_text(encoding="utf-8") == "agent output\n"
    assert "tokens used" in (out / "two.err").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 4. parent stamping — a batch launched from inside a lane records who spawned it
# --------------------------------------------------------------------------

def test_children_carry_the_launching_lanes_parent(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave4.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: gpt-5.6-luna
          effort: low
        entries:
          - id: a
            scope: "lane a"
            prompt: go
          - id: b
            scope: "lane b"
            prompt: go
        """)
    rec = tmp_path / "dispatch_ids_seen.txt"
    body = '#!/bin/sh\nprintf \'%s\\n\' "$HIPPO_DISPATCH" >> "$REC_FILE"\n'
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", body),
                       "HIPPO_DISPATCH": "dparent", "REC_FILE": str(rec)})
    assert proc.returncode == 0, proc.stderr
    dispatches = [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]
    assert len(dispatches) == 2
    assert all(e["parent"] == "dparent" for e in dispatches)
    # Each child runs under its own fresh dispatch id, not the parent's.
    seen = set(rec.read_text(encoding="utf-8").split())
    assert seen == {e["id"] for e in dispatches}


# --------------------------------------------------------------------------
# 5. concurrency — the CLI flag caps parallel children (and beats the manifest)
# --------------------------------------------------------------------------

# mkdir is the atomic test-and-set every platform has; flock is not on macOS.
COUNTER_STUB = """\
#!/bin/sh
lock() { while ! mkdir "$CNT_DIR/lock" 2>/dev/null; do sleep 0.01; done; }
lock
cur=$(( $(cat "$CNT_DIR/cur" 2>/dev/null || echo 0) + 1 ))
printf '%s' "$cur" > "$CNT_DIR/cur"
max=$(cat "$CNT_DIR/max" 2>/dev/null || echo 0)
[ "$cur" -gt "$max" ] && printf '%s' "$cur" > "$CNT_DIR/max"
echo x >> "$CNT_DIR/runs"
rmdir "$CNT_DIR/lock"
sleep 0.4
lock
printf '%s' "$(( $(cat "$CNT_DIR/cur") - 1 ))" > "$CNT_DIR/cur"
rmdir "$CNT_DIR/lock"
exit 0
"""


def test_cli_concurrency_flag_caps_parallel_children(tmp_project, tmp_path, run_hippo):
    entries = "\n".join(
        f'  - id: e{i}\n    scope: "lane {i}"\n    prompt: go' for i in range(6))
    manifest = _manifest(tmp_project, "wave5.yaml", (
        "concurrency: 5\n"      # the CLI flag must win over this
        "defaults:\n"
        "  kind: impl\n"
        "  executor: codex\n"
        "  model: gpt-5.6-luna\n"
        "  effort: low\n"
        "entries:\n" + entries + "\n"))
    cnt = tmp_path / "cnt"
    cnt.mkdir()
    proc = _batch(run_hippo, tmp_project, manifest, "--concurrency", "2",
                  env={"PATH": _stub(tmp_path, "codex", COUNTER_STUB), "CNT_DIR": str(cnt)},
                  timeout=60)
    assert proc.returncode == 0, proc.stderr
    s = _summary(proc)
    assert (s["launched"], s["ok"]) == (6, 6)
    assert len((cnt / "runs").read_text(encoding="utf-8").splitlines()) >= 2
    # Three rounds of 0.4s sleeps under a 2-worker pool: exactly 2 is deterministic in
    # practice — under 2 means the pool serialized, over 2 means the cap leaked.
    assert int((cnt / "max").read_text(encoding="utf-8")) == 2


def test_manifest_concurrency_caps_without_the_cli_flag(tmp_project, tmp_path, run_hippo):
    entries = "\n".join(
        f'  - id: m{i}\n    scope: "lane {i}"\n    prompt: go' for i in range(6))
    manifest = _manifest(tmp_project, "wave5b.yaml", (
        "concurrency: 2\n"      # no CLI flag: the manifest's own cap must hold
        "defaults:\n"
        "  kind: impl\n"
        "  executor: codex\n"
        "  model: gpt-5.6-luna\n"
        "  effort: low\n"
        "entries:\n" + entries + "\n"))
    cnt = tmp_path / "cnt"
    cnt.mkdir()
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", COUNTER_STUB), "CNT_DIR": str(cnt)},
                  timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert _summary(proc)["ok"] == 6
    assert int((cnt / "max").read_text(encoding="utf-8")) == 2


# --------------------------------------------------------------------------
# 6. journal guard — an existing journal demands an explicit --resume or --fresh
# --------------------------------------------------------------------------

def test_existing_journal_demands_resume_or_fresh(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave6.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: gpt-5.6-luna
          effort: low
        entries:
          - id: only
            scope: "only lane"
            prompt: go
        """)
    env = {"PATH": _stub(tmp_path, "codex", STUB_OK)}

    both = _batch(run_hippo, tmp_project, manifest, "--resume", "--fresh", env=env)
    assert both.returncode == 2
    assert not _journal_path(manifest).exists()

    first = _batch(run_hippo, tmp_project, manifest, env=env)
    assert first.returncode == 0, first.stderr
    assert _journal_path(manifest).exists()

    blocked = _batch(run_hippo, tmp_project, manifest, env=env)
    assert blocked.returncode == 2
    assert "--resume" in blocked.stderr and "--fresh" in blocked.stderr
    assert len([e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]) == 1

    fresh = _batch(run_hippo, tmp_project, manifest, "--fresh", env=env)
    assert fresh.returncode == 0, fresh.stderr
    (bak,) = tmp_project.glob("wave6.journal.jsonl.*.bak")
    assert re.fullmatch(r"wave6\.journal\.jsonl\.\d{8}T\d{6}Z\.bak", bak.name)
    journal = _journal(manifest)
    assert len([l for l in journal if l["event"] == "exit"]) == 1
    assert not [l for l in journal if l["event"] == "skip"]  # fresh means start over


# --------------------------------------------------------------------------
# 7. resume — DONE entries are skipped, failures relaunch under a NEW dispatch id
# --------------------------------------------------------------------------

FAIL_MARKER_STUB = (
    "#!/bin/sh\n"
    'for a in "$@"; do last="$a"; done\n'
    'case "$last" in *FAILME*) exit 1;; esac\n'
    "exit 0\n"
)


def test_resume_skips_done_entries_and_relaunches_failures(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave7.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: gpt-5.6-luna
          effort: low
        entries:
          - id: good
            scope: "good lane"
            prompt: fine
          - id: bad
            scope: "bad lane"
            prompt: FAILME
        """)
    env = {"PATH": _stub(tmp_path, "codex", FAIL_MARKER_STUB)}
    first = _batch(run_hippo, tmp_project, manifest, env=env)
    assert first.returncode == 1
    s1 = _summary(first)
    assert (s1["ok"], s1["failed"]) == (1, 1)

    second = _batch(run_hippo, tmp_project, manifest, "--resume", env=env)
    assert second.returncode == 1  # the stub is deterministic: bad fails again
    s2 = _summary(second)
    assert (s2["launched"], s2["skipped"], s2["failed"]) == (1, 1, 1)

    journal = _journal(manifest)
    (skip,) = [l for l in journal if l["event"] == "skip"]
    assert skip["id"] == "good"
    bad_exits = [l for l in journal if l["event"] == "exit" and l["id"] == "bad"]
    assert sorted(l["attempt"] for l in bad_exits) == [1, 2]
    assert len({l["dispatch"] for l in bad_exits}) == 2  # two launches are two facts
    good_exits = [l for l in journal if l["event"] == "exit" and l["id"] == "good"]
    assert [l["attempt"] for l in good_exits] == [1]

    dispatches = [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]
    assert len([e for e in dispatches if e["scope"] == "good lane"]) == 1
    bad_rows = [e for e in dispatches if e["scope"] == "bad lane"]
    assert len(bad_rows) == 2 and len({e["id"] for e in bad_rows}) == 2


# --------------------------------------------------------------------------
# 8. check — a failing check counts the entry failed, and stays evidence only
# --------------------------------------------------------------------------

def test_failing_check_counts_failed_and_lands_in_the_check_file(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave8.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: gpt-5.6-luna
          effort: low
        entries:
          - id: checked
            scope: "checked lane"
            prompt: go
            check: "echo check-stdout; echo check-stderr >&2; exit 3"
        """)
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", STUB_OK)})
    assert proc.returncode == 1
    s = _summary(proc)
    assert (s["ok"], s["failed"]) == (0, 1)
    assert "check=fail" in proc.stderr
    (exit_line,) = [l for l in _journal(manifest) if l["event"] == "exit"]
    assert exit_line["rc"] == 0 and exit_line["check_rc"] == 3
    check_out = (_outdir(manifest) / "checked.check").read_text(encoding="utf-8")
    assert "check-stdout" in check_out and "check-stderr" in check_out
    # A check result is journal evidence, never a verdict: main judges.
    assert not [e for e in read_ledger(tmp_project) if e.get("ev") == "outcome"]


# --------------------------------------------------------------------------
# 9. breaker — a lane's expensive wave stops before launch one; main never gated
# --------------------------------------------------------------------------

BREAKER_MANIFEST = """\
defaults:
  kind: impl
  executor: codex
  model: gpt-5.6-sol
  effort: high
entries:
  - id: w1
    scope: "wave one"
    prompt: go
  - id: w2
    scope: "wave two"
    prompt: go
  - id: w3
    scope: "wave three"
    prompt: go
"""


def test_breaker_stops_a_lanes_wave_but_never_mains(tmp_project, tmp_path, run_hippo):
    _seed_children(tmp_project, 45)  # 45 × $11 reserved ≈ $495; the 46th sol breaks $500
    capture = tmp_path / "launched.txt"
    env = {"PATH": _stub(tmp_path, "codex", f'#!/bin/sh\ntouch "{capture}"\n')}

    lane = _manifest(tmp_project, "wave9a.yaml", BREAKER_MANIFEST)
    stopped = _batch(run_hippo, tmp_project, lane, env={**env, "HIPPO_DISPATCH": "dorch"})
    assert stopped.returncode == 2
    s = _summary(stopped)
    assert s["stopped"] is True
    assert (s["total"], s["launched"]) == (3, 0)
    assert not capture.exists(), "nothing may launch after the breaker stops the wave"
    stops = [l for l in _journal(lane) if l["event"] == "stopped"]
    assert stops and all("budget" in l["reason"] for l in stops)
    assert not [e for e in read_ledger(tmp_project) if e.get("scope", "").startswith("wave ")]

    # The same manifest from main (no HIPPO_DISPATCH): main is never gated.
    main = _manifest(tmp_project, "wave9b.yaml", BREAKER_MANIFEST)
    free = _batch(run_hippo, tmp_project, main, env=env)
    assert free.returncode == 0, free.stderr
    assert _summary(free)["ok"] == 3
    mains = [e for e in read_ledger(tmp_project) if e.get("scope", "").startswith("wave ")]
    assert len(mains) == 3 and all("parent" not in e for e in mains)


def test_breaker_warn_prints_once_per_run_not_per_child(tmp_project, tmp_path, run_hippo):
    # 25 × $11 reserved ≈ $275 — past the $250 warn line; luna children stay far
    # under the $500 stop, so every verdict says "warn" and the line must dedupe.
    _seed_children(tmp_project, 25)
    manifest = _manifest(tmp_project, "wave9c.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: gpt-5.6-luna
          effort: low
        entries:
          - id: c1
            scope: "cheap one"
            prompt: go
          - id: c2
            scope: "cheap two"
            prompt: go
          - id: c3
            scope: "cheap three"
            prompt: go
        """)
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", STUB_OK),
                       "HIPPO_DISPATCH": "dorch"})
    assert proc.returncode == 0, proc.stderr
    s = _summary(proc)
    assert (s["launched"], s["ok"]) == (3, 3)
    assert s["stopped"] is False
    warns = [ln for ln in proc.stderr.splitlines() if "budget" in ln]
    assert len(warns) == 1, proc.stderr
    assert not [l for l in _journal(manifest) if l["event"] == "stopped"]


# --------------------------------------------------------------------------
# 10. claude adapter — measured single-JSON stdout, mapped without inventing $
# --------------------------------------------------------------------------

CLAUDE_JSON = json.dumps({
    "type": "result",
    "total_cost_usd": 1.23,
    "usage": {"input_tokens": 7, "cache_read_input_tokens": 200,
              "cache_creation_input_tokens": 93, "output_tokens": 400},
    "modelUsage": {"claude-sonnet-5": {"outputTokens": 400}},
})


def test_claude_adapter_argv_and_usage_mapping(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave10.yaml", """\
        defaults:
          kind: impl
          executor: claude
          model: claude-x-manifest
          effort: medium
        entries:
          - id: sonnet
            scope: "claude lane"
            prompt: hello
        """)
    rec = tmp_path / "claude_argv.bin"
    body = (
        "#!/bin/sh\n"
        'for a in "$@"; do printf \'%s\\0\' "$a" >> "$ARGV_FILE"; done\n'
        f"cat <<'EOF'\n{CLAUDE_JSON}\nEOF\n"
    )
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "claude", body), "ARGV_FILE": str(rec)})
    assert proc.returncode == 0, proc.stderr
    assert _summary(proc)["ok"] == 1

    argv = rec.read_text(encoding="utf-8").split("\0")[:-1]
    # Effort is a label only for claude: it reaches exec, never the argv. `--` seals the
    # argv here too — a prompt opening with `-` must never be read as a flag.
    assert argv == ["-p", "--output-format", "json", "--model", "claude-x-manifest",
                    "--", "hello"]

    rows = read_ledger(tmp_project)
    (d,) = [e for e in rows if e.get("ev") == "dispatch"]
    assert d["exec"] == "claude/claude-x-manifest/medium"
    (u,) = [e for e in rows if e.get("ev") == "usage"]
    assert u["ref"] == d["id"]
    # tin folds all three input buckets; the model is modelUsage's single key, not
    # the manifest's; total_cost_usd is deliberately dropped ($ derives from prices.yaml).
    assert (u["tin"], u["tcached"], u["tout"], u["tokens"]) == (300, 200, 400, 700)
    assert u["model"] == "claude-sonnet-5"
    assert "total_cost_usd" not in u


# --------------------------------------------------------------------------
# 11. timeout — expiry kills the child and is journaled, counted failed
# --------------------------------------------------------------------------

def test_child_timeout_kills_and_journals_timed_out(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave11.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: gpt-5.6-luna
          effort: low
          timeout: 1
        entries:
          - id: slow
            scope: "slow lane"
            prompt: go
        """)
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", "#!/bin/sh\nexec sleep 5\n")})
    assert proc.returncode == 1
    s = _summary(proc)
    assert (s["ok"], s["failed"]) == (0, 1)
    (exit_line,) = [l for l in _journal(manifest) if l["event"] == "exit"]
    assert exit_line["timed_out"] is True
    assert exit_line["rc"] != 0


# --------------------------------------------------------------------------
# 12. vars and args — {k} resolves in prompt and check; manifest args ride the argv
# --------------------------------------------------------------------------

def test_vars_substitute_in_prompt_and_check_only(tmp_project, tmp_path, run_hippo):
    (tmp_project / "common.md").write_text("COMMON says {test}\n", encoding="utf-8")
    (tmp_project / "brief12.md").write_text("BRIEF part\n", encoding="utf-8")
    manifest = _manifest(tmp_project, "wave12.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: gpt-5.6-luna
          effort: medium
          briefs: [common.md]
          args: ["--sandbox", "read-only"]
          check: "printf 'checking {test} n={n}'"
        entries:
          - id: varsub
            scope: "vars lane"
            brief: brief12.md
            prompt: "run {test} on {branch} n={n}"
            vars: {test: tests/test_x.py, n: 3}
        """)
    rec = tmp_path / "codex_argv.bin"
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", STUB_ARGV), "ARGV_FILE": str(rec)})
    assert proc.returncode == 0, proc.stderr
    assert _summary(proc)["ok"] == 1

    argv = rec.read_text(encoding="utf-8").split("\0")[:-1]
    assert argv[:5] == ["exec", "-m", "gpt-5.6-luna", "-c", "model_reasoning_effort=medium"]
    # Manifest args ride between the effort flag and the prompt; `--` seals the argv so a
    # prompt opening with `-` (or `---` frontmatter) can never be read as a flag.
    assert argv[5:7] == ["--sandbox", "read-only"]
    assert argv[-2] == "--"
    prompt = argv[-1]
    # Assembly order: defaults.briefs, then the entry brief, then the inline prompt.
    # {test}/{n} resolve everywhere; {branch} has no var and passes through untouched.
    assert prompt.index("COMMON says tests/test_x.py") < prompt.index("BRIEF part")
    assert prompt.index("BRIEF part") < prompt.index("run tests/test_x.py on {branch} n=3")
    check_out = (_outdir(manifest) / "varsub.check").read_text(encoding="utf-8")
    assert check_out == "checking tests/test_x.py n=3"


# --------------------------------------------------------------------------
# 13. flag surface — batch form accepts exactly its four flags
# --------------------------------------------------------------------------

def test_stray_token_dies_with_batch_usage(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave13.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: gpt-5.6-luna
          effort: low
        entries:
          - id: only
            scope: "only lane"
            prompt: go
        """)
    capture = tmp_path / "launched.txt"
    proc = _batch(run_hippo, tmp_project, manifest, "--bogus",
                  env={"PATH": _stub(tmp_path, "codex", f'#!/bin/sh\ntouch "{capture}"\n')})
    assert proc.returncode == 2
    assert "usage: hippo dispatch --batch" in proc.stderr
    assert not capture.exists()
    assert not _journal_path(manifest).exists()


# --------------------------------------------------------------------------
# 14. task and depth — entry fields reach the dispatch row (and the child's env)
# --------------------------------------------------------------------------

def test_entry_task_reaches_the_dispatch_row(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave14a.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: gpt-5.6-luna
          effort: low
        entries:
          - id: tasked
            scope: "tasked lane"
            task: feat/x
            prompt: go
        """)
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", STUB_OK)})
    assert proc.returncode == 0, proc.stderr
    (d,) = [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]
    assert d["task"] == "feat/x"


def test_entry_depth_reaches_the_row_and_the_child_env(tmp_project, tmp_path, run_hippo):
    manifest = _manifest(tmp_project, "wave14b.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: gpt-5.6-luna
          effort: low
        entries:
          - id: deep
            scope: "deep lane"
            depth: 1
            prompt: go
        """)
    rec = tmp_path / "depth_seen.txt"
    body = '#!/bin/sh\nprintf \'%s\\n\' "$HIPPO_DEPTH" >> "$REC_FILE"\n'
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", body), "REC_FILE": str(rec)})
    assert proc.returncode == 0, proc.stderr
    (d,) = [e for e in read_ledger(tmp_project) if e.get("ev") == "dispatch"]
    assert d["depth"] == 1
    assert rec.read_text(encoding="utf-8") == "1\n"


# --------------------------------------------------------------------------
# 15. record_failed — a lost record makes a sound and a journal line, never a
#     blocked launch
# --------------------------------------------------------------------------

def test_invalid_dispatch_record_journals_record_failed_but_launches(
        tmp_project, tmp_path, run_hippo):
    # A model with a space survives manifest validation (any non-empty string) but
    # fails the dispatch event's exec shape — exactly the record-vs-launch seam.
    manifest = _manifest(tmp_project, "wave15.yaml", """\
        defaults:
          kind: impl
          executor: codex
          model: "gpt 5.6-luna"
          effort: low
        entries:
          - id: unrecorded
            scope: "unrecorded lane"
            prompt: go
        """)
    capture = tmp_path / "launched.txt"
    proc = _batch(run_hippo, tmp_project, manifest,
                  env={"PATH": _stub(tmp_path, "codex", f'#!/bin/sh\ntouch "{capture}"\n')})
    assert proc.returncode == 0, proc.stderr
    s = _summary(proc)
    assert (s["launched"], s["ok"]) == (1, 1)
    assert "record failed" in proc.stderr
    assert capture.exists(), "a lost record must never block the launch"
    (rf,) = [l for l in _journal(manifest) if l["event"] == "record_failed"]
    assert rf["id"] == "unrecorded"
    # No dispatch row, and no orphan usage row either — the ledger stays clean.
    assert read_ledger(tmp_project) == []
