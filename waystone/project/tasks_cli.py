#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Structured selected-work registry CLI — read and mutate tasks.yaml safely.

A long-lived registry grows to thousands of lines; reading or `Edit`-ing it whole is slow and
error-prone. These verbs give the agent a small surface so it never touches the raw file:

  waystone task list   [root] [--status S] [--type T] [--milestone M] [--round R]   compact one-line view
  waystone task show   <id> [root]                                                  one task's full record
  waystone task add    <id> [root] --title "..." [--status/--severity/--deps/...]   insert a validated block
  waystone task set    <id> <field> <value> [root]                                  set one field (deps: comma-separated ids)
  waystone task set    <id> [root] --accept-add "criterion" [--accept-add ...]       append exact criteria
  waystone task set    <id> [root] --scope-add "prefix" [--scope-add ...]            append repo-relative path prefixes
  waystone task drop   <id> [root]                                                  status -> dropped
  waystone task archive [root] [--threshold N] [--keep K]                           relocate old done/dropped

Mutations are comment-preserving AST-bounded text surgery and validate the
result before writing — a write that would break the schema is refused, nothing is written.
`archive` moves done/dropped tasks (most-recent-by-round kept live, oldest archived, and never one a
remaining task still depends on) into `tasks.archive.yaml` once the registry passes a size
threshold; the live registry stays small.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import yaml  # noqa: E402

import validate  # noqa: E402
from common import (
    WorkflowError, canonical_scope_prefixes, find_project_root, hold_project_lock, load_tasks,
    migrate_project_state, normalize_scope_prefix, require_initialized_root, write_text_atomic,
)  # noqa: E402
from waystone.adapters.git import build_git_environment  # noqa: E402

ARCHIVE_NAME = "tasks.archive.yaml"
ARCHIVE_THRESHOLD = 100   # only archive once the registry has at least this many tasks
ARCHIVE_KEEP = 10         # keep this many most-recent done/dropped tasks live (decision-remind)
TERMINAL = ("done", "dropped")

# field order for a written task block (only fields actually supplied are emitted)
_FIELD_ORDER = ("title", "status", "severity", "milestone", "deps",
                "anchor", "origin", "branch", "notes", "scope", "round", "ruling", "result")

# task fields whose value is a YAML list, set from a comma-separated CLI value (like `add --deps`)
_LIST_FIELDS = ("deps",)

# `accept` remains unavailable through generic add/set because comma-splitting would distort it.
# Exact repeated additions use the dedicated --accept-add path; one-off human criteria use run --accept.
ACCEPT_REJECT_MSG = ("accept is a YAML list of free-text criteria — use repeated "
                     "`waystone task set <id> --accept-add <criterion>` or pass --accept at delegation time")
SCOPE_REJECT_MSG = ("scope is a YAML list of repo-relative path prefixes — use repeated "
                    "`waystone task set <id> --scope-add <prefix>`")


def _compose_mapping(text: str) -> "yaml.MappingNode":
    node = yaml.compose(text)
    if not isinstance(node, yaml.MappingNode):
        raise WorkflowError("document top level must be a mapping")
    return node


def _top_level(root: "yaml.MappingNode", key: str):
    found = [value for name, value in root.value
             if isinstance(name, yaml.ScalarNode) and name.value == key]
    if len(found) > 1:
        raise WorkflowError(f"ambiguous document: {len(found)} top-level {key!r} keys")
    return found[0] if found else None


