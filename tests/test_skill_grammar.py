"""The grammar block in skills/hippo/SKILL.md is the document models actually read (856 reads of
the skill by Codex lanes, 405 `--help` calls — measured 2026-09-23), so it may not drift from
build_parser: every line must parse, and every command, flag and enum value must be on it."""

import argparse
import re
import sys

import pytest

from conftest import REPO_ROOT

INTERNAL = {("scribe",)}  # the Stop hook's surface, never typed by a model


def _cli():
    cli = str(REPO_ROOT / "cli")
    if cli not in sys.path:
        sys.path.insert(0, cli)
    import hippo_cli
    return hippo_cli


def _block(path):
    text = path.read_text(encoding="utf-8")
    m = re.search(r"## Grammar\n\n```\n(.*?)```", text, re.S) or re.search(
        r"## CLI cheat sheet\n.*?```\n(.*?)```", text, re.S)
    assert m, f"no grammar block in {path}"
    return m.group(1)


def _commands(block):
    """One entry per `hippo …` line, with its indented continuation lines joined on."""
    cmds = []
    for ln in block.splitlines():
        if ln.startswith("hippo "):
            cmds.append(ln)
        elif ln.startswith("    ") and cmds:
            cmds[-1] += " " + ln.strip()
    return cmds


def _leaves(parser, path=()):
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        yield path, parser
        return
    for name, sp in subs[0].choices.items():
        yield from _leaves(sp, (*path, name))


def _path(parser, words):
    """The longest run of subcommand names at the head of `words`."""
    path = []
    for w in words:
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs or w not in subs[0].choices:
            break
        path.append(w)
        parser = subs[0].choices[w]
    return tuple(path)


BLOCK = _block(REPO_ROOT / "skills" / "hippo" / "SKILL.md")


def test_readme_cheat_sheet_is_the_skill_block():
    assert _block(REPO_ROOT / "README.md") == BLOCK


@pytest.mark.parametrize("line", _commands(BLOCK))
def test_every_line_names_a_real_command(line):
    parser = _cli().build_parser()
    path = _path(parser, line.split()[1:])
    assert path, line
    with pytest.raises(SystemExit) as ex:
        parser.parse_args([*path, "-h"])
    assert ex.value.code == 0, line


def test_every_command_flag_and_choice_is_in_the_block():
    h = _cli()
    parser = h.build_parser()
    by_path = {}
    for line in _commands(BLOCK):
        by_path.setdefault(_path(parser, line.split()[1:]), []).append(line)
    missing = []
    for path, leaf in _leaves(parser):
        if path in INTERNAL:
            continue
        lines = " ".join(by_path.get(path, []))
        if not lines:
            missing.append(" ".join(path))
            continue
        if path == ("dispatch",):
            # Intercepted before argparse; its grammar is DISPATCH_USAGE.
            wanted = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", h.DISPATCH_USAGE))
        else:
            wanted = {o for a in leaf._actions if a.help != argparse.SUPPRESS
                      for o in a.option_strings if o not in ("-h", "--help")}
            wanted |= {str(c) for a in leaf._actions if a.choices for c in a.choices}
        missing += [f"{' '.join(path)} {w}" for w in sorted(wanted)
                    if not re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", lines)]
    assert not missing, missing
