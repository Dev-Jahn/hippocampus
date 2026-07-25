"""One-task run-engine composition facade.

This module owns no retry policy and implements no effect, verification, or
cancellation protocol.  It wires the frozen inputs and typed results exposed by
the neighbouring run modules into the M1-B one-task vertical path.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from waystone.adapters.git import GitReadError, git_full_sha, git_rc, git_read_bytes
from waystone.core import WorkflowError
from waystone.jobs import completion, work_brief
from waystone.jobs.domain import ExecutionCategory, Role
from waystone.jobs.profile import RunAssembly as ProductionRunAssembly
from waystone.project.brief import FrameStatusRef, ProjectFactRef
from waystone.runs.artifacts import (
    ArtifactReference, ArtifactReferenceKind, ArtifactStore, StoredArtifact,
    validate_sha256_digest,
)
from waystone.runs.assurance import (
    AssurancePlan,
    Candidate,
    EvaluationEvidence,
    assert_evaluation_generation_available,
    ReviewCycleExhausted,
    ReviewCycle,
    ReviewerEvidence,
    assert_promotion_unblocked,
    execute_assurance_dag,
    parse_assurance_plan_bytes,
    parse_candidate_bytes,
    parse_evaluation_evidence_bytes,
    parse_evaluation_spec_bytes,
    parse_reviewer_evidence_bytes,
    parse_review_cycle_bytes,
    canonical_json as assurance_json,
)
from waystone.runs.cancel import CancellationEngine, CancellationResult
from waystone.runs.effects import (
    EffectEngine,
    EffectKind,
    EffectResultState,
    EffectStateRefusal,
    GitRefEffect,
    RunnerExecutionEffect,
)
from waystone.runs.environment import (
    RunnerEnvironment,
    RunnerExecutionDescriptor,
    RunnerExecutionDescriptorRefusal,
    build_runner_environment,
    freeze_runner_execution_descriptor,
    parse_runner_execution_descriptor,
    require_runner_execution_match,
)
from waystone.runs.lease import LeaseManager
from waystone.runs.observe import (
    RunSnapshot,
    json_projection,
    render_human,
    snapshot_run,
    watch_run,
)
from waystone.runs.preflight import (
    CapabilitySet,
    DispatchReady,
    EnvironmentPreparationReceipt,
    MaterializedToolchain,
    NetworkCacheRequirements,
    ObservationStatus,
    RoleCapability,
    RunnerCapabilities,
    RunnerContext,
    RunnerProof,
    RuntimeObservation,
    SandboxContract,
    VerificationPlan,
    VerificationPlanDefinition,
    freeze_verification_plan,
    load_dispatch_ready,
    load_verification_plan,
    preflight_for_dispatch,
    record_runner_proof,
)
from waystone.runs.outcome import OutcomePublication, publish_outcome
from waystone.runs.spec import (
    RunSpec,
    load_run_spec,
    plan_one_task_run,
    prepare_run_spec_revision,
)
from waystone.runs.store import (
    ContextNotCurrent,
    EntityKind,
    FilesystemInfo,
    RecordNotFoundError,
    RunStore,
    TransitionReason,
)
from waystone.runs.supervisor import (
    LivenessState, RunnerCandidateContext, RunnerInvocation, Supervisor,
    fingerprint_candidate_root,
)
from waystone.runs.transport import (
    ActionPlanRefusal,
    ActionTransport,
    EngineExecutorUnavailable,
    RunNotActionable,
)
from waystone.runs.worker_result import (
    AdaptedWorkerResult,
    ContextRequestedWorkerResult,
    CompletedWorkerResult,
    ContextResponse,
    RESULT_CONTROL_FILE,
    WorkerResultAdapter,
    parse_runner_completion_marker_v2_bytes,
    parse_context_response_bytes,
    revise_work_brief_for_response,
    capture_result_snapshot,
)
from waystone.runs.verify import (
    ActorIdentity,
    ApplyResult,
    CriterionResult,
    DecisionInput,
    DecisionOutcome,
    EngineCheckExecutor,
    FixtureVerifierResult,
    IntegrationDecision,
    VerifierAdapter,
    VerifierBlocker,
    VerifierEvidence,
    VerifierOutput,
    execute_verifier,
    fingerprint_materialized_root,
    record_integration_decision,
    reload_integration_decision,
    reload_verifier_evidence,
    _checked_out_target_ref,
)
from waystone.runs import store as store_module


class EngineAssemblyError(WorkflowError):
    """The requested vertical path lacks an explicit composition input."""

    code = "run_engine_assembly_error"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


class EngineConfigurationUnavailable(EngineAssemblyError):
    code = "run_engine_configuration_unavailable"


class EngineBindingRefusal(EngineAssemblyError):
    code = "run_engine_binding_refused"


class StageRunnerFailed(EngineAssemblyError):
    code = "stage_runner_failed"


class _PromotionRejected(Exception):
    def __init__(self, decision: IntegrationDecision):
        self.decision = decision
        super().__init__("promotion rejected")


_PROMOTION_COMPOSITION = object()
_PROMOTION_EXECUTION_DESCRIPTOR_PREFIX = "promotion-execution-descriptor:"


class ReadOnlyStoreUnavailable(EngineAssemblyError):
    code = "run_status_unavailable"


class CancelReason(str, Enum):
    """The M1-B CLI's single owner-authored cancellation reason."""

    USER_REQUESTED = "user-requested"


@dataclass(frozen=True)
class PreflightInputs:
    capabilities: CapabilitySet
    materialized_toolchains: tuple[MaterializedToolchain, ...]
    runner_context: RunnerContext
    runner_proof: RunnerProof


@dataclass(frozen=True)
class RunAssembly:
    """Explicit adapters needed to execute the otherwise protocol-only slice."""

    verification_plan: VerificationPlanDefinition
    preflight_inputs: Callable[[VerificationPlan], PreflightInputs]
    runner_invocations: Callable[[DispatchReady], Mapping[str, RunnerInvocation]]
    result_ref: str
    worker_actor_id: str
    verifier_actor: ActorIdentity
    coordinator_actor: ActorIdentity
    check_executor: EngineCheckExecutor
    verifier_adapter: VerifierAdapter
    decision_input: Callable[[VerifierEvidence, ActorIdentity], DecisionInput]

    def __post_init__(self) -> None:
        if not isinstance(self.verification_plan, VerificationPlanDefinition):
            raise TypeError("verification_plan must be a VerificationPlanDefinition")
        for value, label in (
                (self.preflight_inputs, "preflight_inputs"),
                (self.runner_invocations, "runner_invocations"),
                (self.check_executor, "check_executor"),
                (self.decision_input, "decision_input")):
            if not callable(value):
                raise TypeError(f"{label} must be callable")
        if not isinstance(self.result_ref, str) or not self.result_ref.startswith("refs/"):
            raise ValueError("result_ref must be a full refs/* name")
        if not isinstance(self.worker_actor_id, str) or not self.worker_actor_id.strip():
            raise ValueError("worker_actor_id must be non-empty")
        if (not isinstance(self.verifier_actor, ActorIdentity)
                or self.verifier_actor.role is not Role.VERIFIER):
            raise ValueError("verifier_actor must have the verifier role")
        if (not isinstance(self.coordinator_actor, ActorIdentity)
                or self.coordinator_actor.role is not Role.COORDINATOR):
            raise ValueError("coordinator_actor must have the coordinator role")
        if not isinstance(self.verifier_adapter, VerifierAdapter):
            raise TypeError("verifier_adapter must be a VerifierAdapter")


@dataclass(frozen=True)
class StartResult:
    run_id: str
    dispatch: Mapping[str, object]


@dataclass(frozen=True)
class CompletionResult:
    run_id: str
    verifier: VerifierEvidence
    decision: IntegrationDecision
    applied: ApplyResult


@dataclass(frozen=True)
class ResumeResult:
    run_id: str
    dispatch: Mapping[str, object] | None = None
    cancellation: CancellationResult | None = None
    completion: CompletionResult | None = None


@dataclass(frozen=True)
class StagedStartResult:
    spec: RunSpec
    attempt_id: str


@dataclass(frozen=True)
class PendingContext:
    run_id: str
    request_digest: str
    request: Mapping[str, object]


@dataclass(frozen=True)
class ContextResumeResult:
    spec: RunSpec
    response: ContextResponse
    attempt_id: str


def load_review_cycle_chain(
        assembly: ProductionRunAssembly,
        promotion_lineage_id: str,
        inherited_head_digest: str | None,
) -> tuple[ReviewCycle, ...]:
    """Load the inherited CAS chain plus every durable cycle appended to this lineage."""
    cycles = []
    head = inherited_head_digest
    while head is not None:
        cycle = parse_review_cycle_bytes(assembly.artifact_store.read(head))
        cycles.append(cycle)
        head = cycle.supersedes_digest
    cycles.reverse()
    indexed = {cycle.cycle: cycle for cycle in cycles}
    prefix = f"review-cycle:{promotion_lineage_id}:"
    with assembly.store._connection_lock:  # noqa: SLF001 - lineage-head projection
        rows = assembly.store._connection.execute(  # noqa: SLF001
            "SELECT reference_id FROM artifacts WHERE reference_id LIKE ?",
            (prefix + "%",),
        ).fetchall()
    for row in rows:
        reference_id = row["reference_id"]
        suffix = reference_id.removeprefix(prefix)
        if not suffix.isdigit() or int(suffix) < 1:
            raise EngineBindingRefusal("durable review cycle reference identity is invalid")
        reference = assembly.store.get_artifact_reference(reference_id)
        cycle = parse_review_cycle_bytes(
            assembly.artifact_store.read_reference(reference))
        prior = indexed.get(cycle.cycle)
        if prior is not None and prior.digest != cycle.digest:
            raise EngineBindingRefusal("durable review cycle number is divergent")
        indexed[cycle.cycle] = cycle
    ordered = tuple(indexed[index] for index in sorted(indexed))
    prior_digest = None
    for index, cycle in enumerate(ordered, start=1):
        if (cycle.promotion_lineage_id != promotion_lineage_id
                or cycle.cycle != index
                or cycle.supersedes_digest != prior_digest):
            raise EngineBindingRefusal("durable review cycle chain is divergent or non-contiguous")
        prior_digest = cycle.digest
    return ordered


def _validate_promotion_decision(
    plan: AssurancePlan,
    *,
    expected_outcome: DecisionOutcome,
    expected_run_id: str,
    expected_run_spec_digest: str,
    expected_candidate_digest: str,
    expected_candidate_oid: str,
    expected_evaluation_evidence_digest: str,
    expected_target_result_digest: str,
    verifier: object,
    review: tuple[ReviewCycle, ReviewerEvidence] | None,
    decision: object,
) -> None:
    """Validate one exact promotion evidence tuple for its declared decision outcome."""
    if not isinstance(plan, AssurancePlan) or plan.lifecycle_stage.value != "promote":
        raise EngineBindingRefusal("promotion evidence requires a frozen promote AssurancePlan")
    if not isinstance(verifier, VerifierEvidence):
        raise EngineBindingRefusal(
            "independent-verify must return a typed VerifierEvidence artifact")
    if (verifier.run_id != expected_run_id
            or verifier.run_spec_digest != expected_run_spec_digest
            or verifier.actor.role is not Role.VERIFIER
            or verifier.verifier_sandbox.filesystem != "read-only"
            or verifier.result.result_oid != expected_candidate_oid
            or verifier.execution_descriptor_reference is None
            or verifier.verifier_launch_reference is None):
        raise EngineBindingRefusal(
            "VerifierEvidence is not bound to the exact promotion candidate "
            "and verifier execution authority")

    reviewer_digest = None
    reviewer_actor_id = None
    if plan.requires("adversarial-review"):
        if (not isinstance(review, tuple) or len(review) != 2
                or not isinstance(review[0], ReviewCycle)
                or not isinstance(review[1], ReviewerEvidence)):
            raise EngineBindingRefusal(
                "risk-gated promotion requires a typed ReviewCycle and reviewer artifact")
        cycle, reviewer = review
        if (cycle.promotion_lineage_id != plan.review.get("promotion_lineage_id")
                or cycle.target_result_digest != expected_target_result_digest
                or cycle.review_digest != reviewer.digest
                or reviewer.promotion_lineage_id != cycle.promotion_lineage_id
                or reviewer.target_run_spec_digest != expected_run_spec_digest
                or reviewer.candidate_digest != expected_candidate_digest
                or reviewer.target_result_digest != expected_target_result_digest):
            raise EngineBindingRefusal(
                "reviewer evidence names a different promotion lineage, candidate, or result")
        reviewer_digest = reviewer.digest
        reviewer_actor_id = reviewer.actor["actor_id"]
    elif review is not None:
        raise EngineBindingRefusal(
            "reviewer evidence cannot be added outside the frozen review action")

    if not isinstance(decision, IntegrationDecision):
        raise EngineBindingRefusal(
            "integration-decision must return a typed coordinator decision artifact")
    expected_reviewers = () if reviewer_digest is None else (reviewer_digest,)
    if (decision.run_id != expected_run_id
            or decision.actor.role is not Role.COORDINATOR
            or decision.outcome is not expected_outcome
            or decision.result_digest != verifier.result.result_digest
            or decision.verifier_reference_id != verifier.artifact_reference.reference_id
            or decision.verifier_artifact_digest != verifier.artifact_reference.digest
            or decision.candidate_digest != expected_candidate_digest
            or decision.evaluation_evidence_digest
            != expected_evaluation_evidence_digest
            or decision.reviewer_artifact_digests != expected_reviewers):
        raise EngineBindingRefusal(
            "integration decision does not bind the exact promotion evidence tuple")

    actor_ids = [verifier.actor.actor_id, decision.actor.actor_id]
    if reviewer_actor_id is not None:
        actor_ids.append(reviewer_actor_id)
    artifact_digests = [
        verifier.artifact_reference.digest,
        decision.artifact_reference.digest,
    ]
    if reviewer_digest is not None:
        artifact_digests.append(reviewer_digest)
    if len(actor_ids) != len(set(actor_ids)):
        raise EngineBindingRefusal(
            "verifier, reviewer, and coordinator actor identities must be distinct")
    if len(artifact_digests) != len(set(artifact_digests)):
        raise EngineBindingRefusal(
            "verifier, reviewer, and decision artifacts must be distinct")


