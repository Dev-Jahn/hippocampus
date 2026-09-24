"""State files are replaced whole or not at all.

Measured on b200 (2026-09-23): a node failure during the scribe's in-place worklog rewrite left
a 412KB worklog.md at 0 bytes. A failure between writing and renaming must leave the old file."""

import sys

import pytest

from conftest import REPO_ROOT


@pytest.fixture
def cli():
    path = str(REPO_ROOT / "cli")
    if path not in sys.path:
        sys.path.insert(0, path)
    import hippo_cli
    return hippo_cli


def test_a_crash_before_the_rename_keeps_the_old_file(cli, tmp_path, monkeypatch):
    p = tmp_path / "worklog.md"
    p.write_text("old history\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("node died")

    monkeypatch.setattr(cli.os, "replace", boom)
    with pytest.raises(OSError):
        cli.write_durable(p, "new history\n")
    assert p.read_text(encoding="utf-8") == "old history\n"


def test_the_worklog_is_replaced_whole_and_leaves_no_tmp(cli, tmp_path):
    hp = tmp_path / ".hippo"
    hp.mkdir()
    (hp / "worklog.md").write_text("## 2020-01-01\n\n- 09:00 first\n", encoding="utf-8")
    cli.worklog_append(hp, "second")
    text = (hp / "worklog.md").read_text(encoding="utf-8")
    assert text.startswith("## 2020-01-01\n\n- 09:00 first\n") and text.rstrip().endswith("second")
    assert sorted(x.name for x in hp.iterdir()) == ["worklog.md"]
