"""Independent result verification, integration decision, and CAS apply."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from waystone.adapters.git import GitReadError, git_rc, git_read_bytes
from waystone.core import WorkflowError
from waystone.jobs.domain import ExecutionCategory, Role, RoleBinding
from waystone.project import hold_project_lock
from waystone.runs.artifacts import (
    ArtifactReference,
    ArtifactReferenceKind,
    ArtifactStore,
    validate_sha256_digest,
)
from waystone.runs.assurance import (
    AssurancePlan,
    parse_assurance_plan_bytes,
    parse_reviewer_evidence_bytes,
)
from waystone.runs.effects import (
    ArtifactWriteEffect,
    EffectEngine,
    EffectRetryRefused,
    EffectResultState,
    PatchApprovalDigests,
    PatchIntegrationEffect,
    RunnerCompletionMarker,
    RunnerExecutionEffect,
    publish_runner_completion,
)
from waystone.runs.environment import (
    RunnerExecutionDescriptor,
    RunnerExecutionDescriptorRefusal,
    parse_runner_execution_descriptor,
)
from waystone.runs.lease import LeaseManager
from waystone.runs.preflight import (
    DispatchReady,
    EngineCheckAction,
    RoleCapability,
    SandboxContract,
    VerificationPlan,
    load_dispatch_ready,
    load_terminal_safe_dispatch_authority,
    load_verification_plan,
)
from waystone.runs.spec import BaseSnapshot, RunSpec, load_run_spec, read_base_snapshot
from waystone.runs.store import (
    EntityKind,
    RunStore,
    TransitionReason,
)


_EVIDENCE_SCHEMA = "waystone-verifier-evidence-1"
_ENGINE_CHECK_SCHEMA = "waystone-engine-check-evidence-1"
_DECISION_SCHEMA = "waystone-integration-decision-1"
_DECISION_INTENT_SCHEMA = "waystone-integration-decision-intent-1"
_EFFECT_PLAN_SCHEMA = "waystone-effect-plan-1"
_EFFECT_OBSERVATION_SCHEMA = "waystone-effect-observation-1"
_VERIFICATION_TRANSCRIPT_SCHEMA = "waystone-verification-transcript-1"
_INTEGRATION_REF_PREFIX = "refs/waystone/integration/"
_OID_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


class VerifyError(WorkflowError):
    """Base class for typed verify/decision/apply failures."""

    code = "run_verify_error"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class GitResultError(VerifyError):
    code = "git_result_invalid"


class VerifierActorRefusal(VerifyError):
    code = "verifier_actor_refused"


class VerifierBindingRefusal(VerifyError):
    code = "verifier_binding_refused"


class EngineCheckExecutionFailed(VerifyError):
    code = "engine_check_execution_failed"


class InvalidEngineCheckOutput(VerifyError):
    code = "invalid_engine_check_output"


class VerifierExecutionFailed(VerifyError):
    code = "verifier_execution_failed"


class InvalidVerifierOutput(VerifyError):
    code = "invalid_verifier_output"


class VerifierMutationRefusal(VerifyError):
    code = "verifier_mutated_worktree"


class EvidenceBindingRefusal(VerifyError):
    code = "verifier_evidence_binding_refused"


class MissingCriterionRefusal(VerifyError):
    code = "decision_missing_criterion"


class ExtraCriterionRefusal(VerifyError):
    code = "decision_extra_criterion"


class DecisionResultDigestRefusal(VerifyError):
    code = "decision_result_digest_mismatch"


class DecisionActorRefusal(VerifyError):
    code = "decision_actor_refused"


class BlockerOverrideRefusal(VerifyError):
    code = "unsupported_blocker_override"


class EngineCheckFailedRefusal(VerifyError):
    code = "decision_engine_check_failed"


class DecisionNotAcceptedRefusal(VerifyError):
    code = "decision_not_accepted"


class ApplyBindingRefusal(VerifyError):
    code = "apply_binding_refused"


class ApplyDriftRefusal(VerifyError):
    code = "apply_result_drift"


class ApplyConcurrentDriftRefusal(VerifyError):
    code = "apply_concurrent_drift"


class CheckedOutTargetRefRefusal(VerifyError):
    code = "apply_target_ref_checked_out"


@dataclass(frozen=True)
class ActorIdentity:
    actor_id: str
    role: Role

    def __post_init__(self) -> None:
        _nonempty(self.actor_id, "actor_id")
        try:
            object.__setattr__(self, "role", Role(self.role))
        except (TypeError, ValueError) as error:
            raise ValueError("actor role is not canonical") from error


@dataclass(frozen=True)
class GitResultTriple:
    base_oid: str
    base_tree_oid: str
    result_oid: str
    result_tree_oid: str
    changed_files: tuple[bytes, ...]
    patch_bytes: bytes
    result_digest: str

    def canonical_bytes(self) -> bytes:
        return _canonical_json(_triple_payload(self, include_digest=False))


@dataclass(frozen=True)
class WorktreeFingerprint:
    digest: str


@dataclass(frozen=True)
class CriterionResult:
    criterion: str
    passed: bool
    evidence_digests: tuple[str, ...]


@dataclass(frozen=True)
class EngineCheckOutput:
    check_id: str
    exit_code: int
    evidence: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True)
class EngineCheckRequest:
    action: EngineCheckAction
    base_snapshot: BaseSnapshot
    base_snapshot_digest: str
    execution_root: Path
    result: GitResultTriple


@dataclass(frozen=True)
class EngineCheckResult:
    check_id: str
    action_digest: str
    command: tuple[str, ...]
    command_input_digest: str
    prepared_input_digest: str
    exit_code: int
    expected_exit_codes: tuple[int, ...]
    passed: bool
    evidence_digests: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class EngineCheckEvidence:
    run_id: str
    job_id: str
    attempt_id: str
    action_id: str
    run_spec_digest: str
    base_snapshot_digest: str
    verification_plan_digest: str
    preflight_evidence_digest: str
    result_digest: str
    runner_observation_digest: str
    runner_stdout_digest: str
    runner_stderr_digest: str
    results: tuple[EngineCheckResult, ...]
    artifact_reference: ArtifactReference


@dataclass(frozen=True)
class VerifierBlocker:
    blocker_id: str
    detail: str


@dataclass(frozen=True)
class VerifierOutput:
    actor: ActorIdentity
    result_digest: str
    criterion_results: tuple[CriterionResult, ...]
    blockers: tuple[VerifierBlocker, ...]
    summary: str


@dataclass(frozen=True)
class FixtureVerifierResult:
    returncode: int
    output: object
    stderr: bytes = b""


@dataclass(frozen=True)
class VerifierAdapter:
    binding: RoleBinding
    sandbox: SandboxContract
    executor: Callable[["VerifierRequest"], FixtureVerifierResult]

    def __post_init__(self) -> None:
        if not isinstance(self.binding, RoleBinding):
            raise TypeError("verifier adapter binding must be a RoleBinding")
        if self.binding.role is not Role.VERIFIER:
            raise ValueError("verifier adapter binding must select the verifier role")
        if not isinstance(self.sandbox, SandboxContract):
            raise TypeError("verifier adapter sandbox must be a SandboxContract")
        if not callable(self.executor):
            raise TypeError("verifier adapter executor must be callable")


@dataclass(frozen=True)
class VerifierRequest:
    run_id: str
    job_id: str
    verification_plan_digest: str
    owner_criteria: tuple[str, ...]
    base_snapshot: BaseSnapshot
    base_snapshot_digest: str
    review_root: Path
    review_root_fingerprint: str
    engine_check_results: tuple[EngineCheckResult, ...]
    verifier_binding: RoleBinding
    verifier_sandbox: SandboxContract
    verifier_capability_digest: str
    result: GitResultTriple


@dataclass(frozen=True)
class VerifierEvidence:
    run_id: str
    job_id: str
    attempt_id: str
    action_id: str
    worker_actor_id: str
    actor: ActorIdentity
    run_spec_digest: str
    verification_plan_digest: str
    preflight_evidence_digest: str
    engine_checks: EngineCheckEvidence
    verifier_binding: RoleBinding
    verifier_sandbox: SandboxContract
    verifier_capability_digest: str
    runner_observation_digest: str
    runner_stdout_digest: str
    runner_stderr_digest: str
    result: GitResultTriple
    criterion_results: tuple[CriterionResult, ...]
    blockers: tuple[VerifierBlocker, ...]
    summary: str
    artifact_reference: ArtifactReference
    execution_descriptor_reference: ArtifactReference | None = None
    verifier_launch_reference: ArtifactReference | None = None


class DecisionOutcome(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"


@dataclass(frozen=True)
class BlockerOverride:
    blocker_id: str
    check_id: str
    evidence_digest: str


@dataclass(frozen=True)
class DecisionInput:
    actor: ActorIdentity
    outcome: DecisionOutcome
    criteria: tuple[str, ...]
    result_digest: str
    verifier_reference_id: str
    verifier_artifact_digest: str
    engine_check_reference_id: str
    engine_check_artifact_digest: str
    blocker_overrides: tuple[BlockerOverride, ...] = ()
    candidate_digest: str | None = None
    evaluation_evidence_digest: str | None = None
    reviewer_artifact_digests: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegrationDecision:
    run_id: str
    job_id: str
    attempt_id: str
    action_id: str
    actor: ActorIdentity
    outcome: DecisionOutcome
    criteria: tuple[str, ...]
    result_digest: str
    verifier_reference_id: str
    verifier_artifact_digest: str
    engine_check_reference_id: str
    engine_check_artifact_digest: str
    blocker_overrides: tuple[BlockerOverride, ...]
    producer_effect_digest: str
    artifact_reference: ArtifactReference
    candidate_digest: str | None = None
    evaluation_evidence_digest: str | None = None
    reviewer_artifact_digests: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApplyResult:
    action_id: str
    target_ref: str
    result_oid: str
    observed_digest: str


EngineCheckExecutor = Callable[[EngineCheckRequest], EngineCheckOutput]
RaceHook = Callable[[], None]


def _read_repository_bytes(repository: Path, *args: str) -> bytes:
    try:
        return git_read_bytes(repository, *args)
    except (GitReadError, ValueError) as error:
        raise GitResultError(str(error)) from error


def _oid(repository: Path, expression: str, label: str) -> str:
    raw = _read_repository_bytes(
        repository, "rev-parse", "--verify", expression).rstrip(b"\r\n")
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise GitResultError(f"{label} is not an ASCII Git OID") from error
    if _OID_PATTERN.fullmatch(value) is None:
        raise GitResultError(f"{label} is not one full lowercase Git OID")
    return value


def _parse_nul_paths(payload: bytes, label: str) -> tuple[bytes, ...]:
    if not payload:
        return ()
    if not payload.endswith(b"\0"):
        raise GitResultError(f"{label} is not NUL terminated")
    paths = tuple(payload[:-1].split(b"\0"))
    if any(not path or path.startswith(b"/") for path in paths):
        raise GitResultError(f"{label} contains an unsafe path")
    return paths


def _triple_payload(
        triple: GitResultTriple, *, include_digest: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "base_oid": triple.base_oid,
        "base_tree_oid": triple.base_tree_oid,
        "changed_files": [
            base64.b64encode(path).decode("ascii") for path in triple.changed_files
        ],
        "patch_bytes": base64.b64encode(triple.patch_bytes).decode("ascii"),
        "result_oid": triple.result_oid,
        "result_tree_oid": triple.result_tree_oid,
    }
    if include_digest:
        payload["result_digest"] = triple.result_digest
    return payload


def _validated_triple(triple: GitResultTriple) -> GitResultTriple:
    for value, label in (
            (triple.base_oid, "base_oid"),
            (triple.base_tree_oid, "base_tree_oid"),
            (triple.result_oid, "result_oid"),
            (triple.result_tree_oid, "result_tree_oid")):
        if _OID_PATTERN.fullmatch(value) is None:
            raise GitResultError(f"{label} is not a full lowercase Git OID")
    if tuple(sorted(set(triple.changed_files))) != triple.changed_files:
        raise GitResultError("changed files are not unique and byte-sorted")
    if any(not path for path in triple.changed_files):
        raise GitResultError("changed file path is empty")
    expected = _digest(triple.canonical_bytes())
    if validate_sha256_digest(triple.result_digest) != expected:
        raise GitResultError("result digest does not rederive from base, patch, and result")
    return triple


def derive_git_result(
        repository: Path, base_ref: str, result_ref: str) -> GitResultTriple:
    """Derive the byte-preserving direct-child result triple from Git authority."""
    try:
        root = Path(repository).resolve(strict=True)
    except OSError as error:
        raise GitResultError(f"repository is unavailable: {error}") from error
    base_oid = _oid(root, f"{_nonempty(base_ref, 'base_ref')}^{{commit}}", "base")
    result_oid = _oid(root, f"{_nonempty(result_ref, 'result_ref')}^{{commit}}", "result")
    raw_parents = _read_repository_bytes(
        root, "rev-parse", f"{result_oid}^@").splitlines()
    try:
        parents = tuple(line.decode("ascii") for line in raw_parents if line)
    except UnicodeDecodeError as error:
        raise GitResultError("result parent observation is not ASCII") from error
    if parents != (base_oid,):
        raise GitResultError("result commit must be an existing direct child of frozen base")
    base_tree = _oid(root, f"{base_oid}^{{tree}}", "base tree")
    result_tree = _oid(root, f"{result_oid}^{{tree}}", "result tree")
    patch = _read_repository_bytes(
        root, "diff", "--binary", "--full-index", "--no-color", "--no-ext-diff",
        "--no-renames", "--no-textconv",
        base_oid, result_oid, "--",
    )
    changed = tuple(sorted(_parse_nul_paths(_read_repository_bytes(
        root, "diff", "--name-only", "-z", "--no-color", "--no-ext-diff",
        "--no-renames", "--no-textconv", base_oid, result_oid, "--",
    ), "changed-file observation")))
    candidate = GitResultTriple(
        base_oid=base_oid,
        base_tree_oid=base_tree,
        result_oid=result_oid,
        result_tree_oid=result_tree,
        changed_files=changed,
        patch_bytes=patch,
        result_digest="sha256:" + "0" * 64,
    )
    return _validated_triple(GitResultTriple(
        **{**candidate.__dict__, "result_digest": _digest(candidate.canonical_bytes())},
    ))


def _safe_result_path(raw_path: bytes) -> Path:
    if (not raw_path or raw_path.startswith(b"/") or b"\0" in raw_path
            or any(part in (b"", b".", b"..") for part in raw_path.split(b"/"))):
        raise VerifierBindingRefusal("result tree contains an unsafe path")
    return Path(os.fsdecode(raw_path))


def _safe_symlink_target(raw_path: bytes, target: bytes) -> None:
    if not target or target.startswith(b"/") or b"\0" in target:
        raise VerifierBindingRefusal(
            "result tree contains an absolute or empty symlink target")
    parent = posixpath.dirname(raw_path)
    normalized = posixpath.normpath(posixpath.join(parent, target))
    if (normalized == b".." or normalized.startswith(b"../")
            or normalized.startswith(b"/")):
        raise VerifierBindingRefusal(
            "result tree contains a symlink target outside the review root")


def _registered_worktrees(repository: Path) -> tuple[dict[str, str], ...]:
    try:
        returncode, output, error = git_rc(
            repository, "worktree", "list", "--porcelain", "-z")
    except (OSError, UnicodeError) as cause:
        raise VerifierBindingRefusal(
            f"cannot observe registered worktrees: {cause}") from cause
    if returncode != 0 or error:
        raise VerifierBindingRefusal(
            error or f"git worktree list exited {returncode}")
    if not output.endswith("\0\0"):
        raise VerifierBindingRefusal("registered-worktree observation is malformed")
    raw_records = output[:-2].split("\0\0")
    if not raw_records or any(not record for record in raw_records):
        raise VerifierBindingRefusal("registered-worktree observation is empty or malformed")
    records: list[dict[str, str]] = []
    for raw_record in raw_records:
        fields: dict[str, str] = {}
        for field in raw_record.split("\0"):
            key, separator, value = field.partition(" ")
            if not key or key in fields:
                raise VerifierBindingRefusal("registered-worktree record is malformed")
            fields[key] = value if separator else ""
        if (not fields.get("worktree")
                or _OID_PATTERN.fullmatch(fields.get("HEAD", "")) is None):
            raise VerifierBindingRefusal(
                "registered-worktree record lacks path or HEAD")
        records.append(fields)
    return tuple(records)


def _blob_oid(content: bytes, oid_length: int) -> str:
    framed = f"blob {len(content)}\0".encode("ascii") + content
    if oid_length == 40:
        return hashlib.sha1(framed).hexdigest()  # noqa: S324 - Git SHA-1 object format
    if oid_length == 64:
        return hashlib.sha256(framed).hexdigest()
    raise VerifierBindingRefusal("result tree uses an unsupported Git object format")


def _read_result_worktree_entry(
        worktree: Path, raw_path: bytes, mode: str, oid: str) -> bytes:
    path = worktree / _safe_result_path(raw_path)
    try:
        before = path.lstat()
        if mode == "120000":
            if not stat.S_ISLNK(before.st_mode):
                raise VerifierBindingRefusal(
                    "registered result worktree differs from its symlink tree entry")
            content = os.fsencode(os.readlink(path))
            _safe_symlink_target(raw_path, content)
        else:
            if not stat.S_ISREG(before.st_mode):
                raise VerifierBindingRefusal(
                    "registered result worktree differs from its file tree entry")
            executable = bool(before.st_mode & 0o111)
            if executable != (mode == "100755"):
                raise VerifierBindingRefusal(
                    "registered result worktree file mode differs from its tree entry")
            content = path.read_bytes()
        after = path.lstat()
    except VerifierBindingRefusal:
        raise
    except OSError as error:
        raise VerifierBindingRefusal(
            f"cannot read registered result path {os.fsdecode(raw_path)!r}: {error}") from error
    observed = (
        before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns)
    confirmed = (
        after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
    if observed != confirmed or _blob_oid(content, len(oid)) != oid:
        raise VerifierBindingRefusal(
            "registered result worktree changed or differs from the Git result tree")
    return content


def _read_result_object_entry(
        repository: Path, raw_path: bytes, mode: str, oid: str) -> bytes:
    content = _read_repository_bytes(repository, "cat-file", "blob", oid)
    if _blob_oid(content, len(oid)) != oid:
        raise VerifierBindingRefusal(
            "Git result object bytes differ from their tree entry")
    if mode == "120000":
        _safe_symlink_target(raw_path, content)
    return content


def _result_tree_entries(
        repository: Path, result_ref: str, result: GitResultTriple,
        *, require_registered_worktree: bool = True,
        ) -> tuple[tuple[bytes, str, bytes], ...]:
    if not isinstance(require_registered_worktree, bool):
        raise TypeError("require_registered_worktree must be boolean")
    result_worktree = None
    if require_registered_worktree:
        worktrees = _registered_worktrees(repository)
        matches = tuple(record for record in worktrees
                        if record.get("branch") == result_ref
                        and record["HEAD"] == result.result_oid)
        if len(matches) != 1:
            raise VerifierBindingRefusal(
                "exact result ref is not checked out in one registered worker worktree")
        try:
            result_worktree = Path(matches[0]["worktree"]).resolve(strict=True)
        except OSError as error:
            raise VerifierBindingRefusal(
                f"registered result worktree is unavailable: {error}") from error
    raw = _read_repository_bytes(
        repository, "ls-tree", "-r", "-z", "--full-tree", result.result_oid)
    if raw and not raw.endswith(b"\0"):
        raise VerifierBindingRefusal("result tree listing is not NUL terminated")
    entries: list[tuple[bytes, str, bytes]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            raw_mode, raw_kind, raw_oid = header.split(b" ", 2)
            mode = raw_mode.decode("ascii")
            oid = raw_oid.decode("ascii")
        except (ValueError, UnicodeDecodeError) as error:
            raise VerifierBindingRefusal(
                "result tree listing is malformed") from error
        if (raw_kind != b"blob" or mode not in {"100644", "100755", "120000"}
                or _OID_PATTERN.fullmatch(oid) is None):
            raise VerifierBindingRefusal(
                "result tree contains an unsupported entry")
        _safe_result_path(raw_path)
        content = (
            _read_result_worktree_entry(result_worktree, raw_path, mode, oid)
            if result_worktree is not None else
            _read_result_object_entry(repository, raw_path, mode, oid)
        )
        entries.append((raw_path, mode, content))
    paths = tuple(path for path, _mode, _content in entries)
    if len(paths) != len(set(paths)):
        raise VerifierBindingRefusal("result tree contains duplicate paths")
    return tuple(entries)


def _restore_materialized_permissions(root: Path) -> None:
    try:
        root.chmod(0o700)
    except OSError:
        return
    for directory, names, filenames in os.walk(root):
        current = Path(directory)
        for name in names:
            path = current / name
            if not path.is_symlink():
                try:
                    path.chmod(0o700)
                except OSError:
                    pass
        for name in filenames:
            path = current / name
            if not path.is_symlink():
                try:
                    path.chmod(0o600)
                except OSError:
                    pass


@contextmanager
def _materialized_result_root(
        repository: Path, result_ref: str, result: GitResultTriple, *, read_only: bool,
        require_registered_worktree: bool = True):
    entries = _result_tree_entries(
        repository,
        result_ref,
        result,
        require_registered_worktree=require_registered_worktree,
    )
    with tempfile.TemporaryDirectory(prefix="waystone-verify-") as temporary:
        root = Path(temporary)
        try:
            for raw_path, mode, content in entries:
                relative = _safe_result_path(raw_path)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if mode == "120000":
                    os.symlink(os.fsdecode(content), path)
                else:
                    path.write_bytes(content)
                    path.chmod(0o755 if mode == "100755" else 0o644)
            if read_only:
                for directory, names, filenames in os.walk(root, topdown=False):
                    current = Path(directory)
                    for name in filenames:
                        path = current / name
                        if not path.is_symlink():
                            path.chmod(0o555 if os.access(path, os.X_OK) else 0o444)
                    for name in names:
                        path = current / name
                        if not path.is_symlink():
                            path.chmod(0o555)
                root.chmod(0o555)
            yield root
        finally:
            _restore_materialized_permissions(root)


def fingerprint_materialized_root(root: Path) -> str:
    """Digest one isolated verifier root without trusting its pathname."""
    entries: list[dict[str, object]] = []
    try:
        root_info = root.lstat()
        entries.append({
            "kind": "directory",
            "mode": stat.S_IMODE(root_info.st_mode),
            "path": "",
        })
        for directory, names, filenames in os.walk(root):
            current = Path(directory)
            for name in sorted((*names, *filenames), key=os.fsencode):
                path = current / name
                relative = os.fsencode(os.path.relpath(path, root))
                info = path.lstat()
                row: dict[str, object] = {
                    "mode": stat.S_IMODE(info.st_mode),
                    "path": base64.b64encode(relative).decode("ascii"),
                }
                if stat.S_ISLNK(info.st_mode):
                    row.update({
                        "kind": "symlink",
                        "content": base64.b64encode(
                            os.fsencode(os.readlink(path))).decode("ascii"),
                    })
                elif stat.S_ISDIR(info.st_mode):
                    row["kind"] = "directory"
                elif stat.S_ISREG(info.st_mode):
                    row.update({"kind": "file", "content_digest": _digest(path.read_bytes())})
                else:
                    raise OSError(f"unsupported materialized entry {path}")
                entries.append(row)
    except OSError as error:
        raise VerifierMutationRefusal(
            f"cannot re-observe isolated review root: {error}") from error
    entries[1:] = sorted(entries[1:], key=lambda row: str(row["path"]))
    return _digest(_canonical_json(entries))


def _fingerprint_path(root: Path, raw_path: bytes) -> dict[str, object]:
    path = root / os.fsdecode(raw_path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": base64.b64encode(raw_path).decode("ascii"), "state": "missing"}
    except OSError as error:
        raise GitResultError(f"cannot inspect worktree path {path}: {error}") from error
    common = {
        "mode": stat.S_IMODE(info.st_mode),
        "path": base64.b64encode(raw_path).decode("ascii"),
    }
    try:
        if stat.S_ISLNK(info.st_mode):
            content = os.fsencode(os.readlink(path))
            kind = "symlink"
        elif stat.S_ISREG(info.st_mode):
            content = path.read_bytes()
            kind = "file"
        else:
            raise GitResultError(f"worktree path {path} is not a file or symlink")
    except OSError as error:
        raise GitResultError(f"cannot read worktree path {path}: {error}") from error
    return {**common, "content_digest": _digest(content), "kind": kind, "state": "present"}


def fingerprint_worktree(repository: Path) -> WorktreeFingerprint:
    """Hash HEAD, index facts, and tracked/untracked/ignored user bytes read-only."""
    root = Path(repository).resolve(strict=True)
    head = _oid(root, "HEAD^{commit}", "worktree HEAD")
    cached = _parse_nul_paths(
        _read_repository_bytes(root, "ls-files", "-z", "--cached"),
        "cached path observation")
    untracked = _parse_nul_paths(
        _read_repository_bytes(
            root, "ls-files", "-z", "--others", "--exclude-standard"),
        "untracked path observation",
    )
    ignored = _parse_nul_paths(
        _read_repository_bytes(
            root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard"),
        "ignored path observation",
    )
    paths = tuple(sorted({
        path for path in (*cached, *untracked, *ignored)
        if path != b".waystone" and not path.startswith(b".waystone/")
    }))
    payload = {
        "head": head,
        "index": base64.b64encode(
            _read_repository_bytes(
                root, "ls-files", "--stage", "-z")).decode("ascii"),
        "index_flags": base64.b64encode(
            _read_repository_bytes(
                root, "ls-files", "-v", "-z", "--cached")).decode("ascii"),
        "paths": [_fingerprint_path(root, path) for path in paths],
    }
    return WorktreeFingerprint(_digest(_canonical_json(payload)))


def _load_authority(
        run_id: str, root: Path,
        dispatch_loader: Callable[..., DispatchReady],
        ) -> tuple[RunSpec, BaseSnapshot, VerificationPlan, DispatchReady]:
    spec = load_run_spec(run_id, start=root)
    snapshot = read_base_snapshot(run_id, start=root)
    plan = load_verification_plan(run_id, start=root)
    dispatch = dispatch_loader(run_id, start=root)
    if (snapshot.head != spec.base_snapshot.head
            or plan.run_id != spec.run_id or plan.job_id != spec.job_id
            or plan.run_spec_digest != spec.run_spec_digest
            or plan.base_snapshot_digest != spec.base_snapshot.digest
            or dispatch.verification_plan_digest != plan.verification_plan_digest):
        raise EvidenceBindingRefusal("RunSpec, VerificationPlan, and preflight disagree")
    return spec, snapshot, plan, dispatch


def _authority(
        run_id: str, root: Path,
        ) -> tuple[RunSpec, BaseSnapshot, VerificationPlan, DispatchReady]:
    return _load_authority(run_id, root, load_dispatch_ready)


def _terminal_reload_authority(
        run_id: str, root: Path,
        ) -> tuple[RunSpec, BaseSnapshot, VerificationPlan, DispatchReady]:
    return _load_authority(run_id, root, load_terminal_safe_dispatch_authority)


def _verifier_capability(plan: VerificationPlan) -> RoleCapability:
    return RoleCapability(
        binding=plan.binding_for(Role.VERIFIER).binding,
        sandbox=plan.verifier_sandbox,
        accepts_frozen_base=True,
        accepts_patch_bytes=True,
        accepts_result_digest=True,
        emits_artifacts=True,
    )


def _criterion_payload(result: CriterionResult) -> dict[str, object]:
    return {
        "criterion": result.criterion,
        "evidence_digests": list(result.evidence_digests),
        "passed": result.passed,
    }


def _blocker_payload(blocker: VerifierBlocker) -> dict[str, object]:
    return {"blocker_id": blocker.blocker_id, "detail": blocker.detail}


def _actor_payload(actor: ActorIdentity) -> dict[str, str]:
    return {"actor_id": actor.actor_id, "role": actor.role.value}


def _binding_payload(binding: RoleBinding) -> dict[str, str]:
    return {
        "backend": binding.backend,
        "execution_category": binding.execution_category.value,
        "role": binding.role.value,
    }


def _sandbox_payload(sandbox: SandboxContract) -> dict[str, str]:
    return {
        "filesystem": sandbox.filesystem,
        "network": sandbox.network,
        "process": sandbox.process,
    }


def _engine_action_payload(action: EngineCheckAction) -> dict[str, object]:
    return {
        "check_id": action.check_id,
        "child_environment": [{
            "name": item.name,
            "normalization": item.normalization.value,
            "source": item.source.value,
            "value_digest": item.value_digest,
        } for item in action.child_environment],
        "command": list(action.command),
        "command_input_digest": action.command_input_digest,
        "environment_preparation_artifact_digest": (
            action.environment_preparation_artifact_digest),
        "executor_kind": action.executor_kind.value,
        "expected_evidence_kinds": list(action.expected_evidence_kinds),
        "expected_exit_codes": list(action.expected_exit_codes),
        "phase": action.phase.value,
        "prepared_input_digest": action.prepared_input_digest,
        "red_expected_exit_codes": list(action.red_expected_exit_codes),
        "run_id": action.run_id,
        "verification_plan_digest": action.verification_plan_digest,
        "working_directory": action.working_directory.value,
    }


def _verification_invocation_digest(
        *, spec: RunSpec, plan: VerificationPlan, dispatch: DispatchReady,
        actor: ActorIdentity, worker_actor_id: str, result: GitResultTriple,
        verifier_capability: RoleCapability,
        execution_descriptor_digest: str | None = None) -> str:
    authority = {
        "actor": _actor_payload(actor),
        "base_snapshot_digest": spec.base_snapshot.digest,
        "engine_actions": [
            _engine_action_payload(item) for item in dispatch.engine_actions
        ],
        "job_id": spec.job_id,
        "owner_criteria": list(spec.job_input.acceptance_criteria),
        "preflight_evidence_digest": dispatch.preflight_evidence_digest,
        "result_digest": result.result_digest,
        "run_id": spec.run_id,
        "verifier_binding": _binding_payload(verifier_capability.binding),
        "verifier_capability_digest": verifier_capability.probe_artifact_digest,
        "verifier_sandbox": _sandbox_payload(verifier_capability.sandbox),
        "verification_plan_digest": plan.verification_plan_digest,
        "worker_actor_id": worker_actor_id,
    }
    if execution_descriptor_digest is not None:
        authority["execution_descriptor_digest"] = validate_sha256_digest(
            execution_descriptor_digest)
    return _digest(_canonical_json(authority))


def _engine_result_payload(result: EngineCheckResult) -> dict[str, object]:
    return {
        "action_digest": result.action_digest,
        "check_id": result.check_id,
        "command": list(result.command),
        "command_input_digest": result.command_input_digest,
        "evidence_digests": [list(item) for item in result.evidence_digests],
        "exit_code": result.exit_code,
        "expected_exit_codes": list(result.expected_exit_codes),
        "passed": result.passed,
        "prepared_input_digest": result.prepared_input_digest,
    }


def _validated_engine_check_output(
        root: Path, action: EngineCheckAction,
        output: object) -> EngineCheckResult:
    if not isinstance(output, EngineCheckOutput):
        raise InvalidEngineCheckOutput(
            f"check {action.check_id!r} output is absent or not structured")
    if output.check_id != action.check_id:
        raise InvalidEngineCheckOutput(
            f"check {action.check_id!r} returned a different check_id")
    if isinstance(output.exit_code, bool) or not isinstance(output.exit_code, int):
        raise InvalidEngineCheckOutput(
            f"check {action.check_id!r} exit code is not an integer")
    if not isinstance(output.evidence, tuple):
        raise InvalidEngineCheckOutput(
            f"check {action.check_id!r} evidence is not a tuple")
    raw_evidence: dict[str, bytes] = {}
    for item in output.evidence:
        if (not isinstance(item, tuple) or len(item) != 2
                or not isinstance(item[0], str) or not item[0].strip()
                or not isinstance(item[1], bytes)):
            raise InvalidEngineCheckOutput(
                f"check {action.check_id!r} evidence entry is malformed")
        kind, payload = item
        if kind in raw_evidence:
            raise InvalidEngineCheckOutput(
                f"check {action.check_id!r} duplicates evidence kind {kind!r}")
        raw_evidence[kind] = payload
    if set(raw_evidence) != set(action.expected_evidence_kinds):
        raise InvalidEngineCheckOutput(
            f"check {action.check_id!r} lacks the exact frozen evidence kinds")
    artifact_store = ArtifactStore(root)
    digests: list[tuple[str, str]] = []
    for kind in action.expected_evidence_kinds:
        stored = artifact_store.write(raw_evidence[kind])
        if artifact_store.read(stored.digest) != raw_evidence[kind]:
            raise InvalidEngineCheckOutput(
                f"check {action.check_id!r} evidence failed verified publication")
        digests.append((kind, stored.digest))
    action_digest = _digest(_canonical_json(_engine_action_payload(action)))
    return EngineCheckResult(
        check_id=action.check_id,
        action_digest=action_digest,
        command=action.command,
        command_input_digest=action.command_input_digest,
        prepared_input_digest=action.prepared_input_digest,
        exit_code=output.exit_code,
        expected_exit_codes=action.expected_exit_codes,
        passed=output.exit_code in action.expected_exit_codes,
        evidence_digests=tuple(digests),
    )


def _engine_check_evidence_payload(
        *, spec: RunSpec, plan: VerificationPlan, dispatch: DispatchReady,
        attempt_id: str, action_id: str, result_digest: str,
        runner_observation_digest: str, runner_stdout_digest: str,
        runner_stderr_digest: str,
        results: tuple[EngineCheckResult, ...]) -> dict[str, object]:
    return {
        "action_id": action_id,
        "attempt_id": attempt_id,
        "base_snapshot_digest": spec.base_snapshot.digest,
        "job_id": spec.job_id,
        "preflight_evidence_digest": dispatch.preflight_evidence_digest,
        "result_digest": result_digest,
        "results": [_engine_result_payload(item) for item in results],
        "run_id": spec.run_id,
        "run_spec_digest": spec.run_spec_digest,
        "runner_observation_digest": runner_observation_digest,
        "runner_stderr_digest": runner_stderr_digest,
        "runner_stdout_digest": runner_stdout_digest,
        "schema": _ENGINE_CHECK_SCHEMA,
        "verification_plan_digest": plan.verification_plan_digest,
    }


def _validated_verifier_output(
        output: object, *, actor: ActorIdentity, worker_actor_id: str,
        owner_criteria: tuple[str, ...], result_digest: str) -> VerifierOutput:
    if not isinstance(output, VerifierOutput):
        raise InvalidVerifierOutput("verifier output is absent or not structured")
    if actor.role is not Role.VERIFIER or output.actor != actor:
        raise VerifierActorRefusal("output actor is not the selected verifier")
    if actor.actor_id == worker_actor_id:
        raise VerifierActorRefusal("worker and verifier actor identities must differ")
    try:
        observed_digest = validate_sha256_digest(output.result_digest)
    except ValueError as error:
        raise InvalidVerifierOutput(str(error)) from error
    if observed_digest != result_digest:
        raise InvalidVerifierOutput("verifier output names a different result digest")
    if not isinstance(output.summary, str) or not output.summary.strip():
        raise InvalidVerifierOutput("verifier output summary is empty")
    if not isinstance(output.criterion_results, tuple):
        raise InvalidVerifierOutput("verifier criterion results are not a tuple")
    results = output.criterion_results
    if not results:
        raise InvalidVerifierOutput("verifier output contains no criterion results")
    criteria: list[str] = []
    normalized_results: list[CriterionResult] = []
    for result in results:
        if not isinstance(result, CriterionResult):
            raise InvalidVerifierOutput("criterion result is not structured")
        criterion = _nonempty(result.criterion, "criterion")
        if not isinstance(result.passed, bool):
            raise InvalidVerifierOutput("criterion result passed flag is not boolean")
        try:
            if not isinstance(result.evidence_digests, tuple):
                raise ValueError("criterion evidence digests are not a tuple")
            digests = tuple(sorted(validate_sha256_digest(item)
                                   for item in result.evidence_digests))
        except (TypeError, ValueError) as error:
            raise InvalidVerifierOutput(str(error)) from error
        if len(digests) != len(set(digests)):
            raise InvalidVerifierOutput("criterion evidence digests are duplicated")
        criteria.append(criterion)
        normalized_results.append(CriterionResult(criterion, result.passed, digests))
    if len(criteria) != len(set(criteria)) or set(criteria) != set(owner_criteria):
        raise InvalidVerifierOutput("verifier output lacks the exact owner criterion set")
    if not isinstance(output.blockers, tuple):
        raise InvalidVerifierOutput("verifier blockers are not a tuple")
    blockers: list[VerifierBlocker] = []
    for blocker in output.blockers:
        if not isinstance(blocker, VerifierBlocker):
            raise InvalidVerifierOutput("blocker is not structured")
        blockers.append(VerifierBlocker(
            _nonempty(blocker.blocker_id, "blocker_id"),
            _nonempty(blocker.detail, "blocker detail"),
        ))
    if len({item.blocker_id for item in blockers}) != len(blockers):
        raise InvalidVerifierOutput("blocker identifiers are duplicated")
    by_criterion = {item.criterion: item for item in normalized_results}
    return VerifierOutput(
        actor=actor,
        result_digest=result_digest,
        criterion_results=tuple(by_criterion[item] for item in owner_criteria),
        blockers=tuple(sorted(blockers, key=lambda item: item.blocker_id)),
        summary=output.summary,
    )


def _evidence_payload(
        *, spec: RunSpec, plan: VerificationPlan, dispatch: DispatchReady,
        attempt_id: str, action_id: str, worker_actor_id: str,
        output: VerifierOutput, result: GitResultTriple,
        engine_checks: EngineCheckEvidence,
        verifier_capability: RoleCapability,
        runner_observation_digest: str, runner_stdout_digest: str,
        runner_stderr_digest: str,
        execution_descriptor_reference: ArtifactReference | None = None,
        verifier_launch_reference: ArtifactReference | None = None,
        ) -> dict[str, object]:
    if (execution_descriptor_reference is None) != (
            verifier_launch_reference is None):
        raise ValueError(
            "verifier evidence requires descriptor and launch authority together")
    payload = {
        "action_id": action_id,
        "actor": _actor_payload(output.actor),
        "attempt_id": attempt_id,
        "base_snapshot_digest": spec.base_snapshot.digest,
        "blockers": [_blocker_payload(item) for item in output.blockers],
        "criterion_results": [_criterion_payload(item) for item in output.criterion_results],
        "engine_check_artifact_digest": engine_checks.artifact_reference.digest,
        "engine_check_reference_id": engine_checks.artifact_reference.reference_id,
        "job_id": spec.job_id,
        "preflight_evidence_digest": dispatch.preflight_evidence_digest,
        "result": _triple_payload(result),
        "run_id": spec.run_id,
        "run_spec_digest": spec.run_spec_digest,
        "runner_observation_digest": runner_observation_digest,
        "runner_stderr_digest": runner_stderr_digest,
        "runner_stdout_digest": runner_stdout_digest,
        "schema": _EVIDENCE_SCHEMA,
        "summary": output.summary,
        "verifier_binding": _binding_payload(verifier_capability.binding),
        "verifier_capability_digest": verifier_capability.probe_artifact_digest,
        "verifier_sandbox": _sandbox_payload(verifier_capability.sandbox),
        "verification_plan_digest": plan.verification_plan_digest,
        "worker_actor_id": worker_actor_id,
    }
    if execution_descriptor_reference is not None:
        assert verifier_launch_reference is not None
        payload.update({
            "execution_descriptor_digest": execution_descriptor_reference.digest,
            "execution_descriptor_reference_id": (
                execution_descriptor_reference.reference_id),
            "verifier_launch_artifact_digest": verifier_launch_reference.digest,
            "verifier_launch_reference_id": verifier_launch_reference.reference_id,
        })
    return payload


def _record_attempt_reference(
        root: Path, run_id: str, attempt_id: str, *, next_state: str,
        reason: TransitionReason, reference: ArtifactReference) -> None:
    with RunStore.open(root) as store:
        attempt = store.get_entity(EntityKind.ATTEMPT, attempt_id)
        if attempt.run_id != run_id:
            raise EvidenceBindingRefusal("attempt belongs to a different run")
        store.record_transition(
            EntityKind.ATTEMPT,
            attempt_id,
            expected_version=attempt.version,
            next_state=next_state,
            reason=reason,
            evidence_digest=reference.digest,
            artifact_references=(reference,),
        )


def _effect_engine(
        root: Path, *, runner_executor=None, runner_identity_verifier=None,
        ) -> tuple[RunStore, EffectEngine]:
    store = RunStore.open(root)
    return store, EffectEngine(
        store,
        LeaseManager(store),
        runner_executor=runner_executor,
        runner_identity_verifier=runner_identity_verifier,
    )


def _refuse_successful_verifier_retry(
        root: Path, *, spec: RunSpec, plan: VerificationPlan,
        dispatch: DispatchReady, invocation_digest: str,
        retry_of: str | None) -> None:
    run_id = spec.run_id
    job_id = spec.job_id
    lineage_key = _digest(_canonical_json({
        "kind": "runner-execution",
        "run_id": run_id,
        "job_id": job_id,
        "invocation_digest": invocation_digest,
    }))
    lineage_prefix = f"runner-invocation:{lineage_key}:"
    with RunStore.open(root) as store:
        with store._connection_lock:  # noqa: SLF001 - terminal lineage query
            rows = store._connection.execute(  # noqa: SLF001
                "SELECT x.action_id, v.reference_id FROM actions x "
                "JOIN artifacts l ON l.entity_kind = ? AND l.entity_id = x.action_id "
                "JOIN artifacts v ON v.run_id = x.run_id "
                "AND v.reference_id = 'verifier-evidence:' || x.action_id "
                "WHERE x.run_id = ? AND x.job_id = ? "
                "AND l.reference_id = ? || x.action_id "
                "ORDER BY v.transition_id",
                (EntityKind.ACTION.value, run_id, job_id, lineage_prefix),
            ).fetchall()
    published = tuple((
        row["action_id"],
        _load_verifier_evidence(
            root, row["reference_id"], spec=spec, plan=plan, dispatch=dispatch),
    ) for row in rows)
    successful = tuple(
        action_id for action_id, evidence in published
        if (all(item.passed for item in evidence.criterion_results)
            and all(item.passed for item in evidence.engine_checks.results)
            and not evidence.blockers)
    )
    if successful:
        raise EffectRetryRefused(
            successful[-1],
            "published verifier evidence is terminal and cannot be retried",
        )
    if not published:
        return
    raise EffectRetryRefused(
        published[-1][0],
        "semantic verifier rejection is terminal for this candidate; "
        "retry requires a new candidate or owner ruling",
    )


def _verifier_stdout(result: FixtureVerifierResult) -> bytes:
    if isinstance(result.output, VerifierOutput):
        try:
            return _canonical_json({
                "actor": _actor_payload(result.output.actor),
                "blockers": [_blocker_payload(item) for item in result.output.blockers],
                "criterion_results": [
                    _criterion_payload(item) for item in result.output.criterion_results
                ],
                "result_digest": result.output.result_digest,
                "summary": result.output.summary,
            })
        except (AttributeError, TypeError, ValueError):
            return repr(result.output).encode("utf-8", errors="backslashreplace")
    if isinstance(result.output, bytes):
        return result.output
    if result.output is None:
        return b""
    return repr(result.output).encode("utf-8", errors="backslashreplace")


def _verification_transcript(
        result: FixtureVerifierResult,
        engine_results: tuple[EngineCheckResult, ...]) -> bytes:
    return _canonical_json({
        "engine_check_results": [
            _engine_result_payload(item) for item in engine_results
        ],
        "returncode": result.returncode,
        "schema": _VERIFICATION_TRANSCRIPT_SCHEMA,
        "verifier_output": base64.b64encode(
            _verifier_stdout(result)).decode("ascii"),
    })


def execute_verifier(
        run_id: str, attempt_id: str, action_id: str, repository: Path,
        result_ref: str, worker_actor_id: str, actor: ActorIdentity,
        check_executor: EngineCheckExecutor, verifier_adapter: VerifierAdapter, *,
        retry_of: str | None = None,
        start: Path | None = None,
        assurance_plan: AssurancePlan | None = None,
        require_registered_result_worktree: bool = True,
        execution_descriptor_reference_id: str | None = None,
        verifier_launch_reference_id: str | None = None,
        ) -> VerifierEvidence:
    """Serialize one verifier lineage through terminal evidence publication."""
    if assurance_plan is not None:
        if not isinstance(assurance_plan, AssurancePlan):
            raise TypeError("assurance_plan must be an AssurancePlan")
        if assurance_plan.verification.get("independent") != "required":
            raise EvidenceBindingRefusal(
                "frozen stage assurance does not authorize independent verification")
    if not isinstance(require_registered_result_worktree, bool):
        raise TypeError("require_registered_result_worktree must be boolean")
    if (execution_descriptor_reference_id is None) != (
            verifier_launch_reference_id is None):
        raise ValueError(
            "execution descriptor and verifier launch references are required together")
    if execution_descriptor_reference_id is not None:
        _nonempty(
            execution_descriptor_reference_id,
            "execution descriptor reference_id")
        _nonempty(verifier_launch_reference_id, "verifier launch reference_id")
    root = Path(repository).resolve(strict=True)
    authority_root = root if start is None else Path(start).resolve(strict=True)
    if authority_root != root:
        raise EvidenceBindingRefusal(
            "verification repository is not the authority project root")
    with hold_project_lock(root):
        return _execute_verifier_locked(
            run_id,
            attempt_id,
            action_id,
            root,
            result_ref,
            worker_actor_id,
            actor,
            check_executor,
            verifier_adapter,
            retry_of=retry_of,
            start=root,
            require_registered_result_worktree=require_registered_result_worktree,
            execution_descriptor_reference_id=execution_descriptor_reference_id,
            verifier_launch_reference_id=verifier_launch_reference_id,
        )


def _execute_verifier_locked(
        run_id: str, attempt_id: str, action_id: str, repository: Path,
        result_ref: str, worker_actor_id: str, actor: ActorIdentity,
        check_executor: EngineCheckExecutor, verifier_adapter: VerifierAdapter, *,
        retry_of: str | None,
        start: Path,
        require_registered_result_worktree: bool,
        execution_descriptor_reference_id: str | None,
        verifier_launch_reference_id: str | None,
        ) -> VerifierEvidence:
    """Run frozen engine checks and publish evidence while holding the project lock."""
    root = Path(repository).resolve(strict=True)
    authority_root = Path(start).resolve(strict=True)
    if authority_root != root:
        raise EvidenceBindingRefusal("verification repository is not the authority project root")
    if not isinstance(actor, ActorIdentity) or actor.role is not Role.VERIFIER:
        raise VerifierActorRefusal("selected actor is not a verifier")
    worker_identity = _nonempty(worker_actor_id, "worker_actor_id")
    if actor.actor_id == worker_identity:
        raise VerifierActorRefusal("worker cannot verify its own result")
    if not callable(check_executor):
        raise TypeError("check_executor must be callable")
    if not isinstance(verifier_adapter, VerifierAdapter):
        raise TypeError("verifier_adapter must be a VerifierAdapter")

    spec, snapshot, plan, dispatch = _authority(run_id, root)
    if (spec.lifecycle_stage.value == "promote"
            and execution_descriptor_reference_id is None):
        raise EvidenceBindingRefusal(
            "promotion verification requires frozen execution descriptor "
            "and launch authority")
    verifier_capability = _verifier_capability(plan)
    if (verifier_adapter.binding != verifier_capability.binding
            or verifier_adapter.sandbox != verifier_capability.sandbox):
        raise VerifierBindingRefusal(
            "fixture verifier adapter differs from the frozen preflight binding or sandbox")
    result = derive_git_result(root, spec.base_snapshot.head, result_ref)
    before = fingerprint_worktree(root)
    execution_descriptor = None
    execution_descriptor_reference = None
    if execution_descriptor_reference_id is not None:
        execution_descriptor, execution_descriptor_reference = (
            _load_execution_descriptor(
                root,
                execution_descriptor_reference_id,
                spec=spec,
                plan=plan,
            ))
    invocation_digest = _verification_invocation_digest(
        spec=spec,
        plan=plan,
        dispatch=dispatch,
        actor=actor,
        worker_actor_id=worker_identity,
        result=result,
        verifier_capability=verifier_capability,
        execution_descriptor_digest=(
            None if execution_descriptor is None else execution_descriptor.digest),
    )
    _refuse_successful_verifier_retry(
        root,
        spec=spec,
        plan=plan,
        dispatch=dispatch,
        invocation_digest=invocation_digest,
        retry_of=retry_of,
    )
    captured: list[FixtureVerifierResult] = []
    captured_checks: list[EngineCheckResult] = []
    check_refusals: list[InvalidEngineCheckOutput] = []
    execution_failures: list[tuple[str, Exception]] = []
    mutation_attempts: list[Exception] = []
    captured_stdout_digests: list[str] = []
    captured_stderr_digests: list[str] = []

    def run_fixture(intent) -> None:
        try:
            with _materialized_result_root(
                    root, result_ref, result, read_only=False,
                    require_registered_worktree=(
                        require_registered_result_worktree)) as execution_root:
                results = tuple(_validated_engine_check_output(
                    root,
                    action,
                    check_executor(EngineCheckRequest(
                        action=action,
                        base_snapshot=snapshot,
                        base_snapshot_digest=spec.base_snapshot.digest,
                        execution_root=execution_root,
                        result=result,
                    )),
                ) for action in dispatch.engine_actions)
            if tuple(item.check_id for item in results) != tuple(
                    item.check_id for item in dispatch.engine_actions):
                raise InvalidEngineCheckOutput(
                    "engine check output does not cover the exact frozen action set")
            captured_checks.extend(results)
            with _materialized_result_root(
                    root, result_ref, result, read_only=True,
                    require_registered_worktree=(
                        require_registered_result_worktree)) as review_root:
                review_before = fingerprint_materialized_root(review_root)
                try:
                    request = VerifierRequest(
                        run_id=run_id,
                        job_id=spec.job_id,
                        verification_plan_digest=plan.verification_plan_digest,
                        owner_criteria=spec.job_input.acceptance_criteria,
                        base_snapshot=snapshot,
                        base_snapshot_digest=spec.base_snapshot.digest,
                        review_root=review_root,
                        review_root_fingerprint=review_before,
                        engine_check_results=results,
                        verifier_binding=verifier_capability.binding,
                        verifier_sandbox=verifier_capability.sandbox,
                        verifier_capability_digest=(
                            verifier_capability.probe_artifact_digest),
                        result=result,
                    )
                    response = verifier_adapter.executor(request)
                finally:
                    if fingerprint_materialized_root(review_root) != review_before:
                        mutation_attempts.append(VerifierMutationRefusal(
                            "fixture verifier changed its isolated review root"))
        except (FrozenInstanceError, PermissionError, VerifierMutationRefusal) as error:
            mutation_attempts.append(error)
            response = FixtureVerifierResult(
                returncode=1,
                output=None,
                stderr=str(error).encode("utf-8", errors="backslashreplace"),
            )
        except InvalidEngineCheckOutput as error:
            check_refusals.append(error)
            response = FixtureVerifierResult(
                returncode=1,
                output=None,
                stderr=str(error).encode("utf-8", errors="backslashreplace"),
            )
        except Exception as error:  # fixture failure is a failed runner result, never evidence
            phase = "verifier" if captured_checks else "engine-check"
            execution_failures.append((phase, error))
            response = FixtureVerifierResult(
                returncode=1,
                output=None,
                stderr=str(error).encode("utf-8", errors="backslashreplace"),
            )
        if not isinstance(response, FixtureVerifierResult):
            response = FixtureVerifierResult(
                returncode=1,
                output=response,
                stderr=b"executor did not return FixtureVerifierResult",
            )
        returncode = response.returncode
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            response = FixtureVerifierResult(
                returncode=1,
                output=response.output,
                stderr=b"executor returncode is not an integer",
            )
        if not isinstance(response.stderr, bytes):
            response = FixtureVerifierResult(
                returncode=1,
                output=response.output,
                stderr=b"executor stderr is not bytes",
            )
        if response.returncode == 0:
            try:
                canonical_output = _validated_verifier_output(
                    response.output,
                    actor=actor,
                    worker_actor_id=worker_identity,
                    owner_criteria=spec.job_input.acceptance_criteria,
                    result_digest=result.result_digest,
                )
            except (VerifyError, AttributeError, TypeError, ValueError):
                pass
            else:
                response = FixtureVerifierResult(
                    returncode=0,
                    output=canonical_output,
                    stderr=response.stderr,
                )
        captured.append(response)
        artifacts = ArtifactStore(root)
        stdout = artifacts.write(_verification_transcript(
            response, tuple(captured_checks)))
        stderr = artifacts.write(response.stderr)
        captured_stdout_digests.append(stdout.digest)
        captured_stderr_digests.append(stderr.digest)
        publish_runner_completion(
            intent.completion_marker_path,
            RunnerCompletionMarker(
                run_id=intent.run_id,
                job_id=intent.job_id,
                action_id=intent.action_id,
                fencing_epoch=intent.fencing_epoch,
                launch_token=intent.launch_token,
                process_identity=f"fixture-verification:{intent.action_id}",
                started_at="fixture-started",
                finished_at="fixture-finished",
                returncode=response.returncode,
                signal=None,
                stdout_artifact_digest=stdout.digest,
                stderr_artifact_digest=stderr.digest,
            ),
        )

    def identity_matches(marker: RunnerCompletionMarker) -> bool:
        return marker.process_identity == f"fixture-verification:{marker.action_id}"

    store, effects = _effect_engine(
        root,
        runner_executor=run_fixture,
        runner_identity_verifier=identity_matches,
    )
    try:
        effect = RunnerExecutionEffect(invocation_digest)
        plan_effect = (
            effects.plan_effect(
                run_id, spec.job_id, attempt_id, action_id, effect)
            if retry_of is None else
            effects.plan_retry_effect(
                retry_of,
                run_id=run_id,
                job_id=spec.job_id,
                attempt_id=attempt_id,
                action_id=action_id,
                effect=effect,
            )
        )
        claimed = effects.claim_effect(plan_effect, ttl_seconds=30)
        effect_result = effects.execute_effect(claimed)
    finally:
        store.close()
    after = fingerprint_worktree(root)
    if before != after:
        raise VerifierMutationRefusal(
            "review worktree or index changed while verifier ran; no evidence was published")
    if derive_git_result(root, spec.base_snapshot.head, result_ref) != result:
        raise VerifierBindingRefusal(
            "result ref changed while verification ran; no evidence was published")
    if mutation_attempts:
        raise VerifierMutationRefusal(
            "fixture verifier attempted to mutate its immutable review input")
    if effect_result.state is not EffectResultState.COMPLETED or len(captured) != 1:
        raise VerifierExecutionFailed(
            effect_result.reason or "verifier runner lacks positive completion evidence")
    if (effect_result.observed_digest is None
            or len(captured_stdout_digests) != 1
            or len(captured_stderr_digests) != 1):
        raise VerifierExecutionFailed(
            "verifier runner lacks bound output observation evidence")
    if check_refusals:
        raise check_refusals[0]
    if execution_failures and not captured_checks:
        raise EngineCheckExecutionFailed(
            f"engine check fixture failed: {execution_failures[0][1]}")
    if len(captured_checks) != len(dispatch.engine_actions):
        raise EngineCheckExecutionFailed(
            "engine checks did not produce the exact frozen action set")
    verifier_launch_reference = None
    if verifier_launch_reference_id is not None:
        if execution_descriptor is None or execution_descriptor_reference is None:
            raise EvidenceBindingRefusal(
                "verifier launch authority lacks its execution descriptor")
        verifier_launch_reference = _load_verifier_launch(
            root,
            verifier_launch_reference_id,
            spec=spec,
            attempt_id=attempt_id,
            action_id=action_id,
            result=result,
            descriptor=execution_descriptor,
            descriptor_reference=execution_descriptor_reference,
        )

    check_payload = _canonical_json(_engine_check_evidence_payload(
        spec=spec,
        plan=plan,
        dispatch=dispatch,
        attempt_id=attempt_id,
        action_id=action_id,
        result_digest=result.result_digest,
        runner_observation_digest=effect_result.observed_digest,
        runner_stdout_digest=captured_stdout_digests[0],
        runner_stderr_digest=captured_stderr_digests[0],
        results=tuple(captured_checks),
    ))
    stored_checks = ArtifactStore(root).write(check_payload)
    check_reference = ArtifactReference(
        reference_id=f"engine-check-evidence:{action_id}",
        kind=ArtifactReferenceKind.EVIDENCE,
        digest=stored_checks.digest,
        size=stored_checks.size,
    )
    _record_attempt_reference(
        root,
        run_id,
        attempt_id,
        next_state="engine-checks-recorded",
        reason=TransitionReason.EFFECT_OBSERVED,
        reference=check_reference,
    )
    engine_checks = EngineCheckEvidence(
        run_id=run_id,
        job_id=spec.job_id,
        attempt_id=attempt_id,
        action_id=action_id,
        run_spec_digest=spec.run_spec_digest,
        base_snapshot_digest=spec.base_snapshot.digest,
        verification_plan_digest=plan.verification_plan_digest,
        preflight_evidence_digest=dispatch.preflight_evidence_digest,
        result_digest=result.result_digest,
        runner_observation_digest=effect_result.observed_digest,
        runner_stdout_digest=captured_stdout_digests[0],
        runner_stderr_digest=captured_stderr_digests[0],
        results=tuple(captured_checks),
        artifact_reference=check_reference,
    )
    response = captured[0]
    if response.returncode != 0:
        raise VerifierExecutionFailed(
            f"verifier exited {response.returncode}; no evidence was published")
    try:
        output = _validated_verifier_output(
            response.output,
            actor=actor,
            worker_actor_id=worker_identity,
            owner_criteria=spec.job_input.acceptance_criteria,
            result_digest=result.result_digest,
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise InvalidVerifierOutput(str(error)) from error
    payload = _canonical_json(_evidence_payload(
        spec=spec,
        plan=plan,
        dispatch=dispatch,
        attempt_id=attempt_id,
        action_id=action_id,
        worker_actor_id=worker_identity,
        output=output,
        result=result,
        engine_checks=engine_checks,
        verifier_capability=verifier_capability,
        runner_observation_digest=effect_result.observed_digest,
        runner_stdout_digest=captured_stdout_digests[0],
        runner_stderr_digest=captured_stderr_digests[0],
        execution_descriptor_reference=execution_descriptor_reference,
        verifier_launch_reference=verifier_launch_reference,
    ))
    stored = ArtifactStore(root).write(payload)
    reference = ArtifactReference(
        reference_id=f"verifier-evidence:{action_id}",
        kind=ArtifactReferenceKind.EVIDENCE,
        digest=stored.digest,
        size=stored.size,
    )
    _record_attempt_reference(
        root,
        run_id,
        attempt_id,
        next_state="verification-recorded",
        reason=TransitionReason.EFFECT_OBSERVED,
        reference=reference,
    )
    return VerifierEvidence(
        run_id=run_id,
        job_id=spec.job_id,
        attempt_id=attempt_id,
        action_id=action_id,
        worker_actor_id=worker_identity,
        actor=actor,
        run_spec_digest=spec.run_spec_digest,
        verification_plan_digest=plan.verification_plan_digest,
        preflight_evidence_digest=dispatch.preflight_evidence_digest,
        engine_checks=engine_checks,
        verifier_binding=verifier_capability.binding,
        verifier_sandbox=verifier_capability.sandbox,
        verifier_capability_digest=verifier_capability.probe_artifact_digest,
        runner_observation_digest=effect_result.observed_digest,
        runner_stdout_digest=captured_stdout_digests[0],
        runner_stderr_digest=captured_stderr_digests[0],
        result=result,
        criterion_results=output.criterion_results,
        blockers=output.blockers,
        summary=output.summary,
        artifact_reference=reference,
        execution_descriptor_reference=execution_descriptor_reference,
        verifier_launch_reference=verifier_launch_reference,
    )


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise EvidenceBindingRefusal(f"{label} fields are not canonical")
    return value


def _decode_b64(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise EvidenceBindingRefusal(f"{label} is not base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as error:
        raise EvidenceBindingRefusal(f"{label} is invalid base64") from error


def _parse_triple(value: object) -> GitResultTriple:
    row = _exact_keys(value, {
        "base_oid", "base_tree_oid", "changed_files", "patch_bytes",
        "result_digest", "result_oid", "result_tree_oid",
    }, "result triple")
    raw_paths = row["changed_files"]
    if not isinstance(raw_paths, list):
        raise EvidenceBindingRefusal("changed_files is not a list")
    try:
        triple = GitResultTriple(
            base_oid=str(row["base_oid"]),
            base_tree_oid=str(row["base_tree_oid"]),
            result_oid=str(row["result_oid"]),
            result_tree_oid=str(row["result_tree_oid"]),
            changed_files=tuple(_decode_b64(item, "changed path") for item in raw_paths),
            patch_bytes=_decode_b64(row["patch_bytes"], "patch bytes"),
            result_digest=validate_sha256_digest(row["result_digest"]),  # type: ignore[arg-type]
        )
        return _validated_triple(triple)
    except (TypeError, ValueError, GitResultError) as error:
        raise EvidenceBindingRefusal(str(error)) from error


def _parse_actor(value: object) -> ActorIdentity:
    row = _exact_keys(value, {"actor_id", "role"}, "actor")
    try:
        return ActorIdentity(str(row["actor_id"]), Role(row["role"]))
    except (TypeError, ValueError) as error:
        raise EvidenceBindingRefusal(str(error)) from error


def _parse_criterion(value: object) -> CriterionResult:
    row = _exact_keys(
        value, {"criterion", "evidence_digests", "passed"}, "criterion result")
    raw_digests = row["evidence_digests"]
    if not isinstance(raw_digests, list) or not isinstance(row["passed"], bool):
        raise EvidenceBindingRefusal("criterion result values are malformed")
    try:
        digests = tuple(validate_sha256_digest(item) for item in raw_digests)
    except ValueError as error:
        raise EvidenceBindingRefusal(str(error)) from error
    if tuple(sorted(set(digests))) != digests:
        raise EvidenceBindingRefusal("criterion evidence digests are not canonical")
    return CriterionResult(
        _nonempty(row["criterion"], "criterion"), row["passed"], digests)


def _parse_blocker(value: object) -> VerifierBlocker:
    row = _exact_keys(value, {"blocker_id", "detail"}, "blocker")
    return VerifierBlocker(
        _nonempty(row["blocker_id"], "blocker_id"),
        _nonempty(row["detail"], "blocker detail"),
    )


def _parse_binding(value: object) -> RoleBinding:
    row = _exact_keys(
        value, {"backend", "execution_category", "role"}, "verifier binding")
    try:
        return RoleBinding(
            role=Role(row["role"]),
            execution_category=ExecutionCategory(row["execution_category"]),
            backend=_nonempty(row["backend"], "verifier backend"),
        )
    except (TypeError, ValueError) as error:
        raise EvidenceBindingRefusal(str(error)) from error


def _parse_sandbox(value: object) -> SandboxContract:
    row = _exact_keys(
        value, {"filesystem", "network", "process"}, "verifier sandbox")
    try:
        return SandboxContract(
            filesystem=_nonempty(row["filesystem"], "sandbox filesystem"),
            process=_nonempty(row["process"], "sandbox process"),
            network=_nonempty(row["network"], "sandbox network"),
        )
    except (TypeError, ValueError) as error:
        raise EvidenceBindingRefusal(str(error)) from error


def _load_attributed_evidence(
        root: Path, reference_id: str, *, run_id: str,
        entity_kind: EntityKind, entity_id: str,
        expected_digest: str | None = None,
        ) -> tuple[ArtifactReference, bytes]:
    try:
        identity = _nonempty(reference_id, "artifact reference_id")
        with RunStore.open(root) as store:
            reference = store.get_artifact_reference(identity)
            with store._connection_lock:  # noqa: SLF001 - package provenance query
                row = store._connection.execute(  # noqa: SLF001
                    "SELECT run_id, entity_kind, entity_id, reference_kind, digest, size "
                    "FROM artifacts WHERE reference_id = ?",
                    (identity,),
                ).fetchone()
        payload = ArtifactStore(root).read_reference(reference)
        expected = (
            reference.digest if expected_digest is None
            else validate_sha256_digest(expected_digest)
        )
    except (TypeError, ValueError, WorkflowError) as error:
        raise EvidenceBindingRefusal(
            f"cannot load execution authority {reference_id!r}: {error}") from error
    if (row is None
            or reference.kind is not ArtifactReferenceKind.EVIDENCE
            or reference.digest != expected
            or row["run_id"] != run_id
            or row["entity_kind"] != entity_kind.value
            or row["entity_id"] != entity_id
            or row["reference_kind"] != ArtifactReferenceKind.EVIDENCE.value
            or row["digest"] != reference.digest
            or row["size"] != reference.size):
        raise EvidenceBindingRefusal(
            f"execution authority {reference_id!r} lacks expected attribution")
    return reference, payload


def _load_execution_descriptor(
        root: Path, reference_id: str, *, spec: RunSpec,
        plan: VerificationPlan, expected_digest: str | None = None,
        ) -> tuple[RunnerExecutionDescriptor, ArtifactReference]:
    expected_reference_id = f"promotion-execution-descriptor:{spec.run_id}"
    if reference_id != expected_reference_id:
        raise EvidenceBindingRefusal(
            "execution descriptor reference is not the run's start-time authority")
    reference, payload = _load_attributed_evidence(
        root,
        reference_id,
        run_id=spec.run_id,
        entity_kind=EntityKind.RUN,
        entity_id=spec.run_id,
        expected_digest=expected_digest,
    )
    try:
        descriptor = parse_runner_execution_descriptor(
            payload, expected_digest=reference.digest)
    except RunnerExecutionDescriptorRefusal as error:
        raise EvidenceBindingRefusal(str(error)) from error
    binding = plan.binding_for(Role.VERIFIER).binding
    binding_digest = _digest(_canonical_json(_binding_payload(binding)))
    if (descriptor.verifier_backend != binding.backend
            or descriptor.verifier_binding_digest != binding_digest):
        raise EvidenceBindingRefusal(
            "execution descriptor differs from the frozen verifier binding")
    return descriptor, reference


def _load_verifier_launch(
        root: Path, reference_id: str, *, spec: RunSpec,
        attempt_id: str, action_id: str, result: GitResultTriple,
        descriptor: RunnerExecutionDescriptor,
        descriptor_reference: ArtifactReference,
        expected_digest: str | None = None,
        ) -> ArtifactReference:
    reference, payload = _load_attributed_evidence(
        root,
        reference_id,
        run_id=spec.run_id,
        entity_kind=EntityKind.ATTEMPT,
        entity_id=attempt_id,
        expected_digest=expected_digest,
    )
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceBindingRefusal(
            f"promotion verifier launch is not JSON: {error}") from error
    row = _exact_keys(decoded, {
        "action_id", "attempt_id", "candidate_oid", "cwd",
        "environment_digest", "executable_content_digest",
        "execution_descriptor_digest", "execution_descriptor_reference_id",
        "invocation_digest", "job_id", "resolved_executable",
        "root_fingerprint", "run_id", "run_spec_digest", "schema",
    }, "promotion verifier launch")
    try:
        validate_sha256_digest(row["invocation_digest"])  # type: ignore[arg-type]
        validate_sha256_digest(row["root_fingerprint"])  # type: ignore[arg-type]
    except ValueError as error:
        raise EvidenceBindingRefusal(str(error)) from error
    cwd = row["cwd"]
    if (reference.reference_id != f"verifier-launch:{action_id}"
            or row["schema"] != "waystone-promotion-verifier-launch-3"
            or row["run_id"] != spec.run_id
            or row["job_id"] != spec.job_id
            or row["attempt_id"] != attempt_id
            or row["action_id"] != action_id
            or row["candidate_oid"] != result.result_oid
            or row["run_spec_digest"] != spec.run_spec_digest
            or not isinstance(cwd, str) or not Path(cwd).is_absolute()
            or row["execution_descriptor_reference_id"]
            != descriptor_reference.reference_id
            or row["execution_descriptor_digest"] != descriptor.digest
            or row["environment_digest"] != descriptor.environment_digest
            or row["resolved_executable"] != descriptor.resolved_executable
            or row["executable_content_digest"]
            != descriptor.executable_content_digest
            or payload != _canonical_json(row)):
        raise EvidenceBindingRefusal(
            "promotion verifier launch differs from execution descriptor authority")
    return reference


def _require_runner_observation(
        root: Path, *, run_id: str, job_id: str, action_id: str,
        observation_digest: str, stdout_digest: str, stderr_digest: str) -> None:
    try:
        observed = validate_sha256_digest(observation_digest)
        stdout = validate_sha256_digest(stdout_digest)
        stderr = validate_sha256_digest(stderr_digest)
        suffix = observed.split(":", 1)[1]
        reference_id = f"effect-observation:{action_id}:{suffix}"
        with RunStore.open(root) as store:
            reference = store.get_artifact_reference(reference_id)
            payload = ArtifactStore(root).read_reference(reference)
            with store._connection_lock:  # noqa: SLF001 - provenance receipt query
                attribution = store._connection.execute(  # noqa: SLF001
                    "SELECT a.run_id, a.entity_kind, a.entity_id, a.digest, a.size, "
                    "t.next_state, t.reason, t.evidence_digest "
                    "FROM artifacts a JOIN transitions t "
                    "ON t.transition_id = a.transition_id "
                    "WHERE a.reference_id = ?",
                    (reference_id,),
                ).fetchone()
        decoded = json.loads(payload.decode("utf-8"))
        row = _exact_keys(decoded, {
            "action_id", "evidence", "job_id", "kind", "observed_digest",
            "run_id", "schema",
        }, "runner observation receipt")
        evidence = _exact_keys(row["evidence"], {
            "completion_marker", "marker", "stderr_size", "stdout_size",
        }, "runner observation evidence")
        marker = _exact_keys(evidence["marker"], {
            "action_id", "fencing_epoch", "finished_at", "job_id", "launch_token",
            "process_identity", "returncode", "run_id", "schema", "signal",
            "started_at", "stderr_artifact_digest", "stdout_artifact_digest",
        }, "runner completion marker")
        stdout_bytes = ArtifactStore(root).read(stdout)
        stderr_bytes = ArtifactStore(root).read(stderr)
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError,
            WorkflowError) as error:
        if isinstance(error, EvidenceBindingRefusal):
            raise
        raise EvidenceBindingRefusal(
            f"runner observation receipt is unavailable or malformed: {error}") from error
    expected_observed = _digest(_canonical_json({
        "action_id": action_id,
        "evidence": evidence,
        "kind": "runner-execution",
    }))
    if (reference.kind is not ArtifactReferenceKind.EVIDENCE
            or attribution is None or attribution["run_id"] != run_id
            or attribution["entity_kind"] != EntityKind.ACTION.value
            or attribution["entity_id"] != action_id
            or attribution["digest"] != reference.digest
            or attribution["size"] != reference.size
            or attribution["next_state"] != "observed"
            or attribution["reason"] != TransitionReason.EFFECT_OBSERVED.value
            or attribution["evidence_digest"] != observed
            or row["schema"] != _EFFECT_OBSERVATION_SCHEMA
            or row["run_id"] != run_id or row["job_id"] != job_id
            or row["action_id"] != action_id or row["kind"] != "runner-execution"
            or row["observed_digest"] != observed
            or observed != expected_observed
            or marker["run_id"] != run_id or marker["job_id"] != job_id
            or marker["action_id"] != action_id
            or marker["process_identity"] != f"fixture-verification:{action_id}"
            or marker["returncode"] != 0 or marker["signal"] is not None
            or marker["stdout_artifact_digest"] != stdout
            or marker["stderr_artifact_digest"] != stderr
            or evidence["stdout_size"] != len(stdout_bytes)
            or evidence["stderr_size"] != len(stderr_bytes)
            or payload != _canonical_json(decoded)):
        raise EvidenceBindingRefusal(
            "runner observation receipt is not bound to exact completed output")


def _require_reference_attribution(
        root: Path, reference: ArtifactReference, *, run_id: str,
        job_id: str, attempt_id: str, producer_action_id: str,
        expected_kind: ArtifactReferenceKind, expected_effect_kind: str,
        expected_invocation_digest: str | None = None,
        expected_content: bytes | None = None,
        expected_content_digest: str | None = None,
        runner_observation_digest: str | None = None,
        runner_stdout_digest: str | None = None,
        runner_stderr_digest: str | None = None) -> bytes | None:
    supplied_inputs = sum(value is not None for value in (
        expected_invocation_digest, expected_content, expected_content_digest))
    if supplied_inputs > 1:
        raise ValueError("producer attribution accepts one expected effect input")
    runner_outputs = (
        runner_observation_digest, runner_stdout_digest, runner_stderr_digest)
    if any(value is not None for value in runner_outputs) and not all(
            value is not None for value in runner_outputs):
        raise ValueError("runner attribution requires observation, stdout, and stderr")
    with RunStore.open(root) as store:
        action = store.get_entity(EntityKind.ACTION, producer_action_id)
        plan_reference = store.get_artifact_reference(
            f"effect-plan:{producer_action_id}")
        try:
            plan_payload = json.loads(
                ArtifactStore(root).read_reference(plan_reference).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise EvidenceBindingRefusal(
                f"producer action {producer_action_id!r} has invalid plan evidence") from error
        with store._connection_lock:  # noqa: SLF001 - package-internal provenance query
            row = store._connection.execute(  # noqa: SLF001
                "SELECT run_id, entity_kind, entity_id, reference_kind, digest, size "
                "FROM artifacts WHERE reference_id = ?",
                (reference.reference_id,),
            ).fetchone()
    spec = plan_payload.get("spec") if isinstance(plan_payload, dict) else None
    input_matches = isinstance(spec, dict)
    if input_matches and expected_invocation_digest is not None:
        input_matches = spec.get("invocation_digest") == expected_invocation_digest
    effect_content: bytes | None = None
    if input_matches and (expected_content is not None
                          or expected_content_digest is not None):
        try:
            encoded = spec["content_base64"]  # type: ignore[index]
            if not isinstance(encoded, str):
                raise TypeError("content_base64 is not text")
            effect_content = base64.b64decode(encoded, validate=True)
            content_digest = _digest(effect_content)
            input_matches = (
                (expected_content is None or effect_content == expected_content)
                and (expected_content_digest is None
                     or content_digest == expected_content_digest)
                and spec.get("content_digest") == content_digest
                and spec.get("size") == len(effect_content)
            )
        except (KeyError, TypeError, ValueError):
            input_matches = False
    if (row is None or row["run_id"] != run_id
            or row["entity_kind"] != EntityKind.ATTEMPT.value
            or row["entity_id"] != attempt_id
            or row["reference_kind"] != expected_kind.value
            or row["digest"] != reference.digest or row["size"] != reference.size
            or action.run_id != run_id or action.parent_job_id != job_id
            or action.parent_attempt_id != attempt_id
            or action.state != "completed" or not isinstance(plan_payload, dict)
            or plan_payload.get("schema") != _EFFECT_PLAN_SCHEMA
            or plan_payload.get("run_id") != run_id
            or plan_payload.get("job_id") != job_id
            or plan_payload.get("attempt_id") != attempt_id
            or plan_payload.get("action_id") != producer_action_id
            or plan_payload.get("kind") != expected_effect_kind
            or not input_matches):
        raise EvidenceBindingRefusal(
            f"artifact reference {reference.reference_id!r} lacks expected producer provenance")
    if runner_observation_digest is not None:
        _require_runner_observation(
            root,
            run_id=run_id,
            job_id=job_id,
            action_id=producer_action_id,
            observation_digest=runner_observation_digest,
            stdout_digest=runner_stdout_digest,  # type: ignore[arg-type]
            stderr_digest=runner_stderr_digest,  # type: ignore[arg-type]
        )
    return effect_content


def _parse_engine_result(
        root: Path, value: object, action: EngineCheckAction) -> EngineCheckResult:
    row = _exact_keys(value, {
        "action_digest", "check_id", "command", "command_input_digest",
        "evidence_digests", "exit_code", "expected_exit_codes", "passed",
        "prepared_input_digest",
    }, "engine check result")
    evidence = row["evidence_digests"]
    if (not isinstance(row["command"], list)
            or not all(isinstance(item, str) for item in row["command"])
            or not isinstance(row["expected_exit_codes"], list)
            or not isinstance(evidence, list)
            or isinstance(row["exit_code"], bool)
            or not isinstance(row["exit_code"], int)
            or not isinstance(row["passed"], bool)):
        raise EvidenceBindingRefusal("engine check result values are malformed")
    try:
        parsed_evidence = tuple(
            (_nonempty(item[0], "engine evidence kind"),
             validate_sha256_digest(item[1]))
            for item in evidence
            if isinstance(item, list) and len(item) == 2
        )
    except (TypeError, ValueError) as error:
        raise EvidenceBindingRefusal(str(error)) from error
    if len(parsed_evidence) != len(evidence):
        raise EvidenceBindingRefusal("engine evidence digest entries are malformed")
    expected_action_digest = _digest(_canonical_json(_engine_action_payload(action)))
    expected_kinds = action.expected_evidence_kinds
    if (row["check_id"] != action.check_id
            or row["action_digest"] != expected_action_digest
            or tuple(row["command"]) != action.command
            or row["command_input_digest"] != action.command_input_digest
            or row["prepared_input_digest"] != action.prepared_input_digest
            or tuple(row["expected_exit_codes"]) != action.expected_exit_codes
            or tuple(kind for kind, _digest_value in parsed_evidence) != expected_kinds
            or row["passed"] != (row["exit_code"] in action.expected_exit_codes)):
        raise EvidenceBindingRefusal(
            f"engine result for {action.check_id!r} differs from its frozen action")
    artifact_store = ArtifactStore(root)
    try:
        for _kind, digest in parsed_evidence:
            artifact_store.read(digest)
    except WorkflowError as error:
        raise EvidenceBindingRefusal(
            f"engine result for {action.check_id!r} has unavailable evidence: {error}") from error
    return EngineCheckResult(
        check_id=action.check_id,
        action_digest=expected_action_digest,
        command=action.command,
        command_input_digest=action.command_input_digest,
        prepared_input_digest=action.prepared_input_digest,
        exit_code=row["exit_code"],
        expected_exit_codes=action.expected_exit_codes,
        passed=row["passed"],
        evidence_digests=parsed_evidence,
    )


def _load_engine_check_evidence(
        root: Path, reference_id: str, *, spec: RunSpec,
        plan: VerificationPlan, dispatch: DispatchReady,
        result_digest: str, invocation_digest: str) -> EngineCheckEvidence:
    try:
        with RunStore.open(root) as store:
            reference = store.get_artifact_reference(reference_id)
            payload = ArtifactStore(root).read_reference(reference)
    except WorkflowError as error:
        raise EvidenceBindingRefusal(
            f"cannot load engine check evidence: {error}") from error
    if reference.kind is not ArtifactReferenceKind.EVIDENCE:
        raise EvidenceBindingRefusal("engine check reference is not EVIDENCE")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceBindingRefusal(f"engine check evidence is not JSON: {error}") from error
    row = _exact_keys(decoded, {
        "action_id", "attempt_id", "base_snapshot_digest", "job_id",
        "preflight_evidence_digest", "result_digest", "results", "run_id",
        "run_spec_digest", "runner_observation_digest", "runner_stderr_digest",
        "runner_stdout_digest", "schema", "verification_plan_digest",
    }, "engine check evidence")
    raw_results = row["results"]
    if not isinstance(raw_results, list) or len(raw_results) != len(dispatch.engine_actions):
        raise EvidenceBindingRefusal("engine check evidence lacks the exact frozen action set")
    results = tuple(
        _parse_engine_result(root, value, action)
        for value, action in zip(raw_results, dispatch.engine_actions)
    )
    attempt_id = _nonempty(row["attempt_id"], "engine check attempt_id")
    action_id = _nonempty(row["action_id"], "engine check action_id")
    try:
        runner_observation_digest = validate_sha256_digest(
            row["runner_observation_digest"])
        runner_stdout_digest = validate_sha256_digest(row["runner_stdout_digest"])
        runner_stderr_digest = validate_sha256_digest(row["runner_stderr_digest"])
    except ValueError as error:
        raise EvidenceBindingRefusal(str(error)) from error
    if (reference.reference_id != f"engine-check-evidence:{action_id}"
            or row["schema"] != _ENGINE_CHECK_SCHEMA
            or row["run_id"] != spec.run_id or row["job_id"] != spec.job_id
            or row["run_spec_digest"] != spec.run_spec_digest
            or row["base_snapshot_digest"] != spec.base_snapshot.digest
            or row["verification_plan_digest"] != plan.verification_plan_digest
            or row["preflight_evidence_digest"] != dispatch.preflight_evidence_digest
            or row["result_digest"] != result_digest):
        raise EvidenceBindingRefusal(
            "engine check evidence is not bound to current authority and result")
    normalized = _engine_check_evidence_payload(
        spec=spec,
        plan=plan,
        dispatch=dispatch,
        attempt_id=attempt_id,
        action_id=action_id,
        result_digest=result_digest,
        runner_observation_digest=runner_observation_digest,
        runner_stdout_digest=runner_stdout_digest,
        runner_stderr_digest=runner_stderr_digest,
        results=results,
    )
    if payload != _canonical_json(normalized):
        raise EvidenceBindingRefusal("engine check evidence bytes are not canonical")
    _require_reference_attribution(
        root, reference, run_id=spec.run_id, job_id=spec.job_id,
        attempt_id=attempt_id,
        producer_action_id=action_id,
        expected_kind=ArtifactReferenceKind.EVIDENCE,
        expected_effect_kind="runner-execution",
        expected_invocation_digest=invocation_digest,
        runner_observation_digest=runner_observation_digest,
        runner_stdout_digest=runner_stdout_digest,
        runner_stderr_digest=runner_stderr_digest)
    return EngineCheckEvidence(
        run_id=spec.run_id,
        job_id=spec.job_id,
        attempt_id=attempt_id,
        action_id=action_id,
        run_spec_digest=spec.run_spec_digest,
        base_snapshot_digest=spec.base_snapshot.digest,
        verification_plan_digest=plan.verification_plan_digest,
        preflight_evidence_digest=dispatch.preflight_evidence_digest,
        result_digest=result_digest,
        runner_observation_digest=runner_observation_digest,
        runner_stdout_digest=runner_stdout_digest,
        runner_stderr_digest=runner_stderr_digest,
        results=results,
        artifact_reference=reference,
    )


def _load_verifier_evidence(
        root: Path, reference_id: str, *, spec: RunSpec,
        plan: VerificationPlan, dispatch: DispatchReady) -> VerifierEvidence:
    try:
        with RunStore.open(root) as store:
            reference = store.get_artifact_reference(reference_id)
            payload = ArtifactStore(root).read_reference(reference)
    except WorkflowError as error:
        raise EvidenceBindingRefusal(
            f"cannot load verifier evidence: {error}") from error
    if reference.kind is not ArtifactReferenceKind.EVIDENCE:
        raise EvidenceBindingRefusal("verifier reference is not EVIDENCE")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceBindingRefusal(f"verifier evidence is not JSON: {error}") from error
    evidence_fields = {
        "action_id", "actor", "attempt_id", "base_snapshot_digest", "blockers",
        "criterion_results", "engine_check_artifact_digest",
        "engine_check_reference_id", "job_id", "preflight_evidence_digest",
        "result", "run_id", "run_spec_digest", "runner_observation_digest",
        "runner_stderr_digest", "runner_stdout_digest", "schema", "summary",
        "verification_plan_digest", "verifier_binding",
        "verifier_capability_digest", "verifier_sandbox", "worker_actor_id",
    }
    execution_fields = {
        "execution_descriptor_digest", "execution_descriptor_reference_id",
        "verifier_launch_artifact_digest", "verifier_launch_reference_id",
    }
    if isinstance(decoded, dict) and set(decoded) & execution_fields:
        evidence_fields |= execution_fields
    row = _exact_keys(decoded, evidence_fields, "verifier evidence")
    if row["schema"] != _EVIDENCE_SCHEMA or row["run_id"] != spec.run_id:
        raise EvidenceBindingRefusal("verifier evidence schema or run identity is invalid")
    raw_criteria = row["criterion_results"]
    raw_blockers = row["blockers"]
    if not isinstance(raw_criteria, list) or not isinstance(raw_blockers, list):
        raise EvidenceBindingRefusal("verifier evidence collections are malformed")
    result = _parse_triple(row["result"])
    actor = _parse_actor(row["actor"])
    binding = _parse_binding(row["verifier_binding"])
    sandbox = _parse_sandbox(row["verifier_sandbox"])
    capability = _verifier_capability(plan)
    try:
        capability_digest = validate_sha256_digest(row["verifier_capability_digest"])
        check_digest = validate_sha256_digest(row["engine_check_artifact_digest"])
        runner_observation_digest = validate_sha256_digest(
            row["runner_observation_digest"])
        runner_stdout_digest = validate_sha256_digest(row["runner_stdout_digest"])
        runner_stderr_digest = validate_sha256_digest(row["runner_stderr_digest"])
    except ValueError as error:
        raise EvidenceBindingRefusal(str(error)) from error
    check_reference_id = _nonempty(
        row["engine_check_reference_id"], "engine check reference_id")
    worker_actor_id = _nonempty(row["worker_actor_id"], "worker_actor_id")
    attempt_id = _nonempty(row["attempt_id"], "verifier attempt_id")
    action_id = _nonempty(row["action_id"], "verifier action_id")
    execution_descriptor_reference = None
    verifier_launch_reference = None
    execution_descriptor = None
    raw_execution_authority = (
        row.get("execution_descriptor_reference_id"),
        row.get("execution_descriptor_digest"),
        row.get("verifier_launch_reference_id"),
        row.get("verifier_launch_artifact_digest"),
    )
    if (spec.lifecycle_stage.value == "promote"
            and not any(item is not None for item in raw_execution_authority)):
        raise EvidenceBindingRefusal(
            "promotion verifier evidence lacks mandatory execution authority")
    if any(item is not None for item in raw_execution_authority):
        if not all(isinstance(item, str) and item for item in raw_execution_authority):
            raise EvidenceBindingRefusal(
                "verifier evidence execution authority is incomplete")
        try:
            descriptor_digest = validate_sha256_digest(
                raw_execution_authority[1])
            launch_digest = validate_sha256_digest(raw_execution_authority[3])
        except ValueError as error:
            raise EvidenceBindingRefusal(str(error)) from error
        execution_descriptor, execution_descriptor_reference = (
            _load_execution_descriptor(
                root,
                raw_execution_authority[0],
                spec=spec,
                plan=plan,
                expected_digest=descriptor_digest,
            ))
        verifier_launch_reference = _load_verifier_launch(
            root,
            raw_execution_authority[2],
            spec=spec,
            attempt_id=attempt_id,
            action_id=action_id,
            result=result,
            descriptor=execution_descriptor,
            descriptor_reference=execution_descriptor_reference,
            expected_digest=launch_digest,
        )
    invocation_digest = _verification_invocation_digest(
        spec=spec,
        plan=plan,
        dispatch=dispatch,
        actor=actor,
        worker_actor_id=worker_actor_id,
        result=result,
        verifier_capability=capability,
        execution_descriptor_digest=(
            None if execution_descriptor is None else execution_descriptor.digest),
    )
    engine_checks = _load_engine_check_evidence(
        root,
        check_reference_id,
        spec=spec,
        plan=plan,
        dispatch=dispatch,
        result_digest=result.result_digest,
        invocation_digest=invocation_digest,
    )
    criteria = tuple(_parse_criterion(item) for item in raw_criteria)
    blockers = tuple(_parse_blocker(item) for item in raw_blockers)
    if (reference.reference_id != f"verifier-evidence:{action_id}"
            or actor.role is not Role.VERIFIER or actor.actor_id == worker_actor_id
            or row["job_id"] != spec.job_id
            or row["run_spec_digest"] != spec.run_spec_digest
            or row["base_snapshot_digest"] != spec.base_snapshot.digest
            or row["verification_plan_digest"] != plan.verification_plan_digest
            or row["preflight_evidence_digest"] != dispatch.preflight_evidence_digest
            or binding != capability.binding or sandbox != capability.sandbox
            or capability_digest != capability.probe_artifact_digest
            or check_reference_id != engine_checks.artifact_reference.reference_id
            or check_digest != engine_checks.artifact_reference.digest
            or engine_checks.attempt_id != attempt_id
            or engine_checks.action_id != action_id
            or engine_checks.runner_observation_digest != runner_observation_digest
            or engine_checks.runner_stdout_digest != runner_stdout_digest
            or engine_checks.runner_stderr_digest != runner_stderr_digest
            or tuple(item.criterion for item in criteria)
            != spec.job_input.acceptance_criteria
            or len({item.blocker_id for item in blockers}) != len(blockers)
            or not isinstance(row["summary"], str) or not row["summary"].strip()):
        raise EvidenceBindingRefusal("verifier evidence is not bound to current authority")
    normalized = _evidence_payload(
        spec=spec,
        plan=plan,
        dispatch=dispatch,
        attempt_id=attempt_id,
        action_id=action_id,
        worker_actor_id=worker_actor_id,
        output=VerifierOutput(actor, result.result_digest, criteria, blockers, row["summary"]),
        result=result,
        engine_checks=engine_checks,
        verifier_capability=capability,
        runner_observation_digest=runner_observation_digest,
        runner_stdout_digest=runner_stdout_digest,
        runner_stderr_digest=runner_stderr_digest,
        execution_descriptor_reference=execution_descriptor_reference,
        verifier_launch_reference=verifier_launch_reference,
    )
    if payload != _canonical_json(normalized):
        raise EvidenceBindingRefusal("verifier evidence bytes are not canonical")
    expected_transcript = _verification_transcript(
        FixtureVerifierResult(
            returncode=0,
            output=VerifierOutput(
                actor, result.result_digest, criteria, blockers, row["summary"]),
        ),
        engine_checks.results,
    )
    try:
        observed_transcript = ArtifactStore(root).read(runner_stdout_digest)
    except WorkflowError as error:
        raise EvidenceBindingRefusal(
            f"verifier transcript is unavailable: {error}") from error
    if observed_transcript != expected_transcript:
        raise EvidenceBindingRefusal(
            "verifier evidence differs from the observed runner transcript")
    _require_reference_attribution(
        root, reference, run_id=spec.run_id, job_id=spec.job_id,
        attempt_id=attempt_id,
        producer_action_id=action_id,
        expected_kind=ArtifactReferenceKind.EVIDENCE,
        expected_effect_kind="runner-execution",
        expected_invocation_digest=invocation_digest,
        runner_observation_digest=runner_observation_digest,
        runner_stdout_digest=runner_stdout_digest,
        runner_stderr_digest=runner_stderr_digest)
    return VerifierEvidence(
        run_id=spec.run_id,
        job_id=spec.job_id,
        attempt_id=attempt_id,
        action_id=action_id,
        worker_actor_id=worker_actor_id,
        actor=actor,
        run_spec_digest=spec.run_spec_digest,
        verification_plan_digest=plan.verification_plan_digest,
        preflight_evidence_digest=dispatch.preflight_evidence_digest,
        engine_checks=engine_checks,
        verifier_binding=capability.binding,
        verifier_sandbox=capability.sandbox,
        verifier_capability_digest=capability.probe_artifact_digest,
        runner_observation_digest=runner_observation_digest,
        runner_stdout_digest=runner_stdout_digest,
        runner_stderr_digest=runner_stderr_digest,
        result=result,
        criterion_results=criteria,
        blockers=blockers,
        summary=row["summary"],
        artifact_reference=reference,
        execution_descriptor_reference=execution_descriptor_reference,
        verifier_launch_reference=verifier_launch_reference,
    )


def reload_verifier_evidence(
        run_id: str, attempt_id: str, action_id: str, *,
        start: Path | None = None) -> VerifierEvidence:
    """Reload and fully revalidate one published terminal verifier result."""
    root = Path.cwd().resolve() if start is None else Path(start).resolve(strict=True)
    expected_attempt = _nonempty(attempt_id, "attempt_id")
    expected_action = _nonempty(action_id, "action_id")
    spec, _snapshot, plan, dispatch = _terminal_reload_authority(run_id, root)
    evidence = _load_verifier_evidence(
        root,
        f"verifier-evidence:{expected_action}",
        spec=spec,
        plan=plan,
        dispatch=dispatch,
    )
    if (evidence.attempt_id != expected_attempt
            or evidence.action_id != expected_action):
        raise EvidenceBindingRefusal(
            "verifier evidence does not belong to the expected terminal lineage")
    return evidence


def _override_payload(value: BlockerOverride) -> dict[str, str]:
    return {
        "blocker_id": value.blocker_id,
        "check_id": value.check_id,
        "evidence_digest": value.evidence_digest,
    }


def _decision_lineage_key(spec: RunSpec, decision: DecisionInput) -> str:
    return _digest(_canonical_json({
        "candidate_digest": decision.candidate_digest,
        "evaluation_evidence_digest": decision.evaluation_evidence_digest,
        "job_id": spec.job_id,
        "reviewer_artifact_digests": list(decision.reviewer_artifact_digests),
        "run_id": spec.run_id,
        "verifier_reference_id": decision.verifier_reference_id,
    }))


def _decision_intent_payload(
        *, spec: RunSpec, attempt_id: str, action_id: str,
        decision: DecisionInput) -> dict[str, object]:
    outcome = DecisionOutcome(decision.outcome)
    return {
        "action_id": action_id,
        "actor": _actor_payload(decision.actor),
        "attempt_id": attempt_id,
        "blocker_overrides": [
            _override_payload(item) for item in decision.blocker_overrides
        ],
        "criteria": list(decision.criteria),
        "candidate_digest": decision.candidate_digest,
        "decision_lineage_key": _decision_lineage_key(spec, decision),
        "engine_check_artifact_digest": decision.engine_check_artifact_digest,
        "engine_check_reference_id": decision.engine_check_reference_id,
        "evaluation_evidence_digest": decision.evaluation_evidence_digest,
        "job_id": spec.job_id,
        "outcome": outcome.value,
        "result_digest": decision.result_digest,
        "reviewer_artifact_digests": list(decision.reviewer_artifact_digests),
        "run_id": spec.run_id,
        "schema": _DECISION_INTENT_SCHEMA,
        "verifier_artifact_digest": decision.verifier_artifact_digest,
        "verifier_reference_id": decision.verifier_reference_id,
    }


def _decision_payload(
        *, spec: RunSpec, attempt_id: str, action_id: str,
        decision: DecisionInput, producer_effect_digest: str) -> dict[str, object]:
    payload = _decision_intent_payload(
        spec=spec,
        attempt_id=attempt_id,
        action_id=action_id,
        decision=decision,
    )
    payload["producer_effect_digest"] = validate_sha256_digest(
        producer_effect_digest)
    payload["schema"] = _DECISION_SCHEMA
    return payload


def _validate_decision(
        root: Path, spec: RunSpec, evidence: VerifierEvidence,
        decision: DecisionInput) -> tuple[DecisionOutcome, tuple[BlockerOverride, ...]]:
    owner = spec.job_input.acceptance_criteria
    if not isinstance(decision.criteria, tuple) or not all(
            isinstance(item, str) for item in decision.criteria):
        raise ExtraCriterionRefusal("decision criteria are not a string tuple")
    supplied = decision.criteria
    missing = tuple(item for item in owner if item not in supplied)
    if missing:
        raise MissingCriterionRefusal("missing owner criteria: " + ", ".join(missing))
    extra = tuple(item for item in supplied if item not in owner)
    if extra or len(supplied) != len(owner) or len(set(supplied)) != len(supplied):
        detail = extra or ("duplicate criterion",)
        raise ExtraCriterionRefusal("extra owner criteria: " + ", ".join(detail))
    try:
        decision_digest = validate_sha256_digest(decision.result_digest)
        evidence_digest = validate_sha256_digest(decision.verifier_artifact_digest)
        engine_digest = validate_sha256_digest(decision.engine_check_artifact_digest)
    except ValueError as error:
        raise DecisionResultDigestRefusal(str(error)) from error
    if decision_digest != evidence.result.result_digest:
        raise DecisionResultDigestRefusal("decision names a different result digest")
    if (decision.verifier_reference_id != evidence.artifact_reference.reference_id
            or evidence_digest != evidence.artifact_reference.digest):
        raise DecisionResultDigestRefusal("decision names different verifier evidence")
    if (decision.engine_check_reference_id
            != evidence.engine_checks.artifact_reference.reference_id
            or engine_digest != evidence.engine_checks.artifact_reference.digest):
        raise DecisionResultDigestRefusal("decision names different engine check evidence")
    stage = spec.lifecycle_stage.value
    reviewer_actor_ids: tuple[str, ...] = ()
    if stage == "promote":
        candidate = spec.candidate
        evaluation = spec.evaluation
        evidence_ref = None if evaluation is None else evaluation.get("evidence")
        if not isinstance(candidate, dict) or not isinstance(evidence_ref, dict):
            raise DecisionResultDigestRefusal(
                "promotion decision requires frozen candidate and evaluation evidence")
        try:
            candidate_digest = validate_sha256_digest(decision.candidate_digest)
            evaluation_digest = validate_sha256_digest(
                decision.evaluation_evidence_digest)
            reviewer_digests = tuple(validate_sha256_digest(item)
                                     for item in decision.reviewer_artifact_digests)
        except (TypeError, ValueError) as error:
            raise DecisionResultDigestRefusal(str(error)) from error
        if (candidate_digest != candidate.get("digest")
                or evaluation_digest != evidence_ref.get("digest")
                or len(reviewer_digests) != len(set(reviewer_digests))):
            raise DecisionResultDigestRefusal(
                "promotion decision names different candidate, evaluation, or reviewer evidence")
        plan = parse_assurance_plan_bytes(
            ArtifactStore(root).read(spec.assurance_plan.digest))
        if plan.requires("adversarial-review") != bool(reviewer_digests):
            raise DecisionResultDigestRefusal(
                "promotion decision reviewer evidence differs from frozen review requirement")
        if plan.requires("adversarial-review") and len(reviewer_digests) != 1:
            raise DecisionResultDigestRefusal(
                "promotion decision requires exactly one attached review-cycle artifact")
        reviewers = tuple(parse_reviewer_evidence_bytes(
            ArtifactStore(root).read(digest)) for digest in reviewer_digests)
        if any(
                reviewer.promotion_lineage_id
                != plan.review.get("promotion_lineage_id")
                or reviewer.target_run_spec_digest != spec.run_spec_digest
                or reviewer.candidate_digest != candidate_digest
                or reviewer.target_result_digest
                != candidate.get("producer_result_digest")
                or reviewer.digest != digest
                for reviewer, digest in zip(reviewers, reviewer_digests)):
            raise DecisionResultDigestRefusal(
                "promotion decision reviewer evidence names a different lineage or result")
        reviewer_actor_ids = tuple(
            reviewer.actor["actor_id"] for reviewer in reviewers)
    elif (decision.candidate_digest is not None
            or decision.evaluation_evidence_digest is not None
            or decision.reviewer_artifact_digests):
        raise DecisionResultDigestRefusal(
            "non-promotion decision cannot carry promotion evidence inputs")
    actor = decision.actor
    if (not isinstance(actor, ActorIdentity) or actor.role is not Role.COORDINATOR
            or actor.actor_id == evidence.worker_actor_id
            or actor.actor_id == evidence.actor.actor_id
            or actor.actor_id in reviewer_actor_ids
            or evidence.actor.actor_id in reviewer_actor_ids):
        raise DecisionActorRefusal(
            "integration decision must be a distinct coordinator, never the worker/verifier")
    try:
        outcome = DecisionOutcome(decision.outcome)
    except (TypeError, ValueError) as error:
        raise DecisionActorRefusal("decision outcome is not canonical") from error
    check_results = {item.check_id: item for item in evidence.engine_checks.results}
    overrides: list[BlockerOverride] = []
    for override in decision.blocker_overrides:
        if not isinstance(override, BlockerOverride):
            raise BlockerOverrideRefusal("blocker override is not structured")
        blocker_id = _nonempty(override.blocker_id, "override blocker_id")
        check_id = _nonempty(override.check_id, "override check_id")
        check_result = check_results.get(check_id)
        if check_result is None or not check_result.passed:
            raise BlockerOverrideRefusal(
                f"blocker {blocker_id!r} is not grounded in a passing engine check")
        try:
            digest = validate_sha256_digest(override.evidence_digest)
            if digest not in {
                    value for _kind, value in check_result.evidence_digests}:
                raise BlockerOverrideRefusal(
                    f"blocker {blocker_id!r} override is absent from check {check_id!r}")
            ArtifactStore(root).read(digest)
        except BlockerOverrideRefusal:
            raise
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise BlockerOverrideRefusal(
                f"blocker {blocker_id!r} has no verified override evidence") from error
        overrides.append(BlockerOverride(blocker_id, check_id, digest))
    blocker_ids = {item.blocker_id for item in evidence.blockers}
    override_ids = {item.blocker_id for item in overrides}
    failed = tuple(item.criterion for item in evidence.criterion_results if not item.passed)
    failed_checks = tuple(
        item.check_id for item in evidence.engine_checks.results if not item.passed)
    if outcome is DecisionOutcome.ACCEPT:
        if failed_checks:
            raise EngineCheckFailedRefusal(
                "failed engine checks cannot be accepted: " + ", ".join(failed_checks))
        if failed:
            raise BlockerOverrideRefusal(
                "failed owner criteria cannot be overridden: " + ", ".join(failed))
        if blocker_ids != override_ids or len(overrides) != len(override_ids):
            raise BlockerOverrideRefusal(
                "accept must ground an exact override for every verifier blocker")
    elif overrides:
        raise BlockerOverrideRefusal("reject decisions cannot claim blocker overrides")
    return outcome, tuple(sorted(overrides, key=lambda item: item.blocker_id))


def _decision_lineage_actions(
        root: Path, spec: RunSpec, lineage_key: str,
        ) -> tuple[tuple[str, str, str, bool], ...]:
    run_id = spec.run_id
    with RunStore.open(root) as store:
        with store._connection_lock:  # noqa: SLF001 - package-internal lineage query
            rows = store._connection.execute(  # noqa: SLF001
                "SELECT x.action_id, x.attempt_id, x.state, a.digest "
                "FROM actions x JOIN artifacts a "
                "ON a.entity_kind = ? AND a.entity_id = x.action_id "
                "WHERE x.run_id = ? AND a.reference_id = 'effect-plan:' || x.action_id "
                "ORDER BY a.transition_id",
                (EntityKind.ACTION.value, run_id),
            ).fetchall()
            decisions = {
                row["action_id"] for row in store._connection.execute(  # noqa: SLF001
                    "SELECT substr(reference_id, length('integration-decision:') + 1) "
                    "AS action_id FROM artifacts WHERE run_id = ? "
                    "AND reference_id LIKE 'integration-decision:%'",
                    (run_id,),
                ).fetchall()
            }
    matches: list[tuple[str, str, str, bool]] = []
    artifact_store = ArtifactStore(root)
    for row in rows:
        try:
            envelope = json.loads(artifact_store.read(row["digest"]).decode("utf-8"))
            if (not isinstance(envelope, dict)
                    or envelope.get("schema") != _EFFECT_PLAN_SCHEMA
                    or envelope.get("run_id") != run_id
                    or envelope.get("action_id") != row["action_id"]
                    or envelope.get("attempt_id") != row["attempt_id"]):
                raise ValueError("effect plan identity is malformed")
            if envelope.get("kind") != "artifact-write":
                continue
            effect_spec = envelope.get("spec")
            if (not isinstance(effect_spec, dict)
                    or set(effect_spec) != {
                        "content_base64", "content_digest", "size"}):
                raise ValueError("artifact effect spec is not an object")
            encoded = effect_spec["content_base64"]
            if not isinstance(encoded, str):
                raise TypeError("artifact effect content is not base64 text")
            content = base64.b64decode(encoded, validate=True)
            if (effect_spec["content_digest"] != _digest(content)
                    or effect_spec["size"] != len(content)):
                raise ValueError("artifact effect content metadata does not rederive")
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise EvidenceBindingRefusal(
                f"cannot establish decision retry lineage: {error}") from error
        try:
            candidate = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        if (not isinstance(candidate, dict)
                or candidate.get("schema") != _DECISION_INTENT_SCHEMA):
            continue
        try:
            candidate, candidate_input, candidate_attempt, candidate_action = (
                _parse_decision_document(
                    candidate,
                    schema=_DECISION_INTENT_SCHEMA,
                    producer_digest=False,
                    label="integration decision intent",
                )
            )
            candidate_lineage = validate_sha256_digest(
                candidate["decision_lineage_key"])  # type: ignore[arg-type]
            derived_lineage = _decision_lineage_key(spec, candidate_input)
            if (candidate["run_id"] != run_id
                    or candidate_action != row["action_id"]
                    or candidate_attempt != row["attempt_id"]
                    or candidate["job_id"] != spec.job_id
                    or candidate["job_id"] != envelope.get("job_id")
                    or candidate_lineage != derived_lineage):
                raise ValueError("decision intent identity differs from its effect plan")
            if derived_lineage != lineage_key:
                continue
        except (TypeError, ValueError, VerifyError) as error:
            raise EvidenceBindingRefusal(
                f"cannot establish decision retry lineage: {error}") from error
        action_id = row["action_id"]
        matches.append((
            action_id,
            row["attempt_id"],
            row["state"],
            action_id in decisions,
        ))
    return tuple(matches)


def _validate_decision_retry_lineage(
        root: Path, spec: RunSpec, decision: DecisionInput,
        retry_of: str | None) -> None:
    lineage_key = _decision_lineage_key(spec, decision)
    matches = _decision_lineage_actions(root, spec, lineage_key)
    if not matches:
        if retry_of is not None:
            raise EffectRetryRefused(
                retry_of, "retry lineage does not name the same integration decision")
        return
    published = tuple(item for item in matches if item[3])
    if published:
        raise EffectRetryRefused(
            published[-1][0],
            "a published integration decision is terminal and cannot be retried",
        )
    if retry_of is None:
        raise EffectRetryRefused(
            matches[-1][0], "a repeated integration decision requires explicit retry lineage")
    prior = next((item for item in matches if item[0] == retry_of), None)
    if prior is None:
        raise EffectRetryRefused(
            retry_of, "retry lineage does not name the same integration decision")


def record_integration_decision(
        run_id: str, attempt_id: str, action_id: str,
        decision_input: DecisionInput, *, retry_of: str | None = None,
        start: Path | None = None) -> IntegrationDecision:
    """Validate and append one coordinator decision bound to exact verifier evidence."""
    root = Path.cwd().resolve() if start is None else Path(start).resolve(strict=True)
    with hold_project_lock(root):
        return _record_integration_decision_locked(
            run_id,
            attempt_id,
            action_id,
            decision_input,
            retry_of=retry_of,
            start=root,
        )


def _record_integration_decision_locked(
        run_id: str, attempt_id: str, action_id: str,
        decision_input: DecisionInput, *, retry_of: str | None,
        start: Path) -> IntegrationDecision:
    root = Path(start).resolve(strict=True)
    if not isinstance(decision_input, DecisionInput):
        raise TypeError("decision_input must be a DecisionInput")
    spec, _snapshot, plan, dispatch = _authority(run_id, root)
    _validate_decision_retry_lineage(root, spec, decision_input, retry_of)
    candidate_payload = _canonical_json(_decision_intent_payload(
        spec=spec,
        attempt_id=attempt_id,
        action_id=action_id,
        decision=decision_input,
    ))
    store, effects = _effect_engine(root)
    try:
        effect = ArtifactWriteEffect(candidate_payload)
        effect_plan = (
            effects.plan_effect(
                run_id, spec.job_id, attempt_id, action_id, effect)
            if retry_of is None else
            effects.plan_retry_effect(
                retry_of,
                run_id=run_id,
                job_id=spec.job_id,
                attempt_id=attempt_id,
                action_id=action_id,
                effect=effect,
            )
        )
        claimed = effects.claim_effect(effect_plan, ttl_seconds=30)
        effect_result = effects.execute_effect(claimed)
    finally:
        store.close()
    if effect_result.state is not EffectResultState.COMPLETED:
        raise EvidenceBindingRefusal(
            effect_result.reason or "decision artifact effect did not complete")
    evidence = _load_verifier_evidence(
        root,
        decision_input.verifier_reference_id,
        spec=spec,
        plan=plan,
        dispatch=dispatch,
    )
    outcome, overrides = _validate_decision(root, spec, evidence, decision_input)
    normalized = DecisionInput(
        actor=decision_input.actor,
        outcome=outcome,
        criteria=spec.job_input.acceptance_criteria,
        result_digest=evidence.result.result_digest,
        verifier_reference_id=evidence.artifact_reference.reference_id,
        verifier_artifact_digest=evidence.artifact_reference.digest,
        engine_check_reference_id=evidence.engine_checks.artifact_reference.reference_id,
        engine_check_artifact_digest=evidence.engine_checks.artifact_reference.digest,
        blocker_overrides=overrides,
        candidate_digest=decision_input.candidate_digest,
        evaluation_evidence_digest=decision_input.evaluation_evidence_digest,
        reviewer_artifact_digests=decision_input.reviewer_artifact_digests,
    )
    producer_effect_digest = _digest(candidate_payload)
    normalized_payload = _canonical_json(_decision_payload(
        spec=spec,
        attempt_id=attempt_id,
        action_id=action_id,
        decision=normalized,
        producer_effect_digest=producer_effect_digest,
    ))
    stored = ArtifactStore(root).write(normalized_payload)
    reference = ArtifactReference(
        reference_id=f"integration-decision:{action_id}",
        kind=ArtifactReferenceKind.DECISION,
        digest=stored.digest,
        size=stored.size,
    )
    _record_attempt_reference(
        root,
        run_id,
        attempt_id,
        next_state="decision-recorded",
        reason=TransitionReason.COMPLETED,
        reference=reference,
    )
    return IntegrationDecision(
        run_id=run_id,
        job_id=spec.job_id,
        attempt_id=attempt_id,
        action_id=action_id,
        actor=normalized.actor,
        outcome=outcome,
        criteria=normalized.criteria,
        result_digest=normalized.result_digest,
        verifier_reference_id=normalized.verifier_reference_id,
        verifier_artifact_digest=normalized.verifier_artifact_digest,
        engine_check_reference_id=normalized.engine_check_reference_id,
        engine_check_artifact_digest=normalized.engine_check_artifact_digest,
        blocker_overrides=overrides,
        producer_effect_digest=producer_effect_digest,
        artifact_reference=reference,
        candidate_digest=normalized.candidate_digest,
        evaluation_evidence_digest=normalized.evaluation_evidence_digest,
        reviewer_artifact_digests=normalized.reviewer_artifact_digests,
    )


def _parse_override(value: object) -> BlockerOverride:
    row = _exact_keys(
        value, {"blocker_id", "check_id", "evidence_digest"}, "blocker override")
    try:
        return BlockerOverride(
            _nonempty(row["blocker_id"], "override blocker_id"),
            _nonempty(row["check_id"], "override check_id"),
            validate_sha256_digest(row["evidence_digest"]),  # type: ignore[arg-type]
        )
    except ValueError as error:
        raise ApplyBindingRefusal(str(error)) from error


def _parse_decision_document(
        value: object, *, schema: str, producer_digest: bool,
        label: str) -> tuple[dict[str, object], DecisionInput, str, str]:
    expected = {
        "action_id", "actor", "attempt_id", "blocker_overrides", "candidate_digest",
        "criteria",
        "decision_lineage_key", "engine_check_artifact_digest",
        "engine_check_reference_id", "evaluation_evidence_digest", "job_id", "outcome",
        "result_digest", "reviewer_artifact_digests", "run_id", "schema",
        "verifier_artifact_digest", "verifier_reference_id",
    }
    if producer_digest:
        expected.add("producer_effect_digest")
    try:
        row = _exact_keys(value, expected, label)
        if row["schema"] != schema:
            raise ApplyBindingRefusal(f"{label} schema is invalid")
        raw_criteria = row["criteria"]
        raw_overrides = row["blocker_overrides"]
        raw_reviewers = row["reviewer_artifact_digests"]
        if not isinstance(raw_criteria, list) or not all(
                isinstance(item, str) for item in raw_criteria):
            raise ApplyBindingRefusal(f"{label} criteria are malformed")
        if not isinstance(raw_overrides, list):
            raise ApplyBindingRefusal(f"{label} overrides are malformed")
        if not isinstance(raw_reviewers, list):
            raise ApplyBindingRefusal(f"{label} reviewer evidence is malformed")
        decision_input = DecisionInput(
            actor=_parse_actor(row["actor"]),
            outcome=DecisionOutcome(row["outcome"]),
            criteria=tuple(raw_criteria),
            result_digest=validate_sha256_digest(  # type: ignore[arg-type]
                row["result_digest"]),
            verifier_reference_id=_nonempty(
                row["verifier_reference_id"], "verifier reference_id"),
            verifier_artifact_digest=validate_sha256_digest(  # type: ignore[arg-type]
                row["verifier_artifact_digest"]),
            engine_check_reference_id=_nonempty(
                row["engine_check_reference_id"], "engine check reference_id"),
            engine_check_artifact_digest=validate_sha256_digest(  # type: ignore[arg-type]
                row["engine_check_artifact_digest"]),
            blocker_overrides=tuple(_parse_override(item) for item in raw_overrides),
            candidate_digest=(
                None if row["candidate_digest"] is None
                else validate_sha256_digest(row["candidate_digest"])),  # type: ignore[arg-type]
            evaluation_evidence_digest=(
                None if row["evaluation_evidence_digest"] is None
                else validate_sha256_digest(  # type: ignore[arg-type]
                    row["evaluation_evidence_digest"])),
            reviewer_artifact_digests=tuple(
                validate_sha256_digest(item) for item in raw_reviewers),
        )
        attempt_id = _nonempty(row["attempt_id"], f"{label} attempt_id")
        action_id = _nonempty(row["action_id"], f"{label} action_id")
    except (TypeError, ValueError, EvidenceBindingRefusal) as error:
        if isinstance(error, ApplyBindingRefusal):
            raise
        raise ApplyBindingRefusal(str(error)) from error
    return row, decision_input, attempt_id, action_id


def _load_integration_decision(
        root: Path, reference_id: str, *, spec: RunSpec,
        evidence: VerifierEvidence) -> IntegrationDecision:
    try:
        with RunStore.open(root) as store:
            reference = store.get_artifact_reference(reference_id)
            payload = ArtifactStore(root).read_reference(reference)
    except WorkflowError as error:
        raise ApplyBindingRefusal(
            f"cannot load integration decision: {error}") from error
    if reference.kind is not ArtifactReferenceKind.DECISION:
        raise ApplyBindingRefusal("integration reference is not DECISION")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ApplyBindingRefusal(f"decision is not JSON: {error}") from error
    row, decision_input, attempt_id, action_id = _parse_decision_document(
        decoded,
        schema=_DECISION_SCHEMA,
        producer_digest=True,
        label="integration decision",
    )
    try:
        producer_effect_digest = validate_sha256_digest(  # type: ignore[arg-type]
            row["producer_effect_digest"])
    except ValueError as error:
        raise ApplyBindingRefusal(str(error)) from error
    if (reference.reference_id != f"integration-decision:{action_id}"
            or row["schema"] != _DECISION_SCHEMA or row["run_id"] != spec.run_id
            or row["job_id"] != spec.job_id
            or row["decision_lineage_key"] != _decision_lineage_key(
                spec, decision_input)):
        raise ApplyBindingRefusal("decision schema or run/job identity is invalid")
    try:
        outcome, overrides = _validate_decision(root, spec, evidence, decision_input)
    except VerifyError as error:
        raise ApplyBindingRefusal(str(error)) from error
    normalized = DecisionInput(
        actor=decision_input.actor,
        outcome=outcome,
        criteria=spec.job_input.acceptance_criteria,
        result_digest=evidence.result.result_digest,
        verifier_reference_id=evidence.artifact_reference.reference_id,
        verifier_artifact_digest=evidence.artifact_reference.digest,
        engine_check_reference_id=evidence.engine_checks.artifact_reference.reference_id,
        engine_check_artifact_digest=evidence.engine_checks.artifact_reference.digest,
        blocker_overrides=overrides,
        candidate_digest=decision_input.candidate_digest,
        evaluation_evidence_digest=decision_input.evaluation_evidence_digest,
        reviewer_artifact_digests=decision_input.reviewer_artifact_digests,
    )
    expected_payload = _canonical_json(_decision_payload(
        spec=spec,
        attempt_id=attempt_id,
        action_id=action_id,
        decision=normalized,
        producer_effect_digest=producer_effect_digest,
    ))
    if payload != expected_payload:
        raise ApplyBindingRefusal("decision bytes are not canonical or authority-bound")
    try:
        intent_payload = _require_reference_attribution(
            root, reference, run_id=spec.run_id, job_id=spec.job_id,
            attempt_id=attempt_id,
            producer_action_id=action_id,
            expected_kind=ArtifactReferenceKind.DECISION,
            expected_effect_kind="artifact-write",
            expected_content_digest=producer_effect_digest)
    except EvidenceBindingRefusal as error:
        raise ApplyBindingRefusal(str(error)) from error
    if intent_payload is None:
        raise ApplyBindingRefusal("decision producer effect input is absent")
    try:
        intent_decoded = json.loads(intent_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ApplyBindingRefusal(f"decision intent is not JSON: {error}") from error
    intent_row, intent_input, intent_attempt_id, intent_action_id = (
        _parse_decision_document(
            intent_decoded,
            schema=_DECISION_INTENT_SCHEMA,
            producer_digest=False,
            label="integration decision intent",
        )
    )
    if (intent_attempt_id != attempt_id or intent_action_id != action_id
            or intent_row["run_id"] != spec.run_id
            or intent_row["job_id"] != spec.job_id
            or intent_row["decision_lineage_key"] != _decision_lineage_key(
                spec, intent_input)
            or intent_payload != _canonical_json(_decision_intent_payload(
                spec=spec,
                attempt_id=attempt_id,
                action_id=action_id,
                decision=intent_input,
            ))):
        raise ApplyBindingRefusal("decision intent is not canonical or authority-bound")
    try:
        intent_outcome, intent_overrides = _validate_decision(
            root, spec, evidence, intent_input)
    except VerifyError as error:
        raise ApplyBindingRefusal(str(error)) from error
    normalized_intent = DecisionInput(
        actor=intent_input.actor,
        outcome=intent_outcome,
        criteria=spec.job_input.acceptance_criteria,
        result_digest=evidence.result.result_digest,
        verifier_reference_id=evidence.artifact_reference.reference_id,
        verifier_artifact_digest=evidence.artifact_reference.digest,
        engine_check_reference_id=evidence.engine_checks.artifact_reference.reference_id,
        engine_check_artifact_digest=evidence.engine_checks.artifact_reference.digest,
        blocker_overrides=intent_overrides,
        candidate_digest=intent_input.candidate_digest,
        evaluation_evidence_digest=intent_input.evaluation_evidence_digest,
        reviewer_artifact_digests=intent_input.reviewer_artifact_digests,
    )
    if normalized_intent != normalized:
        raise ApplyBindingRefusal(
            "published decision differs from its producer effect input")
    return IntegrationDecision(
        run_id=spec.run_id,
        job_id=spec.job_id,
        attempt_id=attempt_id,
        action_id=action_id,
        actor=normalized.actor,
        outcome=outcome,
        criteria=normalized.criteria,
        result_digest=normalized.result_digest,
        verifier_reference_id=normalized.verifier_reference_id,
        verifier_artifact_digest=normalized.verifier_artifact_digest,
        engine_check_reference_id=normalized.engine_check_reference_id,
        engine_check_artifact_digest=normalized.engine_check_artifact_digest,
        blocker_overrides=overrides,
        producer_effect_digest=producer_effect_digest,
        artifact_reference=reference,
        candidate_digest=normalized.candidate_digest,
        evaluation_evidence_digest=normalized.evaluation_evidence_digest,
        reviewer_artifact_digests=normalized.reviewer_artifact_digests,
    )


def reload_integration_decision(
        run_id: str, attempt_id: str, action_id: str,
        verifier_action_id: str, *, start: Path | None = None,
        ) -> IntegrationDecision:
    """Reload and fully revalidate one published terminal integration decision."""
    root = Path.cwd().resolve() if start is None else Path(start).resolve(strict=True)
    expected_attempt = _nonempty(attempt_id, "attempt_id")
    expected_action = _nonempty(action_id, "action_id")
    expected_verifier_action = _nonempty(
        verifier_action_id, "verifier_action_id")
    spec, _snapshot, plan, dispatch = _terminal_reload_authority(run_id, root)
    try:
        evidence = _load_verifier_evidence(
            root,
            f"verifier-evidence:{expected_verifier_action}",
            spec=spec,
            plan=plan,
            dispatch=dispatch,
        )
    except EvidenceBindingRefusal as error:
        raise ApplyBindingRefusal(
            f"verifier evidence authority is invalid: {error}") from error
    if (evidence.attempt_id != expected_attempt
            or evidence.action_id != expected_verifier_action):
        raise ApplyBindingRefusal(
            "verifier evidence does not belong to the expected decision lineage")
    decision = _load_integration_decision(
        root,
        f"integration-decision:{expected_action}",
        spec=spec,
        evidence=evidence,
    )
    if (decision.attempt_id != expected_attempt
            or decision.action_id != expected_action):
        raise ApplyBindingRefusal(
            "integration decision does not belong to the expected terminal lineage")
    return decision


def _checked_out_target_ref(repository: Path, target_ref: str) -> None:
    components = target_ref.split("/")
    forbidden = set(" ~^:?*[\\")
    if (not target_ref.startswith(_INTEGRATION_REF_PREFIX)
            or target_ref == _INTEGRATION_REF_PREFIX
            or ".." in target_ref or "@{" in target_ref
            or any(not item or item in {".", ".."} or item.startswith(".")
                   or item.endswith((".", ".lock")) for item in components)
            or any(ord(character) < 32 or ord(character) == 127
                   or character in forbidden for character in target_ref)):
        raise ApplyBindingRefusal(
            "apply target must be one canonical private refs/waystone/integration/* name")
    try:
        symbolic_rc, symbolic_target, symbolic_error = git_rc(
            repository, "symbolic-ref", "--quiet", target_ref)
    except (OSError, UnicodeError) as error:
        raise ApplyBindingRefusal(
            f"cannot establish direct integration ref authority: {error}") from error
    if symbolic_rc == 0:
        raise ApplyBindingRefusal(
            f"integration target must be a direct ref, not symbolic to {symbolic_target!r}")
    if symbolic_rc != 1 or symbolic_target or symbolic_error:
        raise ApplyBindingRefusal(
            symbolic_error
            or f"cannot establish direct integration ref authority (git rc={symbolic_rc})")
    try:
        records = _registered_worktrees(repository)
    except VerifierBindingRefusal as error:
        raise ApplyBindingRefusal(str(error)) from error
    for record in records:
        if record.get("branch") == target_ref:
            raise CheckedOutTargetRefRefusal(
                f"target ref {target_ref!r} is checked out in a registered worktree")


def _reload_apply_authority(
        root: Path, run_id: str, verifier_reference_id: str,
        decision_reference_id: str,
        ) -> tuple[RunSpec, VerifierEvidence, IntegrationDecision]:
    try:
        spec, _snapshot, plan, dispatch = _authority(run_id, root)
        evidence = _load_verifier_evidence(
            root, verifier_reference_id, spec=spec, plan=plan, dispatch=dispatch)
        decision = _load_integration_decision(
            root, decision_reference_id, spec=spec, evidence=evidence)
    except ApplyBindingRefusal:
        raise
    except WorkflowError as error:
        raise ApplyBindingRefusal(
            f"cannot reload execution-time approval authority: {error}") from error
    if decision.outcome is not DecisionOutcome.ACCEPT:
        raise DecisionNotAcceptedRefusal("only an accepted result can be applied")
    return spec, evidence, decision


def _require_apply_attempt(attempt_id: str, decision: IntegrationDecision) -> None:
    supplied = _nonempty(attempt_id, "attempt_id")
    if supplied != decision.attempt_id:
        raise ApplyBindingRefusal(
            "apply action must belong to the attempt that recorded the decision")


def apply_integration_decision(
        run_id: str, attempt_id: str, action_id: str, repository: Path,
        result_ref: str, target_ref: str, verifier_reference_id: str,
        decision_reference_id: str, *, race_hook: RaceHook | None = None,
        start: Path | None = None) -> ApplyResult:
    """Revalidate all authority and CAS-adopt the result on a non-checked-out ref."""
    root = Path(repository).resolve(strict=True)
    authority_root = root if start is None else Path(start).resolve(strict=True)
    if authority_root != root:
        raise ApplyBindingRefusal("integration repository is not the authority project root")
    _checked_out_target_ref(root, _nonempty(target_ref, "target_ref"))
    spec, evidence, decision = _reload_apply_authority(
        root, run_id, verifier_reference_id, decision_reference_id)
    _require_apply_attempt(attempt_id, decision)
    fresh = derive_git_result(root, spec.base_snapshot.head, result_ref)
    if (fresh != evidence.result or fresh.result_digest != decision.result_digest
            or evidence.result.base_oid != spec.base_snapshot.head):
        raise ApplyDriftRefusal(
            "fresh base, patch bytes, result commit, or result digest differs from approval")
    target_oid = _oid(root, f"{target_ref}^{{commit}}", "integration target")
    if target_oid != fresh.base_oid:
        raise ApplyConcurrentDriftRefusal(
            "integration target no longer names the approved base commit")
    before = fingerprint_worktree(root)
    if race_hook is not None:
        if not callable(race_hook):
            raise TypeError("race_hook must be callable")
        race_hook()

    # Execution-time reload: neither approval-time artifacts nor the first Git read are trusted.
    spec, evidence, decision = _reload_apply_authority(
        root, run_id, verifier_reference_id, decision_reference_id)
    _require_apply_attempt(attempt_id, decision)
    fresh = derive_git_result(root, spec.base_snapshot.head, result_ref)
    if fresh != evidence.result or fresh.result_digest != decision.result_digest:
        raise ApplyDriftRefusal("result changed during apply preconditions")
    if fingerprint_worktree(root) != before:
        raise ApplyConcurrentDriftRefusal(
            "live worktree or index changed before integration; no ref was written")
    _checked_out_target_ref(root, target_ref)

    store, effects = _effect_engine(root)
    try:
        effect_plan = effects.plan_effect(
            run_id,
            spec.job_id,
            attempt_id,
            action_id,
            PatchIntegrationEffect(
                repository=root,
                target_ref=target_ref,
                expected_parent_oid=fresh.base_oid,
                expected_parent_tree_oid=fresh.base_tree_oid,
                integration_commit_oid=fresh.result_oid,
                integration_tree_oid=fresh.result_tree_oid,
                approval_digests=PatchApprovalDigests(
                    run_spec_digest=spec.run_spec_digest,
                    verification_plan_digest=evidence.verification_plan_digest,
                    verifier_evidence_digest=evidence.artifact_reference.digest,
                    integration_decision_digest=decision.artifact_reference.digest,
                ),
            ),
        )
        claimed = effects.claim_effect(effect_plan, ttl_seconds=30)
        effect_result = effects.execute_effect(claimed)
    finally:
        store.close()
    if effect_result.state is not EffectResultState.COMPLETED:
        raise ApplyConcurrentDriftRefusal(
            effect_result.reason or "patch integration CAS did not complete")
    if effect_result.observed_digest is None:
        raise ApplyBindingRefusal("completed patch integration lacks observed evidence")
    return ApplyResult(
        action_id=action_id,
        target_ref=target_ref,
        result_oid=fresh.result_oid,
        observed_digest=effect_result.observed_digest,
    )


__all__ = [
    "ActorIdentity",
    "ApplyBindingRefusal",
    "ApplyConcurrentDriftRefusal",
    "ApplyDriftRefusal",
    "ApplyResult",
    "BlockerOverride",
    "BlockerOverrideRefusal",
    "CheckedOutTargetRefRefusal",
    "CriterionResult",
    "DecisionActorRefusal",
    "DecisionInput",
    "DecisionNotAcceptedRefusal",
    "DecisionOutcome",
    "DecisionResultDigestRefusal",
    "EngineCheckEvidence",
    "EngineCheckExecutionFailed",
    "EngineCheckFailedRefusal",
    "EngineCheckOutput",
    "EngineCheckRequest",
    "EngineCheckResult",
    "EvidenceBindingRefusal",
    "ExtraCriterionRefusal",
    "FixtureVerifierResult",
    "GitResultError",
    "GitResultTriple",
    "IntegrationDecision",
    "InvalidEngineCheckOutput",
    "InvalidVerifierOutput",
    "MissingCriterionRefusal",
    "VerifierActorRefusal",
    "VerifierAdapter",
    "VerifierBindingRefusal",
    "VerifierBlocker",
    "VerifierEvidence",
    "VerifierExecutionFailed",
    "VerifierMutationRefusal",
    "VerifierOutput",
    "VerifierRequest",
    "VerifyError",
    "WorktreeFingerprint",
    "apply_integration_decision",
    "derive_git_result",
    "execute_verifier",
    "fingerprint_worktree",
    "fingerprint_materialized_root",
    "record_integration_decision",
    "reload_integration_decision",
    "reload_verifier_evidence",
]
