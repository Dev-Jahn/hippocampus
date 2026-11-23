"""The hippo:lane panel row and relay, tested through Claude Code's own mod test kit.
`claude plugin test` runs both TypeScript suites against a fake engine with no model or network.

The kit runs every `*.test.ts` under the folder it is given, and a checkout's `.claude/worktrees/`
may hold other branches' tests, so it runs on a copy of just the manifest, the hooks and this
two test files."""
import shutil
import subprocess

import pytest

from conftest import REPO_ROOT

CLAUDE = shutil.which("claude")


@pytest.mark.skipif(CLAUDE is None, reason="needs the claude binary for `claude plugin test`")
def test_the_lane_modules_pass_their_kit_tests(tmp_path):
    plugin = tmp_path / "hippo"
    shutil.copytree(REPO_ROOT / ".claude-plugin", plugin / ".claude-plugin",
                    ignore=shutil.ignore_patterns("types"))
    shutil.copytree(REPO_ROOT / "hooks", plugin / "hooks")
    (plugin / "tests").mkdir()
    for name in ("lane_row.test.ts", "lane.test.ts"):
        shutil.copy(REPO_ROOT / "tests" / name, plugin / "tests")
    proc = subprocess.run([CLAUDE, "plugin", "test", str(plugin)], cwd=tmp_path,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "lane_row.test.ts" in proc.stdout and "lane.test.ts" in proc.stdout, proc.stdout
    assert " 0 fail" in proc.stdout, proc.stdout
