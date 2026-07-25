"""Frozen verification plans and fail-closed dispatch capability preflight."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from waystone.core import WorkflowError
from waystone.jobs.domain import ExecutionCategory, ExecutorKind, Role, RoleBinding
from waystone.jobs.profile import (
    PROFILE_SCHEMA,
    CanonicalProfile,
    ProfileError,
    ProfileUnreadable,
    read_profile_bytes,
)
from waystone.project import find_project_root
from waystone.runs.artifacts import (
    ArtifactError,
    ArtifactReference,
    ArtifactReferenceKind,
    ArtifactStore,
    validate_sha256_digest,
)
from waystone.runs.spec import RunSpec, load_run_spec
from waystone.runs.store import (
    EntityKind,
    RecordNotFoundError,
    RunStore,
    TransitionReason,
)


_PLAN_SCHEMA = "waystone-verification-plan-1"
_PREFLIGHT_SCHEMA = "waystone-verification-preflight-1"
_RUNNER_PROOF_SCHEMA = "waystone-runner-proof-1"
_PLAN_REFERENCE_PREFIX = "verification-plan:"
_PREFLIGHT_REFERENCE_PREFIX = "verification-preflight:"
_PROFILE_LOCATOR = ".waystone/profile.yml"
_PROJECT_CONFIG_LOCATOR = ".waystone.yml"


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _prepared_input_digest(
        command_input_digest: str, preparation_artifact_digest: str,
        environment: tuple["EnvironmentInput", ...]) -> str:
    return _digest(_canonical_json({
        "child_environment": _jsonable(environment),
        "command_input_digest": command_input_digest,
        "environment_preparation_artifact_digest": preparation_artifact_digest,
    }))


def _nonempty_line(value: object, label: str) -> str:
    if (not isinstance(value, str) or not value.strip()
            or "\n" in value or "\r" in value):
        raise ValueError(f"{label} must be one non-empty line")
    return value


def _canonical_digest(value: object, label: str) -> str:
    try:
        return validate_sha256_digest(value)  # type: ignore[arg-type]
    except ValueError as error:
        raise ValueError(f"{label}: {error}") from error


def _unique(values: Iterable[object], label: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{label} must be unique")


def _relative_locator(value: str, label: str) -> str:
    locator = PurePosixPath(_nonempty_line(value, label))
    if locator.is_absolute() or any(part in ("", ".", "..") for part in locator.parts):
        raise ValueError(f"{label} must be a normalized project-relative POSIX path")
    return locator.as_posix()


class PreflightError(WorkflowError):
    """Base class for typed verification-plan and dispatch-preflight failures."""

    code = "verification_preflight_error"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class VerificationPlanIncompleteError(PreflightError):
    code = "verification_plan_incomplete"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class VerificationPlanMissingError(PreflightError):
    code = "verification_plan_missing"

    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"run {run_id!r} has no frozen VerificationPlan")


class VerificationPlanArtifactError(PreflightError):
    code = "verification_plan_artifact_invalid"

    def __init__(self, run_id: str, detail: str):
        self.run_id = run_id
        self.detail = detail
        super().__init__(f"run {run_id!r}: {detail}")


class PreflightEvidenceArtifactError(PreflightError):
    code = "preflight_evidence_artifact_invalid"

    def __init__(self, run_id: str, detail: str):
        self.run_id = run_id
        self.detail = detail
        super().__init__(f"run {run_id!r}: {detail}")


class VerificationPlanStateError(PreflightError):
    code = "verification_plan_state_invalid"

    def __init__(self, run_id: str, state: str, expected: str):
        self.run_id = run_id
        self.state = state
        self.expected = expected
        super().__init__(
            f"run {run_id!r} is {state!r}; expected {expected!r} for this operation")


class ProfilePreflightRefusal(PreflightError):
    code = "verification_profile_refused"

    def __init__(self, refusal: ProfileError):
        self.refusal = refusal
        self.profile_code = refusal.code
        self.profile_reason = refusal.detail
        self.legacy_role = None
        self.legacy_execution = None
        super().__init__(f"{refusal.code}: {refusal.detail}")


class CapabilityPreflightRefusal(PreflightError):
    """Base class for a capability gap that must not launch a worker."""


class UnsupportedExecutionCategoryError(CapabilityPreflightRefusal):
    code = "unsupported_execution_category"

    def __init__(self, binding: RoleBinding):
        self.binding = binding
        super().__init__(
            f"{binding.role.value} requires unsupported execution category "
            f"{binding.execution_category.value!r}")


class UnsupportedBindingError(CapabilityPreflightRefusal):
    code = "unsupported_backend_binding"

    def __init__(self, binding: RoleBinding):
        self.binding = binding
        super().__init__(
            f"no exact capability exists for {binding.role.value}/"
            f"{binding.execution_category.value}/{binding.backend}")


class UnsupportedSandboxError(CapabilityPreflightRefusal):
    code = "unsupported_sandbox_capability"

    def __init__(self, owner: str, sandbox: "SandboxContract"):
        self.owner = owner
        self.sandbox = sandbox
        super().__init__(f"{owner} requires unsupported sandbox {sandbox}")


class EnvironmentPreparationUnavailableError(CapabilityPreflightRefusal):
    code = "verification_environment_unavailable"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class ChildEnvironmentNotAllowedError(CapabilityPreflightRefusal):
    code = "child_env_not_allowed"

    def __init__(self, name: str, detail: str):
        self.name = name
        self.detail = detail
        super().__init__(f"child environment {name!r}: {detail}")


class ChildEnvironmentRequiredMissingError(CapabilityPreflightRefusal):
    code = "child_env_required_missing"

    def __init__(self, check_id: str, name: str):
        self.check_id = check_id
        self.name = name
        super().__init__(
            f"check {check_id!r} cannot establish required child environment {name!r}")


class RequiredCheckUnexecutableError(CapabilityPreflightRefusal):
    code = "required_check_unexecutable"

    def __init__(self, check_id: str, target: "ProbeTarget", detail: str):
        self.check_id = check_id
        self.target = target
        self.detail = detail
        super().__init__(f"check {check_id!r} in {target.value}: {detail}")


class RedFirstEvidenceUnavailableError(CapabilityPreflightRefusal):
    code = "red_first_evidence_unavailable"

    def __init__(self, check_id: str, detail: str):
        self.check_id = check_id
        self.detail = detail
        super().__init__(f"RED-first check {check_id!r}: {detail}")


class VerifierCapabilityUnavailableError(CapabilityPreflightRefusal):
    code = "verifier_capability_unavailable"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class RunnerProofRevalidationRequired(PreflightError):
    code = "runner_proof_revalidation_required"

    def __init__(self, mismatched_axes: Iterable[str]):
        self.mismatched_axes = tuple(sorted(set(mismatched_axes)))
        super().__init__(
            "runner proof differs on: " + ", ".join(self.mismatched_axes))


class RunnerProbeUnavailableError(PreflightError):
    code = "runner_probe_unavailable"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class ToolchainUnavailableError(CapabilityPreflightRefusal):
    code = "toolchain_unavailable"

    def __init__(self, toolchain_id: str, detail: str):
        self.toolchain_id = toolchain_id
        self.detail = detail
        super().__init__(f"toolchain {toolchain_id!r}: {detail}")


class ToolchainDigestMismatchError(CapabilityPreflightRefusal):
    code = "toolchain_digest_mismatch"

    def __init__(self, toolchain_id: str, expected: str, observed: str):
        self.toolchain_id = toolchain_id
        self.expected = expected
        self.observed = observed
        super().__init__(
            f"toolchain {toolchain_id!r} expected {expected}, observed {observed}")


class NonAuthoritativeCheckResultError(PreflightError):
    code = "worker_check_not_authoritative"

    def __init__(self, check_id: str, executor_kind: ExecutorKind):
        self.check_id = check_id
        self.executor_kind = executor_kind
        super().__init__(
            f"check {check_id!r} was reported by {executor_kind.value}; "
            "only an engine-owned action is authoritative")


class WorkingDirectoryRule(str, Enum):
    JOB_ROOT = "job-root"
    INTEGRATION_ROOT = "integration-root"


class ObservationStatus(str, Enum):
    OBSERVED = "observed"
    NOT_OBSERVED = "not-observed"


class ProbeTarget(str, Enum):
    ENGINE_ENVIRONMENT = "engine-environment"
    WORKER_ENVIRONMENT = "worker-environment"


class CheckPhase(str, Enum):
    VERIFICATION = "verification"
    RED_FIRST = "red-first"


class ChildEnvironmentSource(str, Enum):
    ENGINE_RUNTIME = "engine-runtime"
    FROZEN_ACTION = "frozen-action"
    CREDENTIAL_BINDING = "credential-binding"


class ChildEnvironmentNormalization(str, Enum):
    BOOLEAN_01 = "boolean-01"
    UTF8_EXACT = "utf8-exact"
    ISOLATED_PATH = "isolated-path"
    TOOLCHAIN_PATH = "toolchain-path"


_REQUIRED_RUNTIME_OBSERVATIONS = {
    "cache-boundary": ("engine:cache-boundary", False),
    "platform-kernel": ("engine:platform-kernel", False),
    "process-security": ("engine:process-security", True),
    "runner-binary": ("runner-adapter:binary", False),
    "runner-config-content": ("runner-adapter:config", True),
    "runner-version": ("runner-adapter:version", False),
    "sandbox-contract": ("engine:sandbox-contract", False),
}


_SUPPORTED_CHILD_ENVIRONMENT = {
    "CLICOLOR": (
        ChildEnvironmentSource.ENGINE_RUNTIME,
        ChildEnvironmentNormalization.UTF8_EXACT,
    ),
    "CLICOLOR_FORCE": (
        ChildEnvironmentSource.ENGINE_RUNTIME,
        ChildEnvironmentNormalization.UTF8_EXACT,
    ),
    "FORCE_COLOR": (
        ChildEnvironmentSource.ENGINE_RUNTIME,
        ChildEnvironmentNormalization.UTF8_EXACT,
    ),
    "LANG": (
        ChildEnvironmentSource.ENGINE_RUNTIME,
        ChildEnvironmentNormalization.UTF8_EXACT,
    ),
    "LC_ALL": (
        ChildEnvironmentSource.ENGINE_RUNTIME,
        ChildEnvironmentNormalization.UTF8_EXACT,
    ),
    "NO_COLOR": (
        ChildEnvironmentSource.ENGINE_RUNTIME,
        ChildEnvironmentNormalization.UTF8_EXACT,
    ),
    "PATH": (
        ChildEnvironmentSource.FROZEN_ACTION,
        ChildEnvironmentNormalization.TOOLCHAIN_PATH,
    ),
    "TMPDIR": (
        ChildEnvironmentSource.ENGINE_RUNTIME,
        ChildEnvironmentNormalization.ISOLATED_PATH,
    ),
    "UV_CACHE_DIR": (
        ChildEnvironmentSource.ENGINE_RUNTIME,
        ChildEnvironmentNormalization.ISOLATED_PATH,
    ),
    "UV_OFFLINE": (
        ChildEnvironmentSource.FROZEN_ACTION,
        ChildEnvironmentNormalization.BOOLEAN_01,
    ),
}


_FORBIDDEN_CHILD_ENVIRONMENT = frozenset({
    "ENTITY_VERSION",
    "FENCING_EPOCH",
    "LOCK_HANDLE",
    "OWNER_TOKEN",
    "WAYSTONE_DB_MUTATION_AUTHORITY",
    "WAYSTONE_ENTITY_VERSION",
    "WAYSTONE_FENCING_EPOCH",
    "WAYSTONE_LOCK_HANDLE",
    "WAYSTONE_OWNER_TOKEN",
})


@dataclass(frozen=True, order=True)
class SandboxContract:
    filesystem: str
    process: str
    network: str

    def __post_init__(self) -> None:
        for label in ("filesystem", "process", "network"):
            _nonempty_line(getattr(self, label), f"sandbox {label}")


@dataclass(frozen=True, order=True)
class EnvironmentInput:
    name: str
    source: ChildEnvironmentSource
    normalization: ChildEnvironmentNormalization
    value_digest: str

    def __post_init__(self) -> None:
        name = _nonempty_line(self.name, "environment name")
        try:
            source = ChildEnvironmentSource(self.source)
            normalization = ChildEnvironmentNormalization(self.normalization)
        except (TypeError, ValueError) as error:
            raise ChildEnvironmentNotAllowedError(
                name, "source or normalization is not in the closed schema") from error
        if name.upper() in _FORBIDDEN_CHILD_ENVIRONMENT:
            raise ChildEnvironmentNotAllowedError(
                name, "lease, fencing, DB, or lock authority cannot enter a child")
        if source is ChildEnvironmentSource.CREDENTIAL_BINDING:
            raise ChildEnvironmentNotAllowedError(
                name,
                "credential bindings require a consent/capability artifact not supported here",
            )
        supported = _SUPPORTED_CHILD_ENVIRONMENT.get(name)
        if supported is None:
            raise ChildEnvironmentNotAllowedError(
                name, "name is outside the supported closed child-environment schema")
        if (source, normalization) != supported:
            raise ChildEnvironmentNotAllowedError(
                name,
                f"requires source={supported[0].value!r} and "
                f"normalization={supported[1].value!r}",
            )
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "normalization", normalization)
        object.__setattr__(
            self, "value_digest", _canonical_digest(self.value_digest, "environment digest"))


@dataclass(frozen=True, order=True)
class DependencyConstraint:
    name: str
    version_constraint: str

    def __post_init__(self) -> None:
        _nonempty_line(self.name, "dependency name")
        _nonempty_line(self.version_constraint, "dependency version constraint")


@dataclass(frozen=True, order=True)
class ToolchainRequirement:
    toolchain_id: str
    executable: str
    runtime: str
    source_id: str
    content_digest: str
    size: int
    dependencies: tuple[DependencyConstraint, ...]

    def __post_init__(self) -> None:
        for label in ("toolchain_id", "executable", "runtime", "source_id"):
            _nonempty_line(getattr(self, label), label)
        object.__setattr__(
            self, "content_digest", _canonical_digest(
                self.content_digest, "toolchain content digest"))
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("toolchain size must be a non-negative integer")
        dependencies = tuple(sorted(self.dependencies))
        _unique((item.name for item in dependencies), "toolchain dependency names")
        object.__setattr__(self, "dependencies", dependencies)


@dataclass(frozen=True)
class MaterializedToolchain:
    toolchain_id: str
    source_id: str
    path: Path

    def __post_init__(self) -> None:
        _nonempty_line(self.toolchain_id, "toolchain_id")
        _nonempty_line(self.source_id, "toolchain source_id")
        path = Path(self.path)
        if not path.is_absolute():
            raise ToolchainUnavailableError(
                self.toolchain_id,
                "materialized path must be absolute and must not depend on ambient cwd",
            )
        object.__setattr__(self, "path", path)


@dataclass(frozen=True, order=True)
class EnvironmentPreparationStep:
    sequence: int
    step_id: str
    command: tuple[str, ...]
    input_toolchain_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (isinstance(self.sequence, bool)
                or not isinstance(self.sequence, int) or self.sequence < 0):
            raise ValueError("environment preparation sequence must be non-negative")
        _nonempty_line(self.step_id, "environment preparation step_id")
        if not self.command:
            raise ValueError("environment preparation command must be non-empty")
        for item in self.command:
            _nonempty_line(item, "environment preparation command argument")
        toolchains = tuple(sorted(self.input_toolchain_ids))
        for toolchain_id in toolchains:
            _nonempty_line(toolchain_id, "environment preparation toolchain id")
        _unique(toolchains, "environment preparation toolchain ids")
        object.__setattr__(self, "input_toolchain_ids", toolchains)


@dataclass(frozen=True)
class NetworkCacheRequirements:
    network_required: bool
    allowed_sources: tuple[str, ...]
    cache_namespace: str
    offline_capable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.network_required, bool) or not isinstance(
                self.offline_capable, bool):
            raise ValueError("network and offline flags must be booleans")
        sources = tuple(sorted(self.allowed_sources))
        for source in sources:
            _nonempty_line(source, "allowed network/cache source")
        _unique(sources, "allowed network/cache sources")
        if self.network_required and not sources:
            raise ValueError("network-required preparation needs an allowed source")
        _nonempty_line(self.cache_namespace, "cache namespace")
        object.__setattr__(self, "allowed_sources", sources)


@dataclass(frozen=True)
class EnvironmentPreparationReceipt:
    environment_preparation_digest: str
    network_cache_requirements: NetworkCacheRequirements
    toolchain_observations: tuple["ToolchainObservation", ...]
    observed_by: ExecutorKind = field(init=False, default=ExecutorKind.ENGINE)
    artifact_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment_preparation_digest", _canonical_digest(
            self.environment_preparation_digest, "environment preparation receipt digest"))
        if not isinstance(self.network_cache_requirements, NetworkCacheRequirements):
            raise TypeError("receipt network/cache requirements are invalid")
        if any(not isinstance(item, ToolchainObservation)
               for item in self.toolchain_observations):
            raise TypeError("receipt toolchains must be ToolchainObservation values")
        observations = tuple(sorted(self.toolchain_observations))
        _unique((item.toolchain_id for item in observations), "receipt toolchain ids")
        object.__setattr__(self, "toolchain_observations", observations)
        object.__setattr__(self, "artifact_digest", "sha256:" + "0" * 64)
        object.__setattr__(self, "artifact_digest", _digest(self.canonical_bytes()))

    def canonical_bytes(self) -> bytes:
        return _canonical_json(_without(self, "artifact_digest"))


@dataclass(frozen=True)
class CheckDefinition:
    check_id: str
    phase: CheckPhase
    command: tuple[str, ...]
    working_directory: WorkingDirectoryRule
    expected_exit_codes: tuple[int, ...]
    expected_evidence_kinds: tuple[str, ...]
    environment: tuple[EnvironmentInput, ...]
    fixture_digests: tuple[str, ...]
    required_toolchain_ids: tuple[str, ...]
    sandbox: SandboxContract
    worker_execution_required: bool
    red_expected_exit_codes: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _nonempty_line(self.check_id, "check_id")
        try:
            phase = CheckPhase(self.phase)
        except (TypeError, ValueError) as error:
            raise VerificationPlanIncompleteError(
                "check phase must be 'verification' or 'red-first'") from error
        if not self.command:
            raise ValueError("check command must be non-empty")
        for item in self.command:
            _nonempty_line(item, "check command argument")
        try:
            working_directory = WorkingDirectoryRule(self.working_directory)
        except (TypeError, ValueError) as error:
            raise ValueError("working_directory must be a frozen working-directory rule") from error
        exits = tuple(sorted(self.expected_exit_codes))
        if not exits or any(
                isinstance(code, bool) or not isinstance(code, int) for code in exits):
            raise ValueError("expected_exit_codes must contain integers")
        _unique(exits, "expected exit codes")
        evidence = tuple(sorted(self.expected_evidence_kinds))
        if not evidence:
            raise ValueError("expected evidence kinds must be non-empty")
        for kind in evidence:
            _nonempty_line(kind, "expected evidence kind")
        _unique(evidence, "expected evidence kinds")
        environment = tuple(sorted(self.environment))
        _unique((item.name for item in environment), "check environment names")
        fixtures = tuple(sorted(
            _canonical_digest(item, "fixture digest") for item in self.fixture_digests))
        _unique(fixtures, "fixture digests")
        toolchains = tuple(sorted(self.required_toolchain_ids))
        if not toolchains:
            raise ValueError("required_toolchain_ids must be non-empty")
        for toolchain_id in toolchains:
            _nonempty_line(toolchain_id, "required toolchain id")
        _unique(toolchains, "required toolchain ids")
        if not isinstance(self.sandbox, SandboxContract):
            raise TypeError("sandbox must be a SandboxContract")
        if not isinstance(self.worker_execution_required, bool):
            raise TypeError("worker_execution_required must be a boolean")
        red_exits = tuple(sorted(self.red_expected_exit_codes))
        if any(isinstance(code, bool) or not isinstance(code, int) for code in red_exits):
            raise ValueError("RED-first expected exit codes must contain integers")
        _unique(red_exits, "RED-first expected exit codes")
        if phase is CheckPhase.RED_FIRST:
            if not red_exits or 0 in red_exits:
                raise VerificationPlanIncompleteError(
                    "RED-first checks require one or more nonzero expected base exit codes")
        elif red_exits:
            raise VerificationPlanIncompleteError(
                "verification checks cannot declare RED-first expected exit codes")
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "working_directory", working_directory)
        object.__setattr__(self, "expected_exit_codes", exits)
        object.__setattr__(self, "expected_evidence_kinds", evidence)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "fixture_digests", fixtures)
        object.__setattr__(self, "required_toolchain_ids", toolchains)
        object.__setattr__(self, "red_expected_exit_codes", red_exits)


@dataclass(frozen=True)
class VerificationPlanDefinition:
    required_checks: tuple[CheckDefinition, ...]
    required_toolchains: tuple[ToolchainRequirement, ...]
    environment_preparation: tuple[EnvironmentPreparationStep, ...]
    network_cache_requirements: NetworkCacheRequirements
    verifier_sandbox: SandboxContract

    def __post_init__(self) -> None:
        try:
            checks = tuple(sorted(self.required_checks, key=lambda item: item.check_id))
            toolchains = tuple(sorted(
                self.required_toolchains, key=lambda item: item.toolchain_id))
            preparation = tuple(sorted(self.environment_preparation))
            if not checks and (toolchains or preparation):
                raise ValueError(
                    "check-free execution safety cannot carry verification toolchains/preparation")
            if checks and (not toolchains or not preparation):
                raise ValueError(
                    "selected verification checks require toolchains and preparation")
            _unique((item.check_id for item in checks), "check ids")
            _unique((item.toolchain_id for item in toolchains), "toolchain ids")
            _unique((item.step_id for item in preparation), "environment preparation step ids")
            _unique((item.sequence for item in preparation), "environment preparation sequence")
            toolchain_ids = {item.toolchain_id for item in toolchains}
            for check in checks:
                missing = set(check.required_toolchain_ids) - toolchain_ids
                if missing:
                    raise ValueError(
                        f"check {check.check_id!r} names unknown toolchain(s): "
                        + ", ".join(sorted(missing)))
            prepared_ids = {
                toolchain_id
                for step in preparation
                for toolchain_id in step.input_toolchain_ids
            }
            if prepared_ids != toolchain_ids:
                raise ValueError("environment preparation must name every and only toolchain")
            sources = {item.source_id for item in toolchains}
            if not sources.issubset(self.network_cache_requirements.allowed_sources):
                raise ValueError("every toolchain source must be declared by network/cache policy")
            if not isinstance(self.verifier_sandbox, SandboxContract):
                raise TypeError("verifier_sandbox must be a SandboxContract")
        except (TypeError, ValueError) as error:
            raise VerificationPlanIncompleteError(str(error)) from error
        object.__setattr__(self, "required_checks", checks)
        object.__setattr__(self, "required_toolchains", toolchains)
        object.__setattr__(self, "environment_preparation", preparation)


@dataclass(frozen=True)
class FrozenRoleBinding:
    binding: RoleBinding
    source_schema: str | None
    legacy_role: str
    legacy_execution: str | None


@dataclass(frozen=True)
class RequiredCheck:
    check_id: str
    phase: CheckPhase
    command: tuple[str, ...]
    working_directory: WorkingDirectoryRule
    expected_exit_codes: tuple[int, ...]
    expected_evidence_kinds: tuple[str, ...]
    environment: tuple[EnvironmentInput, ...]
    fixture_digests: tuple[str, ...]
    required_toolchain_ids: tuple[str, ...]
    sandbox: SandboxContract
    authoritative_executor: ExecutorKind
    worker_execution_required: bool
    red_expected_exit_codes: tuple[int, ...]
    command_input_digest: str


@dataclass(frozen=True)
class VerificationPlan:
    run_id: str
    job_id: str
    task_id: str
    run_spec_digest: str
    base_snapshot_digest: str
    profile_locator: str
    profile_content_digest: str
    role_bindings: tuple[FrozenRoleBinding, ...]
    required_checks: tuple[RequiredCheck, ...]
    required_toolchains: tuple[ToolchainRequirement, ...]
    environment_preparation: tuple[EnvironmentPreparationStep, ...]
    environment_preparation_digest: str
    network_cache_requirements: NetworkCacheRequirements
    verifier_sandbox: SandboxContract
    verification_plan_digest: str

    def canonical_bytes(self) -> bytes:
        return _canonical_json(_plan_payload(self))

    def binding_for(self, role: Role) -> FrozenRoleBinding:
        matches = tuple(item for item in self.role_bindings if item.binding.role is role)
        if len(matches) != 1:
            raise VerificationPlanIncompleteError(
                f"plan must contain exactly one {role.value} binding")
        return matches[0]


@dataclass(frozen=True, order=True)
class RuntimeObservation:
    axis: str
    source: str
    status: ObservationStatus
    value_digest: str | None = None

    def __post_init__(self) -> None:
        axis = _nonempty_line(self.axis, "runtime observation axis")
        source = _nonempty_line(self.source, "runtime observation source")
        required = _REQUIRED_RUNTIME_OBSERVATIONS.get(axis)
        if required is None:
            raise ValueError(f"runtime observation axis {axis!r} is not in the bounded schema")
        if source != required[0]:
            raise ValueError(
                f"runtime observation {axis!r} requires source {required[0]!r}")
        try:
            status_value = ObservationStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ValueError("runtime observation status is invalid") from error
        if status_value is ObservationStatus.OBSERVED:
            if self.value_digest is None:
                raise ValueError("observed runtime axis requires a value digest")
            value_digest = _canonical_digest(
                self.value_digest, "runtime observation value digest")
        else:
            if self.value_digest is not None:
                raise ValueError("not-observed runtime axis cannot claim a value")
            if not required[1]:
                raise RunnerProbeUnavailableError(
                    f"required runtime axis {axis!r} was not observed")
            value_digest = None
        object.__setattr__(self, "status", status_value)
        object.__setattr__(self, "value_digest", value_digest)


@dataclass(frozen=True)
class RunnerContext:
    checkout_identity: str
    machine_identity: str
    principal_identity: str
    project_config_digest: str
    profile_config_digest: str
    runtime_observations: tuple[RuntimeObservation, ...]

    def __post_init__(self) -> None:
        for label in ("checkout_identity", "machine_identity", "principal_identity"):
            object.__setattr__(self, label, _canonical_digest(
                getattr(self, label), f"{label} digest"))
        object.__setattr__(self, "project_config_digest", _canonical_digest(
            self.project_config_digest, "project config content digest"))
        object.__setattr__(self, "profile_config_digest", _canonical_digest(
            self.profile_config_digest, "profile config content digest"))
        if any(not isinstance(item, RuntimeObservation)
               for item in self.runtime_observations):
            raise TypeError("runtime observations must be RuntimeObservation values")
        observations = tuple(sorted(self.runtime_observations))
        _unique((item.axis for item in observations), "runtime observation axes")
        axes = {item.axis for item in observations}
        if axes != set(_REQUIRED_RUNTIME_OBSERVATIONS):
            missing = sorted(set(_REQUIRED_RUNTIME_OBSERVATIONS) - axes)
            extra = sorted(axes - set(_REQUIRED_RUNTIME_OBSERVATIONS))
            detail = "runner proof requires the exact bounded runtime observation set"
            if missing:
                detail += f"; missing={missing}"
            if extra:
                detail += f"; extra={extra}"
            raise RunnerProbeUnavailableError(detail)
        object.__setattr__(self, "runtime_observations", observations)


@dataclass(frozen=True)
class RoleCapability:
    binding: RoleBinding
    sandbox: SandboxContract
    accepts_frozen_base: bool
    accepts_patch_bytes: bool
    accepts_result_digest: bool
    emits_artifacts: bool
    observed_by: ExecutorKind = field(init=False, default=ExecutorKind.ENGINE)
    probe_artifact_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.binding, RoleBinding):
            raise TypeError("role capability binding must be a RoleBinding")
        if not isinstance(self.sandbox, SandboxContract):
            raise TypeError("role capability sandbox must be a SandboxContract")
        flags = (
            self.accepts_frozen_base,
            self.accepts_patch_bytes,
            self.accepts_result_digest,
            self.emits_artifacts,
        )
        if any(not isinstance(item, bool) for item in flags):
            raise TypeError("role capability result flags must be booleans")
        object.__setattr__(self, "probe_artifact_digest", "sha256:" + "0" * 64)
        object.__setattr__(
            self, "probe_artifact_digest", _digest(self.canonical_receipt_bytes()))

    def canonical_receipt_bytes(self) -> bytes:
        return _canonical_json(_without(self, "probe_artifact_digest"))


@dataclass(frozen=True)
class RunnerCapabilities:
    execution_categories: tuple[ExecutionCategory, ...]
    engine_sandboxes: tuple[SandboxContract, ...]
    role_capabilities: tuple[RoleCapability, ...]

    def __post_init__(self) -> None:
        categories = tuple(sorted(
            (ExecutionCategory(item) for item in self.execution_categories),
            key=lambda item: item.value,
        ))
        sandboxes = tuple(sorted(self.engine_sandboxes))
        roles = tuple(sorted(
            self.role_capabilities,
            key=lambda item: (
                item.binding.role.value,
                item.binding.execution_category.value,
                item.binding.backend,
                item.sandbox,
            ),
        ))
        _unique(categories, "execution categories")
        _unique(sandboxes, "engine sandbox contracts")
        _unique(
            ((item.binding, item.sandbox) for item in roles),
            "role binding/sandbox capabilities",
        )
        object.__setattr__(self, "execution_categories", categories)
        object.__setattr__(self, "engine_sandboxes", sandboxes)
        object.__setattr__(self, "role_capabilities", roles)

    def canonical_bytes(self) -> bytes:
        return _canonical_json(_runner_capabilities_payload(self))

    @property
    def digest(self) -> str:
        return _digest(self.canonical_bytes())


@dataclass(frozen=True, order=True)
class CheckCapabilityProbe:
    check_id: str
    target: ProbeTarget
    command: tuple[str, ...]
    command_input_digest: str
    environment_preparation_artifact_digest: str
    child_environment: tuple[EnvironmentInput, ...]
    entrypoint_ready: bool
    structured_result: bool
    exit_code: int
    prepared_input_digest: str = field(init=False)
    observed_by: ExecutorKind = field(init=False, default=ExecutorKind.ENGINE)
    artifact_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _nonempty_line(self.check_id, "probe check_id")
        try:
            target = ProbeTarget(self.target)
        except (TypeError, ValueError) as error:
            raise ValueError("probe target is invalid") from error
        if not self.command:
            raise ValueError("probe command must be non-empty")
        for item in self.command:
            _nonempty_line(item, "probe command argument")
        object.__setattr__(self, "command_input_digest", _canonical_digest(
            self.command_input_digest, "probe command input digest"))
        object.__setattr__(
            self,
            "environment_preparation_artifact_digest",
            _canonical_digest(
                self.environment_preparation_artifact_digest,
                "probe environment preparation artifact digest",
            ),
        )
        environment = tuple(sorted(self.child_environment))
        if any(not isinstance(item, EnvironmentInput) for item in environment):
            raise TypeError("probe child environment must contain EnvironmentInput values")
        _unique((item.name for item in environment), "probe child environment names")
        if not isinstance(self.entrypoint_ready, bool) or not isinstance(
                self.structured_result, bool):
            raise TypeError("probe readiness fields must be booleans")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise TypeError("probe exit_code must be an integer")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "child_environment", environment)
        object.__setattr__(self, "prepared_input_digest", _prepared_input_digest(
            self.command_input_digest,
            self.environment_preparation_artifact_digest,
            environment,
        ))
        object.__setattr__(self, "artifact_digest", "sha256:" + "0" * 64)
        object.__setattr__(self, "artifact_digest", _digest(self.canonical_receipt_bytes()))

    def canonical_receipt_bytes(self) -> bytes:
        return _canonical_json(_without(self, "artifact_digest"))


@dataclass(frozen=True, order=True)
class RedFirstProbe:
    check_id: str
    base_snapshot_digest: str
    command: tuple[str, ...]
    command_input_digest: str
    environment_preparation_artifact_digest: str
    child_environment: tuple[EnvironmentInput, ...]
    structured_result: bool
    exit_code: int
    observed_by: ExecutorKind = field(init=False, default=ExecutorKind.ENGINE)
    artifact_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _nonempty_line(self.check_id, "RED-first probe check_id")
        object.__setattr__(self, "base_snapshot_digest", _canonical_digest(
            self.base_snapshot_digest, "RED-first base snapshot digest"))
        if not self.command:
            raise ValueError("RED-first probe command must be non-empty")
        for item in self.command:
            _nonempty_line(item, "RED-first probe command argument")
        object.__setattr__(self, "command_input_digest", _canonical_digest(
            self.command_input_digest, "RED-first command input digest"))
        object.__setattr__(
            self,
            "environment_preparation_artifact_digest",
            _canonical_digest(
                self.environment_preparation_artifact_digest,
                "RED-first environment preparation artifact digest",
            ),
        )
        environment = tuple(sorted(self.child_environment))
        if any(not isinstance(item, EnvironmentInput) for item in environment):
            raise TypeError("RED-first child environment must contain EnvironmentInput values")
        _unique((item.name for item in environment), "RED-first child environment names")
        if not isinstance(self.structured_result, bool):
            raise TypeError("RED-first structured_result must be a boolean")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise TypeError("RED-first exit_code must be an integer")
        object.__setattr__(self, "child_environment", environment)
        object.__setattr__(self, "artifact_digest", "sha256:" + "0" * 64)
        object.__setattr__(self, "artifact_digest", _digest(self.canonical_receipt_bytes()))

    def canonical_receipt_bytes(self) -> bytes:
        return _canonical_json(_without(self, "artifact_digest"))


@dataclass(frozen=True)
class CapabilitySet:
    runner: RunnerCapabilities
    environment_preparation_receipts: tuple[EnvironmentPreparationReceipt, ...]
    check_probes: tuple[CheckCapabilityProbe, ...]
    red_first_probes: tuple[RedFirstProbe, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.runner, RunnerCapabilities):
            raise TypeError("runner must be RunnerCapabilities")
        for receipt in self.environment_preparation_receipts:
            if not isinstance(receipt, EnvironmentPreparationReceipt):
                raise TypeError(
                    "environment preparation receipts must be typed receipt values")
        environment = tuple(sorted(
            self.environment_preparation_receipts,
            key=lambda item: item.environment_preparation_digest,
        ))
        _unique(
            (item.environment_preparation_digest for item in environment),
            "environment preparation receipt digests",
        )
        if any(not isinstance(probe, CheckCapabilityProbe)
               for probe in self.check_probes):
            raise TypeError("check probes must be CheckCapabilityProbe values")
        probes = tuple(sorted(self.check_probes))
        _unique(
            ((probe.check_id, probe.target) for probe in probes),
            "check capability probe targets",
        )
        if any(not isinstance(probe, RedFirstProbe)
               for probe in self.red_first_probes):
            raise TypeError("RED-first probes must be RedFirstProbe values")
        red_first = tuple(sorted(self.red_first_probes))
        _unique((probe.check_id for probe in red_first), "RED-first probe check ids")
        object.__setattr__(self, "environment_preparation_receipts", environment)
        object.__setattr__(self, "check_probes", probes)
        object.__setattr__(self, "red_first_probes", red_first)

    def canonical_bytes(self) -> bytes:
        return _canonical_json({
            "check_probes": [_probe_payload(item) for item in self.check_probes],
            "environment_preparation_receipts": _jsonable(
                self.environment_preparation_receipts),
            "red_first_probes": [_red_probe_payload(item) for item in self.red_first_probes],
            "runner": _runner_capabilities_payload(self.runner),
        })

    @property
    def digest(self) -> str:
        return _digest(self.canonical_bytes())


@dataclass(frozen=True)
class RunnerProof:
    context: RunnerContext
    runner_capabilities_digest: str
    observed_by: ExecutorKind = field(init=False, default=ExecutorKind.ENGINE)
    proof_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.context, RunnerContext):
            raise TypeError("runner proof context must be a RunnerContext")
        object.__setattr__(self, "runner_capabilities_digest", _canonical_digest(
            self.runner_capabilities_digest, "runner capabilities digest"))
        object.__setattr__(self, "proof_digest", "sha256:" + "0" * 64)
        object.__setattr__(self, "proof_digest", _digest(self.canonical_bytes()))

    def canonical_bytes(self) -> bytes:
        return _canonical_json(_runner_proof_payload(self))


@dataclass(frozen=True, order=True)
class ToolchainObservation:
    toolchain_id: str
    source_id: str
    content_digest: str
    size: int

    def __post_init__(self) -> None:
        _nonempty_line(self.toolchain_id, "observed toolchain_id")
        _nonempty_line(self.source_id, "observed toolchain source_id")
        object.__setattr__(self, "content_digest", _canonical_digest(
            self.content_digest, "observed toolchain content digest"))
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("observed toolchain size must be a non-negative integer")


@dataclass(frozen=True, order=True)
class EngineCheckAction:
    run_id: str
    verification_plan_digest: str
    check_id: str
    phase: CheckPhase
    command: tuple[str, ...]
    command_input_digest: str
    environment_preparation_artifact_digest: str
    child_environment: tuple[EnvironmentInput, ...]
    working_directory: WorkingDirectoryRule
    expected_exit_codes: tuple[int, ...]
    red_expected_exit_codes: tuple[int, ...]
    expected_evidence_kinds: tuple[str, ...]
    prepared_input_digest: str = field(init=False)
    executor_kind: ExecutorKind = field(init=False, default=ExecutorKind.ENGINE)

    def __post_init__(self) -> None:
        _nonempty_line(self.run_id, "engine action run_id")
        object.__setattr__(self, "verification_plan_digest", _canonical_digest(
            self.verification_plan_digest, "engine action VerificationPlan digest"))
        _nonempty_line(self.check_id, "engine action check_id")
        try:
            phase = CheckPhase(self.phase)
            working_directory = WorkingDirectoryRule(self.working_directory)
        except (TypeError, ValueError) as error:
            raise ValueError("engine action phase or working directory is invalid") from error
        if not self.command:
            raise ValueError("engine action command must be non-empty")
        for item in self.command:
            _nonempty_line(item, "engine action command argument")
        object.__setattr__(self, "command_input_digest", _canonical_digest(
            self.command_input_digest, "engine action command input digest"))
        object.__setattr__(
            self,
            "environment_preparation_artifact_digest",
            _canonical_digest(
                self.environment_preparation_artifact_digest,
                "engine action environment preparation artifact digest",
            ),
        )
        environment = tuple(sorted(self.child_environment))
        if any(not isinstance(item, EnvironmentInput) for item in environment):
            raise TypeError("engine action environment must contain EnvironmentInput values")
        _unique((item.name for item in environment), "engine action environment names")
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "child_environment", environment)
        object.__setattr__(self, "working_directory", working_directory)
        object.__setattr__(self, "prepared_input_digest", _prepared_input_digest(
            self.command_input_digest,
            self.environment_preparation_artifact_digest,
            environment,
        ))


@dataclass(frozen=True)
class WorkerCheckReport:
    check_id: str
    command_input_digest: str
    exit_code: int
    evidence_digests: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _nonempty_line(self.check_id, "check result check_id")
        object.__setattr__(self, "command_input_digest", _canonical_digest(
            self.command_input_digest, "check result command input digest"))
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise TypeError("check result exit_code must be an integer")
        evidence = tuple(sorted(
            (_nonempty_line(kind, "check result evidence kind"),
             _canonical_digest(digest, "check result evidence digest"))
            for kind, digest in self.evidence_digests
        ))
        _unique((kind for kind, _digest_value in evidence), "check result evidence kinds")
        object.__setattr__(self, "evidence_digests", evidence)


@dataclass(frozen=True)
class PreflightEvidence:
    run_id: str
    verification_plan_digest: str
    runner_proof_digest: str
    capability_set_digest: str
    environment_preparation_digest: str
    environment_preparation_artifact_digest: str
    toolchain_observations: tuple[ToolchainObservation, ...]
    capability_probes: tuple[CheckCapabilityProbe, ...]
    red_first_probes: tuple[RedFirstProbe, ...]
    receipt_artifact_digests: tuple[str, ...]
    verifier_capability_digest: str
    engine_actions: tuple[EngineCheckAction, ...]
    authority_scope: str
    preflight_evidence_digest: str

    def canonical_bytes(self) -> bytes:
        return _canonical_json(_preflight_evidence_payload(self))


@dataclass(frozen=True)
class DispatchReady:
    run_id: str
    verification_plan_digest: str
    preflight_evidence_digest: str
    engine_actions: tuple[EngineCheckAction, ...]


def _jsonable(value: object) -> object:
    """Convert authority values to JSON without admitting ambient path objects."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Path):
        raise TypeError("authority payloads cannot contain filesystem paths")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported authority payload value {type(value).__name__}")


