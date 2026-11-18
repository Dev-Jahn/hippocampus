"""The hippo:lane agent-panel row (hooks/lane_row.ts, DESIGN §3.6 "The row"), tested through
Claude Code's own mod test kit: `claude plugin test` runs tests/lane_row.test.ts against the
engine with every `$` call answered by the test — no session, no model, no network (0.2s).

The kit runs every `*.test.ts` under the folder it is given, and a checkout's `.claude/worktrees/`
may hold other branches' tests, so it runs on a copy of just the manifest, the hooks and this
one test file."""
import shutil
import subprocess

import pytest

from conftest import REPO_ROOT

CLAUDE = shutil.which("claude")


@pytest.mark.skipif(CLAUDE is None, reason="needs the claude binary for `claude plugin test`")
def test_the_lane_row_module_passes_its_kit_tests(tmp_path):
    plugin = tmp_path / "hippo"
    shutil.copytree(REPO_ROOT / ".claude-plugin", plugin / ".claude-plugin",
                    ignore=shutil.ignore_patterns("types"))
    shutil.copytree(REPO_ROOT / "hooks", plugin / "hooks")
    (plugin / "tests").mkdir()
    shutil.copy(REPO_ROOT / "tests" / "lane_row.test.ts", plugin / "tests")
    proc = subprocess.run([CLAUDE, "plugin", "test", str(plugin)], cwd=tmp_path,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert " 0 fail" in proc.stdout, proc.stdout