def _task_item_span(text: str, task_id: str) -> tuple[int, int]:
    tasks = _top_level(_compose_mapping(text), "tasks")
    if tasks is None:
        raise KeyError("no top-level 'tasks' sequence")
    if not isinstance(tasks, yaml.SequenceNode):
        raise WorkflowError("'tasks' is not a list")
    matches = []
    for item in tasks.value:
        if not isinstance(item, yaml.MappingNode):
            continue
        ids = [value.value for name, value in item.value
               if isinstance(name, yaml.ScalarNode) and name.value == "id"
               and isinstance(value, yaml.ScalarNode)]
        if len(ids) > 1:
            raise WorkflowError(f"task near line {item.start_mark.line + 1} has multiple id keys")
        if ids and ids[0] == task_id:
            matches.append((item.start_mark.line, item.end_mark.line))
    if len(matches) > 1:
        raise WorkflowError(f"ambiguous registry: duplicate task id {task_id!r}")
    if not matches:
        raise KeyError(f"task id not found in registry: {task_id}")
    start, end = matches[0]
    lines = text.splitlines(keepends=True)
    if lines and end == len(lines) - 1 and not lines[-1].endswith("\n"):
        end = len(lines)
    return start, end


def set_task_field(text: str, task_id: str, field: str, value: str) -> str:
    lines = text.splitlines(keepends=True)
    start, end = _task_item_span(text, task_id)
    end = min(end, len(lines))
    nl = "\r\n" if lines[start].endswith("\r\n") else "\n"
    field_indent = len(lines[start]) - len(lines[start].lstrip()) + 2
    field_re = re.compile(rf"^ {{{field_indent}}}{re.escape(field)}:\s*.*$")
    for index in range(start + 1, end):
        if field_re.match(lines[index]):
            finish = index + 1
            while (finish < end and lines[finish].strip()
                   and len(lines[finish]) - len(lines[finish].lstrip()) > field_indent):
                finish += 1
            lines[index:finish] = [f"{' ' * field_indent}{field}: {value}{nl}"]
            return "".join(lines)
    lines.insert(start + 1, f"{' ' * field_indent}{field}: {value}{nl}")
    return "".join(lines)


# ---- pure helpers ------------------------------------------------------------
def _tasks(data: dict) -> list[dict]:
    return [t for t in (data.get("tasks") or []) if isinstance(t, dict) and t.get("id")]


def render_list(data: dict, *, status=None, type_=None, milestone=None, round_=None) -> list[str]:
    """One compact line per task, filtered by any supplied criteria. Pure."""
    out = []
    for t in _tasks(data):
        if status and t.get("status", "pending") != status:
            continue
        if type_ and not t["id"].startswith(f"{type_}/"):
            continue
        if milestone and t.get("milestone") != milestone:
            continue
        if round_ and t.get("round") != round_:
            continue
        sev = f" !{t['severity']}" if t.get("severity") else ""
        out.append(f"{t['id']}  [{t.get('status', 'pending')}]{sev}  {t.get('title', '')}")
    return out


def render_show(data: dict, task_id: str) -> str:
    """The single task's full record as YAML. Raises KeyError if absent."""
    for t in _tasks(data):
        if t["id"] == task_id:
            return yaml.safe_dump(t, sort_keys=False, allow_unicode=True).rstrip()
    raise KeyError(f"task id not found in registry: {task_id}")


def _fmt(v) -> str:
    """A flow-style YAML scalar/sequence that is always valid (strings double-quoted, so a value
    containing ': ', '#', leading specials, unicode, etc. can never break the document)."""
    import json
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return json.dumps(v)
    return json.dumps(str(v), ensure_ascii=False)