def _without(value: object, *names: str) -> dict[str, object]:
    payload = _jsonable(value)
    if not isinstance(payload, dict):
        raise TypeError("authority value must serialize as an object")
    for name in names:
        payload.pop(name)
    return payload


def _environment_preparation_digest(definition: VerificationPlanDefinition) -> str:
    return _digest(_canonical_json({
        "network_cache_requirements": _jsonable(definition.network_cache_requirements),
        "steps": _jsonable(definition.environment_preparation),
        "toolchains": _jsonable(definition.required_toolchains),
    }))


def _command_input_digest(
        check: CheckDefinition, spec: RunSpec,
        toolchains: Sequence[ToolchainRequirement],
        environment_preparation_digest: str) -> str:
    required = {
        item.toolchain_id: item for item in toolchains
        if item.toolchain_id in check.required_toolchain_ids
    }
    return _digest(_canonical_json({
        "base_snapshot_digest": spec.base_snapshot.digest,
        "check": _jsonable(check),
        "environment_preparation_digest": environment_preparation_digest,
        "run_spec_digest": spec.run_spec_digest,
        "toolchains": _jsonable(tuple(
            required[toolchain_id] for toolchain_id in sorted(required))),
    }))


def _plan_payload(plan: VerificationPlan) -> dict[str, object]:
    payload = _without(plan, "verification_plan_digest")
    payload["schema"] = _PLAN_SCHEMA
    return payload


