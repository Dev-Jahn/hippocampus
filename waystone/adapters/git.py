"""Git probes, publication checks, and checkout-context helpers."""
from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Mapping


class GitReadError(RuntimeError):
    """A byte-preserving, read-only Git probe could not establish its fact."""

    code = "git_read_failed"

    def __init__(
            self, args: tuple[str, ...], returncode: int | None, detail: str):
        self.args_tuple = args
        self.returncode = returncode
        self.detail = detail
        command = " ".join(args)
        suffix = f" (rc={returncode})" if returncode is not None else ""
        super().__init__(f"{self.code}: git {command}{suffix}: {detail}")


_READ_ONLY_BYTE_COMMANDS = frozenset({
    "cat-file", "diff", "ls-files", "ls-tree", "rev-parse", "show", "status",
})
_READ_ONLY_COMMANDS = _READ_ONLY_BYTE_COMMANDS | frozenset({
    "check-ref-format", "diff-tree", "for-each-ref", "merge-base", "rev-list",
})
_GIT_ENVIRONMENT_OVERRIDES = frozenset({
    "GIT_AUTHOR_DATE",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_NAME",
    "GIT_COMMITTER_DATE",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_INDEX_FILE",
    "GIT_OPTIONAL_LOCKS",
})


def build_git_environment(
        *, overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build one engine-owned Git environment without ambient repository selectors."""
    environment = {
        name: value for name, value in os.environ.items()
        if not name.startswith("GIT_")
    }
    environment.update({"GIT_PAGER": "cat", "LC_ALL": "C"})
    operation_values = dict(overrides or {})
    refused = sorted(set(operation_values).difference(_GIT_ENVIRONMENT_OVERRIDES))
    if refused:
        raise ValueError(
            "Git environment overrides are not allowed: " + ", ".join(refused))
    environment.update(operation_values)
    return environment


def _git_environment_for(args: tuple[str, ...]) -> dict[str, str]:
    read_only = bool(args) and (
        args[0] in _READ_ONLY_COMMANDS
        or (args[0] == "branch" and "--show-current" in args)
        or (args[0] == "symbolic-ref" and "--quiet" in args)
        or (args[0] == "worktree" and len(args) > 1 and args[1] == "list")
    )
    return build_git_environment(
        overrides={"GIT_OPTIONAL_LOCKS": "0"} if read_only else None)


def git_read_bytes(root: Path, *args: str, timeout: int = 15) -> bytes:
    """Return raw stdout from one explicitly read-only Git command, or fail loudly.

    ``GIT_OPTIONAL_LOCKS=0`` prevents otherwise optional index refreshes from changing the
    user's live index while snapshot facts are collected.
    """
    if not args or args[0] not in _READ_ONLY_BYTE_COMMANDS:
        command = args[0] if args else "<missing>"
        raise ValueError(f"git_read_bytes does not allow command {command!r}")
    environment = build_git_environment(
        overrides={"GIT_OPTIONAL_LOCKS": "0"})
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            env=environment,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitReadError(tuple(args), None, str(error)) from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise GitReadError(
            tuple(args), result.returncode, detail or "command produced no diagnostic")
    return result.stdout


def git_rc(root: Path, *args: str) -> tuple[int, str, str]:
    """Run git; return (returncode, stdout, stderr). Distinguishes failure from empty output."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15,
            env=_git_environment_for(args),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return (127, "", str(e))
    return (out.returncode, out.stdout.strip(), out.stderr.strip())


def git_full_sha(root: Path, ref: str = "HEAD") -> str | None:
    """Full 40-char commit sha for `ref`, or None if it does not resolve."""
    rc, out, _ = git_rc(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return out if rc == 0 and out else None


def upstream_ref(root: Path) -> str | None:
    """The tracked upstream (e.g. 'origin/main') of the current branch, or None."""
    tracking = _upstream_tracking(root)
    return tracking[0] if tracking is not None else None


def _upstream_tracking(root: Path) -> tuple[str, str, str] | None:
    """Return (display name, remote, exact remote branch ref) without resolving its SHA."""
    rc, local_ref, _ = git_rc(root, "symbolic-ref", "--quiet", "HEAD")
    if rc != 0 or not local_ref.startswith("refs/heads/"):
        return None
    fmt = "%(upstream:short)%00%(upstream:remotename)%00%(upstream:remoteref)"
    rc, out, _ = git_rc(root, "for-each-ref", f"--format={fmt}", local_ref)
    fields = out.split("\0") if rc == 0 and out else []
    if (len(fields) != 3 or not all(fields)
            or not fields[2].startswith("refs/heads/")):
        return None
    return fields[0], fields[1], fields[2]


_VERIFY_FETCH_REF_PREFIX = "refs/waystone/verify-fetch-"


_VERIFY_FETCH_REF_RE = re.compile(
    rf"{re.escape(_VERIFY_FETCH_REF_PREFIX)}([1-9][0-9]*)-[0-9a-f]{{32}}")


def _sweep_stale_verify_fetch_refs(root: Path) -> str | None:
    rc, out, error = git_rc(
        root, "for-each-ref", "--format=%(refname)", f"{_VERIFY_FETCH_REF_PREFIX}*")
    if rc != 0:
        return f"cannot enumerate temporary fetch refs: {error or 'git for-each-ref failed'}"
    for ref in out.splitlines():
        match = _VERIFY_FETCH_REF_RE.fullmatch(ref)
        if match is None:
            continue
        # PID is only a same-host liveness locator; uncertainty preserves the ref.
        try:
            os.kill(int(match.group(1)), 0)
        except ProcessLookupError:
            pass
        except (OSError, OverflowError):
            continue
        else:
            continue
        cleanup_rc, _, cleanup_error = git_rc(root, "update-ref", "-d", ref)
        if cleanup_rc != 0:
            return f"cannot delete stale temporary fetch ref {ref}: {cleanup_error or 'error'}"
    return None


def fetch_upstream_head(root: Path) -> tuple[str | None, dict]:
    """Fetch the exact tracked branch into a private ref and return its live commit.

    A command-line refspec deliberately bypasses configured fetch mappings. The fetched SHA is
    read from a unique temporary ref, then the ref is deleted, so concurrent writes to the shared
    FETCH_HEAD pseudoref cannot change the publication evidence.
    """
    sweep_error = _sweep_stale_verify_fetch_refs(root)
    if sweep_error is not None:
        return (None, {
            "reason": f"temporary fetch ref sweep failed — remote unverifiable: {sweep_error}",
        })
    tracking = _upstream_tracking(root)
    if tracking is None:
        return (None, {"reason": "no upstream tracking branch"})
    upstream, remote_name, branch_ref = tracking
    info = {"upstream": upstream, "remote": remote_name, "branch_ref": branch_ref}
    if remote_name == ".":
        return (None, {
            **info,
            "reason": "upstream remote '.' is local repository state, not remote publication",
        })
    temporary_ref = f"{_VERIFY_FETCH_REF_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"
    rc, _, fetch_error = git_rc(
        root, "fetch", "--quiet", "--no-tags", "--force", remote_name,
        f"+{branch_ref}:{temporary_ref}")
    if rc != 0:
        cleanup_rc, _, cleanup_error = git_rc(root, "update-ref", "-d", temporary_ref)
        if cleanup_rc != 0:
            return (None, {
                **info,
                "reason": (
                    "fetch failed and temporary fetch ref cleanup failed — remote unverifiable: "
                    f"{cleanup_error or 'error'}"),
            })
        probe_rc, _, probe_error = git_rc(
            root, "ls-remote", "--exit-code", "--refs", remote_name, branch_ref)
        if probe_rc == 2:
            return (None, {
                **info,
                "reason": f"upstream branch {branch_ref} is absent on remote {remote_name}",
            })
        detail = fetch_error or probe_error or "error"
        return (None, {
            **info, "reason": f"fetch failed — remote unverifiable: {detail}",
        })
    sha_rc, remote_sha, sha_error = git_rc(
        root, "rev-parse", "--verify", f"{temporary_ref}^{{commit}}")
    cleanup_rc, _, cleanup_error = git_rc(root, "update-ref", "-d", temporary_ref)
    if cleanup_rc != 0:
        return (None, {
            **info,
            "reason": (
                "temporary fetch ref cleanup failed — remote unverifiable: "
                f"{cleanup_error or 'error'}"),
        })
    if sha_rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", remote_sha):
        return (None, {
            **info,
            "reason": (
                "fetch succeeded but temporary fetch ref is empty or invalid: "
                f"{sha_error or 'no commit'}"),
        })
    return remote_sha, info


def ancestry_status(root: Path, a: str, b: str) -> tuple[bool | None, str]:
    """Return True/False for containment, or None when Git cannot decide."""
    rc, _, error = git_rc(root, "merge-base", "--is-ancestor", a, b)
    if rc == 0:
        return True, ""
    if rc == 1:
        shallow_rc, shallow, shallow_error = git_rc(
            root, "rev-parse", "--is-shallow-repository")
        if shallow_rc != 0:
            detail = shallow_error or f"git rev-parse exited {shallow_rc}"
            return None, f"cannot determine whether repository is shallow: {detail}"
        if shallow == "true":
            return None, (
                "repository is shallow; git merge-base exit 1 cannot prove non-containment")
        if shallow == "false":
            return False, ""
        return None, (
            "git rev-parse --is-shallow-repository returned unexpected output: "
            f"{shallow!r}")
    return None, error or f"git merge-base exited {rc}"


def is_ancestor(root: Path, a: str, b: str) -> bool:
    """True only when `a` is proven contained in `b`; unverifiable fails closed to False."""
    status, _ = ancestry_status(root, a, b)
    return status is True


def head_pushed(root: Path, fetch: bool = True) -> tuple[bool, dict]:
    """Is the current HEAD contained in its tracked upstream (i.e. actually pushed)?
    Returns (pushed, info). Fail-closed: a fetch failure (network/auth/remote) returns
    (False, reason) rather than trusting a stale ref. Offline verification cannot prove
    publication and therefore also fails closed."""
    if not fetch:
        tracking = _upstream_tracking(root)
        if tracking is None:
            return (False, {"reason": "no upstream tracking branch"})
        return (False, {
            "reason": "live remote fetch required — offline state cannot prove publication",
            "upstream": tracking[0],
        })
    remote_sha, info = fetch_upstream_head(root)
    if remote_sha is None:
        return (False, info)
    head = git_full_sha(root, "HEAD")
    if head is None:
        return (False, {**info, "reason": "local HEAD is not a commit"})
    pushed, ancestry_error = ancestry_status(root, head, remote_sha)
    if pushed is None:
        return (False, {
            **info,
            "reason": (
                "cannot determine whether local HEAD is contained in the live upstream: "
                f"{ancestry_error}"),
            "head": head,
            "remote_sha": remote_sha,
        })
    rc, out, _ = git_rc(root, "rev-list", "--count", f"{head}..{remote_sha}")
    behind = int(out) if rc == 0 and out.isdigit() else None
    return (pushed, {**info, "head": head, "remote_sha": remote_sha, "behind": behind})


def git(root: Path, *args: str) -> str:
    """Run a git command in `root`; return stdout or '' on failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=10,
            env=_git_environment_for(args),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def git_branch_info(root: Path) -> dict:
    branch = git(root, "branch", "--show-current") or "(detached)"
    dirty = len([ln for ln in git(root, "status", "--porcelain").splitlines() if ln])
    ahead_behind = git(root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    behind, ahead = (ahead_behind.split() + ["", ""])[:2] if ahead_behind else ("?", "?")
    return {"branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind}