def append_task_block(text: str, fields: dict) -> str:
    """Insert a new task block at the end of the top-level `tasks:` sequence, preserving all
    existing content/comments and the file's line ending. `fields` must include `id`; other fields
    are emitted in a stable order. Raises WorkflowError if there is no usable `tasks:` key."""
    tid = fields["id"]
    nl = "\r\n" if "\r\n" in text else "\n"
    block = f"  - id: {tid}{nl}" + "".join(
        f"    {k}: {_fmt(fields[k])}{nl}" for k in _FIELD_ORDER if fields.get(k) is not None
    )

    root = _compose_mapping(text)
    node = _top_level(root, "tasks")
    lines = text.splitlines(keepends=True)

    if isinstance(node, yaml.SequenceNode) and node.value:
        insert_at = node.value[-1].end_mark.line
        # same no-trailing-newline correction as _task_item_span: PyYAML's end_mark for the final
        # node lands ON its last content line, so a naive insert would split the previous task.
        if lines and insert_at == len(lines) - 1 and not lines[-1].endswith("\n"):
            insert_at = len(lines)
        insert_at = min(insert_at, len(lines))
        if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
            lines[insert_at - 1] += nl  # terminate the previous last line before appending
        lines.insert(insert_at, block)
        return "".join(lines)

    # empty (`tasks: []` / `tasks:`): rewrite the key line into a block-sequence header
    key_lines = [k.start_mark.line for k, _ in root.value
                 if isinstance(k, yaml.ScalarNode) and k.value == "tasks"]
    if not key_lines:
            raise WorkflowError("document has no top-level 'tasks' key")
    i = key_lines[0]
    lines[i] = f"tasks:{nl}"
    lines.insert(i + 1, block)
    return "".join(lines)


def select_for_archive(data: dict, *, threshold: int, keep: int) -> list[str]:
    """Ids of done/dropped tasks to relocate: only once the registry has >= `threshold` tasks; the
    most-recent `keep` terminal tasks (by `round` when closed, then file order) stay live; and no
    terminal task that any REMAINING task still depends on is archived — iterated to a fixed point so
    a protected task's own transitive deps are protected too, keeping the live registry valid. Pure."""
    tasks = _tasks(data)
    if len(tasks) < threshold:
        return []
    terminal = [(i, t) for i, t in enumerate(tasks) if t.get("status") in TERMINAL]
    if keep > 0:
        ranked = sorted(terminal, key=lambda it: (it[1].get("round") or "", it[0]))
        keep_ids = {t["id"] for _, t in ranked[-keep:]}
    else:
        keep_ids = set()
    archive = {t["id"] for _, t in terminal if t["id"] not in keep_ids}
    changed = True
    while changed:
        changed = False
        for t in tasks:
            if t["id"] in archive:
                continue  # references from tasks that are themselves being archived don't pin
            for d in list(t.get("deps") or []) + list((t.get("lane") or {}).get("depends_on") or []):
                if d in archive:
                    archive.discard(d)
                    changed = True
    return [t["id"] for _, t in terminal if t["id"] in archive]


def remove_task_blocks(text: str, ids: list[str]) -> str:
    """Delete each named task's block (AST-located, no-trailing-newline-corrected) from `text`,
    preserving everything else."""
    lines = text.splitlines(keepends=True)
    spans = []
    for tid in ids:
        try:
            s, e = _task_item_span(text, tid)
        except KeyError:
            continue
        spans.append((s, min(e, len(lines))))
    for s, e in sorted(spans, reverse=True):
        del lines[s:e]
    return "".join(lines)


# ---- CLI commands ------------------------------------------------------------
def _write_validated(tasks_path: Path, new_text: str, what: str) -> int:
    try:
        data = yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        print(f"task {what}: result is not valid YAML — nothing written ({e})", file=sys.stderr)
        return 2
    errs = validate.validate(data)
    if errs:
        print(f"task {what}: would make tasks.yaml invalid ({len(errs)} issue(s)) — nothing written:",
              file=sys.stderr)
        for e in errs[:10]:
            print(f"  - {e}", file=sys.stderr)
        return 2
    write_text_atomic(tasks_path, new_text)
    return 0


def cmd_add(root: Path, fields: dict) -> int:
    tasks_path = root / "tasks.yaml"
    try:
        new_text = append_task_block(tasks_path.read_text(encoding="utf-8"), fields)
    except WorkflowError as e:
        print(f"task add: {e}", file=sys.stderr)
        return 1
    rc = _write_validated(tasks_path, new_text, "add")
    if rc == 0:
        print(f"task add: registered {fields['id']}")
    return rc