def _runner_capabilities_payload(capabilities: RunnerCapabilities) -> dict[str, object]:
    return _without(capabilities)


def _runner_proof_payload(proof: RunnerProof) -> dict[str, object]:
    payload = _without(proof, "proof_digest")
    payload["schema"] = _RUNNER_PROOF_SCHEMA
    return payload


def _probe_payload(probe: CheckCapabilityProbe) -> dict[str, object]:
    return _without(probe)


def _red_probe_payload(probe: RedFirstProbe) -> dict[str, object]:
    return _without(probe)


def _action_payload(action: EngineCheckAction) -> dict[str, object]:
    return _without(action)


def _preflight_evidence_payload(evidence: PreflightEvidence) -> dict[str, object]:
    payload = _without(evidence, "preflight_evidence_digest")
    payload["schema"] = _PREFLIGHT_SCHEMA
    return payload


def _project_root(start: Path | None) -> Path:
    requested = Path.cwd() if start is None else Path(start)
    root = find_project_root(requested)
    if root is None:
        raise VerificationPlanIncompleteError(
            f"no initialized project contains {requested}")
    try:
        return root.resolve(strict=True)
    except OSError as error:
        raise VerificationPlanIncompleteError(
            f"project root cannot be resolved: {error}") from error


def _read_profile(root: Path) -> tuple[bytes, CanonicalProfile]:
    path = root / _PROFILE_LOCATOR
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ProfilePreflightRefusal(ProfileUnreadable(
            f"canonical profile snapshot cannot be read from {path!r}: {error}",
        )) from error
    try:
        return payload, read_profile_bytes(payload)
    except ProfileError as error:
        raise ProfilePreflightRefusal(error) from error


