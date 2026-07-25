"""Resolve canonical project authority before any project-local state is opened."""
from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from waystone.adapters.git import build_git_environment
from waystone.core import WorkflowError
from waystone.project import registry_path


class ProjectContextError(WorkflowError):
    """A canonical project/checkout mapping could not be proven."""

    code = "project_context_unavailable"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


class ProjectContextUnregistered(ProjectContextError):
    code = "project_context_unregistered"


class ProjectContextAmbiguous(ProjectContextError):
    code = "project_context_ambiguous"


class CanonicalRootIsLinkedWorktree(ProjectContextError):
    code = "canonical_root_is_linked_worktree"


class WorktreeSelectorRequired(ProjectContextError):
    code = "worktree_selector_required"


class WorktreeSelectorMismatch(ProjectContextError):
    code = "worktree_selector_mismatch"


@dataclass(frozen=True)
class ProjectContext:
    project_id: str
    canonical_root: Path
    active_worktree_root: Path
    git_common_dir: Path
    checkout_identity: str
    database_path: Path

    @property
    def is_canonical_checkout(self) -> bool:
        return self.active_worktree_root == self.canonical_root


@dataclass(frozen=True)
class _GitContext:
    worktree_root: Path
    git_dir: Path
    common_dir: Path

    @property
    def is_linked(self) -> bool:
        return self.git_dir != self.common_dir