def validate_promotion_evidence(
    plan: AssurancePlan,
    *,
    expected_run_id: str,
    expected_run_spec_digest: str,
    expected_candidate_digest: str,
    expected_candidate_oid: str,
    expected_evaluation_evidence_digest: str,
    expected_target_result_digest: str,
    verifier: object,
    review: tuple[ReviewCycle, ReviewerEvidence] | None,
    decision: object,
) -> None:
    """Require an accept decision over one exact, actor-separated promotion tuple."""
    _validate_promotion_decision(
        plan,
        expected_outcome=DecisionOutcome.ACCEPT,
        expected_run_id=expected_run_id,
        expected_run_spec_digest=expected_run_spec_digest,
        expected_candidate_digest=expected_candidate_digest,
        expected_candidate_oid=expected_candidate_oid,
        expected_evaluation_evidence_digest=expected_evaluation_evidence_digest,
        expected_target_result_digest=expected_target_result_digest,
        verifier=verifier,
        review=review,
        decision=decision,
    )


def validate_promotion_rejection(
    plan: AssurancePlan,
    *,
    expected_run_id: str,
    expected_run_spec_digest: str,
    expected_candidate_digest: str,
    expected_candidate_oid: str,
    expected_evaluation_evidence_digest: str,
    expected_target_result_digest: str,
    verifier: object,
    review: tuple[ReviewCycle, ReviewerEvidence] | None,
    decision: object,
) -> None:
    """Require a reject decision over one exact, actor-separated promotion tuple."""
    _validate_promotion_decision(
        plan,
        expected_outcome=DecisionOutcome.REJECT,
        expected_run_id=expected_run_id,
        expected_run_spec_digest=expected_run_spec_digest,
        expected_candidate_digest=expected_candidate_digest,
        expected_candidate_oid=expected_candidate_oid,
        expected_evaluation_evidence_digest=expected_evaluation_evidence_digest,
        expected_target_result_digest=expected_target_result_digest,
        verifier=verifier,
        review=review,
        decision=decision,
    )