def _frozen_role_bindings(profile: CanonicalProfile) -> tuple[FrozenRoleBinding, ...]:
    selected: list[FrozenRoleBinding] = []
    for role in (Role.WORKER, Role.VERIFIER):
        adapted = profile.binding_for(role)
        selected.append(FrozenRoleBinding(
            binding=adapted.binding,
            source_schema=PROFILE_SCHEMA,
            legacy_role=role.value,
            legacy_execution=None,
        ))
    return tuple(selected)


def _new_plan(
        spec: RunSpec, definition: VerificationPlanDefinition,
        profile_bytes: bytes, role_bindings: tuple[FrozenRoleBinding, ...]) -> VerificationPlan:
    preparation_digest = _environment_preparation_digest(definition)
    checks = tuple(
        RequiredCheck(
            check_id=check.check_id,
            phase=check.phase,
            command=check.command,
            working_directory=check.working_directory,
            expected_exit_codes=check.expected_exit_codes,
            expected_evidence_kinds=check.expected_evidence_kinds,
            environment=check.environment,
            fixture_digests=check.fixture_digests,
            required_toolchain_ids=check.required_toolchain_ids,
            sandbox=check.sandbox,
            authoritative_executor=ExecutorKind.ENGINE,
            worker_execution_required=check.worker_execution_required,
            red_expected_exit_codes=check.red_expected_exit_codes,
            command_input_digest=_command_input_digest(
                check, spec, definition.required_toolchains, preparation_digest),
        )
        for check in definition.required_checks
    )
    candidate = VerificationPlan(
        run_id=spec.run_id,
        job_id=spec.job_id,
        task_id=spec.job_input.task_id,
        run_spec_digest=spec.run_spec_digest,
        base_snapshot_digest=spec.base_snapshot.digest,
        profile_locator=_PROFILE_LOCATOR,
        profile_content_digest=_digest(profile_bytes),
        role_bindings=role_bindings,
        required_checks=checks,
        required_toolchains=definition.required_toolchains,
        environment_preparation=definition.environment_preparation,
        environment_preparation_digest=preparation_digest,
        network_cache_requirements=definition.network_cache_requirements,
        verifier_sandbox=definition.verifier_sandbox,
        verification_plan_digest="sha256:" + "0" * 64,
    )
    return replace(
        candidate,
        verification_plan_digest=_digest(candidate.canonical_bytes()),
    )