def _absolute_from(root: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve(strict=True)


def _git_context(start: Path) -> _GitContext:
    try:
        supplied = Path(start).expanduser().resolve(strict=True)
    except OSError as error:
        raise ProjectContextError(f"cannot resolve checkout selector {start}: {error}") from error
    try:
        result = subprocess.run(
            [
                "git", "-C", str(supplied), "rev-parse", "--show-toplevel",
                "--absolute-git-dir", "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env=build_git_environment(
                overrides={"GIT_OPTIONAL_LOCKS": "0"}),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProjectContextError(f"cannot inspect Git checkout {supplied}: {error}") from error
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 3 or not all(lines):
        detail = result.stderr.strip() or f"git exited {result.returncode}"
        raise ProjectContextError(f"Git checkout observation failed for {supplied}: {detail}")
    try:
        worktree_root = Path(lines[0]).resolve(strict=True)
        git_dir = _absolute_from(supplied, lines[1])
        common_dir = _absolute_from(supplied, lines[2])
    except OSError as error:
        raise ProjectContextError(
            f"Git returned an unavailable administrative path for {supplied}: {error}") from error
    return _GitContext(worktree_root, git_dir, common_dir)


def _read_registry(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ProjectContextError(f"machine registry is not a regular file: {path}")
        document = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError as error:
        raise ProjectContextUnregistered(f"machine registry does not exist: {path}") from error
    except ProjectContextError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProjectContextError(f"machine registry cannot be read: {path}: {error}") from error
    projects = document.get("projects") if isinstance(document, dict) else None
    if not isinstance(projects, list) or any(not isinstance(item, dict) for item in projects):
        raise ProjectContextError("machine registry projects must be a list of objects")
    return tuple(projects)


def _registered_context(entry: dict[str, Any], source: Path) -> tuple[str, _GitContext] | None:
    raw_path = entry.get("path")
    if raw_path is None:
        return None
    project_id = entry.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ProjectContextError(
            f"registry entry for {raw_path!r} lacks an opaque project_id; refusing path-derived identity")
    if not isinstance(raw_path, str) or not raw_path:
        raise ProjectContextError(f"registry entry {project_id!r} has an invalid canonical path")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise ProjectContextError(
            f"registry entry {project_id!r} canonical path is not absolute: {source}")
    context = _git_context(path)
    if context.worktree_root != path.resolve(strict=True):
        raise ProjectContextError(
            f"registry entry {project_id!r} path is not the Git worktree root")
    if context.is_linked:
        raise CanonicalRootIsLinkedWorktree(
            f"registered canonical root {context.worktree_root} is a linked worktree")
    return project_id, context


def _registered_worktree_roots(context: _GitContext) -> frozenset[Path]:
    try:
        result = subprocess.run(
            ["git", "--git-dir", str(context.common_dir), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
            env=build_git_environment(
                overrides={"GIT_OPTIONAL_LOCKS": "0"}),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProjectContextError(f"cannot enumerate registered Git worktrees: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or f"git exited {result.returncode}"
        raise ProjectContextError(f"cannot enumerate registered Git worktrees: {detail}")
    roots = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            try:
                roots.append(Path(line.removeprefix("worktree ")).resolve(strict=True))
            except OSError as error:
                raise ProjectContextError(
                    f"registered Git worktree is unavailable: {line}: {error}") from error
    if not roots:
        raise ProjectContextError("Git reported no registered worktrees")
    return frozenset(roots)


def _checkout_identity(active: _GitContext) -> str:
    if not active.is_linked:
        return "canonical"
    try:
        relative = active.git_dir.relative_to(active.common_dir)
    except ValueError as error:
        raise ProjectContextError(
            "linked checkout private Git directory is outside the common directory") from error
    if len(relative.parts) != 2 or relative.parts[0] != "worktrees":
        raise ProjectContextError("linked checkout has an unsupported private Git identity")
    digest = hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()
    return f"worktree:sha256:{digest}"


def resolve_project_context(
    start: Path | None = None,
    *,
    from_worktree: Path | None = None,
    require_run_input: bool = False,
    registry: Path | None = None,
) -> ProjectContext:
    """Resolve registry, Git family, checkout identity, and canonical DB path in that order."""
    requested = Path.cwd() if start is None else Path(start)
    caller = _git_context(requested)
    active = caller if from_worktree is None else _git_context(from_worktree)
    if from_worktree is not None and caller.common_dir != active.common_dir:
        raise WorktreeSelectorMismatch(
            "--from-worktree must belong to the caller's registered Git worktree family")
    if caller.is_linked and from_worktree is not None and active.worktree_root != caller.worktree_root:
        raise WorktreeSelectorMismatch(
            "a linked checkout may select only its current worktree as run input")
    if require_run_input and active.is_linked and from_worktree is None:
        raise WorktreeSelectorRequired(
            "linked worktree run input requires an explicit --from-worktree selector")

    source = registry_path() if registry is None else Path(registry)
    matches: list[tuple[str, _GitContext]] = []
    project_ids: set[str] = set()
    for entry in _read_registry(source):
        resolved = _registered_context(entry, source)
        if resolved is None:
            continue
        project_id, canonical = resolved
        if project_id in project_ids:
            raise ProjectContextAmbiguous(f"project_id {project_id!r} is registered more than once")
        project_ids.add(project_id)
        if canonical.common_dir == active.common_dir:
            matches.append(resolved)
    if not matches:
        raise ProjectContextUnregistered(
            f"no canonical project registration matches Git common dir {active.common_dir}")
    if len(matches) != 1:
        raise ProjectContextAmbiguous(
            f"multiple canonical registrations match Git common dir {active.common_dir}")
    project_id, canonical = matches[0]
    registered_roots = _registered_worktree_roots(canonical)
    if canonical.worktree_root not in registered_roots or active.worktree_root not in registered_roots:
        raise ProjectContextError("active or canonical checkout is not a current Git worktree registration")
    return ProjectContext(
        project_id=project_id,
        canonical_root=canonical.worktree_root,
        active_worktree_root=active.worktree_root,
        git_common_dir=active.common_dir,
        checkout_identity=_checkout_identity(active),
        database_path=canonical.worktree_root / ".waystone" / "state.db",
    )


__all__ = [
    "CanonicalRootIsLinkedWorktree",
    "ProjectContext",
    "ProjectContextAmbiguous",
    "ProjectContextError",
    "ProjectContextUnregistered",
    "WorktreeSelectorMismatch",
    "WorktreeSelectorRequired",
    "resolve_project_context",
]
