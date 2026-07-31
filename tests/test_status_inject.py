"""Item (4): `hippo status --inject`.

DESIGN.md §3.3 + §6: in an initialized project it must print the resident
capsule (a `[hippo]`-prefixed block, at most 6 lines per §6's own example
and the "anything larger is a regression" ceiling). Outside any `.hippo/` project
the global common rule applies: complete silent no-op — 0 bytes on stdout
*and* stderr, exit 0.
"""


def test_status_inject_initialized_project_emits_capsule(tmp_project, run_hippo):
    proc = run_hippo(["status", "--inject"], cwd=tmp_project)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() != ""

    lines = proc.stdout.splitlines()
    assert lines[0].startswith("[hippo]")
    assert len(lines) <= 6, f"resident capsule must stay <=6 lines, got {len(lines)}"


def test_status_inject_uninitialized_dir_is_fully_silent(uninitialized_dir, run_hippo):
    proc = run_hippo(["status", "--inject"], cwd=uninitialized_dir)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_task_list_uninitialized_dir_is_fully_silent(uninitialized_dir, run_hippo):
    """The no-op rule is stated as universal (every surface), not specific to
    `status --inject` — spot-check one more entry point cheaply."""
    proc = run_hippo(["task", "list"], cwd=uninitialized_dir)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