def freeze_verification_plan(
        run_id: str, definition: VerificationPlanDefinition, *,
        start: Path | None = None) -> VerificationPlan:
    """Freeze one complete plan and bind its immutable artifact to an existing RunSpec."""
    if not isinstance(definition, VerificationPlanDefinition):
        raise TypeError("definition must be a VerificationPlanDefinition")
    root = _project_root(start)
    spec = load_run_spec(run_id, start=root)
    profile_bytes, profile = _read_profile(root)
    role_bindings = _frozen_role_bindings(profile)
    plan = _new_plan(spec, definition, profile_bytes, role_bindings)
    authority_bytes = plan.canonical_bytes()
    supplied = Path.cwd() if start is None else Path(start)
    if not supplied.is_absolute():
        supplied = Path.cwd() / supplied
    forbidden_roots = {os.fsencode(root), os.fsencode(supplied.absolute())}
    if any(candidate in authority_bytes for candidate in forbidden_roots):
        raise VerificationPlanIncompleteError(
            "VerificationPlan inputs cannot contain the worktree absolute path")

    with RunStore.open(root) as store:
        run = store.get_run(run_id)
        if run.state != "frozen-ready":
            raise VerificationPlanStateError(run_id, run.state, "frozen-ready")
        stored = ArtifactStore(root).write(plan.canonical_bytes())
        if stored.digest != plan.verification_plan_digest:
            raise VerificationPlanArtifactError(
                run_id, "stored VerificationPlan digest changed")
        store.record_transition(
            EntityKind.RUN,
            run_id,
            expected_version=run.version,
            next_state="verification-plan-frozen",
            reason=TransitionReason.PLANNED,
            evidence_digest=stored.digest,
            artifact_references=(ArtifactReference(
                reference_id=f"{_PLAN_REFERENCE_PREFIX}{run_id}",
                kind=ArtifactReferenceKind.EVIDENCE,
                digest=stored.digest,
                size=stored.size,
            ),),
        )
    return plan


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields are not canonical")


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a string list")
    return tuple(value)


def _parse_sandbox(value: object) -> SandboxContract:
    row = _mapping(value, "sandbox")
    _exact_keys(row, {"filesystem", "network", "process"}, "sandbox")
    return SandboxContract(row["filesystem"], row["process"], row["network"])


def _parse_toolchain(value: object) -> ToolchainRequirement:
    row = _mapping(value, "toolchain")
    _exact_keys(row, {
        "content_digest", "dependencies", "executable", "runtime", "size",
        "source_id", "toolchain_id",
    }, "toolchain")
    raw_dependencies = row["dependencies"]
    if not isinstance(raw_dependencies, list):
        raise ValueError("toolchain dependencies must be a list")
    dependencies: list[DependencyConstraint] = []
    for value in raw_dependencies:
        dependency = _mapping(value, "toolchain dependency")
        _exact_keys(
            dependency, {"name", "version_constraint"}, "toolchain dependency")
        dependencies.append(DependencyConstraint(
            dependency["name"], dependency["version_constraint"]))
    return ToolchainRequirement(
        toolchain_id=row["toolchain_id"],
        executable=row["executable"],
        runtime=row["runtime"],
        source_id=row["source_id"],
        content_digest=row["content_digest"],
        size=row["size"],
        dependencies=tuple(dependencies),
    )


def _parse_preparation(value: object) -> EnvironmentPreparationStep:
    row = _mapping(value, "environment preparation step")
    _exact_keys(
        row, {"command", "input_toolchain_ids", "sequence", "step_id"},
        "environment preparation step")
    return EnvironmentPreparationStep(
        sequence=row["sequence"],
        step_id=row["step_id"],
        command=_string_tuple(row["command"], "environment preparation command"),
        input_toolchain_ids=_string_tuple(
            row["input_toolchain_ids"], "environment preparation toolchains"),
    )


def _parse_network(value: object) -> NetworkCacheRequirements:
    row = _mapping(value, "network/cache requirements")
    _exact_keys(row, {
        "allowed_sources", "cache_namespace", "network_required", "offline_capable",
    }, "network/cache requirements")
    return NetworkCacheRequirements(
        network_required=row["network_required"],
        allowed_sources=_string_tuple(row["allowed_sources"], "allowed sources"),
        cache_namespace=row["cache_namespace"],
        offline_capable=row["offline_capable"],
    )


def _parse_binding(value: object) -> FrozenRoleBinding:
    row = _mapping(value, "role binding")
    _exact_keys(row, {
        "binding", "legacy_execution", "legacy_role", "source_schema",
    }, "role binding")
    binding_row = _mapping(row["binding"], "canonical role binding")
    _exact_keys(
        binding_row, {"backend", "execution_category", "role"},
        "canonical role binding")
    return FrozenRoleBinding(
        binding=RoleBinding(
            role=Role(binding_row["role"]),
            execution_category=ExecutionCategory(binding_row["execution_category"]),
            backend=binding_row["backend"],
        ),
        source_schema=row["source_schema"],
        legacy_role=row["legacy_role"],
        legacy_execution=row["legacy_execution"],
    )


def _parse_required_check(value: object) -> RequiredCheck:
    row = _mapping(value, "required check")
    _exact_keys(row, {
        "authoritative_executor", "check_id", "command", "command_input_digest",
        "environment", "expected_evidence_kinds", "expected_exit_codes",
        "fixture_digests", "phase", "red_expected_exit_codes",
        "required_toolchain_ids", "sandbox", "worker_execution_required",
        "working_directory",
    }, "required check")
    raw_environment = row["environment"]
    if not isinstance(raw_environment, list):
        raise ValueError("required check environment must be a list")
    environment: list[EnvironmentInput] = []
    for value in raw_environment:
        env = _mapping(value, "required check environment")
        _exact_keys(
            env,
            {"name", "normalization", "source", "value_digest"},
            "required check environment",
        )
        environment.append(EnvironmentInput(
            name=env["name"],
            source=ChildEnvironmentSource(env["source"]),
            normalization=ChildEnvironmentNormalization(env["normalization"]),
            value_digest=env["value_digest"],
        ))
    exits = row["expected_exit_codes"]
    red_exits = row["red_expected_exit_codes"]
    if not isinstance(exits, list) or not isinstance(red_exits, list):
        raise ValueError("required check expected exits must be lists")
    definition = CheckDefinition(
        check_id=row["check_id"],
        phase=row["phase"],
        command=_string_tuple(row["command"], "required check command"),
        working_directory=WorkingDirectoryRule(row["working_directory"]),
        expected_exit_codes=tuple(exits),
        expected_evidence_kinds=_string_tuple(
            row["expected_evidence_kinds"], "required check evidence kinds"),
        environment=tuple(environment),
        fixture_digests=_string_tuple(
            row["fixture_digests"], "required check fixture digests"),
        required_toolchain_ids=_string_tuple(
            row["required_toolchain_ids"], "required check toolchains"),
        sandbox=_parse_sandbox(row["sandbox"]),
        worker_execution_required=row["worker_execution_required"],
        red_expected_exit_codes=tuple(red_exits),
    )
    executor = ExecutorKind(row["authoritative_executor"])
    if executor is not ExecutorKind.ENGINE:
        raise ValueError("authoritative deterministic check executor must be engine")
    return RequiredCheck(
        **definition.__dict__,
        authoritative_executor=executor,
        command_input_digest=_canonical_digest(
            row["command_input_digest"], "command input digest"),
    )


def _parse_plan(
        payload: bytes, reference_digest: str, spec: RunSpec) -> VerificationPlan:
    try:
        decoded = json.loads(payload.decode("utf-8"))
        row = _mapping(decoded, "VerificationPlan")
        _exact_keys(row, {
            "base_snapshot_digest", "environment_preparation",
            "environment_preparation_digest", "job_id", "network_cache_requirements",
            "profile_content_digest", "profile_locator", "required_checks",
            "required_toolchains", "role_bindings", "run_id", "run_spec_digest",
            "schema", "task_id", "verifier_sandbox",
        }, "VerificationPlan")
        if row["schema"] != _PLAN_SCHEMA or row["run_id"] != spec.run_id:
            raise ValueError("VerificationPlan schema or run identity is invalid")
        raw_checks = row["required_checks"]
        raw_toolchains = row["required_toolchains"]
        raw_preparation = row["environment_preparation"]
        raw_bindings = row["role_bindings"]
        if not all(isinstance(item, list) for item in (
                raw_checks, raw_toolchains, raw_preparation, raw_bindings)):
            raise ValueError("VerificationPlan collections must be lists")
        plan = VerificationPlan(
            run_id=row["run_id"],
            job_id=row["job_id"],
            task_id=row["task_id"],
            run_spec_digest=_canonical_digest(
                row["run_spec_digest"], "run spec digest"),
            base_snapshot_digest=_canonical_digest(
                row["base_snapshot_digest"], "base snapshot digest"),
            profile_locator=_relative_locator(row["profile_locator"], "profile locator"),
            profile_content_digest=_canonical_digest(
                row["profile_content_digest"], "profile content digest"),
            role_bindings=tuple(_parse_binding(item) for item in raw_bindings),
            required_checks=tuple(_parse_required_check(item) for item in raw_checks),
            required_toolchains=tuple(
                _parse_toolchain(item) for item in raw_toolchains),
            environment_preparation=tuple(
                _parse_preparation(item) for item in raw_preparation),
            environment_preparation_digest=_canonical_digest(
                row["environment_preparation_digest"],
                "environment preparation digest"),
            network_cache_requirements=_parse_network(
                row["network_cache_requirements"]),
            verifier_sandbox=_parse_sandbox(row["verifier_sandbox"]),
            verification_plan_digest=_canonical_digest(
                reference_digest, "VerificationPlan reference digest"),
        )
        if (plan.job_id != spec.job_id
                or plan.task_id != spec.job_input.task_id
                or plan.run_spec_digest != spec.run_spec_digest
                or plan.base_snapshot_digest != spec.base_snapshot.digest):
            raise ValueError("VerificationPlan does not match its frozen RunSpec")
        if plan.profile_locator != _PROFILE_LOCATOR:
            raise ValueError("VerificationPlan profile locator is not canonical")
        if tuple(item.check_id for item in plan.required_checks) != tuple(sorted(
                item.check_id for item in plan.required_checks)):
            raise ValueError("required checks are not canonically ordered")
        definition = VerificationPlanDefinition(
            required_checks=tuple(CheckDefinition(
                check_id=item.check_id,
                phase=item.phase,
                command=item.command,
                working_directory=item.working_directory,
                expected_exit_codes=item.expected_exit_codes,
                expected_evidence_kinds=item.expected_evidence_kinds,
                environment=item.environment,
                fixture_digests=item.fixture_digests,
                required_toolchain_ids=item.required_toolchain_ids,
                sandbox=item.sandbox,
                worker_execution_required=item.worker_execution_required,
                red_expected_exit_codes=item.red_expected_exit_codes,
            ) for item in plan.required_checks),
            required_toolchains=plan.required_toolchains,
            environment_preparation=plan.environment_preparation,
            network_cache_requirements=plan.network_cache_requirements,
            verifier_sandbox=plan.verifier_sandbox,
        )
        if _environment_preparation_digest(definition) != plan.environment_preparation_digest:
            raise ValueError("environment preparation digest does not match plan inputs")
        for check, definition_check in zip(plan.required_checks, definition.required_checks):
            if check.command_input_digest != _command_input_digest(
                    definition_check, spec, plan.required_toolchains,
                    plan.environment_preparation_digest):
                raise ValueError(
                    f"check {check.check_id!r} command input digest does not match")
            if check.authoritative_executor is not ExecutorKind.ENGINE:
                raise ValueError("required check is not engine-owned")
        if {item.binding.role for item in plan.role_bindings} != {
                Role.WORKER, Role.VERIFIER} or len(plan.role_bindings) != 2:
            raise ValueError("plan lacks exact worker/verifier role bindings")
        if plan.canonical_bytes() != payload or _digest(payload) != plan.verification_plan_digest:
            raise ValueError("VerificationPlan bytes are not canonical or digest-bound")
        return plan
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError,
            VerificationPlanIncompleteError) as error:
        raise VerificationPlanArtifactError(spec.run_id, str(error)) from error


