"""1.0 CLI surface reshape (2026-07-31): bare-noun defaults, moved
subcommands, removed surfaces.

New shape (spec):
  log [dispatch|outcome|review|review-status|raw|tail]   (bare -> tail)
  directive [list|add|retract]                           (bare -> list)
  prior [show|distill]                                   (bare -> show)
  task [...]                                             (bare -> list)

Removed: top-level `retract`, `ledger`, `distill`, and `log directive`.
The silent-no-op rule outside a project is unchanged: removed surfaces are
plain argparse errors, so inside a project they are loud (rc!=0, stderr) and
outside a project they stay byte-silent rc 0 — same order of checks as before.
"""
import json
import re

from conftest import read_ledger

REMOVED_SURFACES = (
    ["retract", "gpu-01"],
    ["ledger", "tail"],
    ["distill"],
    ["log", "directive", "--id", "x", "--text", "t", "--lifetime", "phase"],
)


def _seed_dispatch(tmp_project, run_hippo, did="d001"):
    proc = run_hippo(
        [
            "log",
            "dispatch",
            "--id",
            did,
            "--kind",
            "docs",
            "--exec",
            "codex/gpt-5.6-luna/low",
            "--scope",
            "surface test seed",
        ],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr


def _seed_directive(tmp_project, run_hippo, did="gpu-01"):
    proc = run_hippo(
        ["directive", "add", "--id", did, "--text", "Use GPUs 0 and 1 only", "--lifetime", "phase"],
        cwd=tmp_project,
    )
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------
# ① bare noun == explicit default subcommand (identical output)
# --------------------------------------------------------------------------

def test_bare_task_equals_task_list(tmp_project, run_hippo):
    add = run_hippo(
        ["task", "add", "feat/bare", "--title", "bare default"], cwd=tmp_project
    )
    assert add.returncode == 0, add.stderr
    bare = run_hippo(["task"], cwd=tmp_project)
    full = run_hippo(["task", "list"], cwd=tmp_project)
    assert bare.returncode == 0 and full.returncode == 0
    assert full.stdout.strip() != ""
    assert bare.stdout == full.stdout


def test_bare_log_equals_log_tail(tmp_project, run_hippo):
    _seed_dispatch(tmp_project, run_hippo)
    bare = run_hippo(["log"], cwd=tmp_project)
    full = run_hippo(["log", "tail"], cwd=tmp_project)
    assert bare.returncode == 0 and full.returncode == 0
    assert full.stdout.strip() != ""
    assert bare.stdout == full.stdout


def test_bare_directive_equals_directive_list(tmp_project, run_hippo):
    _seed_directive(tmp_project, run_hippo)
    bare = run_hippo(["directive"], cwd=tmp_project)
    full = run_hippo(["directive", "list"], cwd=tmp_project)
    assert bare.returncode == 0 and full.returncode == 0
    assert full.stdout.strip() != ""
    assert bare.stdout == full.stdout


def test_bare_prior_equals_prior_show(tmp_project, run_hippo):
    (tmp_project / ".hippo" / "PRIORS.md").write_text(
        "# PRIORS\n\n- kernel-impl: sol/high measured ahead\n", encoding="utf-8"
    )
    bare = run_hippo(["prior"], cwd=tmp_project)
    full = run_hippo(["prior", "show"], cwd=tmp_project)
    assert bare.returncode == 0 and full.returncode == 0
    assert "PRIORS" in full.stdout
    assert bare.stdout == full.stdout


def test_bare_noun_with_flag_gets_default_sub(tmp_project, run_hippo):
    """A leading '-' token (not help) still triggers insertion:
    `hippo log --ev dispatch` == `hippo log tail --ev dispatch`."""
    _seed_dispatch(tmp_project, run_hippo)
    _seed_directive(tmp_project, run_hippo)
    bare = run_hippo(["log", "--ev", "dispatch"], cwd=tmp_project)
    full = run_hippo(["log", "tail", "--ev", "dispatch"], cwd=tmp_project)
    assert bare.returncode == 0, bare.stderr
    assert bare.stdout == full.stdout
    assert bare.stdout.strip() != ""


# --------------------------------------------------------------------------
# ② directive add (incl. auto-id) -> list --active -> retract roundtrip
# --------------------------------------------------------------------------

def test_directive_add_autoid_roundtrip(tmp_project, run_hippo):
    add = run_hippo(
        ["directive", "add", "--text", "Use GPUs 0 and 1 only", "--lifetime", "phase"],
        cwd=tmp_project,
    )
    assert add.returncode == 0, add.stderr
    rec = json.loads(add.stdout)
    did = rec["id"]
    # auto id = slug(from text) + '-' + sha1(text)[:4]
    assert re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{4}", did), did
    assert rec["state"] == "active"

    active = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    assert active.returncode == 0, active.stderr
    assert did in {d["id"] for d in json.loads(active.stdout)}

    retract = run_hippo(["directive", "retract", did], cwd=tmp_project)
    assert retract.returncode == 0, retract.stderr

    after = run_hippo(["directive", "list", "--active", "--json"], cwd=tmp_project)
    assert after.returncode == 0, after.stderr
    assert did not in {d["id"] for d in json.loads(after.stdout)}


def test_directive_add_autoid_is_deterministic_for_same_text(
    tmp_project, run_hippo
):
    text = "keep review replies in context"
    first = run_hippo(
        ["directive", "add", "--text", text, "--lifetime", "durable"], cwd=tmp_project
    )
    second = run_hippo(
        ["directive", "add", "--text", text, "--lifetime", "durable"], cwd=tmp_project
    )
    assert first.returncode == 0 and second.returncode == 0
    assert json.loads(first.stdout)["id"] == json.loads(second.stdout)["id"]


def test_directive_add_without_text_and_id_rejected(tmp_project, run_hippo):
    proc = run_hippo(["directive", "add", "--lifetime", "phase"], cwd=tmp_project)
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""


def test_directive_retract_unknown_id_rejected(tmp_project, run_hippo):
    proc = run_hippo(["directive", "retract", "no-such-id"], cwd=tmp_project)
    assert proc.returncode != 0
    assert proc.stderr.strip() != ""


# --------------------------------------------------------------------------
# ③ log tail: --ev filter and -n cap
# --------------------------------------------------------------------------

def test_log_tail_ev_filter(tmp_project, run_hippo):
    _seed_dispatch(tmp_project, run_hippo, "d-f1")
    _seed_directive(tmp_project, run_hippo, "f-dir")
    _seed_dispatch(tmp_project, run_hippo, "d-f2")

    proc = run_hippo(["log", "tail", "--ev", "dispatch"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr
    events = [json.loads(ln) for ln in proc.stdout.splitlines()]
    assert [e["id"] for e in events] == ["d-f1", "d-f2"]
    assert all(e["ev"] == "dispatch" for e in events)

    none = run_hippo(["log", "tail", "--ev", "review"], cwd=tmp_project)
    assert none.returncode == 0
    assert none.stdout == ""


def test_log_tail_n_limits_to_last_lines(tmp_project, run_hippo):
    for did in ("d-n1", "d-n2", "d-n3"):
        _seed_dispatch(tmp_project, run_hippo, did)
    proc = run_hippo(["log", "tail", "-n", "1"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "d-n3"


# --------------------------------------------------------------------------
# ④ removed surfaces are rejected — loud inside a project, and still
#    byte-silent outside one (the pre-existing silent-no-op ordering)
# --------------------------------------------------------------------------

def test_removed_surfaces_rejected_inside_project(tmp_project, run_hippo):
    before = read_ledger(tmp_project)
    for args in REMOVED_SURFACES:
        proc = run_hippo(args, cwd=tmp_project)
        assert proc.returncode != 0, f"{args} must be rejected"
        assert proc.stderr.strip() != "", f"{args} must explain itself"
    assert read_ledger(tmp_project) == before, "a rejected call must not append"


def test_removed_surfaces_silent_outside_project(uninitialized_dir, run_hippo):
    for args in REMOVED_SURFACES:
        proc = run_hippo(args, cwd=uninitialized_dir)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", ""), args


# --------------------------------------------------------------------------
# ⑤ `-h` on a bare noun shows the noun's own help (preprocessing must not
#    shadow it with the default subcommand's help)
# --------------------------------------------------------------------------

def test_task_dash_h_shows_task_usage(tmp_project, run_hippo):
    proc = run_hippo(["task", "-h"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr
    assert "usage: hippo task" in proc.stdout
    assert "{add,set,done,list,show,drop}" in proc.stdout


def test_noun_help_lists_subcommands_even_outside_project(
    uninitialized_dir, run_hippo
):
    for noun, marker in (
        ("task", "{add,set,done,list,show,drop}"),
        ("log", "tail"),
        ("directive", "{add,list,retract}"),
        ("prior", "{show,distill}"),
    ):
        for flag in ("-h", "--help"):
            proc = run_hippo([noun, flag], cwd=uninitialized_dir)
            assert proc.returncode == 0, f"{noun} {flag}: {proc.stderr}"
            assert f"usage: hippo {noun}" in proc.stdout
            assert marker in proc.stdout, f"{noun} {flag}: {proc.stdout!r}"


def test_top_level_help_states_the_mental_model(uninitialized_dir, run_hippo):
    proc = run_hippo(["--help"], cwd=uninitialized_dir)
    assert proc.returncode == 0
    assert "derived view" in proc.stdout


# --------------------------------------------------------------------------
# moved surface: `prior distill` (old top-level `distill`, same behavior)
# --------------------------------------------------------------------------

def test_prior_distill_regenerates_priors_md(tmp_project, run_hippo, tmp_path):
    _seed_dispatch(tmp_project, run_hippo)
    mock = tmp_path / "distill_mock.md"
    mock.write_text("# PRIORS\n\n- mock distillation result\n", encoding="utf-8")

    proc = run_hippo(
        ["prior", "distill", "--days", "7"],
        cwd=tmp_project,
        env={"HIPPO_CLERK_BACKEND": "mock", "HIPPO_MOCK_OUTPUT": str(mock)},
    )
    assert proc.returncode == 0, proc.stderr

    priors = tmp_project / ".hippo" / "PRIORS.md"
    assert priors.exists()
    assert "mock distillation result" in priors.read_text(encoding="utf-8")

    clerk = [e for e in read_ledger(tmp_project) if e.get("ev") == "clerk"]
    assert clerk and clerk[-1]["name"] == "distiller" and clerk[-1]["ok"] is True

    show = run_hippo(["prior", "show"], cwd=tmp_project)
    assert show.returncode == 0
    assert "mock distillation result" in show.stdout


# --------------------------------------------------------------------------
# Injected surface: durable directives are never folded away (§6)
# --------------------------------------------------------------------------

def _add_directive(run_hippo, cwd, did, text, lifetime):
    proc = run_hippo(
        ["directive", "add", "--id", did, "--text", text, "--lifetime", lifetime], cwd=cwd
    )
    assert proc.returncode == 0, proc.stderr


def test_inject_never_folds_durable_directives(tmp_project, run_hippo):
    """A durable directive is a user ruling with no lifetime — invisible at session start is the
    same as absent. No number of phase/turn directives may push one out."""
    for i in range(6):
        _add_directive(run_hippo, tmp_project, f"dur-{i}", f"durable directive {i}", "durable")
    for i in range(20):
        _add_directive(
            run_hippo, tmp_project, f"ph-{i}", f"phase directive {i} " + "noise " * 40, "phase"
        )

    out = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert out.returncode == 0, out.stderr
    live = [ln for ln in out.stdout.splitlines() if ln.startswith("· live(")]
    assert sum(1 for ln in live if ln.startswith("· live(durable)")) == 6
    for i in range(6):
        assert f"durable directive {i}" in out.stdout
    # When something is folded, name the command that shows the full text ("+N more" alone
    # tells the reader nothing to do).
    folded = [ln for ln in out.stdout.splitlines() if "more" in ln]
    assert folded and "hippo directive" in folded[0]


def test_inject_keeps_durable_text_readable(tmp_project, run_hippo):
    """Cutting a durable line at 80 chars would drop its operative clause — keep up to 200."""
    text = "x" * 150 + " tail clause"
    _add_directive(run_hippo, tmp_project, "long-dur", text, "durable")
    out = run_hippo(["status", "--inject"], cwd=tmp_project)
    body = [ln for ln in out.stdout.splitlines() if ln.startswith("· live(durable)")][0]
    body = body.split(": ", 1)[1]
    assert len(body) > 80 and len(body) <= 200
    assert "tail clause" in body


# --------------------------------------------------------------------------
# .hippo/briefs/ — the brief convention (DESIGN §3.1)
# --------------------------------------------------------------------------

def test_init_creates_the_briefs_directory(tmp_project):
    """A convention nobody can find is not a convention: init makes the directory so a brief has
    a project-relative home instead of a per-session absolute scratchpad path."""
    assert (tmp_project / ".hippo" / "briefs").is_dir()


def test_briefs_directory_is_never_read_by_hippo(tmp_project, run_hippo):
    """hippo does not parse briefs — a malformed file there must not disturb any surface."""
    (tmp_project / ".hippo" / "briefs" / "junk.md").write_text("{ not json", encoding="utf-8")
    for argv in (["status"], ["status", "--inject"], ["task"], ["log"], ["directive"]):
        proc = run_hippo(argv, cwd=tmp_project)
        assert proc.returncode == 0, (argv, proc.stderr)


# --------------------------------------------------------------------------
# The two PRIORS axes are contracts, not hints (measured on a real ledger)
# --------------------------------------------------------------------------

def _dispatch(run_hippo, cwd, did="d-real", exec_="codex/gpt-5.6-sol/high"):
    return run_hippo(
        ["log", "dispatch", "--id", did, "--kind", "impl", "--exec", exec_, "--scope", "s"],
        cwd=cwd,
    )


def test_outcome_ref_must_name_a_real_dispatch(tmp_project, run_hippo):
    """54% of outcomes in a real ledger never joined: half passed a task id, half were ids the
    scribe invented. Both looked like data and both vanished from the priors."""
    assert _dispatch(run_hippo, tmp_project).returncode == 0

    for bad in ("feat/some-task", "d-invented"):
        proc = run_hippo(
            ["log", "outcome", "--ref", bad, "--result", "accepted"], cwd=tmp_project
        )
        assert proc.returncode != 0, bad
        assert "not a known dispatch id" in proc.stderr
        assert "hippo log tail --ev dispatch" in proc.stderr
    # a task id is the confusion worth naming outright
    proc = run_hippo(
        ["log", "outcome", "--ref", "feat/x", "--result", "accepted"], cwd=tmp_project
    )
    assert "task id is not a dispatch id" in proc.stderr

    ok = run_hippo(["log", "outcome", "--ref", "d-real", "--result", "accepted"], cwd=tmp_project)
    assert ok.returncode == 0, ok.stderr


def test_review_status_ref_must_name_a_real_review(tmp_project, run_hippo):
    proc = run_hippo(
        ["log", "review-status", "--ref", "r-nope", "--addressed", "full"], cwd=tmp_project
    )
    assert proc.returncode != 0
    assert "not a known review id" in proc.stderr


def test_exec_must_be_executor_model_effort(tmp_project, run_hippo):
    """A free-form exec produced 24 spellings for 3 real executors: 8 of the 11 distinct first
    slots were category errors, mostly a launch mechanism where the agent belonged."""
    for bad in (
        "gpt-5.6-sol/high",                    # executor missing
        "tools/dispatch/gpt-5.6-sol/xhigh",    # the shim path taken as the executor
        "background/sol xhigh",                # whitespace
        "fork agent",                          # prose
        "executor/model/effort unknown",       # the placeholder itself
        "executor/gpt-5.6-sol/high",           # right shape, placeholder word as the value
    ):
        proc = _dispatch(run_hippo, tmp_project, did=f"d-{abs(hash(bad))%9999}", exec_=bad)
        assert proc.returncode != 0, bad
        assert "executor/model/effort" in proc.stderr

    for good in ("codex/gpt-5.6-sol/high", "fork/fable/inherit", "workflow/fable/xhigh"):
        proc = _dispatch(run_hippo, tmp_project, did=f"ok-{abs(hash(good))%9999}", exec_=good)
        assert proc.returncode == 0, (good, proc.stderr)