class StagedRunEngine:
    """A2 production entry point over an already assembled canonical kernel graph."""

    def __init__(self, assembly: ProductionRunAssembly):
        if not isinstance(assembly, ProductionRunAssembly):
            raise TypeError("assembly must be a production RunAssembly")
        self.assembly = assembly
        self.root = assembly.context.canonical_root
        self.input_root = assembly.context.active_worktree_root

    @staticmethod
    def _observed_digest(value: bytes) -> str:
        return "sha256:" + hashlib.sha256(value).hexdigest()

    def _prepare_promotion_verification(self, spec: RunSpec) -> None:
        """Freeze and prove the existing check-free verifier path for one promote run."""
        sandbox = SandboxContract("read-only", "isolated", "denied")
        definition = VerificationPlanDefinition(
            required_checks=(),
            required_toolchains=(),
            environment_preparation=(),
            network_cache_requirements=NetworkCacheRequirements(
                False, (), "promotion-verifier", True),
            verifier_sandbox=sandbox,
        )
        plan = freeze_verification_plan(spec.run_id, definition, start=self.root)
        worker = self.assembly.profile.binding_for(Role.WORKER).binding
        verifier_profile = self.assembly.profile.binding_for(Role.VERIFIER)
        verifier = verifier_profile.binding
        environment = build_runner_environment()
        try:
            descriptor = freeze_runner_execution_descriptor(
                environment,
                executable="codex",
                cwd=self.root,
                verifier_backend=verifier.backend,
                verifier_binding_digest=verifier_profile.binding_digest,
            )
        except RunnerExecutionDescriptorRefusal as error:
            raise EngineBindingRefusal(
                f"promotion verifier execution descriptor cannot be frozen: {error}") from error
        runner = RunnerCapabilities(
            execution_categories=tuple(sorted(
                {worker.execution_category, verifier.execution_category},
                key=lambda item: item.value,
            )),
            engine_sandboxes=(),
            role_capabilities=(
                RoleCapability(worker, sandbox, False, False, False, False),
                RoleCapability(verifier, sandbox, True, True, True, True),
            ),
        )
        preparation = EnvironmentPreparationReceipt(
            plan.environment_preparation_digest,
            plan.network_cache_requirements,
            (),
        )
        capabilities = CapabilitySet(runner, (preparation,), (), ())
        try:
            project_bytes = (self.root / ".waystone.yml").read_bytes()
            profile_bytes = (self.root / ".waystone" / "profile.yml").read_bytes()
        except OSError as error:
            raise EngineBindingRefusal(
                f"promotion verifier capability bytes are unavailable: {error}") from error
        observations = (
            RuntimeObservation(
                "cache-boundary", "engine:cache-boundary", ObservationStatus.OBSERVED,
                self._observed_digest(plan.network_cache_requirements.cache_namespace.encode())),
            RuntimeObservation(
                "platform-kernel", "engine:platform-kernel", ObservationStatus.OBSERVED,
                self._observed_digest(repr(os.uname()).encode())),
            RuntimeObservation(
                "process-security", "engine:process-security",
                ObservationStatus.NOT_OBSERVED, None),
            RuntimeObservation(
                "runner-binary", "runner-adapter:binary", ObservationStatus.OBSERVED,
                descriptor.executable_content_digest),
            RuntimeObservation(
                "runner-config-content", "runner-adapter:config",
                ObservationStatus.NOT_OBSERVED, None),
            RuntimeObservation(
                "runner-version", "runner-adapter:version", ObservationStatus.OBSERVED,
                descriptor.executable_content_digest),
            RuntimeObservation(
                "sandbox-contract", "engine:sandbox-contract", ObservationStatus.OBSERVED,
                self._observed_digest(repr(sandbox).encode())),
        )
        context = RunnerContext(
            checkout_identity=self._observed_digest(
                self.assembly.context.checkout_identity.encode()),
            machine_identity=self._observed_digest(repr(os.uname()).encode()),
            principal_identity=self._observed_digest(
                f"uid={os.getuid()};gid={os.getgid()}".encode()),
            project_config_digest=self._observed_digest(project_bytes),
            profile_config_digest=self._observed_digest(profile_bytes),
            runtime_observations=observations,
        )
        preflight_for_dispatch(
            spec.run_id,
            capabilities=capabilities,
            materialized_toolchains=(),
            current_runner_context=context,
            reusable_runner_proof=record_runner_proof(context, runner),
            start=self.root,
        )
        self._record_promotion_execution_descriptor(spec, descriptor)

    @staticmethod
    def _promotion_execution_descriptor_reference_id(run_id: str) -> str:
        return f"{_PROMOTION_EXECUTION_DESCRIPTOR_PREFIX}{run_id}"

    def _record_promotion_execution_descriptor(
            self, spec: RunSpec,
            descriptor: RunnerExecutionDescriptor) -> ArtifactReference:
        stored = self.assembly.artifact_store.write(descriptor.canonical_bytes())
        if stored.digest != descriptor.digest:
            raise EngineBindingRefusal(
                "promotion verifier execution descriptor changed during CAS publication")
        reference = ArtifactReference(
            self._promotion_execution_descriptor_reference_id(spec.run_id),
            ArtifactReferenceKind.EVIDENCE,
            stored.digest,
            stored.size,
        )
        run = self.assembly.store.get_run(spec.run_id)
        if run.state != "dispatch-ready":
            raise EngineBindingRefusal(
                "promotion execution descriptor requires dispatch-ready preflight authority")
        self.assembly.store.record_transition(
            EntityKind.RUN,
            spec.run_id,
            expected_version=run.version,
            next_state=run.state,
            reason=TransitionReason.PLANNED,
            evidence_digest=reference.digest,
            artifact_references=(reference,),
        )
        return reference

    def _load_promotion_execution_descriptor(
            self, spec: RunSpec,
            ) -> tuple[RunnerExecutionDescriptor, ArtifactReference, RunnerEnvironment]:
        reference_id = self._promotion_execution_descriptor_reference_id(spec.run_id)
        try:
            reference = self.assembly.store.get_artifact_reference(reference_id)
            if (reference.kind is not ArtifactReferenceKind.EVIDENCE
                    or reference.reference_id != reference_id):
                raise ValueError(
                    "promotion execution descriptor reference is not canonical evidence")
            descriptor = parse_runner_execution_descriptor(
                self.assembly.artifact_store.read_reference(reference),
                expected_digest=reference.digest,
            )
            environment = build_runner_environment()
            verifier = self.assembly.profile.binding_for(Role.VERIFIER)
            require_runner_execution_match(
                descriptor,
                environment,
                executable="codex",
                cwd=self.root,
                verifier_backend=verifier.binding.backend,
                verifier_binding_digest=verifier.binding_digest,
            )
        except (ValueError, WorkflowError) as error:
            raise EngineBindingRefusal(
                "promotion verifier execution authority differs from start-time "
                f"preflight ({error}); run `waystone run start` again") from error
        return descriptor, reference, environment

    def start(
        self,
        task_id: str,
        *,
        work_brief_content: bytes,
        completion_contract_content: bytes,
        assurance_plan_content: bytes,
        frame_status_ref: FrameStatusRef,
        project_fact_refs: Sequence[ProjectFactRef],
        owner_request_reference: ArtifactReference | None = None,
        promotion_lineage=None,
        candidate: Mapping[str, object] | None = None,
        evaluation: Mapping[str, object] | None = None,
        result_policy=None,
    ) -> StagedStartResult:
        spec = plan_one_task_run(
            task_id,
            work_brief_content=work_brief_content,
            completion_contract_content=completion_contract_content,
            assurance_plan_content=assurance_plan_content,
            frame_status_ref=frame_status_ref,
            project_fact_refs=project_fact_refs,
            owner_request_reference=owner_request_reference,
            promotion_lineage=promotion_lineage,
            candidate=candidate,
            evaluation=evaluation,
            result_policy=result_policy,
            artifact_store=self.assembly.artifact_store,
            run_store=self.assembly.store,
            start=self.input_root,
        )
        if spec.lifecycle_stage.value == "promote":
            self._prepare_promotion_verification(spec)
        store = self.assembly.store
        run = store.get_run(spec.run_id)
        if run.state == "frozen-ready":
            store.record_transition(
                EntityKind.RUN,
                spec.run_id,
                expected_version=run.version,
                next_state="dispatch-ready",
                reason=TransitionReason.PLANNED,
                evidence_digest=spec.run_spec_digest,
            )
        elif run.state != "dispatch-ready":
            raise EngineBindingRefusal(
                "stage start did not reach its frozen dispatch-ready authority")
        job = store.get_entity(EntityKind.JOB, spec.job_id)
        store.record_transition(
            EntityKind.JOB,
            spec.job_id,
            expected_version=job.version,
            next_state="running",
            reason=TransitionReason.PROCESS_STARTED,
            evidence_digest=spec.run_spec_digest,
        )
        attempt_id = f"{spec.run_id}:attempt:1"
        attempt = store.create_attempt(
            spec.run_id, spec.job_id, attempt_id, initial_state="dispatch-ready")
        store.record_transition(
            EntityKind.ATTEMPT,
            attempt_id,
            expected_version=attempt.version,
            next_state="running",
            reason=TransitionReason.PROCESS_STARTED,
            evidence_digest=spec.run_spec_digest,
        )
        self._ensure_stage_started(spec, attempt_id)
        return StagedStartResult(spec, attempt_id)

    @staticmethod
    def _invocation_digest(
            invocation: RunnerInvocation,
            execution_descriptor_digest: str | None = None) -> str:
        authority = {
            "argv": list(invocation.argv),
            "cwd": str(invocation.cwd),
            "candidate_context": (
                None if invocation.candidate_context is None
                else invocation.candidate_context.to_payload()
            ),
            "environment_digest": invocation.environment.digest,
        }
        if execution_descriptor_digest is not None:
            authority["execution_descriptor_digest"] = validate_sha256_digest(
                execution_descriptor_digest)
        payload = assurance_json(authority)
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _candidate_stage_root(
            self, spec: RunSpec, attempt_id: str) -> tuple[Path, RunnerCandidateContext]:
        if spec.candidate is None:
            raise EngineBindingRefusal(
                "candidate stage invocation requires a frozen candidate")
        candidate_oid = spec.candidate["target_oid"]
        candidate_ref = spec.candidate["target_ref"]
        if (not isinstance(candidate_oid, str) or not isinstance(candidate_ref, str)
                or git_full_sha(self.input_root, candidate_ref) != candidate_oid):
            raise EngineBindingRefusal(
                "candidate stage invocation differs from the frozen candidate ref")
        path_digest = hashlib.sha256(
            f"{spec.run_id}\0{attempt_id}\0{spec.lifecycle_stage.value}".encode()
        ).hexdigest()
        root = self.root / ".waystone" / "candidate-contexts" / path_digest
        if not root.exists():
            try:
                root.parent.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise EngineBindingRefusal(
                    f"candidate context parent is unavailable: {error}") from error
            returncode, _output, error = git_rc(
                self.input_root,
                "worktree",
                "add",
                "--quiet",
                "--detach",
                str(root),
                candidate_oid,
            )
            if returncode != 0:
                raise EngineBindingRefusal(
                    error or f"candidate worktree creation exited {returncode}")
        if not root.is_dir() or git_full_sha(root) != candidate_oid:
            raise EngineBindingRefusal(
                "engine-owned candidate context does not name the frozen candidate OID")
        try:
            fingerprint = fingerprint_candidate_root(root, candidate_oid)
        except (OSError, ValueError, WorkflowError) as error:
            raise EngineBindingRefusal(
                f"candidate context fingerprint is unavailable: {error}") from error
        return root, RunnerCandidateContext(
            candidate_oid,
            fingerprint,
            spec.run_spec_digest,
        )

    def _worker_result_schema(
            self, spec: RunSpec, attempt_id: str, *, evaluation: bool) -> StoredArtifact:
        common = {
            "schema": {"type": "string", "const": "waystone-worker-result-1"},
            "status": {
                "type": "string",
                "enum": ["completed", "context-requested"],
            },
            "run_spec_digest": {"type": "string", "const": spec.run_spec_digest},
            "attempt_id": {"type": "string", "const": attempt_id},
        }
        summary: dict[str, object] = {"type": "string", "minLength": 1}
        if evaluation:
            summary = {"type": "string", "enum": ["pass", "fail"]}
        evidence_refs = {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "reference_id": {"type": "string", "minLength": 1},
                    "digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                },
                "required": ["reference_id", "digest"],
            },
        }
        context_request = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                field: {"type": "string", "minLength": 1}
                for field in ("question", "blocked_decision", "why_required")
            },
            "required": ["question", "blocked_decision", "why_required"],
        }
        properties = {
            **common,
            "result_summary": {
                "anyOf": [summary, {"type": "null"}],
                "description": "Non-null only when status is completed.",
            },
            "evidence_refs": {
                "anyOf": [evidence_refs, {"type": "null"}],
                "description": "Non-null only when status is completed.",
            },
            "context_request": {
                "anyOf": [context_request, {"type": "null"}],
                "description": "Non-null only when status is context-requested.",
            },
        }
        return self.assembly.artifact_store.write(assurance_json({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(properties),
        }))

    def _stage_invocation(
            self, spec: RunSpec, attempt_id: str, role: Role, *,
            candidate_root: Path | None = None,
            candidate_context: RunnerCandidateContext | None = None,
            output_schema: StoredArtifact | None = None,
            output_path: Path | None = None,
            environment: RunnerEnvironment | None = None,
            execution_descriptor: RunnerExecutionDescriptor | None = None,
            ) -> RunnerInvocation:
        adapter = self.assembly.role_adapters[role]
        if adapter.execution_category is not ExecutionCategory.EXTERNAL:
            raise EngineBindingRefusal(
                f"{role.value} stage execution requires an external binding")
        transport, separator, model = adapter.backend.partition(":")
        if transport != "codex" or not separator or not model:
            raise EngineBindingRefusal(
                f"{role.value} backend {adapter.backend!r} is not an executable codex binding")
        if spec.lifecycle_stage.value == "promote":
            if execution_descriptor is None or environment is None:
                raise EngineBindingRefusal(
                    "promotion verifier invocation requires frozen execution authority")
            try:
                require_runner_execution_match(
                    execution_descriptor,
                    environment,
                    executable="codex",
                    cwd=self.root,
                    verifier_backend=adapter.backend,
                    verifier_binding_digest=adapter.binding_digest,
                )
            except RunnerExecutionDescriptorRefusal as error:
                raise EngineBindingRefusal(
                    f"promotion verifier execution authority mismatch: {error}") from error
            executable = execution_descriptor.resolved_executable
        else:
            executable = shutil.which("codex")
            if executable is None:
                raise EngineBindingRefusal(
                    "codex executable is unavailable for the frozen binding")
        contract = completion.parse_completion_contract_bytes(
            self.input_root,
            self.assembly.artifact_store.read(
                spec.job_input.completion_contract.digest),
            artifact_store=self.assembly.artifact_store,
        )
        brief = work_brief.parse_work_brief_bytes(
            self.assembly.artifact_store.read(spec.work_brief.digest),
            artifact_store=self.assembly.artifact_store,
            completion_contract=contract,
        )
        schema = output_schema or self._worker_result_schema(
            spec, attempt_id,
            evaluation=spec.lifecycle_stage.value in {"evaluate", "promote"})
        sandbox = (
            "read-only"
            if spec.lifecycle_stage.value in {"evaluate", "promote"}
            else "workspace-write"
        )
        cwd = self.input_root
        selected_context = None
        if spec.lifecycle_stage.value in {"evaluate", "promote"}:
            if (candidate_root is None) != (candidate_context is None):
                raise EngineBindingRefusal(
                    "candidate root and launch context must be supplied together")
            if candidate_root is None:
                cwd, selected_context = self._candidate_stage_root(spec, attempt_id)
            else:
                cwd = Path(candidate_root).resolve(strict=True)
                selected_context = candidate_context
                assert selected_context is not None and spec.candidate is not None
                if (selected_context.run_spec_digest != spec.run_spec_digest
                        or selected_context.candidate_oid
                        != spec.candidate["target_oid"]):
                    raise EngineBindingRefusal(
                        "candidate launch context differs from the frozen RunSpec")
                observed_fingerprint = (
                    fingerprint_materialized_root(cwd)
                    if spec.lifecycle_stage.value == "promote"
                    else fingerprint_candidate_root(
                        cwd, selected_context.candidate_oid)
                )
                if observed_fingerprint != selected_context.root_fingerprint:
                    raise EngineBindingRefusal(
                        "candidate launch root fingerprint differs from the frozen context")
        result_path = output_path or (cwd / RESULT_CONTROL_FILE)
        argv = (
            executable,
            "exec",
            "-m",
            model,
            "--sandbox",
            sandbox,
            "--ephemeral",
            "--output-schema",
            str(schema.path),
            "-o",
            str(result_path),
            work_brief.render_semantic_prompt(brief, contract),
        )
        if environment is None:
            return RunnerInvocation(argv, cwd, selected_context)
        return RunnerInvocation(argv, cwd, selected_context, environment)

    def _runner_action_id(self, attempt_id: str, stage: str) -> str:
        action = {
            "explore": "worker",
            "evaluate": "read-only-evaluator",
            "promote": "independent-verify",
        }[stage]
        return f"{attempt_id}:{action}"

    def _ensure_stage_runner(self, spec: RunSpec, attempt_id: str) -> str:
        stage = spec.lifecycle_stage.value
        role = Role.WORKER if stage == "explore" else Role.VERIFIER
        action_id = self._runner_action_id(attempt_id, stage)
        invocation = self._stage_invocation(spec, attempt_id, role)
        invocation_digest = self._invocation_digest(invocation)
        self.assembly.supervisor.bind_invocation(invocation_digest, invocation)
        try:
            action = self.assembly.store.get_entity(EntityKind.ACTION, action_id)
        except RecordNotFoundError:
            plan = self.assembly.effect_executor.plan_effect(
                spec.run_id, spec.job_id, attempt_id, action_id,
                RunnerExecutionEffect(invocation_digest),
            )
            action = self.assembly.store.get_entity(EntityKind.ACTION, action_id)
        else:
            plan = self.assembly.effect_executor._load_plan(action_id)  # noqa: SLF001
            if (plan.kind is not EffectKind.RUNNER_EXECUTION
                    or plan.spec.get("invocation_digest") != invocation_digest):
                raise EngineBindingRefusal(
                    "stage runner action differs from the frozen role invocation")
        if action.state == "planned":
            claimed = self.assembly.effect_executor.claim_effect(plan, ttl_seconds=30)
            self.assembly.effect_executor.execute_effect(claimed)
        return action_id

    def _ensure_stage_started(self, spec: RunSpec, attempt_id: str) -> None:
        if spec.lifecycle_stage.value in {"explore", "evaluate"}:
            self._ensure_stage_runner(spec, attempt_id)

    def observe_worker_result(
        self,
        run_id: str,
        attempt_id: str,
        action_id: str,
    ) -> AdaptedWorkerResult:
        """Follow an observed runner effect to its reserved union result exactly once."""
        store = self.assembly.store
        action = store.get_entity(EntityKind.ACTION, action_id)
        if (action.run_id != run_id or action.parent_attempt_id != attempt_id
                or action.state != "completed"):
            raise EngineBindingRefusal(
                "worker result may be consumed only after its bound runner action is observed")
        spec = load_run_spec(run_id, start=self.root)
        plan = self.assembly.effect_executor._load_plan(action_id)  # noqa: SLF001
        marker_path = Path(plan.spec["completion_marker"])
        try:
            marker_content = marker_path.read_bytes()
            marker_payload = json.loads(marker_content.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EngineBindingRefusal(
                f"completed staged runner lacks a readable marker: {error}") from error
        if (not isinstance(marker_payload, dict)
                or marker_payload.get("run_id") != run_id
                or marker_payload.get("job_id") != spec.job_id
                or marker_payload.get("action_id") != action_id):
            raise EngineBindingRefusal("runner marker identity differs from the bound action")
        if (marker_payload.get("schema") != "waystone-runner-completion-2"
                or marker_payload.get("returncode") != 0
                or marker_payload.get("signal") is not None
                or "worker_result_digest" not in marker_payload):
            failure_class = (
                "runner-signaled" if marker_payload.get("signal") is not None
                else "runner-exit-nonzero" if marker_payload.get("returncode") != 0
                else "worker-result-unavailable"
            )
            self._record_stage_runner_failure(
                spec, attempt_id, action_id, marker_payload, failure_class)
            raise StageRunnerFailed(
                f"stage action {action_id!r} ended with {failure_class}")
        try:
            marker = parse_runner_completion_marker_v2_bytes(marker_content)
        except WorkflowError as error:
            self._record_stage_runner_failure(
                spec, attempt_id, action_id, marker_payload,
                "worker-result-unavailable")
            raise StageRunnerFailed(
                f"stage action {action_id!r} published an invalid result marker") from error
        if (marker.run_id != run_id or marker.job_id != spec.job_id
                or marker.action_id != action_id):
            raise EngineBindingRefusal("runner marker v2 identity differs from the bound action")
        result_root = self.input_root
        expected_snapshot_digest = spec.base_snapshot.digest
        if spec.lifecycle_stage.value == "evaluate":
            invocation = self._stage_invocation(spec, attempt_id, Role.VERIFIER)
            if self._invocation_digest(invocation) != plan.spec.get("invocation_digest"):
                raise EngineBindingRefusal(
                    "evaluator launch no longer binds the frozen candidate context")
            assert invocation.candidate_context is not None
            result_root = invocation.cwd
            expected_snapshot_digest = capture_result_snapshot(result_root).digest
        adapted = WorkerResultAdapter(
            result_root, self.assembly.artifact_store).adapt_published(
                marker.worker_result_digest,
                run_id=run_id,
                job_id=spec.job_id,
                attempt_id=attempt_id,
                run_spec_digest=spec.run_spec_digest,
                work_brief_digest=spec.work_brief.digest,
                base_snapshot_digest=expected_snapshot_digest,
            )
        if spec.lifecycle_stage.value == "evaluate":
            assert invocation.candidate_context is not None
            try:
                observed = fingerprint_candidate_root(
                    result_root, invocation.candidate_context.candidate_oid)
            except (OSError, ValueError, WorkflowError) as error:
                raise EngineBindingRefusal(
                    f"evaluator result root cannot be re-observed: {error}") from error
            if observed != invocation.candidate_context.root_fingerprint:
                raise EngineBindingRefusal(
                    "evaluator result root fingerprint differs from its launch binding")
        if isinstance(adapted.result, ContextRequestedWorkerResult):
            assert adapted.context_request_artifact is not None
            store.record_context_request(
                run_id,
                spec.job_id,
                attempt_id,
                context_request_digest=adapted.context_request_artifact.digest,
                artifact_references=(
                    ArtifactReference(
                        f"worker-result:{attempt_id}",
                        ArtifactReferenceKind.EVIDENCE,
                        adapted.worker_result_artifact.digest,
                        adapted.worker_result_artifact.size,
                    ),
                    ArtifactReference(
                        f"context-request:{run_id}:{spec.revision}",
                        ArtifactReferenceKind.EVIDENCE,
                        adapted.context_request_artifact.digest,
                        adapted.context_request_artifact.size,
                    ),
                ),
            )
        else:
            reference_id = f"worker-result:{attempt_id}"
            try:
                reference = store.get_artifact_reference(reference_id)
            except RecordNotFoundError:
                attempt = store.get_entity(EntityKind.ATTEMPT, attempt_id)
                store.record_transition(
                    EntityKind.ATTEMPT,
                    attempt_id,
                    expected_version=attempt.version,
                    next_state=attempt.state,
                    reason=TransitionReason.EFFECT_OBSERVED,
                    evidence_digest=adapted.worker_result_artifact.digest,
                    artifact_references=(ArtifactReference(
                        reference_id,
                        ArtifactReferenceKind.EVIDENCE,
                        adapted.worker_result_artifact.digest,
                        adapted.worker_result_artifact.size,
                    ),),
                )
            else:
                if reference.digest != adapted.worker_result_artifact.digest:
                    raise EngineBindingRefusal(
                        "completed worker result differs from the frozen attempt result")
        return adapted

    def _record_stage_runner_failure(
            self,
            spec: RunSpec,
            attempt_id: str,
            action_id: str,
            marker: Mapping[str, object],
            failure_class: str,
    ) -> None:
        payload = {
            "schema": "waystone-stage-runner-failure-1",
            "run_id": spec.run_id,
            "job_id": spec.job_id,
            "attempt_id": attempt_id,
            "action_id": action_id,
            "failure_class": failure_class,
            "returncode": marker.get("returncode"),
            "signal": marker.get("signal"),
            "stdout_artifact_digest": validate_sha256_digest(
                marker.get("stdout_artifact_digest")),  # type: ignore[arg-type]
            "stderr_artifact_digest": validate_sha256_digest(
                marker.get("stderr_artifact_digest")),  # type: ignore[arg-type]
        }
        artifact = self.assembly.artifact_store.write(assurance_json(payload))
        reference_id = f"runner-failure:{attempt_id}"
        try:
            reference = self.assembly.store.get_artifact_reference(reference_id)
        except RecordNotFoundError:
            reference = None
        if reference is not None and reference.digest != artifact.digest:
            raise EngineBindingRefusal("runner failure evidence is divergent")
        attempt = self.assembly.store.get_entity(EntityKind.ATTEMPT, attempt_id)
        if attempt.state != "failed":
            references = () if reference is not None else (ArtifactReference(
                reference_id,
                ArtifactReferenceKind.EVIDENCE,
                artifact.digest,
                artifact.size,
            ),)
            self.assembly.store.record_transition(
                EntityKind.ATTEMPT,
                attempt_id,
                expected_version=attempt.version,
                next_state="failed",
                reason=TransitionReason.PROCESS_FAILED,
                evidence_digest=artifact.digest,
                artifact_references=references,
            )
        for kind, identity in (
                (EntityKind.JOB, spec.job_id),
                (EntityKind.RUN, spec.run_id)):
            entity = (
                self.assembly.store.get_run(identity)
                if kind is EntityKind.RUN
                else self.assembly.store.get_entity(kind, identity))
            if entity.state != "failed":
                self.assembly.store.record_transition(
                    kind,
                    identity,
                    expected_version=entity.version,
                    next_state="failed",
                    reason=TransitionReason.PROCESS_FAILED,
                    evidence_digest=artifact.digest,
                )

    def pending_context(self, run_id: str) -> PendingContext:
        store = self.assembly.store
        run = store.get_run(run_id)
        if run.state != "waiting_context":
            raise ContextNotCurrent(run_id, f"run state is {run.state!r}")
        with store._connection_lock:  # noqa: SLF001 - package context-head projection
            row = store._connection.execute(  # noqa: SLF001
                "SELECT reference_id FROM artifacts WHERE run_id = ? "
                "AND reference_id LIKE ? ORDER BY transition_id DESC LIMIT 1",
                (run_id, f"context-request:{run_id}:%"),
            ).fetchone()
        if row is None:
            raise ContextNotCurrent(run_id, "waiting run has no context request head")
        reference = store.get_artifact_reference(row["reference_id"])
        content = self.assembly.artifact_store.read_reference(reference)
        try:
            request = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EngineBindingRefusal(f"context request artifact is invalid: {error}") from error
        if not isinstance(request, dict):
            raise EngineBindingRefusal("context request artifact is not an object")
        return PendingContext(run_id, reference.digest, request)

    def provide_context(self, run_id: str, response_content: bytes) -> ContextResumeResult:
        """Bind a response, derived semantic revisions, and the next attempt in one store CAS."""
        pending = self.pending_context(run_id)
        response_artifact = self.assembly.artifact_store.write(response_content)
        coordinator = self.assembly.profile.binding_for(Role.COORDINATOR)
        response = parse_context_response_bytes(
            response_content,
            expected_request_digest=pending.request_digest,
            expected_binding_digest=coordinator.binding_digest,
        )
        source_digest = response.answer_source.get("digest")
        if response.answer_source.get("kind") in {"owner-artifact", "evidence"}:
            try:
                self.assembly.artifact_store.read(source_digest)  # type: ignore[arg-type]
            except WorkflowError as error:
                raise EngineBindingRefusal(
                    "context answer source bytes are not present in canonical CAS") from error
        previous = load_run_spec(run_id, start=self.root)
        prior_brief = self.assembly.artifact_store.read(previous.work_brief.digest)
        revised_brief = revise_work_brief_for_response(prior_brief, response)
        assurance = self.assembly.artifact_store.read(previous.assurance_plan.digest)
        completion = self.assembly.artifact_store.read(
            previous.job_input.completion_contract.digest)
        prepared = prepare_run_spec_revision(
            previous,
            work_brief_content=revised_brief,
            completion_contract_content=completion,
            assurance_plan_content=assurance,
            resolves_context_request_digest=pending.request_digest,
            start=self.root,
        )
        revision = prepared.spec.revision
        transition = self.assembly.store.provide_context(
            run_id,
            previous.job_id,
            request_digest=pending.request_digest,
            run_spec_digest=prepared.spec.run_spec_digest,
            max_total_attempts=previous.retry.max_total_attempts,
            artifact_references=(
                ArtifactReference(
                    f"context-response:{run_id}:{revision}",
                    ArtifactReferenceKind.INPUT,
                    response_artifact.digest,
                    response_artifact.size,
                ),
                ArtifactReference(
                    prepared.spec.work_brief.reference_id,
                    ArtifactReferenceKind.INPUT,
                    prepared.work_brief_artifact.digest,
                    prepared.work_brief_artifact.size,
                ),
                ArtifactReference(
                    prepared.spec.assurance_plan.reference_id,
                    ArtifactReferenceKind.INPUT,
                    prepared.assurance_plan_artifact.digest,
                    prepared.assurance_plan_artifact.size,
                ),
                ArtifactReference(
                    prepared.spec.job_input.completion_contract.reference_id,
                    ArtifactReferenceKind.INPUT,
                    prepared.completion_contract_artifact.digest,
                    prepared.completion_contract_artifact.size,
                ),
                ArtifactReference(
                    f"run-spec:{run_id}:{revision}",
                    ArtifactReferenceKind.INPUT,
                    prepared.run_spec_artifact.digest,
                    prepared.run_spec_artifact.size,
                ),
            ),
        )
        return ContextResumeResult(prepared.spec, response, transition.attempt.entity_id)

    def _latest_attempt_id(self, spec: RunSpec) -> str:
        with self.assembly.store._connection_lock:  # noqa: SLF001
            row = self.assembly.store._connection.execute(  # noqa: SLF001
                "SELECT attempt_id FROM attempts WHERE run_id = ? AND job_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (spec.run_id, spec.job_id),
            ).fetchone()
        if row is None:
            raise EngineBindingRefusal("staged run has no attempt")
        return row["attempt_id"]

    @staticmethod
    def _promotion_action_id(spec: RunSpec, attempt_id: str, action: str) -> str:
        if attempt_id == f"{spec.run_id}:attempt:1":
            return f"{spec.run_id}:{action}"
        return f"{attempt_id}:{action}"

    def _promotion_decision_for_attempt(
            self, spec: RunSpec, attempt_id: str) -> IntegrationDecision | None:
        with self.assembly.store._connection_lock:  # noqa: SLF001 - attempt evidence head
            rows = self.assembly.store._connection.execute(  # noqa: SLF001
                "SELECT reference_id FROM artifacts WHERE run_id = ? "
                "AND entity_kind = ? AND entity_id = ? AND (reference_id LIKE ? "
                "OR reference_id LIKE ?) ORDER BY transition_id",
                (
                    spec.run_id,
                    EntityKind.ATTEMPT.value,
                    attempt_id,
                    "verifier-evidence:%",
                    "integration-decision:%",
                ),
            ).fetchall()
        verifier_ids = [
            row["reference_id"] for row in rows
            if row["reference_id"].startswith("verifier-evidence:")
        ]
        decision_ids = [
            row["reference_id"] for row in rows
            if row["reference_id"].startswith("integration-decision:")
        ]
        if not verifier_ids and not decision_ids:
            return None
        if len(verifier_ids) != 1 or len(decision_ids) != 1:
            raise EngineBindingRefusal(
                "promotion attempt does not have one exact verifier/decision terminal pair")
        verifier_action_id = verifier_ids[0].removeprefix("verifier-evidence:")
        decision_action_id = decision_ids[0].removeprefix("integration-decision:")
        try:
            return reload_integration_decision(
                spec.run_id,
                attempt_id,
                decision_action_id,
                verifier_action_id,
                start=self.root,
            )
        except WorkflowError as error:
            raise EngineBindingRefusal(
                f"promotion terminal decision cannot be revalidated: {error}") from error

    def _previous_promotion_decision(
            self, spec: RunSpec, attempt_id: str) -> IntegrationDecision | None:
        with self.assembly.store._connection_lock:  # noqa: SLF001 - attempt lineage query
            rows = self.assembly.store._connection.execute(  # noqa: SLF001
                "SELECT attempt_id FROM attempts WHERE run_id = ? AND job_id = ? "
                "ORDER BY rowid",
                (spec.run_id, spec.job_id),
            ).fetchall()
        attempt_ids = [row["attempt_id"] for row in rows]
        try:
            index = attempt_ids.index(attempt_id)
        except ValueError as error:
            raise EngineBindingRefusal("promotion attempt is absent from its run") from error
        for previous_id in reversed(attempt_ids[:index]):
            decision = self._promotion_decision_for_attempt(spec, previous_id)
            if decision is not None:
                return decision
        return None

    def retry_rejected_promotion(self, run_id: str) -> str:
        """Refuse retry of one published semantic promotion rejection."""
        spec = load_run_spec(run_id, start=self.root)
        if spec.lifecycle_stage.value != "promote":
            raise EngineBindingRefusal("only a promotion rejection can be retried")
        store = self.assembly.store
        run = store.get_run(run_id)
        job = store.get_entity(EntityKind.JOB, spec.job_id)
        if run.state != "failed" or job.state != "failed":
            raise EngineBindingRefusal(
                "promotion retry requires a failed run/job rejection terminal")
        try:
            store.get_entity(EntityKind.ACTION, f"{run_id}:outcome-publication")
        except RecordNotFoundError:
            pass
        else:
            raise EngineBindingRefusal(
                "a promotion rejection with a published outcome cannot be retried")
        attempt_id = self._latest_attempt_id(spec)
        attempt = store.get_entity(EntityKind.ATTEMPT, attempt_id)
        decision = self._promotion_decision_for_attempt(spec, attempt_id)
        if (attempt.state != "completed" or decision is None
                or decision.outcome is not DecisionOutcome.REJECT):
            raise EngineBindingRefusal(
                "latest promotion attempt is not an exact completed rejection")
        raise EngineBindingRefusal(
            "semantic verifier rejection is terminal for this candidate; "
            "retry requires a new candidate or owner ruling")

    def _reload_promotion_authority(
            self, spec: RunSpec, attempt_id: str, plan: AssurancePlan,
            ) -> tuple[
                VerifierEvidence,
                tuple[ReviewCycle, ReviewerEvidence] | None,
                IntegrationDecision,
            ]:
        verifier_action_id = self._promotion_action_id(
            spec, attempt_id, "typed-independent-verify")
        decision_action_id = self._promotion_action_id(
            spec, attempt_id, "integration-decision")
        try:
            verifier = reload_verifier_evidence(
                spec.run_id, attempt_id, verifier_action_id, start=self.root)
            decision = reload_integration_decision(
                spec.run_id,
                attempt_id,
                decision_action_id,
                verifier_action_id,
                start=self.root,
            )
            review = self._promotion_review(spec, plan)
        except WorkflowError as error:
            raise EngineBindingRefusal(
                f"published promotion authority cannot be reloaded: {error}") from error
        return verifier, review, decision

    def _apply_candidate(
            self, spec: RunSpec, attempt_id: str, plan: AssurancePlan,
            verifier: VerifierEvidence,
            review: tuple[ReviewCycle, ReviewerEvidence] | None,
            decision: IntegrationDecision) -> str:
        reloaded = self._reload_promotion_authority(spec, attempt_id, plan)
        if reloaded != (verifier, review, decision):
            raise EngineBindingRefusal(
                "promotion apply authority differs from the reloaded published tuple")
        if decision.outcome is not DecisionOutcome.ACCEPT:
            raise EngineBindingRefusal(
                "promotion apply requires a published accept decision")
        assert spec.candidate is not None
        target_ref = spec.result_policy.target_ref
        expected_oid = spec.result_policy.expected_oid
        assert target_ref is not None and expected_oid is not None
        _checked_out_target_ref(self.input_root, target_ref)
        action_id = f"{spec.run_id}:target-ref-apply"
        try:
            plan = self.assembly.effect_executor.plan_effect(
                spec.run_id, spec.job_id, attempt_id, action_id,
                GitRefEffect(
                    self.input_root, target_ref, expected_oid,
                    spec.candidate["target_oid"]),
            )
            claimed = self.assembly.effect_executor.claim_effect(plan, ttl_seconds=30)
            result = self.assembly.effect_executor.execute_effect(claimed)
        except EffectStateRefusal:
            result = self.assembly.effect_executor.reconcile_actions((action_id,))[0]
        if result.state not in {EffectResultState.COMPLETED, EffectResultState.NOOP}:
            raise EngineBindingRefusal(result.reason or "promotion apply did not complete")
        return spec.candidate["target_oid"]

    def _promotion_records(self, spec: RunSpec) -> tuple[str, str, str]:
        contract = completion.parse_completion_contract_bytes(
            self.input_root,
            self.assembly.artifact_store.read(
                spec.job_input.completion_contract.digest),
            artifact_store=self.assembly.artifact_store,
        )
        brief = work_brief.parse_work_brief_bytes(
            self.assembly.artifact_store.read(spec.work_brief.digest),
            artifact_store=self.assembly.artifact_store,
            completion_contract=contract,
        )
        sources = []
        semantic_items = (
            *brief.current_state, *brief.known_failures, *brief.constraints,
            *brief.non_goals, *brief.open_questions,
        )
        for item in semantic_items:
            sources.extend(source.payload for source in item.sources)
        required = []
        for prefix in ("regression-contract:", "supported-scope:", "accepted-risks:"):
            matches = [
                source for source in sources
                if str(source.get("reference_id", "")).startswith(prefix)
            ]
            if len(matches) != 1:
                raise EngineBindingRefusal(
                    f"promotion requires exactly one {prefix} evidence source")
            digest = validate_sha256_digest(matches[0].get("digest"))  # type: ignore[arg-type]
            self.assembly.artifact_store.read(digest)
            required.append(digest)
        return required[0], required[1], required[2]

    def _promotion_verifier_result_schema(
            self, spec: RunSpec, attempt_id: str, candidate_oid: str,
            root_fingerprint: str) -> StoredArtifact:
        properties = {
            "schema": {
                "type": "string",
                "const": "waystone-promotion-verifier-result-1",
            },
            "run_spec_digest": {"type": "string", "const": spec.run_spec_digest},
            "attempt_id": {"type": "string", "const": attempt_id},
            "candidate_oid": {"type": "string", "const": candidate_oid},
            "root_fingerprint": {"type": "string", "const": root_fingerprint},
            "criterion_results": {
                "type": "array",
                "minItems": len(spec.job_input.acceptance_criteria),
                "maxItems": len(spec.job_input.acceptance_criteria),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "criterion": {
                            "type": "string",
                            "enum": list(spec.job_input.acceptance_criteria),
                        },
                        "passed": {"type": "boolean"},
                        "evidence_digests": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "pattern": "^sha256:[0-9a-f]{64}$",
                            },
                        },
                    },
                    "required": ["criterion", "passed", "evidence_digests"],
                },
            },
            "blockers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "blocker_id": {"type": "string", "minLength": 1},
                        "detail": {"type": "string", "minLength": 1},
                    },
                    "required": ["blocker_id", "detail"],
                },
            },
            "summary": {"type": "string", "minLength": 1},
        }
        return self.assembly.artifact_store.write(assurance_json({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(properties),
        }))

    def _record_promotion_verifier_launch(
            self, spec: RunSpec, attempt_id: str, action_id: str,
            invocation: RunnerInvocation,
            execution_descriptor: RunnerExecutionDescriptor,
            execution_descriptor_reference: ArtifactReference,
            ) -> ArtifactReference:
        context = invocation.candidate_context
        if context is None:
            raise EngineBindingRefusal(
                "promotion verifier launch lacks candidate context")
        if (invocation.environment.digest != execution_descriptor.environment_digest
                or invocation.argv[0] != execution_descriptor.resolved_executable
                or execution_descriptor_reference.digest != execution_descriptor.digest):
            raise EngineBindingRefusal(
                "promotion verifier launch differs from frozen execution authority")
        payload = assurance_json({
            "schema": "waystone-promotion-verifier-launch-3",
            "run_id": spec.run_id,
            "job_id": spec.job_id,
            "attempt_id": attempt_id,
            "action_id": action_id,
            "cwd": str(invocation.cwd),
            "candidate_oid": context.candidate_oid,
            "root_fingerprint": context.root_fingerprint,
            "run_spec_digest": context.run_spec_digest,
            "invocation_digest": self._invocation_digest(
                invocation, execution_descriptor.digest),
            "environment_digest": invocation.environment.digest,
            "execution_descriptor_reference_id": (
                execution_descriptor_reference.reference_id),
            "execution_descriptor_digest": execution_descriptor.digest,
            "resolved_executable": execution_descriptor.resolved_executable,
            "executable_content_digest": (
                execution_descriptor.executable_content_digest),
        })
        artifact = self.assembly.artifact_store.write(payload)
        reference = ArtifactReference(
            f"verifier-launch:{action_id}",
            ArtifactReferenceKind.EVIDENCE,
            artifact.digest,
            artifact.size,
        )
        try:
            existing = self.assembly.store.get_artifact_reference(reference.reference_id)
        except RecordNotFoundError:
            attempt = self.assembly.store.get_entity(EntityKind.ATTEMPT, attempt_id)
            self.assembly.store.record_transition(
                EntityKind.ATTEMPT,
                attempt_id,
                expected_version=attempt.version,
                next_state=attempt.state,
                reason=TransitionReason.EFFECT_OBSERVED,
                evidence_digest=artifact.digest,
                artifact_references=(reference,),
            )
        else:
            if existing != reference:
                raise EngineBindingRefusal(
                    "promotion verifier launch record is divergent")
        return reference

    def _parse_promotion_verifier_result(
            self, content: bytes, *, spec: RunSpec, attempt_id: str,
            request, actor: ActorIdentity, raw_digest: str) -> VerifierOutput:
        try:
            document = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EngineBindingRefusal(
                f"promotion verifier result is not JSON: {error}") from error
        fields = {
            "schema", "run_spec_digest", "attempt_id", "candidate_oid",
            "root_fingerprint", "criterion_results", "blockers", "summary",
        }
        if not isinstance(document, dict) or set(document) != fields:
            raise EngineBindingRefusal(
                "promotion verifier result fields are not canonical")
        if (document["schema"] != "waystone-promotion-verifier-result-1"
                or document["run_spec_digest"] != spec.run_spec_digest
                or document["attempt_id"] != attempt_id
                or document["candidate_oid"] != request.result.result_oid
                or document["root_fingerprint"] != request.review_root_fingerprint):
            raise EngineBindingRefusal(
                "promotion verifier result differs from its candidate launch binding")
        criteria = document["criterion_results"]
        blockers = document["blockers"]
        if not isinstance(criteria, list) or not isinstance(blockers, list):
            raise EngineBindingRefusal(
                "promotion verifier result collections are malformed")
        try:
            parsed_criteria = tuple(CriterionResult(
                item["criterion"],
                item["passed"],
                tuple((*item["evidence_digests"], raw_digest)),
            ) for item in criteria)
            parsed_blockers = tuple(VerifierBlocker(
                item["blocker_id"], item["detail"]) for item in blockers)
        except (KeyError, TypeError) as error:
            raise EngineBindingRefusal(
                "promotion verifier result entries are malformed") from error
        return VerifierOutput(
            actor=actor,
            result_digest=request.result.result_digest,
            criterion_results=parsed_criteria,
            blockers=parsed_blockers,
            summary=document["summary"],
        )

    def _execute_promotion_verifier(
        self,
        spec: RunSpec,
        attempt_id: str,
        plan: AssurancePlan,
    ) -> VerifierEvidence:
        execution_descriptor, descriptor_reference, environment = (
            self._load_promotion_execution_descriptor(spec))
        actor = ActorIdentity(
            self.assembly.profile.binding_for(Role.VERIFIER).binding_digest,
            Role.VERIFIER,
        )
        action_id = self._promotion_action_id(
            spec, attempt_id, "typed-independent-verify")

        def verifier_executor(request) -> FixtureVerifierResult:
            assert spec.candidate is not None
            if request.result.result_oid != spec.candidate["target_oid"]:
                raise EngineBindingRefusal(
                    "promotion verifier request names a different candidate OID")
            observed_fingerprint = fingerprint_materialized_root(request.review_root)
            if observed_fingerprint != request.review_root_fingerprint:
                raise EngineBindingRefusal(
                    "promotion verifier review root fingerprint changed before launch")
            context = RunnerCandidateContext(
                request.result.result_oid,
                request.review_root_fingerprint,
                spec.run_spec_digest,
            )
            schema = self._promotion_verifier_result_schema(
                spec,
                attempt_id,
                request.result.result_oid,
                request.review_root_fingerprint,
            )
            with tempfile.TemporaryDirectory(
                    prefix="waystone-promotion-result-") as temporary:
                output_path = Path(temporary) / "result.json"
                invocation = self._stage_invocation(
                    spec,
                    attempt_id,
                    Role.VERIFIER,
                    candidate_root=request.review_root,
                    candidate_context=context,
                    output_schema=schema,
                    output_path=output_path,
                    environment=environment,
                    execution_descriptor=execution_descriptor,
                )
                if (invocation.cwd != request.review_root.resolve(strict=True)
                        or invocation.candidate_context != context):
                    raise EngineBindingRefusal(
                        "promotion verifier invocation differs from its review root")
                self._record_promotion_verifier_launch(
                    spec,
                    attempt_id,
                    action_id,
                    invocation,
                    execution_descriptor,
                    descriptor_reference,
                )
                verifier_binding = self.assembly.profile.binding_for(Role.VERIFIER)
                try:
                    require_runner_execution_match(
                        execution_descriptor,
                        invocation.environment,
                        executable=invocation.argv[0],
                        cwd=invocation.cwd,
                        verifier_backend=verifier_binding.binding.backend,
                        verifier_binding_digest=verifier_binding.binding_digest,
                    )
                except RunnerExecutionDescriptorRefusal as error:
                    raise EngineBindingRefusal(
                        "promotion verifier executable changed before spawn: "
                        f"{error}") from error
                try:
                    completed = subprocess.run(
                        invocation.argv,
                        cwd=invocation.cwd,
                        env=invocation.environment.as_dict(),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                except OSError as error:
                    return FixtureVerifierResult(
                        1, None, str(error).encode("utf-8", errors="backslashreplace"))
                if completed.returncode != 0:
                    return FixtureVerifierResult(
                        completed.returncode, None, completed.stderr)
                try:
                    content = output_path.read_bytes()
                except OSError as error:
                    return FixtureVerifierResult(
                        0, b"", str(error).encode("utf-8", errors="backslashreplace"))
                raw = self.assembly.artifact_store.write(content)
                try:
                    output = self._parse_promotion_verifier_result(
                        content,
                        spec=spec,
                        attempt_id=attempt_id,
                        request=request,
                        actor=actor,
                        raw_digest=raw.digest,
                    )
                except EngineBindingRefusal:
                    return FixtureVerifierResult(0, content, completed.stderr)
                return FixtureVerifierResult(0, output, completed.stderr)

        verification_plan = load_verification_plan(spec.run_id, start=self.root)
        adapter = VerifierAdapter(
            verification_plan.binding_for(Role.VERIFIER).binding,
            verification_plan.verifier_sandbox,
            verifier_executor,
        )

        def no_engine_check(_request):
            raise EngineBindingRefusal(
                "check-free promotion verification cannot execute an engine check")

        assert spec.candidate is not None
        retry_of = None
        if attempt_id != f"{spec.run_id}:attempt:1":
            previous = self._previous_promotion_decision(spec, attempt_id)
            if previous is None or previous.outcome is not DecisionOutcome.REJECT:
                raise EngineBindingRefusal(
                    "promotion verifier retry requires a prior exact reject decision")
            retry_of = previous.verifier_reference_id.removeprefix(
                "verifier-evidence:")
        return execute_verifier(
            spec.run_id,
            attempt_id,
            action_id,
            self.root,
            spec.candidate["target_ref"],
            self.assembly.profile.binding_for(Role.WORKER).binding_digest,
            actor,
            no_engine_check,
            adapter,
            retry_of=retry_of,
            start=self.root,
            assurance_plan=plan,
            require_registered_result_worktree=False,
            execution_descriptor_reference_id=descriptor_reference.reference_id,
            verifier_launch_reference_id=f"verifier-launch:{action_id}",
        )

    def _promotion_review(
        self, spec: RunSpec, plan: AssurancePlan,
    ) -> tuple[ReviewCycle, ReviewerEvidence] | None:
        if not plan.requires("adversarial-review"):
            return None
        assert spec.promotion_lineage is not None
        cycles = load_review_cycle_chain(
            self.assembly,
            spec.promotion_lineage.id,
            spec.promotion_lineage.review_cycle_head_digest,
        )
        if not cycles:
            raise EngineBindingRefusal(
                "declared promotion risk requires an attached reviewer artifact")
        cycle = cycles[-1]
        reviewer = parse_reviewer_evidence_bytes(
            self.assembly.artifact_store.read(cycle.review_digest))
        return cycle, reviewer

    def _record_promotion_decision(
        self,
        spec: RunSpec,
        attempt_id: str,
        verifier: VerifierEvidence,
        review: tuple[ReviewCycle, ReviewerEvidence] | None,
    ) -> IntegrationDecision:
        assert spec.candidate is not None and spec.evaluation is not None
        evidence_ref = spec.evaluation["evidence"]
        assert isinstance(evidence_ref, Mapping)
        accepted = (
            all(item.passed for item in verifier.criterion_results)
            and not verifier.blockers
        )
        decision_input = DecisionInput(
            actor=ActorIdentity(
                self.assembly.profile.binding_for(Role.COORDINATOR).binding_digest,
                Role.COORDINATOR,
            ),
            outcome=(DecisionOutcome.ACCEPT if accepted else DecisionOutcome.REJECT),
            criteria=tuple(item.criterion for item in verifier.criterion_results),
            result_digest=verifier.result.result_digest,
            verifier_reference_id=verifier.artifact_reference.reference_id,
            verifier_artifact_digest=verifier.artifact_reference.digest,
            engine_check_reference_id=(
                verifier.engine_checks.artifact_reference.reference_id),
            engine_check_artifact_digest=(
                verifier.engine_checks.artifact_reference.digest),
            candidate_digest=spec.candidate["digest"],
            evaluation_evidence_digest=evidence_ref["digest"],
            reviewer_artifact_digests=(
                () if review is None else (review[1].digest,)
            ),
        )
        return record_integration_decision(
            spec.run_id,
            attempt_id,
            self._promotion_action_id(
                spec, attempt_id, "integration-decision"),
            decision_input,
            start=self.root,
        )

    def _execute_public_stage(
            self, spec: RunSpec, attempt_id: str) -> tuple[tuple[str, object], ...] | None:
        stage = spec.lifecycle_stage.value
        if stage in {"explore", "evaluate"}:
            action_id = self._runner_action_id(attempt_id, stage)
            adapted = self.observe_worker_result(spec.run_id, attempt_id, action_id)
            if isinstance(adapted.result, ContextRequestedWorkerResult):
                return None
            if not isinstance(adapted.result, CompletedWorkerResult):
                raise EngineBindingRefusal("stage runner did not publish a completed result")
        if stage == "explore":
            target_oid = git_full_sha(self.input_root)
            if target_oid is None:
                raise EngineBindingRefusal("explore worker result has no reachable HEAD")

            def publish() -> Candidate:
                return self.publish_candidate(
                    spec.run_id,
                    attempt_id,
                    adapted,
                    target_oid=target_oid,
                    config_digest=self.assembly.profile.content_digest,
                )

            return self._execute_stage(spec.run_id, {
                "worker": lambda: action_id,
                "result-adapter": lambda: adapted,
                "candidate-publish": publish,
                "completion": lambda: adapted.worker_result_artifact.digest,
            })
        if stage == "evaluate":
            evidence = None

            def publish_evidence() -> EvaluationEvidence:
                nonlocal evidence
                evidence, _reference = self.publish_evaluation_evidence(
                    spec.run_id,
                    evaluator_action_id=action_id,
                    result=adapted.result.result_summary,
                    metric_artifacts=tuple(
                        item.to_dict() for item in adapted.result.evidence_refs),
                )
                return evidence

            return self._execute_stage(spec.run_id, {
                "freeze": lambda: spec.candidate["digest"],  # type: ignore[index]
                "read-only-evaluator": lambda: adapted,
                "evaluation-evidence": publish_evidence,
                "completion": lambda: adapted.worker_result_artifact.digest,
            })
        assert stage == "promote"
        regression, supported, risks = self._promotion_records(spec)
        assert spec.evaluation is not None
        evidence_ref = spec.evaluation["evidence"]
        assert isinstance(evidence_ref, Mapping)
        evidence = parse_evaluation_evidence_bytes(
            self.assembly.artifact_store.read(evidence_ref["digest"]))
        del evidence
        verifier = None
        review = None
        decision = None
        plan = parse_assurance_plan_bytes(
            self.assembly.artifact_store.read(spec.assurance_plan.digest))

        def publish_verifier() -> VerifierEvidence:
            nonlocal verifier
            verifier = self._execute_promotion_verifier(
                spec, attempt_id, plan)
            return verifier

        def consume_review() -> tuple[ReviewCycle, ReviewerEvidence]:
            nonlocal review
            review = self._promotion_review(spec, plan)
            if review is None:
                raise EngineBindingRefusal(
                    "frozen adversarial-review action has no reviewer artifact")
            return review

        def publish_decision() -> IntegrationDecision:
            nonlocal decision
            if verifier is None:
                raise EngineBindingRefusal(
                    "integration decision requires completed VerifierEvidence")
            decision = self._record_promotion_decision(
                spec, attempt_id, verifier, review)
            return decision

        handlers = {
            "evaluated-candidate-freeze": lambda: spec.candidate["digest"],  # type: ignore[index]
            "independent-verify": publish_verifier,
            "integration-decision": publish_decision,
            "target-ref-apply": lambda: None,
            "completion": lambda: None,
        }
        if plan.requires("adversarial-review"):
            handlers["adversarial-review"] = consume_review
        return self._execute_stage(
            spec.run_id,
            handlers,
            regression_contract_digest=regression,
            supported_scope_digest=supported,
            accepted_risks_digest=risks,
            _promotion_composition=_PROMOTION_COMPOSITION,
        )

    def resume(self, run_id: str) -> Mapping[str, object]:
        run = self.assembly.store.get_run(run_id)
        if run.state == "closeout-ready":
            return {
                "action": None, "engine": "idle", "reason": "run_closeout_ready",
                "run_state": run.state,
            }
        if run.state == "failed":
            spec = load_run_spec(run_id, start=self.root)
            if spec.lifecycle_stage.value == "promote":
                decision = self._promotion_decision_for_attempt(
                    spec, self._latest_attempt_id(spec))
                if decision is not None and decision.outcome is DecisionOutcome.REJECT:
                    return {
                        "action": None,
                        "engine": "idle",
                        "reason": "promotion_rejected",
                        "run_state": run.state,
                    }
            return {
                "action": None, "engine": "idle", "reason": "run_failed",
                "run_state": run.state,
            }
        if run.state in {"completed", "waiting_context", "waiting_user"}:
            return self.assembly.transport.actions_next(run_id)
        spec = load_run_spec(run_id, start=self.root)
        attempt_id = self._latest_attempt_id(spec)
        if spec.lifecycle_stage.value in {"explore", "evaluate"}:
            action_id = self._ensure_stage_runner(spec, attempt_id)
            action = self.assembly.store.get_entity(EntityKind.ACTION, action_id)
            if action.state != "completed":
                try:
                    branch = self.assembly.transport.actions_next(run_id)
                except RunNotActionable:
                    branch = None
                action = self.assembly.store.get_entity(EntityKind.ACTION, action_id)
                if action.state != "completed":
                    if branch is None:
                        return {
                            "action": None,
                            "engine": "busy",
                            "poll_after_s": 1,
                            "run_state": self.assembly.store.get_run(run_id).state,
                        }
                    return branch
        completed = self._execute_public_stage(spec, attempt_id)
        if completed is None:
            return self.assembly.transport.actions_next(run_id)
        current = self.assembly.store.get_run(run_id)
        if current.state == "failed":
            return {
                "action": None,
                "engine": "idle",
                "reason": "promotion_rejected",
                "run_state": current.state,
            }
        return {
            "action": None, "engine": "idle", "reason": "run_closeout_ready",
            "run_state": current.state,
        }

    def close(self, run_id: str, outcome_content: bytes) -> OutcomePublication:
        """Publish one evidence-bound outcome pair before completing the run."""
        return publish_outcome(self.assembly, run_id, outcome_content)

    def _record_promotion_rejection(
            self, spec: RunSpec, decision: IntegrationDecision) -> None:
        attempt_id = self._latest_attempt_id(spec)
        if (decision.run_id != spec.run_id or decision.attempt_id != attempt_id
                or decision.outcome is not DecisionOutcome.REJECT):
            raise EngineBindingRefusal(
                "promotion rejection terminal requires the latest exact reject decision")
        store = self.assembly.store
        attempt = store.get_entity(EntityKind.ATTEMPT, attempt_id)
        job = store.get_entity(EntityKind.JOB, spec.job_id)
        run = store.get_run(spec.run_id)
        if run.state == "failed":
            if attempt.state != "completed" or job.state != "failed":
                raise EngineBindingRefusal(
                    "promotion rejection terminal state is internally inconsistent")
            return
        store.record_transition(
            EntityKind.ATTEMPT,
            attempt_id,
            expected_version=attempt.version,
            next_state="completed",
            reason=TransitionReason.COMPLETED,
            evidence_digest=decision.artifact_reference.digest,
        )
        store.record_transition(
            EntityKind.JOB,
            spec.job_id,
            expected_version=job.version,
            next_state="failed",
            reason=TransitionReason.PROCESS_FAILED,
            evidence_digest=decision.artifact_reference.digest,
        )
        store.record_transition(
            EntityKind.RUN,
            spec.run_id,
            expected_version=run.version,
            next_state="failed",
            reason=TransitionReason.PROCESS_FAILED,
            evidence_digest=decision.artifact_reference.digest,
        )

    def _execute_stage(
        self,
        run_id: str,
        handlers: Mapping[str, Callable[[], object]],
        *,
        regression_contract_digest: str | None = None,
        supported_scope_digest: str | None = None,
        accepted_risks_digest: str | None = None,
        _promotion_composition: object | None = None,
    ) -> tuple[tuple[str, object], ...]:
        """Run exactly the frozen stage DAG and close only its declared completion path."""
        spec = load_run_spec(run_id, start=self.root)
        plan = parse_assurance_plan_bytes(
            self.assembly.artifact_store.read(spec.assurance_plan.digest))
        if plan.lifecycle_stage is not spec.lifecycle_stage:
            raise EngineBindingRefusal("AssurancePlan stage differs from RunSpec")
        stage = spec.lifecycle_stage.value
        candidate_ref = None
        candidate_oid = None
        if spec.candidate is not None:
            candidate_ref = spec.candidate["target_ref"]
            candidate_oid = spec.candidate["target_oid"]
            if git_full_sha(self.input_root, candidate_ref) != candidate_oid:
                raise EngineBindingRefusal("frozen candidate ref changed before stage execution")
        if stage == "explore":
            candidate_ref = spec.result_policy.target_ref
            if candidate_ref is None or git_full_sha(self.input_root, candidate_ref) is not None:
                raise EngineBindingRefusal(
                    "explore candidate publication requires an absent run-owned ref")
        if spec.lifecycle_stage.value == "promote":
            if _promotion_composition is not _PROMOTION_COMPOSITION:
                raise EngineBindingRefusal(
                    "promotion authority handlers are engine-owned")
            if spec.frame_status_ref.status != "committed":
                raise EngineBindingRefusal("promotion requires a committed project frame")
            required_records = {
                "regression contract": regression_contract_digest,
                "supported scope": supported_scope_digest,
                "accepted risks": accepted_risks_digest,
            }
            for label, digest in required_records.items():
                if digest is None:
                    raise EngineBindingRefusal(f"promotion requires a {label} record")
                try:
                    self.assembly.artifact_store.read(digest)
                except WorkflowError as error:
                    raise EngineBindingRefusal(
                        f"promotion {label} record is not present in canonical CAS") from error
            if spec.promotion_lineage is None or spec.candidate is None:
                raise EngineBindingRefusal("promotion lineage/candidate is not frozen")
            target_ref = spec.result_policy.target_ref
            expected_oid = spec.result_policy.expected_oid
            if (target_ref is None or expected_oid is None
                    or git_full_sha(self.input_root, target_ref) != expected_oid):
                raise EngineBindingRefusal(
                    "promotion target differs from its frozen expected-old OID")
            candidate_lineage = self._candidate_lineage(spec.candidate["digest"])
            assert_promotion_unblocked(
                self.root / "docs" / "reviews",
                spec.promotion_lineage.id,
                candidate_lineage,
            )
            review = plan.review
            if (plan.requires("adversarial-review")
                    and review["consumed_cycles"] >= review["max_cycles"]):
                exhausted = ReviewCycleExhausted(
                    review["consumed_cycles"], review["max_cycles"])
                self._wait_for_review_budget(spec, exhausted)
                raise exhausted
            original_handlers = dict(handlers)
            promotion_results: dict[str, object] = {}
            attempt_id = self._latest_attempt_id(spec)

            def independent_verify() -> object:
                return original_handlers["independent-verify"]()

            handlers = dict(handlers)
            handlers["independent-verify"] = independent_verify
            if plan.requires("adversarial-review"):
                def adversarial_review() -> object:
                    return original_handlers["adversarial-review"]()

                handlers["adversarial-review"] = adversarial_review

            def integration_decision() -> object:
                original_handlers["integration-decision"]()
                verifier, published_review, decision = (
                    self._reload_promotion_authority(spec, attempt_id, plan))
                promotion_results["independent-verify"] = verifier
                promotion_results["adversarial-review"] = published_review
                promotion_results["integration-decision"] = decision
                evidence_ref = spec.evaluation["evidence"]
                assert isinstance(evidence_ref, Mapping)
                validator = (
                    validate_promotion_rejection
                    if decision.outcome is DecisionOutcome.REJECT
                    else validate_promotion_evidence
                )
                validator(
                    plan,
                    expected_run_id=spec.run_id,
                    expected_run_spec_digest=spec.run_spec_digest,
                    expected_candidate_digest=spec.candidate["digest"],
                    expected_candidate_oid=spec.candidate["target_oid"],
                    expected_evaluation_evidence_digest=evidence_ref["digest"],
                    expected_target_result_digest=(
                        spec.candidate["producer_result_digest"]),
                    verifier=verifier,
                    review=published_review,
                    decision=decision,
                )
                if decision.outcome is DecisionOutcome.REJECT:
                    raise _PromotionRejected(decision)
                return decision

            handlers["integration-decision"] = integration_decision

            def target_ref_apply() -> object:
                verifier = promotion_results.get("independent-verify")
                published_review = promotion_results.get("adversarial-review")
                decision = promotion_results.get("integration-decision")
                if (not isinstance(verifier, VerifierEvidence)
                        or not isinstance(decision, IntegrationDecision)
                        or (published_review is not None and (
                            not isinstance(published_review, tuple)
                            or len(published_review) != 2
                            or not isinstance(published_review[0], ReviewCycle)
                            or not isinstance(published_review[1], ReviewerEvidence)))):
                    raise EngineBindingRefusal(
                        "target apply lacks reloaded published promotion authority")
                return self._apply_candidate(
                    spec,
                    attempt_id,
                    plan,
                    verifier,
                    published_review,  # type: ignore[arg-type]
                    decision,
                )

            handlers["target-ref-apply"] = target_ref_apply
            handlers["completion"] = lambda: (
                promotion_results["integration-decision"].artifact_reference.digest  # type: ignore[union-attr]
            )
        try:
            results = execute_assurance_dag(
                plan,
                handlers,
                mutation_digest=lambda: capture_result_snapshot(self.input_root).digest,
            )
        except _PromotionRejected as rejection:
            self._record_promotion_rejection(spec, rejection.decision)
            return (("integration-decision", rejection.decision),)
        if stage == "explore":
            try:
                reference = self.assembly.store.get_artifact_reference(
                    f"candidate:{run_id}")
            except RecordNotFoundError as error:
                raise EngineBindingRefusal(
                    "explore completed without a published candidate descriptor") from error
            candidate = parse_candidate_bytes(
                self.assembly.artifact_store.read_reference(reference))
            if (candidate.target_ref != candidate_ref
                    or git_full_sha(self.input_root, candidate.target_ref)
                    != candidate.target_oid):
                raise EngineBindingRefusal(
                    "explore candidate ref does not match its frozen published descriptor")
        elif stage == "evaluate":
            if (candidate_ref is None or candidate_oid is None
                    or git_full_sha(self.input_root, candidate_ref) != candidate_oid):
                raise EngineBindingRefusal("evaluate mutated its frozen candidate ref")
            try:
                evidence = self.assembly.store.get_artifact_reference(
                    f"evaluation-evidence:{run_id}")
            except RecordNotFoundError as error:
                raise EngineBindingRefusal(
                    "evaluate completed without bound evaluation evidence") from error
            parse_evaluation_evidence_bytes(
                self.assembly.artifact_store.read_reference(evidence))
        else:
            assert candidate_ref is not None and candidate_oid is not None
            if git_full_sha(self.input_root, candidate_ref) != candidate_oid:
                raise EngineBindingRefusal("promotion mutated its evaluated candidate ref")
            assert spec.result_policy.target_ref is not None
            if git_full_sha(self.input_root, spec.result_policy.target_ref) != candidate_oid:
                raise EngineBindingRefusal(
                    "promotion apply did not publish the evaluated candidate OID")
        evidence_digest = spec.run_spec_digest
        if results and isinstance(results[-1][1], str):
            try:
                validate_sha256_digest(results[-1][1])
            except ValueError:
                pass
            else:
                evidence_digest = results[-1][1]
        self._record_stage_completion(spec, evidence_digest)
        return results

    def publish_candidate(
        self,
        run_id: str,
        attempt_id: str,
        adapted: AdaptedWorkerResult,
        *,
        target_oid: str,
        config_digest: str,
        supersedes_candidate_digest: str | None = None,
        repair_of_finding_refs: Sequence[str] = (),
    ) -> Candidate:
        """Publish one explore result with creation-only candidate-ref CAS."""
        spec = load_run_spec(run_id, start=self.root)
        plan = parse_assurance_plan_bytes(
            self.assembly.artifact_store.read(spec.assurance_plan.digest))
        if (spec.lifecycle_stage.value != "explore"
                or not plan.requires("candidate-publish")
                or not isinstance(adapted.result, CompletedWorkerResult)):
            raise EngineBindingRefusal(
                "candidate publication requires a completed explore result and frozen action")
        target_ref = spec.result_policy.target_ref
        if target_ref != f"refs/waystone/candidates/{run_id}":
            raise EngineBindingRefusal("candidate target ref is not the frozen run-owned ref")
        candidate = Candidate(
            candidate_id=run_id,
            producer={
                "run_id": run_id,
                "run_spec_digest": spec.run_spec_digest,
                "result_digest": adapted.worker_result_artifact.digest,
            },
            code_sha=target_oid,
            config_digest=config_digest,
            target_ref=target_ref,
            target_oid=target_oid,
            supersedes_candidate_digest=supersedes_candidate_digest,
            repair_of_finding_refs=tuple(repair_of_finding_refs),
        )
        candidate_artifact = self.assembly.artifact_store.write(candidate.canonical_bytes())
        action_id = f"{run_id}:candidate-publish"
        try:
            effect_plan = self.assembly.effect_executor.plan_effect(
                run_id, spec.job_id, attempt_id, action_id,
                GitRefEffect(self.input_root, target_ref, None, target_oid),
            )
            claimed = self.assembly.effect_executor.claim_effect(effect_plan, ttl_seconds=30)
            result = self.assembly.effect_executor.execute_effect(claimed)
        except EffectStateRefusal:
            result = self.assembly.effect_executor.reconcile_actions((action_id,))[0]
        if result.state not in {EffectResultState.COMPLETED, EffectResultState.NOOP}:
            raise EngineBindingRefusal(result.reason or "candidate publication did not complete")
        reference_id = f"candidate:{run_id}"
        try:
            reference = self.assembly.store.get_artifact_reference(reference_id)
        except RecordNotFoundError:
            job = self.assembly.store.get_entity(EntityKind.JOB, spec.job_id)
            self.assembly.store.record_transition(
                EntityKind.JOB, spec.job_id, expected_version=job.version,
                next_state=job.state, reason=TransitionReason.CANDIDATE_PUBLISHED,
                evidence_digest=candidate_artifact.digest,
                artifact_references=(ArtifactReference(
                    reference_id, ArtifactReferenceKind.EVIDENCE,
                    candidate_artifact.digest, candidate_artifact.size),),
            )
        else:
            if reference.digest != candidate_artifact.digest:
                raise EngineBindingRefusal(
                    "published candidate reference differs from the deterministic descriptor")
        return candidate

    def publish_evaluation_evidence(
        self,
        run_id: str,
        *,
        evaluator_action_id: str,
        result: str,
        metric_artifacts: Sequence[Mapping[str, str]],
        prior_evidence: Sequence[EvaluationEvidence] = (),
        holdout_exposed: bool = False,
    ) -> tuple[EvaluationEvidence, ArtifactReference]:
        """Bind a read-only evaluator result to the exact candidate/spec generation."""
        spec = load_run_spec(run_id, start=self.root)
        plan = parse_assurance_plan_bytes(
            self.assembly.artifact_store.read(spec.assurance_plan.digest))
        if (spec.lifecycle_stage.value != "evaluate"
                or not plan.requires("read-only-evaluator")
                or spec.candidate is None):
            raise EngineBindingRefusal(
                "evaluation evidence requires a frozen evaluate plan and candidate")
        frozen_spec = spec.evaluation.get("spec")
        if not isinstance(frozen_spec, Mapping):
            raise EngineBindingRefusal("evaluation spec is not frozen")
        try:
            spec_bytes = git_read_bytes(
                self.input_root, "show",
                f"{frozen_spec['commit']}:{frozen_spec['path']}")
        except GitReadError as error:
            raise EngineBindingRefusal(f"evaluation spec is unavailable: {error}") from error
        parsed_spec = parse_evaluation_spec_bytes(spec_bytes)
        with self.assembly.store._connection_lock:  # noqa: SLF001 - evidence index
            rows = self.assembly.store._connection.execute(  # noqa: SLF001
                "SELECT reference_id FROM artifacts WHERE reference_id LIKE ?",
                ("evaluation-evidence:%",),
            ).fetchall()
        indexed_evidence = []
        for row in rows:
            reference = self.assembly.store.get_artifact_reference(row["reference_id"])
            indexed_evidence.append(parse_evaluation_evidence_bytes(
                self.assembly.artifact_store.read_reference(reference)))
        assert_evaluation_generation_available(
            spec.candidate["digest"], parsed_spec,
            (*indexed_evidence, *prior_evidence),
            holdout_exposed=holdout_exposed,
        )
        evidence = EvaluationEvidence(
            candidate_digest=spec.candidate["digest"],
            evaluation_spec_digest=frozen_spec["digest"],
            evaluation_generation=frozen_spec["generation"],
            evaluator_action_id=evaluator_action_id,
            result=result,
            metric_artifacts=tuple(dict(item) for item in metric_artifacts),
        )
        artifact = self.assembly.artifact_store.write(evidence.canonical_bytes())
        reference = ArtifactReference(
            f"evaluation-evidence:{run_id}", ArtifactReferenceKind.EVIDENCE,
            artifact.digest, artifact.size,
        )
        job = self.assembly.store.get_entity(EntityKind.JOB, spec.job_id)
        self.assembly.store.record_transition(
            EntityKind.JOB, spec.job_id, expected_version=job.version,
            next_state=job.state, reason=TransitionReason.EVALUATION_EVIDENCE,
            evidence_digest=artifact.digest, artifact_references=(reference,),
        )
        return evidence, reference

    def append_review_cycle(
        self, run_id: str, *, target_result_digest: str, review_digest: str,
    ) -> ReviewCycle:
        """Append one immutable review cycle without trusting a caller-supplied count."""
        spec = load_run_spec(run_id, start=self.root)
        plan = parse_assurance_plan_bytes(
            self.assembly.artifact_store.read(spec.assurance_plan.digest))
        if (spec.promotion_lineage is None
                or plan.review.get("promotion_lineage_id") != spec.promotion_lineage.id
                or not plan.requires("adversarial-review")):
            raise EngineBindingRefusal("run has no frozen risk-gated review action")
        if not isinstance(spec.candidate, Mapping):
            raise EngineBindingRefusal("review attachment requires a frozen candidate")
        expected_result = validate_sha256_digest(
            spec.candidate.get("producer_result_digest"))  # type: ignore[arg-type]
        if validate_sha256_digest(target_result_digest) != expected_result:
            raise EngineBindingRefusal(
                "review attachment names a different promotion result")
        reviewer = parse_reviewer_evidence_bytes(
            self.assembly.artifact_store.read(review_digest))
        if (reviewer.promotion_lineage_id != spec.promotion_lineage.id
                or reviewer.target_run_spec_digest != spec.run_spec_digest
                or reviewer.candidate_digest != spec.candidate.get("digest")
                or reviewer.target_result_digest != expected_result
                or reviewer.digest != review_digest):
            raise EngineBindingRefusal(
                "review attachment artifact differs from the frozen promotion lineage")
        if self.assembly.profile is not None:
            expected_actor = self.assembly.profile.binding_for(Role.REVIEWER).binding_digest
            if reviewer.actor["actor_id"] != expected_actor:
                raise EngineBindingRefusal(
                    "review attachment actor differs from the frozen reviewer binding")
        cycles = load_review_cycle_chain(
            self.assembly,
            spec.promotion_lineage.id,
            plan.review.get("cycle_chain_head_digest"),
        )
        if len(cycles) < plan.review["consumed_cycles"]:
            raise EngineBindingRefusal(
                "AssurancePlan consumed review count does not rederive from its CAS chain")
        maximum = plan.review["max_cycles"]
        if len(cycles) >= maximum:
            exhausted = ReviewCycleExhausted(len(cycles), maximum)
            self._wait_for_review_budget(spec, exhausted)
            raise exhausted
        cycle = ReviewCycle(
            promotion_lineage_id=spec.promotion_lineage.id,
            cycle=len(cycles) + 1,
            target_result_digest=target_result_digest,
            review_digest=review_digest,
            supersedes_digest=(cycles[-1].digest if cycles else None),
        )
        artifact = self.assembly.artifact_store.write(cycle.canonical_bytes())
        reference_id = f"review-cycle:{spec.promotion_lineage.id}:{cycle.cycle}"
        try:
            reference = self.assembly.store.get_artifact_reference(reference_id)
        except RecordNotFoundError:
            job = self.assembly.store.get_entity(EntityKind.JOB, spec.job_id)
            self.assembly.store.record_transition(
                EntityKind.JOB,
                spec.job_id,
                expected_version=job.version,
                next_state=job.state,
                reason=TransitionReason.REVIEW_CYCLE,
                evidence_digest=artifact.digest,
                artifact_references=(ArtifactReference(
                    reference_id,
                    ArtifactReferenceKind.EVIDENCE,
                    artifact.digest,
                    artifact.size,
                ),),
            )
        else:
            if reference.digest != artifact.digest:
                raise EngineBindingRefusal("durable review cycle reference is divergent")
        return cycle

    def _candidate_lineage(self, candidate_digest: object) -> tuple[str, ...]:
        current = validate_sha256_digest(candidate_digest)  # type: ignore[arg-type]
        lineage = []
        seen = set()
        while current is not None:
            if current in seen:
                raise EngineBindingRefusal("candidate supersedes lineage contains a cycle")
            seen.add(current)
            candidate = parse_candidate_bytes(self.assembly.artifact_store.read(current))
            lineage.append(current)
            current = candidate.supersedes_candidate_digest
        return tuple(reversed(lineage))

    def _wait_for_review_budget(
            self, spec: RunSpec, exhausted: ReviewCycleExhausted) -> None:
        artifact = self.assembly.artifact_store.write(
            json.dumps(
                exhausted.waiting_user(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8"))
        for kind, identity in ((EntityKind.JOB, spec.job_id), (EntityKind.RUN, spec.run_id)):
            entity = (
                self.assembly.store.get_entity(kind, identity)
                if kind is EntityKind.JOB else self.assembly.store.get_run(identity))
            if entity.state != "waiting_user":
                self.assembly.store.record_transition(
                    kind, identity, expected_version=entity.version,
                    next_state="waiting_user",
                    reason=TransitionReason.REVIEW_CYCLE_EXHAUSTED,
                    evidence_digest=artifact.digest,
                )

    def _record_stage_completion(self, spec: RunSpec, evidence_digest: str) -> None:
        with self.assembly.store._connection_lock:  # noqa: SLF001 - final attempt projection
            row = self.assembly.store._connection.execute(  # noqa: SLF001
                "SELECT attempt_id FROM attempts WHERE run_id = ? AND job_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (spec.run_id, spec.job_id),
            ).fetchone()
        if row is None:
            raise EngineBindingRefusal("stage completion requires a final attempt")
        attempt_id = row["attempt_id"]
        for kind, identity, next_state in (
                (EntityKind.ATTEMPT, attempt_id, "completed"),
                (EntityKind.JOB, spec.job_id, "completed"),
                (EntityKind.RUN, spec.run_id, "closeout-ready")):
            entity = (
                self.assembly.store.get_run(identity)
                if kind is EntityKind.RUN
                else self.assembly.store.get_entity(kind, identity))
            if entity.state != next_state:
                self.assembly.store.record_transition(
                    kind, identity, expected_version=entity.version,
                    next_state=next_state, reason=TransitionReason.COMPLETED,
                    evidence_digest=evidence_digest,
                )


_CANCELLATION_STATES = {
    "cancel-requested",
    "stopping",
    "cancel-pending(reason=identity-unknown)",
    "cancel-pending(reason=liveness-unknown)",
    "cancel-pending(reason=unknown-effect)",
    "canceled",
}


def _attempt_id(run_id: str) -> str:
    return f"{run_id}:attempt"


def _runner_action_id(run_id: str, check_id: str) -> str:
    return f"{run_id}:runner:{check_id}"


def _verify_action_id(run_id: str) -> str:
    return f"{run_id}:verify"


def _regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise ReadOnlyStoreUnavailable(f"cannot inspect {label}: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReadOnlyStoreUnavailable(f"{label} is not a regular file")


@contextmanager
def open_read_only_store(project_root: Path) -> Iterator[RunStore]:
    """Open a copied SQLite/WAL snapshot without touching project state bytes."""
    root = Path(project_root).resolve(strict=True)
    marker = root / ".waystone.yml"
    _regular_file(marker, "project marker")
    state = root / ".waystone"
    database = state / "state.db"
    _regular_file(database, "runtime database")

    with tempfile.TemporaryDirectory(prefix="waystone-status-") as temporary:
        snapshot_root = Path(temporary)
        copied_database = snapshot_root / "state.db"
        source = None
        connection = None
        try:
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{database}{suffix}")
                try:
                    info = sidecar.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise ReadOnlyStoreUnavailable(
                        f"runtime database sidecar {sidecar.name} is not a regular file")
            source = sqlite3.connect(
                database.as_uri() + "?mode=ro",
                uri=True,
                timeout=5,
                isolation_level=None,
                check_same_thread=False,
            )
            source.execute("PRAGMA query_only=ON")
            connection = sqlite3.connect(
                copied_database,
                timeout=5,
                isolation_level=None,
                check_same_thread=False,
            )
            source.backup(connection)
            source.close()
            source = None
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            version = store_module._existing_schema_version(connection)  # noqa: SLF001
            store_module._validate_schema(connection)  # noqa: SLF001
            connection.set_authorizer(store_module._store_authorizer)  # noqa: SLF001
        except (OSError, sqlite3.DatabaseError, WorkflowError) as error:
            if source is not None:
                source.close()
            if connection is not None:
                connection.close()
            raise ReadOnlyStoreUnavailable(str(error)) from error
        store = RunStore(
            root,
            copied_database,
            connection,
            FilesystemInfo("read-only-snapshot", snapshot_root, writable=False),
            version,
            _token=store_module._RUN_STORE_CONSTRUCTION_TOKEN,  # noqa: SLF001
        )
        try:
            yield store
        finally:
            store.close()


class RunEngine:
    """Compose the existing M1-B modules for one frozen task run."""

    def __init__(self, project_root: Path, assembly: RunAssembly | None = None):
        self.root = Path(project_root).resolve(strict=True)
        self.assembly = assembly

    def _require_assembly(self) -> RunAssembly:
        if self.assembly is None:
            raise EngineConfigurationUnavailable(
                "no explicit VerificationPlan/backend assembly is configured; "
                "an alternate execution path is not a fallback")
        return self.assembly

    @staticmethod
    def _ensure_attempt(store: RunStore, spec: RunSpec):
        identity = _attempt_id(spec.run_id)
        try:
            return store.get_entity(EntityKind.ATTEMPT, identity)
        except RecordNotFoundError:
            return store.create_attempt(spec.run_id, spec.job_id, identity, initial_state="running")

    def _invocations(self, dispatch: DispatchReady) -> dict[str, RunnerInvocation]:
        assembly = self._require_assembly()
        supplied = dict(assembly.runner_invocations(dispatch))
        expected = {action.prepared_input_digest: action for action in dispatch.engine_actions}
        if set(supplied) != set(expected):
            raise EngineBindingRefusal(
                "runner invocation set does not exactly match frozen engine actions")
        normalized: dict[str, RunnerInvocation] = {}
        for digest, action in expected.items():
            invocation = supplied[digest]
            if not isinstance(invocation, RunnerInvocation):
                raise EngineBindingRefusal(
                    f"invocation {digest!r} is not a RunnerInvocation")
            if invocation.argv != action.command:
                raise EngineBindingRefusal(
                    f"check {action.check_id!r} invocation differs from its frozen command")
            if action.child_environment:
                raise EngineBindingRefusal(
                    f"check {action.check_id!r} requires child environment values that "
                    "RunnerInvocation cannot carry")
            try:
                invocation.cwd.resolve(strict=True)
            except OSError as error:
                raise EngineBindingRefusal(
                    f"check {action.check_id!r} cwd is unavailable: {error}") from error
            normalized[digest] = invocation
        return normalized

    def _runtime(self, store: RunStore, invocations: Mapping[str, RunnerInvocation]):
        leases = LeaseManager(store)
        supervisor = Supervisor(store, leases, invocations=invocations)
        effects = EffectEngine(
            store,
            leases,
            runner_executor=supervisor.runner_executor,
            runner_identity_verifier=supervisor.runner_identity_verifier,
        )
        return leases, supervisor, effects, ActionTransport(store, effects)

    def _materialize_dispatch(self, store: RunStore, spec: RunSpec,
                              dispatch: DispatchReady) -> None:
        attempt = self._ensure_attempt(store, spec)
        _leases, _supervisor, effects, _transport = self._runtime(
            store, self._invocations(dispatch))
        for action in dispatch.engine_actions:
            identity = _runner_action_id(spec.run_id, action.check_id)
            try:
                existing = store.get_entity(EntityKind.ACTION, identity)
            except RecordNotFoundError:
                effects.plan_effect(
                    spec.run_id,
                    spec.job_id,
                    attempt.entity_id,
                    identity,
                    RunnerExecutionEffect(action.prepared_input_digest),
                )
            else:
                plan = effects._load_plan(identity)  # noqa: SLF001 - composition validation
                if (existing.run_id != spec.run_id
                        or plan.kind is not EffectKind.RUNNER_EXECUTION
                        or plan.spec.get("invocation_digest") != action.prepared_input_digest):
                    raise EngineBindingRefusal(
                        f"runner action {identity!r} differs from frozen preflight")

    def start(self, task_id: str) -> StartResult:
        assembly = self._require_assembly()
        spec = plan_one_task_run(task_id, start=self.root)
        plan = freeze_verification_plan(
            spec.run_id, assembly.verification_plan, start=self.root)
        inputs = assembly.preflight_inputs(plan)
        if not isinstance(inputs, PreflightInputs):
            raise EngineBindingRefusal("preflight_inputs did not return PreflightInputs")
        dispatch = preflight_for_dispatch(
            spec.run_id,
            capabilities=inputs.capabilities,
            materialized_toolchains=inputs.materialized_toolchains,
            current_runner_context=inputs.runner_context,
            reusable_runner_proof=inputs.runner_proof,
            start=self.root,
        )
        with RunStore.open(self.root) as store:
            self._materialize_dispatch(store, spec, dispatch)
            branch = self._actions_next_open(store, spec.run_id, dispatch=dispatch)
        return StartResult(spec.run_id, branch)

    def _runner_actions(self, store: RunStore, run_id: str):
        with store._connection_lock:  # noqa: SLF001 - package composition query
            rows = store._connection.execute(  # noqa: SLF001
                "SELECT action_id FROM actions WHERE run_id = ? AND state != 'completed' "
                "ORDER BY action_id",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            action = store.get_entity(EntityKind.ACTION, row["action_id"])
            try:
                reference = store.get_artifact_reference(f"effect-plan:{action.entity_id}")
            except RecordNotFoundError:
                continue
            del reference
            result.append(action)
        return tuple(result)

    def _actions_next_open(self, store: RunStore, run_id: str, *,
                           dispatch: DispatchReady | None = None) -> Mapping[str, object]:
        invocations: Mapping[str, RunnerInvocation] = {}
        if dispatch is not None:
            invocations = self._invocations(dispatch)
        leases, supervisor, effects, transport = self._runtime(store, invocations)
        del leases
        runner_seen = False
        for action in self._runner_actions(store, run_id):
            plan = effects._load_plan(action.entity_id)  # noqa: SLF001
            if plan.kind is not EffectKind.RUNNER_EXECUTION:
                continue
            runner_seen = True
            if action.state == "planned":
                if plan.spec["invocation_digest"] not in invocations:
                    raise EngineExecutorUnavailable(
                        "planned runner has no exact detached Supervisor invocation")
                claimed = effects.claim_effect(plan, ttl_seconds=30)
                effects.execute_effect(claimed)
                current_run = store.get_run(run_id)
                return {
                    "action": None,
                    "engine": "busy",
                    "poll_after_s": 1,
                    "run_state": current_run.state,
                }
            if action.state in {"claimed", "effect", "observed"}:
                observation = supervisor.probe_action(action.entity_id)
                if observation.state is LivenessState.ALIVE:
                    current_run = store.get_run(run_id)
                    return {
                        "action": None,
                        "engine": "busy",
                        "poll_after_s": 1,
                        "run_state": current_run.state,
                    }
                if observation.state is LivenessState.EXITED:
                    effects.reconcile_actions(
                        (action.entity_id,),
                        quiescence_probe=supervisor.quiescence_probe,
                    )
                    continue
                raise RunNotActionable(
                    f"runner {action.entity_id!r} liveness is unknown: {observation.reason}")
        if runner_seen:
            current_run = store.get_run(run_id)
            return {
                "action": None,
                "engine": "busy",
                "poll_after_s": 1,
                "run_state": current_run.state,
            }
        return transport.actions_next(run_id)

    def actions_next(self, run_id: str) -> Mapping[str, object]:
        dispatch = None
        if self.assembly is not None:
            dispatch = load_dispatch_ready(run_id, start=self.root)
        with RunStore.open(self.root) as store:
            return self._actions_next_open(store, run_id, dispatch=dispatch)

    def actions_submit(self, action_id: str, payload: Mapping[str, object]):
        with RunStore.open(self.root) as store:
            _leases, _supervisor, _effects, transport = self._runtime(store, {})
            return transport.submit(action_id, payload)

    def _primary_runner_action(self, store: RunStore, run_id: str) -> str:
        actions = []
        for action in self._runner_actions(store, run_id):
            try:
                leases, supervisor, effects, _transport = self._runtime(store, {})
                del leases, supervisor
                plan = effects._load_plan(action.entity_id)  # noqa: SLF001
                if plan.kind is EffectKind.RUNNER_EXECUTION:
                    actions.append(action.entity_id)
            except RecordNotFoundError:
                continue
        if len(actions) != 1:
            raise ActionPlanRefusal(
                f"run {run_id!r} does not have exactly one cancellable runner action")
        return actions[0]

    def cancel(self, run_id: str, reason: CancelReason) -> CancellationResult:
        typed_reason = CancelReason(reason)
        dispatch = None
        if self.assembly is not None:
            dispatch = load_dispatch_ready(run_id, start=self.root)
        invocations = {} if dispatch is None else self._invocations(dispatch)
        with RunStore.open(self.root) as store:
            action_id = self._primary_runner_action(store, run_id)
            run = store.get_run(run_id)
            if run.state == "dispatch-ready":
                store.record_transition(
                    EntityKind.RUN,
                    run_id,
                    expected_version=run.version,
                    next_state="running",
                    reason=TransitionReason.PROCESS_STARTED,
                )
            leases, supervisor, effects, _transport = self._runtime(store, invocations)
            cancellation = CancellationEngine(store, effects, leases, supervisor)
            return cancellation.request_cancel(
                run_id, action_id, reason=typed_reason.value)

    def _resume_cancel(self, store: RunStore, run_id: str,
                       invocations: Mapping[str, RunnerInvocation]) -> CancellationResult:
        action_id = self._primary_runner_action(store, run_id)
        leases, supervisor, effects, _transport = self._runtime(store, invocations)
        cancellation = CancellationEngine(store, effects, leases, supervisor)
        return cancellation.resume_cancel(run_id, action_id)

    def _complete(self, spec: RunSpec) -> CompletionResult:
        assurance = parse_assurance_plan_bytes(
            ArtifactStore(self.root).read(spec.assurance_plan.digest))
        raise EngineConfigurationUnavailable(
            f"RunSpec v2 {assurance.lifecycle_stage.value} completion must execute "
            "through StagedRunEngine.resume with its frozen exact action DAG")

    def resume(self, run_id: str) -> ResumeResult:
        spec = load_run_spec(run_id, start=self.root)
        completion_started = False
        with RunStore.open(self.root) as store:
            run = store.get_run(run_id)
            if run.state in _CANCELLATION_STATES:
                return ResumeResult(
                    run_id,
                    cancellation=self._resume_cancel(store, run_id, {}),
                )
            if run.state == "completed":
                return ResumeResult(
                    run_id,
                    dispatch={
                        "action": None,
                        "engine": "idle",
                        "reason": "run_completed",
                        "run_state": "completed",
                    },
                )
            try:
                store.get_artifact_reference(
                    f"verifier-evidence:{_verify_action_id(run_id)}")
            except RecordNotFoundError:
                pass
            else:
                completion_started = True
        if completion_started:
            return ResumeResult(run_id, completion=self._complete(spec))
        dispatch = load_dispatch_ready(run_id, start=self.root)
        with RunStore.open(self.root) as store:
            if not self._runner_actions(store, run_id):
                branch = None
            else:
                branch = self._actions_next_open(store, run_id, dispatch=dispatch)
            if branch is not None and (
                    branch.get("engine") == "busy" or branch.get("action") is not None):
                return ResumeResult(run_id, dispatch=branch)
            if branch is not None and self._runner_actions(store, run_id):
                return ResumeResult(run_id, dispatch=branch)
        return ResumeResult(run_id, completion=self._complete(spec))

    def status(self, run_id: str) -> RunSnapshot:
        with open_read_only_store(self.root) as store:
            leases = LeaseManager(store)
            supervisor = Supervisor(store, leases, invocations={})
            effects = EffectEngine(
                store,
                leases,
                runner_identity_verifier=supervisor.runner_identity_verifier,
            )
            return snapshot_run(store, effects, supervisor, run_id)

    def status_human(self, run_id: str) -> str:
        return render_human(self.status(run_id))

    def status_json(self, run_id: str) -> Mapping[str, object]:
        return json_projection(self.status(run_id))

    def watch(self, run_id: str, *, poll_interval: float = 1.0) -> Iterator[str]:
        return watch_run(lambda: self.status(run_id), poll_interval=poll_interval)

    def latest_run_id(self) -> str:
        with open_read_only_store(self.root) as store:
            with store._connection_lock:  # noqa: SLF001
                row = store._connection.execute(  # noqa: SLF001
                    "SELECT run_id FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        if row is None:
            raise RecordNotFoundError("run", "latest")
        return row["run_id"]


__all__ = [
    "CancelReason",
    "CompletionResult",
    "ContextResumeResult",
    "EngineAssemblyError",
    "EngineBindingRefusal",
    "EngineConfigurationUnavailable",
    "PreflightInputs",
    "PendingContext",
    "ReadOnlyStoreUnavailable",
    "ResumeResult",
    "RunAssembly",
    "RunEngine",
    "StagedRunEngine",
    "StagedStartResult",
    "StartResult",
    "load_review_cycle_chain",
    "open_read_only_store",
    "validate_promotion_evidence",
    "validate_promotion_rejection",
]