def load_verification_plan(
        run_id: str, *, start: Path | None = None) -> VerificationPlan:
    """Load and fully revalidate one immutable plan from its run-owned reference."""
    root = _project_root(start)
    spec = load_run_spec(run_id, start=root)
    with RunStore.open(root) as store:
        try:
            reference = store.get_artifact_reference(
                f"{_PLAN_REFERENCE_PREFIX}{run_id}")
        except RecordNotFoundError as error:
            raise VerificationPlanMissingError(run_id) from error
        if reference.kind is not ArtifactReferenceKind.EVIDENCE:
            raise VerificationPlanArtifactError(
                run_id, "VerificationPlan reference is not EVIDENCE")
        payload = ArtifactStore(root).read_reference(reference)
    return _parse_plan(payload, reference.digest, spec)


def record_runner_proof(
        context: RunnerContext, capabilities: RunnerCapabilities, *,
        observed_by: ExecutorKind = ExecutorKind.ENGINE) -> RunnerProof:
    """Record a run-independent runner capability proof over bounded observation axes."""
    if not isinstance(context, RunnerContext):
        raise TypeError("context must be a RunnerContext")
    if not isinstance(capabilities, RunnerCapabilities):
        raise TypeError("capabilities must be RunnerCapabilities")
    observer = ExecutorKind(observed_by)
    if observer is not ExecutorKind.ENGINE:
        raise NonAuthoritativeCheckResultError("runner-capability-probe", observer)
    return RunnerProof(
        context=context,
        runner_capabilities_digest=capabilities.digest,
    )


def require_reusable_runner_proof(
        proof: RunnerProof, current: RunnerContext,
        capabilities: RunnerCapabilities) -> RunnerProof:
    """Require exact bounded-axis equivalence; mismatches demand a fresh probe."""
    if not isinstance(proof, RunnerProof):
        raise TypeError("proof must be a RunnerProof")
    if proof.observed_by is not ExecutorKind.ENGINE:
        raise NonAuthoritativeCheckResultError(
            "runner-capability-probe", proof.observed_by)
    actual_digest = _digest(proof.canonical_bytes())
    if actual_digest != proof.proof_digest:
        raise RunnerProofRevalidationRequired(("proof-digest",))
    mismatches: list[str] = []
    if proof.context.checkout_identity != current.checkout_identity:
        mismatches.append("checkout")
    if proof.context.machine_identity != current.machine_identity:
        mismatches.append("machine")
    if proof.context.principal_identity != current.principal_identity:
        mismatches.append("principal")
    if proof.context.project_config_digest != current.project_config_digest:
        mismatches.append("project-config-content")
    if proof.context.profile_config_digest != current.profile_config_digest:
        mismatches.append("profile-config-content")
    if proof.context.runtime_observations != current.runtime_observations:
        mismatches.append("runtime-observations")
    if proof.runner_capabilities_digest != capabilities.digest:
        mismatches.append("runner-capabilities")
    if mismatches:
        raise RunnerProofRevalidationRequired(mismatches)
    return proof


def runner_probe_unavailable(detail: str) -> None:
    """Fail loud when a required fresh runner probe cannot be observed."""
    raise RunnerProbeUnavailableError(_nonempty_line(detail, "runner probe detail"))


def _read_config_digest(path: Path, label: str) -> str:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("path is not a regular non-symlink file")
        payload = path.read_bytes()
    except OSError as error:
        raise RunnerProbeUnavailableError(f"{label} cannot be read: {error}") from error
    return _digest(payload)


def verify_toolchains_for_execution(
        plan: VerificationPlan,
        materialized: Sequence[MaterializedToolchain]) -> tuple[ToolchainObservation, ...]:
    """Rehash exact declared toolchain bytes without PATH, index, or cache fallback."""
    materialized_by_id: dict[str, MaterializedToolchain] = {}
    for item in materialized:
        if not isinstance(item, MaterializedToolchain):
            raise TypeError("materialized toolchains must be MaterializedToolchain values")
        if item.toolchain_id in materialized_by_id:
            raise ToolchainUnavailableError(item.toolchain_id, "materialization is ambiguous")
        materialized_by_id[item.toolchain_id] = item
    expected_ids = {item.toolchain_id for item in plan.required_toolchains}
    if set(materialized_by_id) != expected_ids:
        missing = sorted(expected_ids - set(materialized_by_id))
        extra = sorted(set(materialized_by_id) - expected_ids)
        detail = "exact materialization set differs"
        if missing:
            detail += f"; missing={missing}"
        if extra:
            detail += f"; extra={extra}"
        raise ToolchainUnavailableError(
            missing[0] if missing else extra[0], detail)

    observations: list[ToolchainObservation] = []
    for requirement in plan.required_toolchains:
        item = materialized_by_id[requirement.toolchain_id]
        if item.source_id != requirement.source_id:
            raise ToolchainUnavailableError(
                requirement.toolchain_id,
                f"source {item.source_id!r} does not match frozen {requirement.source_id!r}",
            )
        try:
            before = item.path.lstat()
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise OSError("materialized path is not a regular non-symlink file")
            payload = item.path.read_bytes()
            after = item.path.lstat()
        except OSError as error:
            raise ToolchainUnavailableError(requirement.toolchain_id, str(error)) from error
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ToolchainUnavailableError(
                requirement.toolchain_id, "bytes changed while being rehashed")
        observed = _digest(payload)
        if observed != requirement.content_digest:
            raise ToolchainDigestMismatchError(
                requirement.toolchain_id, requirement.content_digest, observed)
        if len(payload) != requirement.size:
            raise ToolchainUnavailableError(
                requirement.toolchain_id,
                f"size {len(payload)} does not match frozen {requirement.size}",
            )
        observations.append(ToolchainObservation(
            toolchain_id=requirement.toolchain_id,
            source_id=requirement.source_id,
            content_digest=observed,
            size=len(payload),
        ))
    return tuple(observations)


def _role_capabilities_for(
        capabilities: RunnerCapabilities, binding: RoleBinding) -> tuple[RoleCapability, ...]:
    return tuple(
        item for item in capabilities.role_capabilities if item.binding == binding)


def _check_capabilities(
        plan: VerificationPlan, capabilities: CapabilitySet,
        toolchain_observations: tuple[ToolchainObservation, ...],
        ) -> tuple[RoleCapability, EnvironmentPreparationReceipt]:
    worker = plan.binding_for(Role.WORKER).binding
    verifier = plan.binding_for(Role.VERIFIER).binding
    for binding in (worker, verifier):
        if binding.execution_category not in capabilities.runner.execution_categories:
            raise UnsupportedExecutionCategoryError(binding)
        if not _role_capabilities_for(capabilities.runner, binding):
            raise UnsupportedBindingError(binding)

    for check in plan.required_checks:
        if check.sandbox not in capabilities.runner.engine_sandboxes:
            raise UnsupportedSandboxError(f"engine check {check.check_id}", check.sandbox)
        worker_capability = tuple(
            item for item in _role_capabilities_for(capabilities.runner, worker)
            if item.sandbox == check.sandbox
        )
        if not worker_capability:
            raise UnsupportedSandboxError(f"worker check {check.check_id}", check.sandbox)

    verifier_capability = tuple(
        item for item in _role_capabilities_for(capabilities.runner, verifier)
        if item.sandbox == plan.verifier_sandbox
    )
    if not verifier_capability:
        raise UnsupportedSandboxError("verifier", plan.verifier_sandbox)
    if len(verifier_capability) != 1:
        raise VerifierCapabilityUnavailableError(
            "verifier capability is ambiguous for the frozen binding and sandbox")
    selected_verifier = verifier_capability[0]
    if not all((
            selected_verifier.accepts_frozen_base,
            selected_verifier.accepts_patch_bytes,
            selected_verifier.accepts_result_digest,
            selected_verifier.emits_artifacts,
    )):
        raise VerifierCapabilityUnavailableError(
            "verifier must accept frozen base, patch bytes, and result digest, "
            "then emit an artifact")
    preparation_receipts = tuple(
        item for item in capabilities.environment_preparation_receipts
        if item.environment_preparation_digest == plan.environment_preparation_digest
    )
    if len(preparation_receipts) != 1:
        raise EnvironmentPreparationUnavailableError(
            "frozen environment preparation has no exact capability receipt")
    preparation_receipt = preparation_receipts[0]
    if preparation_receipt.network_cache_requirements != plan.network_cache_requirements:
        raise EnvironmentPreparationUnavailableError(
            "environment preparation receipt does not match frozen network/cache requirements")
    if preparation_receipt.toolchain_observations != toolchain_observations:
        raise EnvironmentPreparationUnavailableError(
            "environment preparation receipt does not match rehashed toolchain observations")

    probe_by_key = {
        (probe.check_id, probe.target): probe for probe in capabilities.check_probes
    }
    expected_probe_keys = {
        (check.check_id, target)
        for check in plan.required_checks for target in ProbeTarget
    }
    extra_probe_keys = sorted(
        set(probe_by_key) - expected_probe_keys,
        key=lambda item: (item[0], item[1].value),
    )
    if extra_probe_keys:
        check_id, target = extra_probe_keys[0]
        raise RequiredCheckUnexecutableError(
            check_id, target, "probe is not declared by the frozen plan")
    for check in plan.required_checks:
        for target in ProbeTarget:
            probe = probe_by_key.get((check.check_id, target))
            if probe is None:
                raise RequiredCheckUnexecutableError(
                    check.check_id, target, "capability probe is absent")
            if (probe.command != check.command
                    or probe.command_input_digest != check.command_input_digest):
                raise RequiredCheckUnexecutableError(
                    check.check_id, target, "probe is not bound to the exact frozen command")
            if (probe.environment_preparation_artifact_digest
                    != preparation_receipt.artifact_digest):
                raise RequiredCheckUnexecutableError(
                    check.check_id, target,
                    "probe is not bound to the selected environment preparation receipt",
                )
            expected_environment = {item.name: item for item in check.environment}
            observed_environment = {
                item.name: item for item in probe.child_environment
            }
            extras = sorted(set(observed_environment) - set(expected_environment))
            if extras:
                raise ChildEnvironmentNotAllowedError(
                    extras[0], f"not declared by frozen check {check.check_id!r}")
            for name, expected in expected_environment.items():
                if observed_environment.get(name) != expected:
                    raise ChildEnvironmentRequiredMissingError(check.check_id, name)
            if not probe.entrypoint_ready or not probe.structured_result:
                raise RequiredCheckUnexecutableError(
                    check.check_id, target,
                    "entrypoint, dependency, plugin, or structured result is unavailable")

    red_by_check = {probe.check_id: probe for probe in capabilities.red_first_probes}
    expected_red_ids = {
        check.check_id for check in plan.required_checks
        if check.phase is CheckPhase.RED_FIRST
    }
    extra_red_ids = sorted(set(red_by_check) - expected_red_ids)
    if extra_red_ids:
        raise RedFirstEvidenceUnavailableError(
            extra_red_ids[0], "plan does not declare RED-first acceptance for this check")
    for check in plan.required_checks:
        if check.phase is not CheckPhase.RED_FIRST:
            continue
        probe = red_by_check.get(check.check_id)
        if probe is None:
            raise RedFirstEvidenceUnavailableError(
                check.check_id, "engine-owned base-snapshot probe is absent")
        if (probe.base_snapshot_digest != plan.base_snapshot_digest
                or probe.command != check.command
                or probe.command_input_digest != check.command_input_digest
                or probe.environment_preparation_artifact_digest
                != preparation_receipt.artifact_digest
                or probe.child_environment != check.environment):
            raise RedFirstEvidenceUnavailableError(
                check.check_id,
                "probe is not bound to the frozen base, command, environment, and preparation",
            )
        if not probe.structured_result:
            raise RedFirstEvidenceUnavailableError(
                check.check_id, "base-snapshot command did not produce a structured result")
        if probe.exit_code not in check.red_expected_exit_codes:
            raise RedFirstEvidenceUnavailableError(
                check.check_id,
                f"base exit {probe.exit_code} is not an expected RED failure",
            )
    return selected_verifier, preparation_receipt