def cmd_set(root: Path, task_id: str, field: str, value: str) -> int:
    tasks_path = root / "tasks.yaml"
    if field in _LIST_FIELDS:
        # a list field (e.g. deps) is given comma-separated, exactly like `add --deps`, and written
        # as a flow sequence — so it can hold several ids (or [] to clear), never a scalar string.
        formatted = _fmt([x.strip() for x in value.split(",") if x.strip()])
    else:
        # quote the scalar (a free-form CLI string may contain ': ', '#', etc.) so it can never
        # produce a malformed document; the schema check then catches semantically-wrong values.
        formatted = _fmt(value)
    try:
        new_text = set_task_field(tasks_path.read_text(encoding="utf-8"), task_id, field, formatted)
    except (KeyError, WorkflowError) as e:
        print(f"task set: {e}", file=sys.stderr)
        return 1
    rc = _write_validated(tasks_path, new_text, "set")
    if rc == 0:
        print(f"task set: {task_id}.{field} = {formatted if field in _LIST_FIELDS else value}")
    return rc


def cmd_accept_add(root: Path, task_id: str, criteria: list[str]) -> int:
    """Append exact free-text criteria through the ordinary validated atomic task-set path."""
    if any(not criterion.strip() for criterion in criteria):
        print("task set: --accept-add criteria must be non-empty", file=sys.stderr)
        return 1
    tasks_path = root / "tasks.yaml"
    try:
        data = load_tasks(root)
        task = next(t for t in _tasks(data) if t["id"] == task_id)
    except StopIteration:
        print(f"task set: task id not found in registry: {task_id}", file=sys.stderr)
        return 1
    existing = task.get("accept", [])
    if not isinstance(existing, list) or any(not isinstance(item, str) for item in existing):
        print(f"task set: {task_id}.accept is not a string list", file=sys.stderr)
        return 1
    acceptance = list(existing)
    for criterion in criteria:
        if criterion not in acceptance:
            acceptance.append(criterion)
    try:
        new_text = set_task_field(
            tasks_path.read_text(encoding="utf-8"), task_id, "accept", _fmt(acceptance))
    except (KeyError, WorkflowError) as e:
        print(f"task set: {e}", file=sys.stderr)
        return 1
    rc = _write_validated(tasks_path, new_text, "set")
    if rc == 0:
        print(f"task set: {task_id}.accept += {_fmt(criteria)}")
    return rc


def cmd_scope_add(root: Path, task_id: str, prefixes: list[str]) -> int:
    """Append validated repo-relative prefixes through the ordinary atomic task-set path."""
    normalized: list[str] = []
    for prefix in prefixes:
        path = normalize_scope_prefix(prefix)
        if path is None:
            print("task set: --scope-add requires a repo-relative path prefix without glob, '..', "
                  "URL, or whitespace", file=sys.stderr)
            return 1
        normalized.append(path)
    try:
        data = load_tasks(root)
        task = next(t for t in _tasks(data) if t["id"] == task_id)
    except StopIteration:
        print(f"task set: task id not found in registry: {task_id}", file=sys.stderr)
        return 1
    try:
        scope = canonical_scope_prefixes(task.get("scope", []))
    except WorkflowError as e:
        print(f"task set: {task_id}.scope is invalid: {e}", file=sys.stderr)
        return 1
    for prefix in normalized:
        if prefix not in scope:
            scope.append(prefix)
    tasks_path = root / "tasks.yaml"
    try:
        new_text = set_task_field(
            tasks_path.read_text(encoding="utf-8"), task_id, "scope", _fmt(scope))
    except (KeyError, WorkflowError) as e:
        print(f"task set: {e}", file=sys.stderr)
        return 1
    rc = _write_validated(tasks_path, new_text, "set")
    if rc == 0:
        print(f"task set: {task_id}.scope += {_fmt(normalized)}")
    return rc