def _engine_actions(
        plan: VerificationPlan,
        preparation_artifact_digest: str,
        ) -> tuple[EngineCheckAction, ...]:
    return tuple(EngineCheckAction(
        run_id=plan.run_id,
        verification_plan_digest=plan.verification_plan_digest,
        check_id=check.check_id,
        phase=check.phase,
        command=check.command,
        command_input_digest=check.command_input_digest,
        environment_preparation_artifact_digest=preparation_artifact_digest,
        child_environment=check.environment,
        working_directory=check.working_directory,
        expected_exit_codes=check.expected_exit_codes,
        red_expected_exit_codes=check.red_expected_exit_codes,
        expected_evidence_kinds=check.expected_evidence_kinds,
    ) for check in plan.required_checks)


def _persist_capability_receipts(
        root: Path, run_id: str,
        payloads: dict[str, bytes]) -> tuple[ArtifactReference, ...]:

    artifact_store = ArtifactStore(root)
    references: list[ArtifactReference] = []
    for digest, payload in sorted(payloads.items()):
        if _digest(payload) != digest:
            raise VerificationPlanArtifactError(
                run_id, f"capability receipt {digest} is not content-bound")
        stored = artifact_store.write(payload)
        if stored.digest != digest or artifact_store.read(digest) != payload:
            raise VerificationPlanArtifactError(
                run_id, f"capability receipt {digest} failed verified publication")
        references.append(ArtifactReference(
            reference_id=f"preflight-receipt:{run_id}:{digest.removeprefix('sha256:')}",
            kind=ArtifactReferenceKind.EVIDENCE,
            digest=digest,
            size=stored.size,
        ))
    return tuple(references)


def _capability_receipt_payloads(
        proof: RunnerProof, capabilities: CapabilitySet) -> dict[str, bytes]:
    payloads = {
        proof.proof_digest: proof.canonical_bytes(),
        capabilities.digest: capabilities.canonical_bytes(),
    }
    for receipt in capabilities.environment_preparation_receipts:
        payloads[receipt.artifact_digest] = receipt.canonical_bytes()
    for capability in capabilities.runner.role_capabilities:
        payloads[capability.probe_artifact_digest] = capability.canonical_receipt_bytes()
    for probe in capabilities.check_probes:
        payloads[probe.artifact_digest] = probe.canonical_receipt_bytes()
    for probe in capabilities.red_first_probes:
        payloads[probe.artifact_digest] = probe.canonical_receipt_bytes()
    return payloads


def _receipt_reference_id(run_id: str, digest: str) -> str:
    return f"preflight-receipt:{run_id}:{digest.removeprefix('sha256:')}"


def _load_receipt_manifest(
        root: Path, store: RunStore, run_id: str,
        digests: tuple[str, ...]) -> dict[str, bytes]:
    artifact_store = ArtifactStore(root)
    payloads: dict[str, bytes] = {}
    for digest in digests:
        try:
            reference = store.get_artifact_reference(
                _receipt_reference_id(run_id, digest))
        except RecordNotFoundError as error:
            raise PreflightEvidenceArtifactError(
                run_id, f"capability receipt reference is missing for {digest}") from error
        if (reference.kind is not ArtifactReferenceKind.EVIDENCE
                or reference.digest != digest):
            raise PreflightEvidenceArtifactError(
                run_id, f"capability receipt reference is invalid for {digest}")
        try:
            payloads[digest] = artifact_store.read_reference(reference)
        except ArtifactError as error:
            raise PreflightEvidenceArtifactError(run_id, str(error)) from error
    return payloads


def _load_dispatch_authority(
        run_id: str, *, accepted_run_states: frozenset[str],
        start: Path | None = None) -> DispatchReady:
    """Reconstruct dispatch authority only from committed, rehashed preflight artifacts."""
    root = _project_root(start)
    plan = load_verification_plan(run_id, start=root)
    with RunStore.open(root) as store:
        run = store.get_run(run_id)
        if run.state not in accepted_run_states:
            expected = (
                next(iter(accepted_run_states))
                if len(accepted_run_states) == 1
                else "|".join(sorted(accepted_run_states))
            )
            raise VerificationPlanStateError(run_id, run.state, expected)
        try:
            reference = store.get_artifact_reference(
                f"{_PREFLIGHT_REFERENCE_PREFIX}{run_id}")
        except RecordNotFoundError as error:
            raise PreflightEvidenceArtifactError(
                run_id, "dispatch-ready run has no preflight evidence reference") from error
        if reference.kind is not ArtifactReferenceKind.EVIDENCE:
            raise PreflightEvidenceArtifactError(
                run_id, "preflight evidence reference is not EVIDENCE")
        try:
            payload = ArtifactStore(root).read_reference(reference)
        except ArtifactError as error:
            raise PreflightEvidenceArtifactError(run_id, str(error)) from error

        try:
            decoded = json.loads(payload.decode("utf-8"))
            row = _mapping(decoded, "preflight evidence")
            _exact_keys(row, {
                "authority_scope", "capability_probes", "capability_set_digest",
                "engine_actions", "environment_preparation_artifact_digest",
                "environment_preparation_digest", "receipt_artifact_digests",
                "red_first_probes", "run_id", "runner_proof_digest", "schema",
                "toolchain_observations", "verification_plan_digest",
                "verifier_capability_digest",
            }, "preflight evidence")
            if (row["schema"] != _PREFLIGHT_SCHEMA
                    or row["run_id"] != run_id
                    or row["authority_scope"] != "dispatch-capability-only"):
                raise ValueError("preflight schema, run, or authority scope is invalid")
            if (row["verification_plan_digest"] != plan.verification_plan_digest
                    or row["environment_preparation_digest"]
                    != plan.environment_preparation_digest):
                raise ValueError("preflight evidence does not match the frozen plan")
            if payload != _canonical_json(row) or _digest(payload) != reference.digest:
                raise ValueError("preflight evidence bytes are not canonical or digest-bound")

            proof_digest = _canonical_digest(
                row["runner_proof_digest"], "preflight runner proof digest")
            capability_digest = _canonical_digest(
                row["capability_set_digest"], "preflight capability set digest")
            preparation_artifact_digest = _canonical_digest(
                row["environment_preparation_artifact_digest"],
                "preflight environment preparation artifact digest",
            )
            verifier_digest = _canonical_digest(
                row["verifier_capability_digest"],
                "preflight verifier capability digest",
            )
            raw_manifest = row["receipt_artifact_digests"]
            if not isinstance(raw_manifest, list):
                raise ValueError("preflight receipt manifest must be a list")
            manifest = tuple(
                _canonical_digest(item, "preflight receipt digest")
                for item in raw_manifest
            )
            if manifest != tuple(sorted(manifest)):
                raise ValueError("preflight receipt manifest is not canonically ordered")
            _unique(manifest, "preflight receipt manifest")
            receipts = _load_receipt_manifest(root, store, run_id, manifest)

            expected_observations = tuple(ToolchainObservation(
                item.toolchain_id, item.source_id, item.content_digest, item.size)
                for item in plan.required_toolchains
            )
            if row["toolchain_observations"] != _jsonable(expected_observations):
                raise ValueError("preflight toolchain observations do not match the plan")
            expected_preparation = EnvironmentPreparationReceipt(
                environment_preparation_digest=plan.environment_preparation_digest,
                network_cache_requirements=plan.network_cache_requirements,
                toolchain_observations=expected_observations,
            )
            if (preparation_artifact_digest != expected_preparation.artifact_digest
                    or receipts.get(preparation_artifact_digest)
                    != expected_preparation.canonical_bytes()):
                raise ValueError("environment preparation receipt is missing or invalid")

            expected_verifier = RoleCapability(
                binding=plan.binding_for(Role.VERIFIER).binding,
                sandbox=plan.verifier_sandbox,
                accepts_frozen_base=True,
                accepts_patch_bytes=True,
                accepts_result_digest=True,
                emits_artifacts=True,
            )
            if (verifier_digest != expected_verifier.probe_artifact_digest
                    or receipts.get(verifier_digest)
                    != expected_verifier.canonical_receipt_bytes()):
                raise ValueError("verifier capability receipt is missing or invalid")

            raw_probes = row["capability_probes"]
            if not isinstance(raw_probes, list):
                raise ValueError("capability probes must be a list")
            checks = {check.check_id: check for check in plan.required_checks}
            reconstructed_probes: list[CheckCapabilityProbe] = []
            for raw_probe in raw_probes:
                probe_row = _mapping(raw_probe, "capability probe")
                check_id = probe_row.get("check_id")
                check = checks.get(check_id)
                if check is None:
                    raise ValueError("capability probe names an unknown check")
                exit_code = probe_row.get("exit_code")
                if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                    raise ValueError("capability probe exit_code is invalid")
                probe = CheckCapabilityProbe(
                    check_id=check.check_id,
                    target=ProbeTarget(probe_row.get("target")),
                    command=check.command,
                    command_input_digest=check.command_input_digest,
                    environment_preparation_artifact_digest=(
                        preparation_artifact_digest),
                    child_environment=check.environment,
                    entrypoint_ready=True,
                    structured_result=True,
                    exit_code=exit_code,
                )
                if probe_row != _probe_payload(probe):
                    raise ValueError("capability probe is not the canonical successful receipt")
                if receipts.get(probe.artifact_digest) != probe.canonical_receipt_bytes():
                    raise ValueError("capability probe receipt bytes are missing or invalid")
                reconstructed_probes.append(probe)
            if {(probe.check_id, probe.target) for probe in reconstructed_probes} != {
                    (check.check_id, target)
                    for check in plan.required_checks for target in ProbeTarget}:
                raise ValueError("preflight lacks the exact engine/worker probe set")

            raw_red = row["red_first_probes"]
            if not isinstance(raw_red, list):
                raise ValueError("RED-first probes must be a list")
            reconstructed_red: list[RedFirstProbe] = []
            for raw_probe in raw_red:
                probe_row = _mapping(raw_probe, "RED-first probe")
                check = checks.get(probe_row.get("check_id"))
                if check is None or check.phase is not CheckPhase.RED_FIRST:
                    raise ValueError("RED-first probe names an ineligible check")
                exit_code = probe_row.get("exit_code")
                if (isinstance(exit_code, bool) or not isinstance(exit_code, int)
                        or exit_code not in check.red_expected_exit_codes):
                    raise ValueError("RED-first probe exit is not the frozen expected failure")
                probe = RedFirstProbe(
                    check_id=check.check_id,
                    base_snapshot_digest=plan.base_snapshot_digest,
                    command=check.command,
                    command_input_digest=check.command_input_digest,
                    environment_preparation_artifact_digest=(
                        preparation_artifact_digest),
                    child_environment=check.environment,
                    structured_result=True,
                    exit_code=exit_code,
                )
                if probe_row != _red_probe_payload(probe):
                    raise ValueError("RED-first probe is not the canonical successful receipt")
                if receipts.get(probe.artifact_digest) != probe.canonical_receipt_bytes():
                    raise ValueError("RED-first probe receipt bytes are missing or invalid")
                reconstructed_red.append(probe)
            if {probe.check_id for probe in reconstructed_red} != {
                    check.check_id for check in plan.required_checks
                    if check.phase is CheckPhase.RED_FIRST}:
                raise ValueError("preflight lacks the exact RED-first probe set")

            required_receipts = {
                proof_digest,
                capability_digest,
                preparation_artifact_digest,
                verifier_digest,
                *(probe.artifact_digest for probe in reconstructed_probes),
                *(probe.artifact_digest for probe in reconstructed_red),
            }
            if not required_receipts.issubset(receipts):
                raise ValueError("preflight receipt manifest is incomplete")
            actions = _engine_actions(plan, preparation_artifact_digest)
            if row["engine_actions"] != [_action_payload(action) for action in actions]:
                raise ValueError("engine actions do not match the frozen prepared plan")
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise PreflightEvidenceArtifactError(run_id, str(error)) from error

    return DispatchReady(
        run_id=run_id,
        verification_plan_digest=plan.verification_plan_digest,
        preflight_evidence_digest=reference.digest,
        engine_actions=actions,
    )


def load_dispatch_ready(
        run_id: str, *, start: Path | None = None) -> DispatchReady:
    """Reconstruct current operational dispatch-ready authority."""
    return _load_dispatch_authority(
        run_id,
        accepted_run_states=frozenset({"dispatch-ready"}),
        start=start,
    )


def load_terminal_safe_dispatch_authority(
        run_id: str, *, start: Path | None = None) -> DispatchReady:
    """Reconstruct immutable dispatch authority in supported public reload states."""
    return _load_dispatch_authority(
        run_id,
        accepted_run_states=frozenset({
            "closeout-ready",
            "completed",
            "dispatch-ready",
            "failed",
        }),
        start=start,
    )


def load_runner_proof(
        run_id: str, *, start: Path | None = None) -> RunnerProof:
    """Load a reusable runner proof through one validated dispatch-ready run."""
    root = _project_root(start)
    load_dispatch_ready(run_id, start=root)
    with RunStore.open(root) as store:
        evidence_reference = store.get_artifact_reference(
            f"{_PREFLIGHT_REFERENCE_PREFIX}{run_id}")
        try:
            evidence = json.loads(
                ArtifactStore(root).read_reference(evidence_reference).decode("utf-8"))
            proof_digest = _canonical_digest(
                evidence["runner_proof_digest"], "runner proof digest")
            proof_payload = _load_receipt_manifest(
                root, store, run_id, (proof_digest,))[proof_digest]
            row = _mapping(
                json.loads(proof_payload.decode("utf-8")), "runner proof")
            _exact_keys(row, {
                "context", "observed_by", "runner_capabilities_digest", "schema",
            }, "runner proof")
            context_row = _mapping(row["context"], "runner proof context")
            _exact_keys(context_row, {
                "checkout_identity", "machine_identity", "principal_identity",
                "profile_config_digest", "project_config_digest",
                "runtime_observations",
            }, "runner proof context")
            raw_observations = context_row["runtime_observations"]
            if not isinstance(raw_observations, list):
                raise ValueError("runner observations must be a list")
            observations: list[RuntimeObservation] = []
            for value in raw_observations:
                observation = _mapping(value, "runner observation")
                _exact_keys(
                    observation,
                    {"axis", "source", "status", "value_digest"},
                    "runner observation",
                )
                observations.append(RuntimeObservation(
                    axis=observation["axis"],
                    source=observation["source"],
                    status=ObservationStatus(observation["status"]),
                    value_digest=observation["value_digest"],
                ))
            if row["schema"] != _RUNNER_PROOF_SCHEMA:
                raise ValueError("runner proof schema is invalid")
            if ExecutorKind(row["observed_by"]) is not ExecutorKind.ENGINE:
                raise ValueError("runner proof is not engine-observed")
            proof = RunnerProof(
                context=RunnerContext(
                    checkout_identity=context_row["checkout_identity"],
                    machine_identity=context_row["machine_identity"],
                    principal_identity=context_row["principal_identity"],
                    project_config_digest=context_row["project_config_digest"],
                    profile_config_digest=context_row["profile_config_digest"],
                    runtime_observations=tuple(observations),
                ),
                runner_capabilities_digest=row["runner_capabilities_digest"],
            )
            if proof.proof_digest != proof_digest or proof.canonical_bytes() != proof_payload:
                raise ValueError("runner proof is not canonical or content-bound")
            return proof
        except (ArtifactError, KeyError, TypeError, ValueError, UnicodeError,
                json.JSONDecodeError) as error:
            raise PreflightEvidenceArtifactError(run_id, str(error)) from error


def preflight_for_dispatch(
        run_id: str, *, capabilities: CapabilitySet,
        materialized_toolchains: Sequence[MaterializedToolchain],
        current_runner_context: RunnerContext,
        reusable_runner_proof: RunnerProof,
        start: Path | None = None) -> DispatchReady:
    """Authorize dispatch only from a durable complete plan and exact current capability proof."""
    root = _project_root(start)
    plan = load_verification_plan(run_id, start=root)
    if not isinstance(capabilities, CapabilitySet):
        raise TypeError("capabilities must be a CapabilitySet")
    require_reusable_runner_proof(
        reusable_runner_proof, current_runner_context, capabilities.runner)
    project_config_digest = _read_config_digest(
        root / _PROJECT_CONFIG_LOCATOR, "project config")
    profile_config_digest = _read_config_digest(
        root / plan.profile_locator, "profile config")
    config_mismatches = []
    if current_runner_context.project_config_digest != project_config_digest:
        config_mismatches.append("project-config-content")
    if current_runner_context.profile_config_digest != profile_config_digest:
        config_mismatches.append("profile-config-content")
    if plan.profile_content_digest != profile_config_digest:
        config_mismatches.append("frozen-profile-content")
    if config_mismatches:
        raise RunnerProofRevalidationRequired(config_mismatches)

    toolchain_observations = verify_toolchains_for_execution(
        plan, materialized_toolchains)
    verifier_capability, preparation_receipt = _check_capabilities(
        plan, capabilities, toolchain_observations)
    actions = _engine_actions(plan, preparation_receipt.artifact_digest)
    receipt_payloads = _capability_receipt_payloads(
        reusable_runner_proof, capabilities)
    candidate = PreflightEvidence(
        run_id=run_id,
        verification_plan_digest=plan.verification_plan_digest,
        runner_proof_digest=reusable_runner_proof.proof_digest,
        capability_set_digest=capabilities.digest,
        environment_preparation_digest=plan.environment_preparation_digest,
        environment_preparation_artifact_digest=preparation_receipt.artifact_digest,
        toolchain_observations=toolchain_observations,
        capability_probes=capabilities.check_probes,
        red_first_probes=capabilities.red_first_probes,
        receipt_artifact_digests=tuple(sorted(receipt_payloads)),
        verifier_capability_digest=verifier_capability.probe_artifact_digest,
        engine_actions=actions,
        authority_scope="dispatch-capability-only",
        preflight_evidence_digest="sha256:" + "0" * 64,
    )
    evidence = replace(
        candidate,
        preflight_evidence_digest=_digest(candidate.canonical_bytes()),
    )
    receipt_references = _persist_capability_receipts(
        root, run_id, receipt_payloads)

    with RunStore.open(root) as store:
        run = store.get_run(run_id)
        if run.state != "verification-plan-frozen":
            raise VerificationPlanStateError(
                run_id, run.state, "verification-plan-frozen")
        stored = ArtifactStore(root).write(evidence.canonical_bytes())
        if stored.digest != evidence.preflight_evidence_digest:
            raise VerificationPlanArtifactError(
                run_id, "stored preflight evidence digest changed")
        store.record_transition(
            EntityKind.RUN,
            run_id,
            expected_version=run.version,
            next_state="dispatch-ready",
            reason=TransitionReason.PLANNED,
            evidence_digest=stored.digest,
            artifact_references=(ArtifactReference(
                reference_id=f"{_PREFLIGHT_REFERENCE_PREFIX}{run_id}",
                kind=ArtifactReferenceKind.EVIDENCE,
                digest=stored.digest,
                size=stored.size,
            ), *receipt_references),
        )
    return load_dispatch_ready(run_id, start=root)


def reject_worker_check_result(
        action: EngineCheckAction, report: WorkerCheckReport) -> None:
    """Refuse a worker report even when it exactly matches the frozen engine action."""
    if not isinstance(action, EngineCheckAction):
        raise TypeError("action must be an EngineCheckAction")
    if not isinstance(report, WorkerCheckReport):
        raise TypeError("report must be a WorkerCheckReport")
    raise NonAuthoritativeCheckResultError(report.check_id, ExecutorKind.CARRIER)


__all__ = [
    "CapabilityPreflightRefusal",
    "CapabilitySet",
    "CheckCapabilityProbe",
    "CheckDefinition",
    "CheckPhase",
    "ChildEnvironmentNormalization",
    "ChildEnvironmentNotAllowedError",
    "ChildEnvironmentRequiredMissingError",
    "ChildEnvironmentSource",
    "DependencyConstraint",
    "DispatchReady",
    "EngineCheckAction",
    "EnvironmentInput",
    "EnvironmentPreparationStep",
    "EnvironmentPreparationReceipt",
    "EnvironmentPreparationUnavailableError",
    "FrozenRoleBinding",
    "MaterializedToolchain",
    "NetworkCacheRequirements",
    "NonAuthoritativeCheckResultError",
    "ObservationStatus",
    "PreflightEvidenceArtifactError",
    "PreflightError",
    "ProbeTarget",
    "ProfilePreflightRefusal",
    "RequiredCheck",
    "RequiredCheckUnexecutableError",
    "RedFirstEvidenceUnavailableError",
    "RedFirstProbe",
    "RoleCapability",
    "RunnerCapabilities",
    "RunnerContext",
    "RunnerProbeUnavailableError",
    "RunnerProof",
    "RunnerProofRevalidationRequired",
    "RuntimeObservation",
    "SandboxContract",
    "ToolchainDigestMismatchError",
    "ToolchainObservation",
    "ToolchainRequirement",
    "ToolchainUnavailableError",
    "UnsupportedBindingError",
    "UnsupportedExecutionCategoryError",
    "UnsupportedSandboxError",
    "VerificationPlan",
    "VerificationPlanArtifactError",
    "VerificationPlanDefinition",
    "VerificationPlanIncompleteError",
    "VerificationPlanMissingError",
    "VerificationPlanStateError",
    "VerifierCapabilityUnavailableError",
    "WorkingDirectoryRule",
    "freeze_verification_plan",
    "load_dispatch_ready",
    "load_runner_proof",
    "load_verification_plan",
    "preflight_for_dispatch",
    "record_runner_proof",
    "reject_worker_check_result",
    "require_reusable_runner_proof",
    "runner_probe_unavailable",
    "verify_toolchains_for_execution",
    "WorkerCheckReport",
]