def cmd_archive(root: Path, threshold: int, keep: int) -> int:
    data = load_tasks(root)
    ids = select_for_archive(data, threshold=threshold, keep=keep)
    if not ids:
        print(f"task archive: nothing to archive ({len(_tasks(data))} tasks, threshold {threshold})")
        return 0
    tasks_path = root / "tasks.yaml"
    by_id = {t["id"]: t for t in _tasks(data)}
    new_text = remove_task_blocks(tasks_path.read_text(encoding="utf-8"), ids)
    try:
        new_data = yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        print(f"task archive: removal produced invalid YAML — aborted ({e})", file=sys.stderr)
        return 2
    errs = validate.validate(new_data)
    if errs:  # selection keeps the registry dependency-closed, so this is a belt-and-suspenders gate
        print(f"task archive: removal would invalidate tasks.yaml ({len(errs)} issue(s)) — aborted:", file=sys.stderr)
        for e in errs[:10]:
            print(f"  - {e}", file=sys.stderr)
        return 2

    archive_path = root / ARCHIVE_NAME
    orig_archive = archive_path.read_text(encoding="utf-8") if archive_path.exists() else None
    doc = yaml.safe_load(orig_archive) if orig_archive else None
    # never silently clobber an existing archive that isn't the shape we expect — it may hold history
    if orig_archive and orig_archive.strip() and not (isinstance(doc, dict) and isinstance(doc.get("tasks"), list)):
        print(f"task archive: existing {ARCHIVE_NAME} is not a {{version, project, tasks: [...]}} doc — "
              f"aborted to avoid destroying history", file=sys.stderr)
        return 2
    if not isinstance(doc, dict):
        doc = {"version": 1, "project": data.get("project", "?"), "tasks": []}
    if not isinstance(doc.get("tasks"), list):
        doc["tasks"] = []
    # the archive is a historical dump, NOT a live registry — intentionally not schema-validated
    # (archived tasks legitimately reference tasks that stayed live, so it is not dependency-closed).
    seen = {t.get("id") for t in doc["tasks"] if isinstance(t, dict)}
    doc["tasks"].extend(by_id[i] for i in ids if i not in seen)  # dedup → a re-run never double-appends
    write_text_atomic(archive_path, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    try:
        write_text_atomic(tasks_path, new_text)
    except OSError as e:  # roll the archive back so the tasks aren't stranded in both files
        if orig_archive is None:
            archive_path.unlink(missing_ok=True)
        else:
            write_text_atomic(archive_path, orig_archive)
        print(f"task archive: failed writing tasks.yaml, rolled back — {e}", file=sys.stderr)
        return 1
    print(f"task archive: moved {len(ids)} done/dropped task(s) to {ARCHIVE_NAME}; "
          f"{len(_tasks(data)) - len(ids)} remain")
    return 0


# ---- arg parsing + dispatch --------------------------------------------------
_VALUE_FLAGS = {"title", "status", "severity", "deps", "milestone", "anchor", "origin",
                "branch", "notes", "round", "type", "threshold", "keep", "reason",
                "accept"}  # accept is recognized only to reject it cleanly (see ACCEPT_REJECT_MSG)
_REPEAT_FLAGS = {"accept-add", "scope-add"}
# Each subcommand owns its grammar: an option that exists but is aimed at the wrong verb is
# rejected just as loudly as an unknown one.
_SUB_OPTIONS = {
    "list": {"status", "type", "milestone", "round"},
    "show": set(),
    "add": {"title", "status", "severity", "deps", "milestone", "anchor", "origin",
            "branch", "notes", "round", "accept"},
    "set": {"accept-add", "scope-add"},
    "drop": {"reason"},
    "archive": {"threshold", "keep"},
}
_MUTATION_SUBCOMMANDS = frozenset({"add", "set", "drop", "archive"})
_READ_SUBCOMMANDS = frozenset({"list", "show"})


def _split(sub: str, rest: list[str]) -> tuple[list[str], dict, str | None]:
    """Parse one subcommand's arguments. Unknown or misplaced options are rejected immediately —
    an option silently becoming a boolean (its value falling through to the positionals) is how
    `task drop --reason <text>` once treated free text as a project root. A value that itself
    starts with `--` must be passed as --name=value, and a value option without a value is an
    error rather than a silent None."""
    allowed = _SUB_OPTIONS[sub]
    pos, opts, i = [], {name: [] for name in _REPEAT_FLAGS}, 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("--"):
            name, eq, inline = a[2:].partition("=")
            if name not in _VALUE_FLAGS and name not in _REPEAT_FLAGS:
                return pos, opts, f"waystone task {sub}: unknown option --{name}"
            if name not in allowed:
                return pos, opts, f"waystone task {sub}: option --{name} is not valid for '{sub}'"
            if eq:
                value = inline
                i += 1
            elif i + 1 >= len(rest):
                return pos, opts, (
                    f"waystone task {sub}: --{name} requires a value (pass --{name}=<value>)")
            elif rest[i + 1].startswith("--"):
                return pos, opts, (
                    f"waystone task {sub}: value for --{name} looks like an option "
                    f"({rest[i + 1]!r}) — pass it literally as --{name}=<value>")
            else:
                value = rest[i + 1]
                i += 2
            if name in _REPEAT_FLAGS:
                opts[name].append(value)
            else:
                opts[name] = value
        else:
            pos.append(a)
            i += 1
    return pos, opts, None


def _resolve_root(explicit: str | None) -> Path | None:
    """Project root: an explicit positional path ('.' or a dir) if given, else discovered from cwd.
    Parsed positionally per-subcommand (never by dir-sniffing) so a free-form value is never mistaken
    for the root."""
    return Path(explicit).resolve() if explicit else find_project_root(Path.cwd())


def _git_checkout_context(root: Path, action: str) -> tuple[Path, Path, Path] | None:
    """Return scrubbed Git/private-common/top-level paths, or None for a non-Git project."""
    has_git_marker = any((candidate / ".git").exists() or (candidate / ".git").is_symlink()
                         for candidate in (root, *root.parents))
    try:
        probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-dir", "--git-common-dir",
             "--show-toplevel"],
            capture_output=True, text=True, timeout=15,
            env=build_git_environment(
                overrides={"GIT_OPTIONAL_LOCKS": "0"}),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        if not has_git_marker:
            return  # without filesystem Git metadata, this root cannot be a linked worktree
        raise WorkflowError(
            f"project_context_unavailable: cannot verify Git checkout context for {root}"
            f" ({e}); refusing {action} — run the command from the canonical checkout") from e
    if probe.returncode != 0:
        if not has_git_marker:
            return  # without filesystem Git metadata, this root cannot be a linked worktree
        detail = probe.stderr.strip() or f"git exited {probe.returncode}"
        raise WorkflowError(
            f"project_context_unavailable: cannot verify Git checkout context for {root} "
            f"({detail}); refusing {action} — "
            "run the command from the canonical checkout")
    observed = probe.stdout.splitlines()
    if len(observed) != 3 or not all(observed):
        raise WorkflowError(
            f"project_context_unavailable: Git returned incomplete checkout context for {root}; "
            f"refusing {action} — run the command from the canonical checkout")

    git_dir_text, common_dir_text, worktree_root_text = observed
    git_dir = Path(git_dir_text)
    common_dir = Path(common_dir_text)
    worktree_root = Path(worktree_root_text)
    git_dir = (git_dir if git_dir.is_absolute() else root / git_dir).resolve()
    common_dir = (common_dir if common_dir.is_absolute() else root / common_dir).resolve()
    worktree_root = (worktree_root if worktree_root.is_absolute()
                     else root / worktree_root).resolve()
    return git_dir, common_dir, worktree_root


def _canonical_read_root(root: Path) -> Path:
    """Normalize a linked checkout read to a proven canonical checkout before any lock write."""
    require_initialized_root(root)
    context = _git_checkout_context(root, "task registry read")
    if context is None:
        return root
    git_dir, common_dir, active_worktree_root = context
    if git_dir == common_dir:
        return root

    unavailable = (f"project_context_unavailable: cannot prove canonical checkout for linked "
                   f"worktree {root}; refusing task registry read — pass the canonical checkout "
                   "root explicitly")
    try:
        project_relative = root.relative_to(active_worktree_root)
    except ValueError:
        raise WorkflowError(unavailable)
    canonical_worktree_root = common_dir.parent.resolve()
    candidate_context = _git_checkout_context(canonical_worktree_root, "task registry read")
    if (candidate_context is None or candidate_context[0] != candidate_context[1]
            or candidate_context[1] != common_dir
            or candidate_context[2] != canonical_worktree_root):
        raise WorkflowError(unavailable)
    canonical_root = (canonical_worktree_root / project_relative).resolve()
    try:
        canonical_root.relative_to(canonical_worktree_root)
    except ValueError:
        raise WorkflowError(unavailable)
    canonical_context = (candidate_context if canonical_root == canonical_worktree_root
                         else _git_checkout_context(canonical_root, "task registry read"))
    if (canonical_context is None or canonical_context[0] != canonical_context[1]
            or canonical_context[1] != common_dir
            or canonical_context[2] != canonical_worktree_root
            or find_project_root(canonical_root) != canonical_root):
        raise WorkflowError(unavailable)
    return canonical_root


def _refuse_linked_worktree_mutation(root: Path) -> None:
    """Reject linked worktrees; fail closed when a checkout's Git context is unverifiable."""
    context = _git_checkout_context(root, "registry mutation")
    if context is None:
        return
    git_dir, common_dir, _worktree_root = context
    if git_dir != common_dir:
        raise WorkflowError(
            f"noncanonical_intent_mutation: refusing task registry mutation for linked worktree "
            f"{root}; run the command from the canonical checkout, or pass the canonical checkout "
            "root as the positional project root")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    sub, rest = argv[0], argv[1:]
    if sub not in _SUB_OPTIONS:
        print(f"waystone task: unknown subcommand {sub!r}\n{__doc__}", file=sys.stderr)
        return 1
    pos, opts, parse_error = _split(sub, rest)
    if parse_error is not None:
        print(parse_error, file=sys.stderr)
        return 1

    def need_root(explicit: str | None) -> Path | None:
        root = _resolve_root(explicit)
        if root is None:
            print("waystone task: no initialized project (run inside one, or pass its path)", file=sys.stderr)
            return None
        try:
            if sub in _READ_SUBCOMMANDS:
                root = _canonical_read_root(root)
            elif sub in _MUTATION_SUBCOMMANDS:
                _refuse_linked_worktree_mutation(root)
        except WorkflowError as e:
            print(f"waystone task: {e}", file=sys.stderr)
            return None
        try:
            # Lazy migration has its own short lock span before the verb's body lock.
            with hold_project_lock(root):
                migrate_project_state(root)
        except (WorkflowError, OSError) as e:
            print(f"waystone task: migration failed: {e}", file=sys.stderr)
            return None
        return root

    def mutate(root: Path, callback) -> int:
        try:
            with hold_project_lock(root):
                return callback()
        except WorkflowError as e:
            print(e, file=sys.stderr)
            return 1

    if sub == "list":
        root = need_root(pos[0] if pos else None)
        if root is None:
            return 1
        for ln in render_list(load_tasks(root), status=opts.get("status"), type_=opts.get("type"),
                              milestone=opts.get("milestone"), round_=opts.get("round")):
            print(ln)
        return 0
    if sub == "show":
        if not pos:
            print("waystone task show: <id> required", file=sys.stderr)
            return 1
        root = need_root(pos[1] if len(pos) > 1 else None)
        if root is None:
            return 1
        try:
            print(render_show(load_tasks(root), pos[0]))
        except KeyError as e:
            print(f"task show: {e}", file=sys.stderr)
            return 1
        return 0
    if sub == "add":
        if not pos:
            print("waystone task add: <id> required", file=sys.stderr)
            return 1
        if "accept" in opts:
            print(f"waystone task add: {ACCEPT_REJECT_MSG}", file=sys.stderr)
            return 1
        if not opts.get("title"):
            print("waystone task add: --title is required", file=sys.stderr)
            return 1
        root = need_root(pos[1] if len(pos) > 1 else None)
        if root is None:
            return 1
        fields = {"id": pos[0], "title": opts["title"], "status": opts.get("status", "pending")}
        for k in ("severity", "milestone", "anchor", "origin", "branch", "notes", "round"):
            if opts.get(k):
                fields[k] = opts[k]
        if opts.get("deps"):
            fields["deps"] = [x.strip() for x in opts["deps"].split(",") if x.strip()]
        return mutate(root, lambda: cmd_add(root, fields))
    if sub == "set":
        if opts.get("accept-add") and opts.get("scope-add"):
            print("waystone task set: choose only one of --accept-add or --scope-add", file=sys.stderr)
            return 1
        if opts.get("accept-add"):
            if not pos or len(pos) > 2:
                print("waystone task set: --accept-add requires <id> and optional project root", file=sys.stderr)
                return 1
            root = need_root(pos[1] if len(pos) > 1 else None)
            if root is None:
                return 1
            return mutate(root, lambda: cmd_accept_add(root, pos[0], opts["accept-add"]))
        if opts.get("scope-add"):
            if not pos or len(pos) > 2:
                print("waystone task set: --scope-add requires <id> and optional project root",
                      file=sys.stderr)
                return 1
            root = need_root(pos[1] if len(pos) > 1 else None)
            if root is None:
                return 1
            return mutate(root, lambda: cmd_scope_add(root, pos[0], opts["scope-add"]))
        if len(pos) < 3:
            print("waystone task set: <id> <field> <value> required", file=sys.stderr)
            return 1
        if pos[1] == "accept":
            print(f"waystone task set: {ACCEPT_REJECT_MSG}", file=sys.stderr)
            return 1
        if pos[1] == "scope":
            print(f"waystone task set: {SCOPE_REJECT_MSG}", file=sys.stderr)
            return 1
        root = need_root(pos[3] if len(pos) > 3 else None)
        if root is None:
            return 1
        return mutate(root, lambda: cmd_set(root, pos[0], pos[1], pos[2]))
    if sub == "drop":
        if not pos:
            print("waystone task drop: <id> required", file=sys.stderr)
            return 1
        reason = opts.get("reason")
        if reason is not None and not str(reason).strip():
            print("waystone task drop: --reason requires a non-empty value", file=sys.stderr)
            return 1
        root = need_root(pos[1] if len(pos) > 1 else None)
        if root is None:
            return 1

        def _drop() -> int:
            if reason:
                task = next((t for t in load_tasks(root).get("tasks", [])
                             if isinstance(t, dict) and t.get("id") == pos[0]), None)
                old = str((task or {}).get("notes") or "").strip()
                merged = f"{old} | dropped: {reason}" if old else f"dropped: {reason}"
                rc = cmd_set(root, pos[0], "notes", merged)
                if rc != 0:
                    return rc
            return cmd_set(root, pos[0], "status", "dropped")

        return mutate(root, _drop)
    if sub == "archive":
        root = need_root(pos[0] if pos else None)
        if root is None:
            return 1
        try:
            threshold = int(opts["threshold"]) if opts.get("threshold") else ARCHIVE_THRESHOLD
            keep = int(opts["keep"]) if opts.get("keep") else ARCHIVE_KEEP
        except (TypeError, ValueError):
            print("waystone task archive: --threshold/--keep must be integers", file=sys.stderr)
            return 1
        if threshold < 0 or keep < 0:
            print("waystone task archive: --threshold/--keep must be >= 0", file=sys.stderr)
            return 1
        return mutate(root, lambda: cmd_archive(root, threshold, keep))

    print(f"waystone task: unknown subcommand {sub!r}\n{__doc__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
