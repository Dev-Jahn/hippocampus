#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Contract tests for verifier evidence, integration decisions, and apply."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import base64
import hashlib
import json
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from unittest import mock

from test_work_brief import (
    completion_contract as semantic_completion_contract,
    payload as work_brief_payload,
    project_bytes,
)
from waystone.features.review_layout import new_run_id
from waystone.jobs import completion
from waystone.jobs.domain import ExecutionCategory, Role, RoleBinding
from waystone.project.brief import read_project_frame_at_commit
from waystone.runs.artifacts import ArtifactStore
from waystone.runs.assurance import compile_assurance_plan
from waystone.runs.effects import ArtifactWriteEffect, EffectEngine, EffectRetryRefused
from waystone.runs.effects import ApprovalAuthorityMismatch
from waystone.runs.lease import LeaseManager
from waystone.runs.preflight import (
    CapabilitySet,
    CheckCapabilityProbe,
    CheckDefinition,
    CheckPhase,
    DependencyConstraint,
    EnvironmentPreparationReceipt,
    EnvironmentPreparationStep,
    MaterializedToolchain,
    NetworkCacheRequirements,
    ObservationStatus,
    ProbeTarget,
    RoleCapability,
    RunnerCapabilities,
    RunnerContext,
    RuntimeObservation,
    SandboxContract,
    ToolchainObservation,
    ToolchainRequirement,
    VerificationPlanStateError,
    VerificationPlanDefinition,
    WorkingDirectoryRule,
    freeze_verification_plan,
    load_dispatch_ready,
    preflight_for_dispatch,
    record_runner_proof,
)
from waystone.runs.spec import plan_one_task_run, read_base_snapshot
from waystone.runs.store import (
    EntityKind,
    FilesystemInfo,
    RunStore,
    TransitionReason,
)
from waystone.runs.verify import (
    ActorIdentity,
    ApplyBindingRefusal,
    ApplyConcurrentDriftRefusal,
    ApplyDriftRefusal,
    BlockerOverride,
    BlockerOverrideRefusal,
    CheckedOutTargetRefRefusal,
    CriterionResult,
    DecisionActorRefusal,
    DecisionInput,
    DecisionOutcome,
    DecisionResultDigestRefusal,
    EngineCheckOutput,
    EvidenceBindingRefusal,
    ExtraCriterionRefusal,
    FixtureVerifierResult,
    InvalidVerifierOutput,
    MissingCriterionRefusal,
    VerifierActorRefusal,
    VerifierAdapter,
    VerifierBindingRefusal,
    VerifierExecutionFailed,
    VerifierMutationRefusal,
    VerifierBlocker,
    VerifierOutput,
    apply_integration_decision,
    derive_git_result,
    execute_verifier,
    fingerprint_worktree,
    record_integration_decision,
    reload_integration_decision,
    reload_verifier_evidence,
)
import waystone.runs.verify as verify_module


def sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class VerifyFixture:
    root: Path
    result_worktree: Path
    spec: object
    plan: object
    ready: object
    result_ref: str
    target_ref: str


class RunVerifyTests(unittest.TestCase):
    """Exercise the M1-B verifier/decision/apply slice with real Git fixtures."""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.base = Path(self._temporary_directory.name)
        self._project_number = 0
        self.check_sandbox = SandboxContract(
            "isolated-worktree-write", "process-exec", "network-denied")
        self.verifier_sandbox = SandboxContract(
            "read-only", "process-exec", "network-denied")
        self.worker = ActorIdentity("worker-fixture", Role.WORKER)
        self.verifier = ActorIdentity("verifier-fixture", Role.VERIFIER)
        self.coordinator = ActorIdentity("coordinator-fixture", Role.COORDINATOR)

    @contextmanager
    def supported_filesystem(self):
        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)):
            yield

    @staticmethod
    def git_bytes(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(root), *args], input=input_bytes,
            capture_output=True, check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
        return result.stdout

    @staticmethod
    def git_oid(root: Path, ref: str) -> str:
        return RunVerifyTests.git_bytes(root, "rev-parse", ref).decode("ascii").strip()

    @staticmethod
    def raw_status(root: Path) -> bytes:
        return RunVerifyTests.git_bytes(
            root, "status", "--porcelain=v1", "-z", "--untracked-files=all")

    @staticmethod
    def index_bytes(root: Path) -> bytes:
        raw = RunVerifyTests.git_bytes(root, "rev-parse", "--git-path", "index")
        path = Path(os.fsdecode(raw.rstrip(b"\n")))
        if not path.is_absolute():
            path = root / path
        return path.read_bytes()

    def project(self) -> tuple[Path, Path]:
        self._project_number += 1
        root = self.base / f"repo-{self._project_number}"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text(
            "version: 1\nproject: verify-fixture\n", encoding="utf-8")
        (root / "tasks.yaml").write_text(
            "version: 1\n"
            "project: verify-fixture\n"
            "tasks:\n"
            "  - id: feat/example\n"
            "    title: verify and integrate one result\n"
            "    status: pending\n"
            "    accept:\n"
            "      - binary patch bytes are preserved\n"
            "      - integration preserves unrelated user work\n",
            encoding="utf-8",
        )
        (root / ".gitignore").write_text(
            ".waystone/\n.toolchains/\nignored.bin\n", encoding="utf-8")
        (root / "tracked.txt").write_bytes(b"tracked-base\n")
        (root / "staged.txt").write_bytes(b"staged-base\n")
        (root / "PROJECT_BRIEF.md").write_bytes(project_bytes())
        git(root, "add", "-A")
        self.assertEqual(git(root, "commit", "-qm", "verify fixture").returncode, 0)

        state = root / ".waystone"
        state.mkdir()
        (state / "profile.yml").write_text(
            "schema: waystone-profile-2\n"
            "bindings:\n"
            "  coordinator: {execution: in-session, backend: 'host:current'}\n"
            "  worker: {execution: external, backend: 'codex:gpt-test'}\n"
            "  verifier: {execution: external, backend: 'codex:gpt-verify'}\n"
            "  reviewer: {execution: external, backend: 'codex:gpt-review'}\n",
            encoding="utf-8",
        )
        toolchain = root / ".toolchains" / "verify.whl"
        toolchain.parent.mkdir()
        toolchain.write_bytes(b"verify-toolchain-v1")
        return root, toolchain

    def definition(self) -> VerificationPlanDefinition:
        source = "lock:verify@https://packages.example/verify.whl"
        toolchain = ToolchainRequirement(
            toolchain_id="verify-wheel",
            executable="verify",
            runtime="python>=3.10",
            source_id=source,
            content_digest=sha256(b"verify-toolchain-v1"),
            size=len(b"verify-toolchain-v1"),
            dependencies=(DependencyConstraint("verify", "==1.0"),),
        )
        return VerificationPlanDefinition(
            required_checks=(CheckDefinition(
                check_id="contract-suite",
                phase=CheckPhase.VERIFICATION,
                command=("uv", "run", "scripts/tests/test_run_verify.py"),
                working_directory=WorkingDirectoryRule.INTEGRATION_ROOT,
                expected_exit_codes=(0,),
                expected_evidence_kinds=("stdout", "stderr"),
                environment=(),
                fixture_digests=(sha256(b"verify-fixture"),),
                required_toolchain_ids=("verify-wheel",),
                sandbox=self.check_sandbox,
                worker_execution_required=True,
            ),),
            required_toolchains=(toolchain,),
            environment_preparation=(EnvironmentPreparationStep(
                sequence=0,
                step_id="materialize-verifier",
                command=("uv", "sync", "--offline"),
                input_toolchain_ids=("verify-wheel",),
            ),),
            network_cache_requirements=NetworkCacheRequirements(
                network_required=False,
                allowed_sources=(source,),
                cache_namespace="verify-fixture-cache",
                offline_capable=True,
            ),
            verifier_sandbox=self.verifier_sandbox,
        )

    @staticmethod
    def observations() -> tuple[RuntimeObservation, ...]:
        rows = (
            ("cache-boundary", "engine:cache-boundary", ObservationStatus.OBSERVED),
            ("platform-kernel", "engine:platform-kernel", ObservationStatus.OBSERVED),
            ("process-security", "engine:process-security", ObservationStatus.NOT_OBSERVED),
            ("runner-binary", "runner-adapter:binary", ObservationStatus.OBSERVED),
            ("runner-config-content", "runner-adapter:config",
             ObservationStatus.NOT_OBSERVED),
            ("runner-version", "runner-adapter:version", ObservationStatus.OBSERVED),
            ("sandbox-contract", "engine:sandbox-contract", ObservationStatus.OBSERVED),
        )
        return tuple(RuntimeObservation(
            axis, source, status,
            None if status is ObservationStatus.NOT_OBSERVED
            else sha256(axis.encode("utf-8")),
        ) for axis, source, status in rows)

    def make_dispatch_ready(self, root: Path, toolchain_path: Path):
        head = self.git_oid(root, "HEAD")
        frame = read_project_frame_at_commit(root, head)
        contract = semantic_completion_contract(root, frame)
        brief_body = work_brief_payload(head, frame, new_run_id())
        brief_body["task_id"] = "feat/example"
        with self.supported_filesystem():
            spec = plan_one_task_run(
                "feat/example",
                work_brief_content=completion.canonical_json(brief_body),
                completion_contract_content=contract.canonical_bytes(),
                assurance_plan_content=compile_assurance_plan("explore").canonical_bytes(),
                frame_status_ref=frame.status_ref,
                project_fact_refs=(frame.fact_ref("hypothesis/solver"),),
                start=root,
            )
            plan = freeze_verification_plan(
                spec.run_id, self.definition(), start=root)

        worker_binding = plan.binding_for(Role.WORKER).binding
        verifier_binding = plan.binding_for(Role.VERIFIER).binding
        runner = RunnerCapabilities(
            execution_categories=(ExecutionCategory.EXTERNAL,),
            engine_sandboxes=(self.check_sandbox,),
            role_capabilities=(
                RoleCapability(
                    binding=worker_binding,
                    sandbox=self.check_sandbox,
                    accepts_frozen_base=False,
                    accepts_patch_bytes=False,
                    accepts_result_digest=False,
                    emits_artifacts=False,
                ),
                RoleCapability(
                    binding=verifier_binding,
                    sandbox=self.verifier_sandbox,
                    accepts_frozen_base=True,
                    accepts_patch_bytes=True,
                    accepts_result_digest=True,
                    emits_artifacts=True,
                ),
            ),
        )
        observed_toolchain = ToolchainObservation(
            toolchain_id="verify-wheel",
            source_id="lock:verify@https://packages.example/verify.whl",
            content_digest=sha256(b"verify-toolchain-v1"),
            size=len(b"verify-toolchain-v1"),
        )
        receipt = EnvironmentPreparationReceipt(
            environment_preparation_digest=plan.environment_preparation_digest,
            network_cache_requirements=plan.network_cache_requirements,
            toolchain_observations=(observed_toolchain,),
        )
        probes = tuple(CheckCapabilityProbe(
            check_id=check.check_id,
            target=target,
            command=check.command,
            command_input_digest=check.command_input_digest,
            environment_preparation_artifact_digest=receipt.artifact_digest,
            child_environment=check.environment,
            entrypoint_ready=True,
            structured_result=True,
            exit_code=0,
        ) for check in plan.required_checks for target in ProbeTarget)
        capabilities = CapabilitySet(
            runner=runner,
            environment_preparation_receipts=(receipt,),
            check_probes=probes,
            red_first_probes=(),
        )
        context = RunnerContext(
            checkout_identity=sha256(b"verify-checkout"),
            machine_identity=sha256(b"verify-machine"),
            principal_identity=sha256(b"verify-principal"),
            project_config_digest=sha256((root / ".waystone.yml").read_bytes()),
            profile_config_digest=sha256(
                (root / ".waystone" / "profile.yml").read_bytes()),
            runtime_observations=self.observations(),
        )
        proof = record_runner_proof(context, runner)
        with self.supported_filesystem():
            ready = preflight_for_dispatch(
                plan.run_id,
                capabilities=capabilities,
                materialized_toolchains=(MaterializedToolchain(
                    "verify-wheel",
                    "lock:verify@https://packages.example/verify.whl",
                    toolchain_path,
                ),),
                current_runner_context=context,
                reusable_runner_proof=proof,
                start=root,
            )
        return spec, plan, ready

    def prepare(self, *, binary_result: bool = True) -> VerifyFixture:
        root, toolchain = self.project()
        spec, plan, ready = self.make_dispatch_ready(root, toolchain)
        result_worktree = self.base / f"result-{self._project_number}"
        result_branch = f"verify-result-{self._project_number}"
        self.git_bytes(
            root, "worktree", "add", "-q", "-b", result_branch,
            str(result_worktree), spec.base_snapshot.head)
        (result_worktree / "f.txt").write_text("result\n", encoding="utf-8")
        if binary_result:
            (result_worktree / "binary.bin").write_bytes(
                b"binary\x00payload\xff\xfe\n")
            (result_worktree / "nonutf.bin").write_bytes(
                b"non-utf8-content\xff\xfe\n")
        self.git_bytes(result_worktree, "add", "-A")
        self.git_bytes(result_worktree, "commit", "-qm", "worker result")
        result_ref = f"refs/heads/{result_branch}"
        target_ref = f"refs/waystone/integration/fixture-{self._project_number}"
        self.git_bytes(root, "update-ref", target_ref, spec.base_snapshot.head)
        return VerifyFixture(
            root=root,
            result_worktree=result_worktree,
            spec=spec,
            plan=plan,
            ready=ready,
            result_ref=result_ref,
            target_ref=target_ref,
        )

    def create_attempt(self, fixture: VerifyFixture, attempt_id: str) -> None:
        with self.supported_filesystem(), RunStore.open(fixture.root) as store:
            store.create_attempt(
                fixture.spec.run_id, fixture.spec.job_id, attempt_id,
                initial_state="running",
            )

    def check_executor(self, *, requests: list | None = None):
        def execute(request):
            if requests is not None:
                requests.append(request)
            return EngineCheckOutput(
                check_id=request.action.check_id,
                exit_code=request.action.expected_exit_codes[0],
                evidence=tuple(
                    (kind, (
                        f"{kind}:{request.action.check_id}:"
                        f"{request.result.result_digest}\n"
                    ).encode("utf-8"))
                    for kind in request.action.expected_evidence_kinds
                ),
            )

        return execute

    def verifier_executor(
            self, *, blockers=(), failed_criteria=(), mutate_request=False,
            raw_output: object | None = None, returncode: int = 0,
            requests: list | None = None, malformed_nested=False):
        def execute(request):
            if requests is not None:
                requests.append(request)
            if mutate_request:
                (request.review_root / "f.txt").write_bytes(
                    b"verifier mutation must be denied\n")
            output = raw_output
            if raw_output is None:
                output = VerifierOutput(
                    actor=self.verifier,
                    result_digest=request.result.result_digest,
                    criterion_results=tuple(CriterionResult(
                        criterion=criterion,
                        passed=criterion not in failed_criteria,
                        evidence_digests=(sha256(
                            b"criterion:" + criterion.encode("utf-8")),),
                    ) for criterion in request.owner_criteria),
                    blockers=((object(),) if malformed_nested else tuple(blockers)),
                    summary="independent fixture verification",
                )
            return FixtureVerifierResult(
                returncode=returncode,
                output=output,
                stderr=b"" if returncode == 0 else b"fixture failure",
            )

        return execute

    def verifier_adapter(self, fixture: VerifyFixture, *, executor=None):
        return VerifierAdapter(
            binding=fixture.plan.binding_for(Role.VERIFIER).binding,
            sandbox=fixture.plan.verifier_sandbox,
            executor=executor or self.verifier_executor(),
        )

    def verify(
            self, fixture: VerifyFixture, *, attempt_id: str = "attempt-verify",
            action_id: str = "action-verify", executor=None,
            check_executor=None, adapter=None,
            retry_of: str | None = None):
        self.create_attempt(fixture, attempt_id)
        selected_executor = executor or self.verifier_executor()
        with self.supported_filesystem():
            return execute_verifier(
                fixture.spec.run_id,
                attempt_id,
                action_id,
                fixture.root,
                fixture.result_ref,
                self.worker.actor_id,
                self.verifier,
                check_executor or self.check_executor(),
                adapter or self.verifier_adapter(
                    fixture, executor=selected_executor),
                retry_of=retry_of,
                start=fixture.root,
            )

    def decision_input(
            self, evidence, *, actor=None, criteria=None, result_digest=None,
            verifier_reference_id=None, verifier_artifact_digest=None, overrides=()):
        engine_check = evidence.engine_checks
        return DecisionInput(
            actor=actor or self.coordinator,
            outcome=DecisionOutcome.ACCEPT,
            criteria=evidence.criterion_results and tuple(
                item.criterion for item in evidence.criterion_results)
                if criteria is None else tuple(criteria),
            result_digest=result_digest or evidence.result.result_digest,
            verifier_reference_id=(
                verifier_reference_id or evidence.artifact_reference.reference_id),
            verifier_artifact_digest=(
                verifier_artifact_digest or evidence.artifact_reference.digest),
            engine_check_reference_id=engine_check.artifact_reference.reference_id,
            engine_check_artifact_digest=engine_check.artifact_reference.digest,
            blocker_overrides=tuple(overrides),
        )

    def decide(
            self, fixture: VerifyFixture, evidence, *,
            attempt_id: str | None = None, action_id: str = "action-decision",
            decision_input=None, retry_of: str | None = None):
        selected_attempt = evidence.attempt_id if attempt_id is None else attempt_id
        if selected_attempt != evidence.attempt_id:
            self.create_attempt(fixture, selected_attempt)
        with self.supported_filesystem():
            return record_integration_decision(
                fixture.spec.run_id,
                selected_attempt,
                action_id,
                decision_input or self.decision_input(evidence),
                retry_of=retry_of,
                start=fixture.root,
            )

    def verified_decision(self, fixture: VerifyFixture):
        evidence = self.verify(fixture)
        decision = self.decide(fixture, evidence)
        return evidence, decision

    def semantic_reference_count(self, root: Path, prefix: str) -> int:
        with self.supported_filesystem(), RunStore.open(root) as store:
            return store._connection.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM artifacts WHERE reference_id LIKE ?",
                (prefix + "%",),
            ).fetchone()[0]

    def complete_artifact_effect(
            self, fixture: VerifyFixture, attempt_id: str,
            action_id: str, content: bytes) -> None:
        with self.supported_filesystem():
            store = RunStore.open(fixture.root)
            try:
                effects = EffectEngine(store, LeaseManager(store))
                plan = effects.plan_effect(
                    fixture.spec.run_id,
                    fixture.spec.job_id,
                    attempt_id,
                    action_id,
                    ArtifactWriteEffect(content),
                )
                claimed = effects.claim_effect(plan, ttl_seconds=30)
                result = effects.execute_effect(claimed)
            finally:
                store.close()
        self.assertEqual(result.state.value, "completed")

    def commit_from_base(
            self, fixture: VerifyFixture, name: str, content: bytes) -> str:
        blob = self.git_bytes(
            fixture.root, "hash-object", "-w", "--stdin", input_bytes=content,
        ).decode("ascii").strip()
        tree = self.git_bytes(
            fixture.root, "mktree",
            input_bytes=f"100644 blob {blob}\t{name}\n".encode("utf-8"),
        ).decode("ascii").strip()
        return self.git_bytes(
            fixture.root,
            "commit-tree",
            tree,
            "-p",
            fixture.spec.base_snapshot.head,
            input_bytes=f"concurrent {name}\n".encode("utf-8"),
        ).decode("ascii").strip()

    def test_pc16_git_triple_and_verifier_artifact_preserve_exact_binary_bytes(self):
        """PC-16: Git authority binds base, exact patch bytes, and result digest."""
        fixture = self.prepare(binary_result=True)
        expected_patch = self.git_bytes(
            fixture.root,
            "diff", "--binary", "--full-index", "--no-ext-diff", "--no-renames",
            fixture.spec.base_snapshot.head, fixture.result_ref, "--",
        )
        expected_paths = tuple(sorted(set(self.git_bytes(
            fixture.root,
            "diff", "--name-only", "-z", "--no-ext-diff", "--no-renames",
            fixture.spec.base_snapshot.head, fixture.result_ref, "--",
        )[:-1].split(b"\0"))))

        with mock.patch(
                "waystone.runs.verify.git_read_bytes",
                wraps=__import__(
                    "waystone.adapters.git", fromlist=["git_read_bytes"]
                ).git_read_bytes) as git_reader:
            derived = derive_git_result(
                fixture.root, fixture.spec.base_snapshot.head, fixture.result_ref)
            evidence = self.verify(fixture)

        self.assertGreater(git_reader.call_count, 0)
        self.assertEqual(derived.patch_bytes, expected_patch)
        self.assertEqual(derived.changed_files, expected_paths)
        self.assertIn(b"nonutf.bin", derived.changed_files)
        self.assertIn(b"\xff\xfe", derived.patch_bytes)
        self.assertEqual(
            self.git_bytes(fixture.root, "show", f"{fixture.result_ref}:nonutf.bin"),
            b"non-utf8-content\xff\xfe\n",
        )
        self.assertEqual(evidence.result, derived)
        self.assertEqual(evidence.result.base_oid, fixture.spec.base_snapshot.head)
        self.assertRegex(evidence.result.result_digest, r"^sha256:[0-9a-f]{64}$")

        payload = json.loads(ArtifactStore(fixture.root).read_reference(
            evidence.artifact_reference))
        self.assertEqual(
            base64.b64decode(payload["result"]["patch_bytes"], validate=True),
            expected_patch,
        )
        self.assertEqual(
            tuple(base64.b64decode(item, validate=True)
                  for item in payload["result"]["changed_files"]),
            expected_paths,
        )

    def test_adr0012_pc20_engine_actions_run_exactly_and_bind_result_evidence(self):
        """ADR-0012/PC-20: engine checks execute frozen actions on the exact result."""
        fixture = self.prepare()
        check_requests: list = []
        verifier_requests: list = []
        isolated: dict[str, object] = {}
        check_fixture = self.check_executor(requests=check_requests)
        verifier_fixture = self.verifier_executor(requests=verifier_requests)

        def check_executor(request):
            isolated["execution_root"] = request.execution_root
            isolated["engine_result"] = (request.execution_root / "f.txt").read_bytes()
            (request.execution_root / "engine-only.txt").write_bytes(b"discarded\n")
            return check_fixture(request)

        def verifier_executor(request):
            isolated["review_root"] = request.review_root
            isolated["verifier_result"] = (request.review_root / "f.txt").read_bytes()
            isolated["review_root_mode"] = stat.S_IMODE(
                request.review_root.lstat().st_mode)
            isolated["review_file_mode"] = stat.S_IMODE(
                (request.review_root / "f.txt").lstat().st_mode)
            isolated["engine_write_visible"] = (
                request.review_root / "engine-only.txt").exists()
            return verifier_fixture(request)

        evidence = self.verify(
            fixture,
            check_executor=check_executor,
            executor=verifier_executor,
        )

        self.assertEqual(len(check_requests), len(fixture.ready.engine_actions))
        self.assertEqual(len(verifier_requests), 1)
        check_request = check_requests[0]
        verifier_request = verifier_requests[0]
        self.assertEqual(check_request.action, fixture.ready.engine_actions[0])
        self.assertEqual(
            check_request.base_snapshot_digest, fixture.spec.base_snapshot.digest)
        self.assertEqual(check_request.result, evidence.result)
        self.assertEqual(isolated["engine_result"], b"result\n")
        self.assertEqual(isolated["verifier_result"], b"result\n")
        self.assertNotEqual(isolated["execution_root"], isolated["review_root"])
        self.assertEqual(isolated["review_root_mode"], 0o555)
        self.assertEqual(isolated["review_file_mode"], 0o444)
        self.assertFalse(isolated["engine_write_visible"])
        self.assertEqual(verifier_request.base_snapshot, check_request.base_snapshot)
        self.assertEqual(
            verifier_request.base_snapshot_digest, check_request.base_snapshot_digest)
        self.assertEqual(verifier_request.result, evidence.result)
        self.assertEqual(
            verifier_request.engine_check_results, evidence.engine_checks.results)
        self.assertEqual(
            verifier_request.verifier_binding,
            fixture.plan.binding_for(Role.VERIFIER).binding,
        )
        self.assertEqual(
            verifier_request.verifier_sandbox, fixture.plan.verifier_sandbox)
        with self.supported_filesystem(), RunStore.open(fixture.root) as store:
            preflight_reference = store.get_artifact_reference(
                f"verification-preflight:{fixture.spec.run_id}")
        preflight_payload = json.loads(
            ArtifactStore(fixture.root).read_reference(preflight_reference))
        self.assertEqual(
            verifier_request.verifier_capability_digest,
            preflight_payload["verifier_capability_digest"],
        )
        engine_check = evidence.engine_checks
        engine_payload = json.loads(ArtifactStore(fixture.root).read_reference(
            engine_check.artifact_reference))
        self.assertEqual(
            engine_payload["results"][0]["check_id"],
            check_request.action.check_id,
        )
        self.assertEqual(
            engine_payload["base_snapshot_digest"], fixture.spec.base_snapshot.digest)
        self.assertEqual(engine_payload["result_digest"], evidence.result.result_digest)
        self.assertEqual(engine_payload["results"][0]["exit_code"], 0)

    def test_adr0012_pc20_frozen_verifier_adapter_substitution_is_refused(self):
        """ADR-0012/PC-20: frozen verifier binding and sandbox reject substitution."""
        for substitution in ("binding", "sandbox"):
            with self.subTest(substitution=substitution):
                fixture = self.prepare()
                frozen_binding = fixture.plan.binding_for(Role.VERIFIER).binding
                binding = frozen_binding
                sandbox = fixture.plan.verifier_sandbox
                if substitution == "binding":
                    binding = RoleBinding(
                        Role.VERIFIER,
                        frozen_binding.execution_category,
                        frozen_binding.backend + ":substitute",
                    )
                else:
                    sandbox = SandboxContract(
                        "isolated-worktree-write", "process-exec", "network-denied")
                check_requests: list = []
                before = fingerprint_worktree(fixture.root)

                with self.assertRaises(VerifierBindingRefusal):
                    self.verify(
                        fixture,
                        check_executor=self.check_executor(requests=check_requests),
                        adapter=VerifierAdapter(
                            binding=binding,
                            sandbox=sandbox,
                            executor=self.verifier_executor(),
                        ),
                    )

                self.assertEqual(check_requests, [])
                self.assertEqual(fingerprint_worktree(fixture.root), before)
                self.assertEqual(
                    self.semantic_reference_count(
                        fixture.root, "verifier-evidence:"), 0)

    def test_pc20_verifier_is_read_only_and_separate_from_worker_and_integrator(self):
        """PC-20: verifier execution is read-only and actor-separated."""
        fixture = self.prepare()
        before = fingerprint_worktree(fixture.root)

        evidence = self.verify(fixture)

        self.assertEqual(fingerprint_worktree(fixture.root), before)
        self.assertEqual(evidence.actor, self.verifier)
        self.assertIs(evidence.actor.role, Role.VERIFIER)
        self.assertNotEqual(evidence.actor.actor_id, self.worker.actor_id)
        self.assertNotEqual(evidence.actor.actor_id, self.coordinator.actor_id)
        self.assertEqual(evidence.worker_actor_id, self.worker.actor_id)

        self.create_attempt(fixture, "attempt-self-verifier")
        with self.supported_filesystem(), self.assertRaises(VerifierActorRefusal):
            execute_verifier(
                fixture.spec.run_id,
                "attempt-self-verifier",
                "action-self-verifier",
                fixture.root,
                fixture.result_ref,
                self.worker.actor_id,
                ActorIdentity(self.worker.actor_id, Role.VERIFIER),
                self.check_executor(),
                self.verifier_adapter(fixture),
                start=fixture.root,
            )

    def test_pc20_empty_invalid_and_failed_output_publish_no_semantic_evidence(self):
        """PC-20: empty, malformed, and failed verifier output emits no evidence."""
        def timeout(_request):
            raise TimeoutError("fixture verifier timed out")

        cases = (
            ("empty", self.verifier_executor(raw_output=b""), InvalidVerifierOutput),
            ("malformed", self.verifier_executor(raw_output={}), InvalidVerifierOutput),
            ("malformed-nested", self.verifier_executor(malformed_nested=True),
             InvalidVerifierOutput),
            ("failed", self.verifier_executor(raw_output=b"ignored", returncode=7),
             VerifierExecutionFailed),
            ("timeout", timeout, VerifierExecutionFailed),
        )
        for label, executor, expected_error in cases:
            with self.subTest(label=label):
                fixture = self.prepare()
                self.assertEqual(
                    self.semantic_reference_count(
                        fixture.root, "verifier-evidence:"), 0)
                with self.assertRaises(expected_error):
                    self.verify(
                        fixture,
                        attempt_id=f"attempt-{label}",
                        action_id=f"action-{label}",
                        executor=executor,
                    )
                self.assertEqual(
                    self.semantic_reference_count(
                        fixture.root, "verifier-evidence:"), 0)

    def test_pc20_valid_output_is_canonical_before_runner_receipt(self):
        """PC-20: valid unordered output stays consumable through its runner receipt."""
        fixture = self.prepare()

        def unordered(request):
            criteria = tuple(reversed(tuple(CriterionResult(
                criterion=criterion,
                passed=True,
                evidence_digests=tuple(reversed((
                    sha256(f"z:{criterion}".encode("utf-8")),
                    sha256(f"a:{criterion}".encode("utf-8")),
                ))),
            ) for criterion in request.owner_criteria)))
            return FixtureVerifierResult(
                returncode=0,
                output=VerifierOutput(
                    actor=self.verifier,
                    result_digest=request.result.result_digest,
                    criterion_results=criteria,
                    blockers=(
                        VerifierBlocker("z-blocker", "last blocker"),
                        VerifierBlocker("a-blocker", "first blocker"),
                    ),
                    summary="unordered but valid verifier output",
                ),
            )

        evidence = self.verify(fixture, executor=unordered)
        self.assertEqual(
            tuple(item.criterion for item in evidence.criterion_results),
            fixture.spec.job_input.acceptance_criteria,
        )
        self.assertEqual(
            tuple(item.blocker_id for item in evidence.blockers),
            ("a-blocker", "z-blocker"),
        )
        overrides = tuple(BlockerOverride(
            blocker.blocker_id,
            evidence.engine_checks.results[0].check_id,
            evidence.engine_checks.results[0].evidence_digests[0][1],
        ) for blocker in evidence.blockers)
        decision = self.decide(
            fixture,
            evidence,
            decision_input=self.decision_input(evidence, overrides=overrides),
        )
        self.assertIs(decision.outcome, DecisionOutcome.ACCEPT)

    def test_pc20_mutating_verifier_is_refused_without_evidence(self):
        """PC-20: immutable verifier input mutation is typed and authority-safe."""
        fixture = self.prepare()
        before = fingerprint_worktree(fixture.root)
        with self.supported_filesystem():
            frozen_head = read_base_snapshot(
                fixture.spec.run_id, start=fixture.root).head

        with self.assertRaises(VerifierMutationRefusal):
            self.verify(
                fixture,
                executor=self.verifier_executor(mutate_request=True),
            )

        self.assertEqual(fingerprint_worktree(fixture.root), before)
        with self.supported_filesystem():
            self.assertEqual(
                read_base_snapshot(fixture.spec.run_id, start=fixture.root).head,
                frozen_head,
            )
        self.assertEqual(
            self.semantic_reference_count(fixture.root, "verifier-evidence:"), 0)

    def test_pc21_decision_refusals_distinguish_every_binding_violation(self):
        """PC-21: criteria, digest, actor, and override violations are distinct."""
        cases = (
            ("missing", MissingCriterionRefusal),
            ("extra", ExtraCriterionRefusal),
            ("wrong-digest", DecisionResultDigestRefusal),
            ("worker-self-acceptance", DecisionActorRefusal),
            ("unsupported-blocker-override", BlockerOverrideRefusal),
        )

        for label, expected_error in cases:
            fixture = self.prepare()
            evidence = self.verify(fixture)
            criteria = tuple(item.criterion for item in evidence.criterion_results)
            if label == "missing":
                decision_input = self.decision_input(
                    evidence, criteria=criteria[:-1])
            elif label == "extra":
                decision_input = self.decision_input(
                    evidence, criteria=criteria + ("owner did not ask",))
            elif label == "wrong-digest":
                decision_input = self.decision_input(
                    evidence, result_digest=sha256(b"wrong-result"))
            elif label == "worker-self-acceptance":
                decision_input = self.decision_input(
                    evidence, actor=ActorIdentity(
                        self.worker.actor_id, Role.COORDINATOR))
            else:
                decision_input = self.decision_input(
                    evidence,
                    overrides=(BlockerOverride(
                        "not-reported",
                        evidence.engine_checks.results[0].check_id,
                        sha256(b"missing-override-proof"),
                    ),),
                )
            with self.subTest(label=label), self.assertRaises(expected_error) as raised:
                self.decide(
                    fixture,
                    evidence,
                    action_id=f"action-decision-{label}",
                    decision_input=decision_input,
                )
            self.assertNotEqual(raised.exception.code, "run_verify_error")
            self.assertEqual(
                self.semantic_reference_count(
                    fixture.root, "integration-decision:"), 0)

    def test_pc21_blocker_override_requires_grounded_engine_check_mapping(self):
        """PC-21: blocker override maps to one frozen check and its stored evidence."""
        for grounded in (False, True):
            with self.subTest(grounded=grounded):
                fixture = self.prepare()
                blocker = VerifierBlocker("blocker-1", "independent concern")
                evidence = self.verify(
                    fixture,
                    executor=self.verifier_executor(blockers=(blocker,)),
                )
                engine_check = evidence.engine_checks.results[0]
                _kind, evidence_digest = engine_check.evidence_digests[0]
                override = BlockerOverride(
                    blocker.blocker_id,
                    engine_check.check_id if grounded else "unbound-check",
                    evidence_digest,
                )
                if grounded:
                    decision = self.decide(
                        fixture,
                        evidence,
                        action_id="action-grounded-override",
                        decision_input=self.decision_input(
                            evidence, overrides=(override,)),
                    )
                    self.assertIs(decision.outcome, DecisionOutcome.ACCEPT)
                    self.assertEqual(decision.blocker_overrides, (override,))
                else:
                    with self.assertRaises(BlockerOverrideRefusal):
                        self.decide(
                            fixture,
                            evidence,
                            action_id="action-ungrounded-override",
                            decision_input=self.decision_input(
                                evidence, overrides=(override,)),
                        )
                    self.assertEqual(
                        self.semantic_reference_count(
                            fixture.root, "integration-decision:"), 0)

    def test_pc21_decision_lineage_skips_unrelated_binary_artifact_write(self):
        """PC-21: unrelated binary artifact effects cannot poison decision lineage."""
        fixture = self.prepare()
        evidence = self.verify(fixture)
        for index, content in enumerate((
                b"\x00\xffunrelated\xfe",
                b"ordinary UTF-8 but not JSON",
                b'{"schema":"another-artifact"}',
        )):
            self.complete_artifact_effect(
                fixture,
                evidence.attempt_id,
                f"action-unrelated-artifact-{index}",
                content,
            )

        decision = self.decide(
            fixture, evidence, action_id="action-decision-after-unrelated")

        self.assertIs(decision.outcome, DecisionOutcome.ACCEPT)
        self.assertEqual(
            self.semantic_reference_count(
                fixture.root, "integration-decision:"), 1)

    def test_pc21_inconsistent_decision_intent_cannot_forge_retry_lineage(self):
        """PC-21: an internally inconsistent intent creates no decision retry authority."""
        fixture = self.prepare()
        evidence = self.verify(fixture)
        normal = self.decision_input(evidence)
        forged = DecisionInput(
            actor=normal.actor,
            outcome=normal.outcome,
            criteria=normal.criteria,
            result_digest=normal.result_digest,
            verifier_reference_id="verifier-evidence:different-result",
            verifier_artifact_digest=normal.verifier_artifact_digest,
            engine_check_reference_id=normal.engine_check_reference_id,
            engine_check_artifact_digest=normal.engine_check_artifact_digest,
            blocker_overrides=normal.blocker_overrides,
        )
        forged_action = "action-forged-decision-intent"
        forged_payload = verify_module._decision_intent_payload(
            spec=fixture.spec,
            attempt_id=evidence.attempt_id,
            action_id=forged_action,
            decision=forged,
        )
        forged_payload["decision_lineage_key"] = (
            verify_module._decision_lineage_key(fixture.spec, normal))
        self.complete_artifact_effect(
            fixture,
            evidence.attempt_id,
            forged_action,
            verify_module._canonical_json(forged_payload),
        )

        with self.supported_filesystem(), self.assertRaises(EvidenceBindingRefusal):
            record_integration_decision(
                fixture.spec.run_id,
                evidence.attempt_id,
                "action-decision-after-forged-intent",
                normal,
                start=fixture.root,
            )
        retry_attempt = "attempt-forged-intent-retry"
        self.create_attempt(fixture, retry_attempt)
        with self.supported_filesystem(), self.assertRaises(EvidenceBindingRefusal):
            record_integration_decision(
                fixture.spec.run_id,
                retry_attempt,
                "action-decision-forged-intent-retry",
                normal,
                retry_of=forged_action,
                start=fixture.root,
            )
        self.assertEqual(
            self.semantic_reference_count(
                fixture.root, "integration-decision:"), 0)

    def test_pc21_concurrent_decisions_serialize_one_lineage(self):
        """PC-21: concurrent coordinator decisions publish one lineage only."""
        fixture = self.prepare()
        evidence = self.verify(fixture)
        attempts = ("attempt-decision-race-a", "attempt-decision-race-b")
        actions = ("action-decision-race-a", "action-decision-race-b")
        for attempt_id in attempts:
            self.create_attempt(fixture, attempt_id)
        barrier = threading.Barrier(2)
        active_lock = threading.Lock()
        active = 0
        maximum_active = 0
        original = verify_module._record_integration_decision_locked

        def tracked_locked(*args, **kwargs):
            nonlocal active, maximum_active
            with active_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.1)
                return original(*args, **kwargs)
            finally:
                with active_lock:
                    active -= 1

        def decide(attempt_id, action_id):
            barrier.wait(timeout=5)
            return record_integration_decision(
                fixture.spec.run_id,
                attempt_id,
                action_id,
                self.decision_input(evidence),
                start=fixture.root,
            )

        outcomes: list[object] = []
        with self.supported_filesystem(), mock.patch.object(
                verify_module,
                "_record_integration_decision_locked",
                side_effect=tracked_locked,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(executor.submit(decide, *item)
                            for item in zip(attempts, actions))
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10))
                except Exception as error:  # asserted below by exact type
                    outcomes.append(error)

        self.assertEqual(maximum_active, 1)
        self.assertEqual(sum(isinstance(item, EffectRetryRefused)
                             for item in outcomes), 1)
        self.assertEqual(sum(not isinstance(item, Exception)
                             for item in outcomes), 1)
        self.assertEqual(
            self.semantic_reference_count(
                fixture.root, "integration-decision:"), 1)

    def test_pc20_pc22_producer_plans_bind_exact_verification_and_decision_inputs(self):
        """PC-20/PC-22: semantic evidence rederives exact producer effect inputs."""
        fixture = self.prepare()
        evidence = self.verify(fixture)
        reversed_criteria = tuple(reversed(tuple(
            item.criterion for item in evidence.criterion_results)))
        decision = self.decide(
            fixture,
            evidence,
            action_id="action-decision-producer-binding",
            decision_input=self.decision_input(
                evidence, criteria=reversed_criteria),
        )
        with self.supported_filesystem(), RunStore.open(fixture.root) as store:
            runner_plan = store.get_artifact_reference(
                f"effect-plan:{evidence.action_id}")
            runner_receipt = store.get_artifact_reference(
                "effect-observation:"
                f"{evidence.action_id}:"
                f"{evidence.runner_observation_digest.split(':', 1)[1]}")
            decision_plan = store.get_artifact_reference(
                f"effect-plan:{decision.action_id}")
        runner_payload = json.loads(
            ArtifactStore(fixture.root).read_reference(runner_plan))
        expected_invocation = verify_module._verification_invocation_digest(
            spec=fixture.spec,
            plan=fixture.plan,
            dispatch=fixture.ready,
            actor=self.verifier,
            worker_actor_id=self.worker.actor_id,
            result=evidence.result,
            verifier_capability=verify_module._verifier_capability(fixture.plan),
        )
        self.assertEqual(
            runner_payload["spec"]["invocation_digest"], expected_invocation)
        receipt_payload = json.loads(
            ArtifactStore(fixture.root).read_reference(runner_receipt))
        self.assertEqual(
            receipt_payload["observed_digest"],
            evidence.runner_observation_digest,
        )
        self.assertEqual(
            receipt_payload["evidence"]["marker"]["stdout_artifact_digest"],
            evidence.runner_stdout_digest,
        )
        self.assertEqual(
            receipt_payload["evidence"]["marker"]["stderr_artifact_digest"],
            evidence.runner_stderr_digest,
        )
        expected_transcript = verify_module._verification_transcript(
            FixtureVerifierResult(
                returncode=0,
                output=VerifierOutput(
                    evidence.actor,
                    evidence.result.result_digest,
                    evidence.criterion_results,
                    evidence.blockers,
                    evidence.summary,
                ),
            ),
            evidence.engine_checks.results,
        )
        self.assertEqual(
            ArtifactStore(fixture.root).read(evidence.runner_stdout_digest),
            expected_transcript,
        )
        decision_effect = json.loads(
            ArtifactStore(fixture.root).read_reference(decision_plan))
        intent_bytes = base64.b64decode(
            decision_effect["spec"]["content_base64"], validate=True)
        intent = json.loads(intent_bytes)
        semantic = json.loads(ArtifactStore(fixture.root).read_reference(
            decision.artifact_reference))
        self.assertEqual(sha256(intent_bytes), decision.producer_effect_digest)
        self.assertEqual(
            decision_effect["spec"]["content_digest"],
            decision.producer_effect_digest,
        )
        self.assertEqual(
            semantic["producer_effect_digest"], decision.producer_effect_digest)
        self.assertEqual(intent["criteria"], list(reversed_criteria))
        self.assertEqual(semantic["criteria"], list(
            fixture.spec.job_input.acceptance_criteria))

        target_before = self.git_oid(fixture.root, fixture.target_ref)
        with mock.patch.object(
                verify_module,
                "_verification_invocation_digest",
                return_value=sha256(b"forged invocation"),
        ), self.supported_filesystem(), self.assertRaises(ApplyBindingRefusal):
            apply_integration_decision(
                fixture.spec.run_id,
                evidence.attempt_id,
                "action-apply-forged-producer",
                fixture.root,
                fixture.result_ref,
                fixture.target_ref,
                evidence.artifact_reference.reference_id,
                decision.artifact_reference.reference_id,
                start=fixture.root,
            )
        self.assertEqual(self.git_oid(fixture.root, fixture.target_ref), target_before)

    def test_pc16_post_decision_result_ref_tamper_is_refused(self):
        """PC-16: replacing the approved result ref after verdict is refused."""
        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)
        base_target = self.git_oid(fixture.root, fixture.target_ref)
        tampered = self.commit_from_base(fixture, "tampered.txt", b"replacement\x00\xff")
        self.git_bytes(
            fixture.root, "update-ref", fixture.result_ref, tampered,
            evidence.result.result_oid)

        with self.supported_filesystem(), self.assertRaises(ApplyDriftRefusal):
            apply_integration_decision(
                fixture.spec.run_id,
                evidence.attempt_id,
                "action-apply-tampered",
                fixture.root,
                fixture.result_ref,
                fixture.target_ref,
                evidence.artifact_reference.reference_id,
                decision.artifact_reference.reference_id,
                start=fixture.root,
            )

        self.assertEqual(self.git_oid(fixture.root, fixture.target_ref), base_target)

    def test_pc17_apply_preserves_dirty_staged_untracked_and_ignored_user_bytes(self):
        """PC-17: apply preserves unrelated live tree/index dirt byte-for-byte."""
        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)
        (fixture.root / "tracked.txt").write_bytes(b"unstaged-user\x00\xff")
        (fixture.root / "staged.txt").write_bytes(b"staged-user\x00\xfe")
        self.git_bytes(fixture.root, "add", "staged.txt")
        (fixture.root / "untracked.bin").write_bytes(b"untracked-user\x00\xfd")
        (fixture.root / "ignored.bin").write_bytes(b"ignored-user\x00\xfc")
        before = {
            "fingerprint": fingerprint_worktree(fixture.root),
            "status": self.raw_status(fixture.root),
            "index": self.index_bytes(fixture.root),
            "head": self.git_oid(fixture.root, "HEAD"),
            "tracked": (fixture.root / "tracked.txt").read_bytes(),
            "staged": (fixture.root / "staged.txt").read_bytes(),
            "untracked": (fixture.root / "untracked.bin").read_bytes(),
            "ignored": (fixture.root / "ignored.bin").read_bytes(),
        }

        with self.supported_filesystem():
            applied = apply_integration_decision(
                fixture.spec.run_id,
                evidence.attempt_id,
                "action-apply-dirty",
                fixture.root,
                fixture.result_ref,
                fixture.target_ref,
                evidence.artifact_reference.reference_id,
                decision.artifact_reference.reference_id,
                start=fixture.root,
            )

        self.assertEqual(applied.result_oid, evidence.result.result_oid)
        self.assertEqual(
            self.git_oid(fixture.root, fixture.target_ref), evidence.result.result_oid)
        after = {
            "fingerprint": fingerprint_worktree(fixture.root),
            "status": self.raw_status(fixture.root),
            "index": self.index_bytes(fixture.root),
            "head": self.git_oid(fixture.root, "HEAD"),
            "tracked": (fixture.root / "tracked.txt").read_bytes(),
            "staged": (fixture.root / "staged.txt").read_bytes(),
            "untracked": (fixture.root / "untracked.bin").read_bytes(),
            "ignored": (fixture.root / "ignored.bin").read_bytes(),
        }
        self.assertEqual(after, before)

    def test_pc17_preexisting_integration_drift_is_atomic_no_write(self):
        """PC-17: target drift refuses atomically without changing live user state."""
        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)
        concurrent = self.commit_from_base(fixture, "concurrent.txt", b"other result\n")
        self.git_bytes(
            fixture.root, "update-ref", fixture.target_ref, concurrent,
            fixture.spec.base_snapshot.head)
        before = fingerprint_worktree(fixture.root)

        with self.supported_filesystem(), self.assertRaises(
                ApplyConcurrentDriftRefusal):
            apply_integration_decision(
                fixture.spec.run_id,
                evidence.attempt_id,
                "action-apply-preexisting-drift",
                fixture.root,
                fixture.result_ref,
                fixture.target_ref,
                evidence.artifact_reference.reference_id,
                decision.artifact_reference.reference_id,
                start=fixture.root,
            )

        self.assertEqual(self.git_oid(fixture.root, fixture.target_ref), concurrent)
        self.assertEqual(fingerprint_worktree(fixture.root), before)

    def test_pc17_apply_refuses_public_or_linked_worktree_target_without_write(self):
        """PC-17: apply never moves public or linked-worktree refs behind user state."""
        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)
        public_ref = f"refs/heads/public-integration-{self._project_number}"
        self.git_bytes(
            fixture.root, "update-ref", public_ref, fixture.spec.base_snapshot.head)
        main_before = fingerprint_worktree(fixture.root)
        public_before = self.git_oid(fixture.root, public_ref)

        with self.supported_filesystem(), self.assertRaises(ApplyBindingRefusal):
            apply_integration_decision(
                fixture.spec.run_id,
                evidence.attempt_id,
                "action-apply-public-ref",
                fixture.root,
                fixture.result_ref,
                public_ref,
                evidence.artifact_reference.reference_id,
                decision.artifact_reference.reference_id,
                start=fixture.root,
            )
        self.assertEqual(self.git_oid(fixture.root, public_ref), public_before)
        self.assertEqual(fingerprint_worktree(fixture.root), main_before)

        linked = self.base / f"linked-target-{self._project_number}"
        self.git_bytes(
            fixture.root,
            "worktree",
            "add",
            "-q",
            "--detach",
            str(linked),
            fixture.spec.base_snapshot.head,
        )
        self.git_bytes(linked, "symbolic-ref", "HEAD", fixture.target_ref)
        linked_before = {
            "head": self.git_oid(linked, "HEAD"),
            "index": self.index_bytes(linked),
            "status": self.raw_status(linked),
            "tracked": (linked / "tracked.txt").read_bytes(),
        }
        private_before = self.git_oid(fixture.root, fixture.target_ref)
        main_before = fingerprint_worktree(fixture.root)

        with self.supported_filesystem(), self.assertRaises(
                CheckedOutTargetRefRefusal):
            apply_integration_decision(
                fixture.spec.run_id,
                evidence.attempt_id,
                "action-apply-linked-ref",
                fixture.root,
                fixture.result_ref,
                fixture.target_ref,
                evidence.artifact_reference.reference_id,
                decision.artifact_reference.reference_id,
                start=fixture.root,
            )

        self.assertEqual(
            self.git_oid(fixture.root, fixture.target_ref), private_before)
        self.assertEqual(fingerprint_worktree(fixture.root), main_before)
        self.assertEqual({
            "head": self.git_oid(linked, "HEAD"),
            "index": self.index_bytes(linked),
            "status": self.raw_status(linked),
            "tracked": (linked / "tracked.txt").read_bytes(),
        }, linked_before)

    def test_pc17_pc22_post_cas_user_edit_does_not_reclassify_success(self):
        """PC-17/PC-22: a user edit after the ref CAS preserves completed success."""
        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)
        original = EffectEngine.execute_effect

        def execute_then_user_edit(engine, claimed):
            result = original(engine, claimed)
            if claimed.plan.kind.value == "patch-integration":
                (fixture.root / "late-user.txt").write_bytes(b"late user edit\n")
            return result

        with mock.patch.object(
                EffectEngine, "execute_effect", autospec=True,
                side_effect=execute_then_user_edit,
        ), self.supported_filesystem():
            applied = apply_integration_decision(
                fixture.spec.run_id,
                evidence.attempt_id,
                "action-apply-late-user-edit",
                fixture.root,
                fixture.result_ref,
                fixture.target_ref,
                evidence.artifact_reference.reference_id,
                decision.artifact_reference.reference_id,
                start=fixture.root,
            )

        self.assertEqual(applied.result_oid, evidence.result.result_oid)
        self.assertEqual(
            self.git_oid(fixture.root, fixture.target_ref), evidence.result.result_oid)
        self.assertEqual(
            (fixture.root / "late-user.txt").read_bytes(), b"late user edit\n")

    def test_pc17_apply_refuses_symbolic_private_ref_escape(self):
        """PC-17: a private symref cannot escape into a checked-out user branch."""
        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)
        user_ref = self.git_bytes(
            fixture.root, "symbolic-ref", "HEAD").decode("ascii").strip()
        user_before = self.git_oid(fixture.root, user_ref)
        tree_before = fingerprint_worktree(fixture.root)
        self.git_bytes(
            fixture.root, "symbolic-ref", fixture.target_ref, user_ref)

        with self.supported_filesystem(), self.assertRaises(ApplyBindingRefusal):
            apply_integration_decision(
                fixture.spec.run_id,
                evidence.attempt_id,
                "action-apply-symbolic-private-ref",
                fixture.root,
                fixture.result_ref,
                fixture.target_ref,
                evidence.artifact_reference.reference_id,
                decision.artifact_reference.reference_id,
                start=fixture.root,
            )

        self.assertEqual(self.git_oid(fixture.root, user_ref), user_before)
        self.assertEqual(fingerprint_worktree(fixture.root), tree_before)

    def test_pc17_pc22_linked_worktree_race_is_rechecked_before_cas(self):
        """PC-17/PC-22: execution-time linked-worktree drift refuses before CAS."""
        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)
        linked = self.base / f"linked-race-{self._project_number}"
        self.git_bytes(
            fixture.root,
            "worktree",
            "add",
            "-q",
            "--detach",
            str(linked),
            fixture.spec.base_snapshot.head,
        )
        linked_before = {
            "head": self.git_oid(linked, "HEAD"),
            "index": self.index_bytes(linked),
            "status": self.raw_status(linked),
            "tracked": (linked / "tracked.txt").read_bytes(),
        }
        target_before = self.git_oid(fixture.root, fixture.target_ref)
        main_before = fingerprint_worktree(fixture.root)

        def race() -> None:
            self.git_bytes(linked, "symbolic-ref", "HEAD", fixture.target_ref)

        with self.supported_filesystem(), self.assertRaises(
                CheckedOutTargetRefRefusal):
            apply_integration_decision(
                fixture.spec.run_id,
                evidence.attempt_id,
                "action-apply-linked-race",
                fixture.root,
                fixture.result_ref,
                fixture.target_ref,
                evidence.artifact_reference.reference_id,
                decision.artifact_reference.reference_id,
                race_hook=race,
                start=fixture.root,
            )

        self.assertEqual(
            self.git_oid(fixture.root, fixture.target_ref), target_before)
        self.assertEqual(fingerprint_worktree(fixture.root), main_before)
        self.assertEqual(
            self.git_bytes(linked, "symbolic-ref", "HEAD").decode("ascii").strip(),
            fixture.target_ref,
        )
        self.assertEqual({
            "head": self.git_oid(linked, "HEAD"),
            "index": self.index_bytes(linked),
            "status": self.raw_status(linked),
            "tracked": (linked / "tracked.txt").read_bytes(),
        }, linked_before)

    def test_pc22_apply_reloads_contract_decision_and_verifier_artifact_digests(self):
        """PC-22: apply reloads and rehashes every approval-time authority artifact."""
        for authority in (
                "run-spec", "base-snapshot", "verification-plan", "preflight",
                "runner-plan", "runner-receipt", "runner-stdout", "engine-check",
                "verifier", "decision-plan", "decision"):
            with self.subTest(authority=authority):
                fixture = self.prepare()
                evidence, decision = self.verified_decision(fixture)
                tamper_digest = None
                with self.supported_filesystem(), RunStore.open(fixture.root) as store:
                    if authority == "run-spec":
                        reference = store.get_artifact_reference(
                            f"run-spec:{fixture.spec.run_id}:1")
                    elif authority == "base-snapshot":
                        reference = store.get_artifact_reference(
                            f"base-snapshot:{fixture.spec.run_id}")
                    elif authority == "verification-plan":
                        reference = store.get_artifact_reference(
                            f"verification-plan:{fixture.spec.run_id}")
                    elif authority == "preflight":
                        reference = store.get_artifact_reference(
                            f"verification-preflight:{fixture.spec.run_id}")
                    elif authority == "runner-plan":
                        reference = store.get_artifact_reference(
                            f"effect-plan:{evidence.action_id}")
                    elif authority == "runner-receipt":
                        reference = store.get_artifact_reference(
                            "effect-observation:"
                            f"{evidence.action_id}:"
                            f"{evidence.runner_observation_digest.split(':', 1)[1]}")
                    elif authority == "runner-stdout":
                        tamper_digest = evidence.runner_stdout_digest
                    elif authority == "engine-check":
                        reference = evidence.engine_checks.artifact_reference
                    elif authority == "verifier":
                        reference = evidence.artifact_reference
                    elif authority == "decision-plan":
                        reference = store.get_artifact_reference(
                            f"effect-plan:{decision.action_id}")
                    else:
                        reference = decision.artifact_reference
                digest = reference.digest if tamper_digest is None else tamper_digest
                tamper_path = ArtifactStore(fixture.root).path_for(digest)
                # Deliberate out-of-band tamper simulation: published artifacts
                # are immutable 0400 (ADR-0013), so open the mode explicitly,
                # corrupt the bytes, and restore the immutable mode.
                os.chmod(tamper_path, 0o600)
                tamper_path.write_bytes(b"tampered approval authority")
                os.chmod(tamper_path, 0o400)
                target_before = self.git_oid(fixture.root, fixture.target_ref)
                tree_before = fingerprint_worktree(fixture.root)

                with self.supported_filesystem(), self.assertRaises(ApplyBindingRefusal):
                    apply_integration_decision(
                        fixture.spec.run_id,
                        evidence.attempt_id,
                        f"action-apply-{authority}-tamper",
                        fixture.root,
                        fixture.result_ref,
                        fixture.target_ref,
                        evidence.artifact_reference.reference_id,
                        decision.artifact_reference.reference_id,
                        start=fixture.root,
                    )

                self.assertEqual(
                    self.git_oid(fixture.root, fixture.target_ref), target_before)
                self.assertEqual(fingerprint_worktree(fixture.root), tree_before)

    def test_pc22_apply_refuses_attempt_outside_decision_lineage(self):
        """PC-22: apply cannot substitute an attempt outside the accepted decision."""
        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)
        unrelated_attempt = "attempt-unrelated-apply"
        action_id = "action-apply-unrelated-attempt"
        self.create_attempt(fixture, unrelated_attempt)
        target_before = self.git_oid(fixture.root, fixture.target_ref)
        tree_before = fingerprint_worktree(fixture.root)

        with self.supported_filesystem(), self.assertRaises(ApplyBindingRefusal):
            apply_integration_decision(
                fixture.spec.run_id,
                unrelated_attempt,
                action_id,
                fixture.root,
                fixture.result_ref,
                fixture.target_ref,
                evidence.artifact_reference.reference_id,
                decision.artifact_reference.reference_id,
                start=fixture.root,
            )

        with self.supported_filesystem(), RunStore.open(fixture.root) as store:
            action_count = store._connection.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()[0]
        self.assertEqual(action_count, 0)
        self.assertEqual(self.git_oid(fixture.root, fixture.target_ref), target_before)
        self.assertEqual(fingerprint_worktree(fixture.root), tree_before)

    def test_pc22_patch_reconcile_rehashes_durable_approval_bundle(self):
        """WS-GPT-602: crash reconcile cannot apply tampered approval bytes."""
        approval_fields = {
            "run_spec_digest": lambda fixture, evidence, decision: (
                fixture.spec.run_spec_digest),
            "verification_plan_digest": lambda fixture, evidence, decision: (
                fixture.plan.verification_plan_digest),
            "verifier_evidence_digest": lambda fixture, evidence, decision: (
                evidence.artifact_reference.digest),
            "integration_decision_digest": lambda fixture, evidence, decision: (
                decision.artifact_reference.digest),
        }

        def crash_after_plan(fixture, evidence, decision, action_id):
            with mock.patch.object(
                    EffectEngine, "execute_effect",
                    side_effect=RuntimeError("simulated apply crash after durable plan")):
                with self.assertRaisesRegex(RuntimeError, "simulated apply crash"):
                    apply_integration_decision(
                        fixture.spec.run_id,
                        evidence.attempt_id,
                        action_id,
                        fixture.root,
                        fixture.result_ref,
                        fixture.target_ref,
                        evidence.artifact_reference.reference_id,
                        decision.artifact_reference.reference_id,
                        start=fixture.root,
                    )

        cases = (
            ("verifier_evidence_digest", "tampered"),
            ("integration_decision_digest", "tampered"),
            ("integration_decision_digest", "missing"),
        )
        for authority, mutation in cases:
            with self.subTest(authority=authority, mutation=mutation):
                fixture = self.prepare()
                evidence, decision = self.verified_decision(fixture)
                action_id = f"action-apply-crash-tamper-{authority}"
                target_before = self.git_oid(fixture.root, fixture.target_ref)
                crash_after_plan(fixture, evidence, decision, action_id)

                expected_approval = {
                    key: resolve(fixture, evidence, decision)
                    for key, resolve in approval_fields.items()
                }
                with self.supported_filesystem(), RunStore.open(fixture.root) as store:
                    effects = EffectEngine(store, LeaseManager(store))
                    plan = effects._load_plan(action_id)  # noqa: SLF001
                    self.assertEqual(
                        plan.spec.get("approval_digests"), expected_approval)

                tamper_digest = expected_approval[authority]
                tamper_path = ArtifactStore(fixture.root).path_for(tamper_digest)
                if mutation == "missing":
                    tamper_path.unlink()
                else:
                    os.chmod(tamper_path, 0o600)
                    tamper_path.write_bytes(b"tampered post-plan approval authority")
                    os.chmod(tamper_path, 0o400)

                with self.supported_filesystem(), RunStore.open(fixture.root) as store:
                    effects = EffectEngine(store, LeaseManager(store))
                    with mock.patch.object(
                            effects, "_execute_driver",
                            wraps=effects._execute_driver) as driver:  # noqa: SLF001
                        with self.assertRaises(
                                ApprovalAuthorityMismatch) as raised:
                            effects.reconcile_actions(
                                (action_id,), quiescence_probe=lambda plan: True)
                    driver.assert_not_called()
                self.assertEqual(
                    raised.exception.code, "approval_authority_mismatch")
                self.assertEqual(raised.exception.authority, authority)
                self.assertEqual(
                    self.git_oid(fixture.root, fixture.target_ref), target_before)

        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)
        action_id = "action-apply-crash-approval-unchanged"
        crash_after_plan(fixture, evidence, decision, action_id)
        with self.supported_filesystem(), RunStore.open(fixture.root) as store:
            effects = EffectEngine(store, LeaseManager(store))
            with mock.patch.object(
                    effects, "_execute_driver",
                    wraps=effects._execute_driver) as driver:  # noqa: SLF001
                result = effects.reconcile_actions(
                    (action_id,), quiescence_probe=lambda plan: True)[0]
            self.assertEqual(result.state.value, "completed")
            self.assertEqual(driver.call_count, 1)
        self.assertEqual(
            self.git_oid(fixture.root, fixture.target_ref),
            self.git_oid(fixture.root, fixture.result_ref),
        )

    def test_pc22_cas_race_is_refused_without_overwriting_concurrent_result(self):
        """PC-22: apply's execution-time CAS preserves a concurrent target update."""
        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)
        concurrent = self.commit_from_base(fixture, "race.txt", b"race winner\n")
        before = fingerprint_worktree(fixture.root)

        def race() -> None:
            self.git_bytes(
                fixture.root, "update-ref", fixture.target_ref, concurrent,
                fixture.spec.base_snapshot.head)

        with self.supported_filesystem(), self.assertRaises(
                ApplyConcurrentDriftRefusal):
            apply_integration_decision(
                fixture.spec.run_id,
                evidence.attempt_id,
                "action-apply-cas-race",
                fixture.root,
                fixture.result_ref,
                fixture.target_ref,
                evidence.artifact_reference.reference_id,
                decision.artifact_reference.reference_id,
                race_hook=race,
                start=fixture.root,
            )

        self.assertEqual(self.git_oid(fixture.root, fixture.target_ref), concurrent)
        self.assertEqual(fingerprint_worktree(fixture.root), before)

    def test_pc20_concurrent_retries_publish_one_terminal_evidence(self):
        """PC-20: concurrent retries serialize through terminal evidence publication."""
        fixture = self.prepare()
        failed_attempt = "attempt-verify-concurrent-failed"
        failed_action = "action-verify-concurrent-failed"
        self.create_attempt(fixture, failed_attempt)
        with self.supported_filesystem(), self.assertRaises(InvalidVerifierOutput):
            execute_verifier(
                fixture.spec.run_id,
                failed_attempt,
                failed_action,
                fixture.root,
                fixture.result_ref,
                self.worker.actor_id,
                self.verifier,
                self.check_executor(),
                self.verifier_adapter(
                    fixture, executor=self.verifier_executor(raw_output=b"")),
                start=fixture.root,
            )

        attempts = ("attempt-verify-concurrent-a", "attempt-verify-concurrent-b")
        actions = ("action-verify-concurrent-a", "action-verify-concurrent-b")
        for attempt_id in attempts:
            self.create_attempt(fixture, attempt_id)

        def retry(attempt_id, action_id):
            return execute_verifier(
                fixture.spec.run_id,
                attempt_id,
                action_id,
                fixture.root,
                fixture.result_ref,
                self.worker.actor_id,
                self.verifier,
                self.check_executor(),
                self.verifier_adapter(fixture),
                retry_of=failed_action,
                start=fixture.root,
            )

        outcomes: list[object] = []
        with self.supported_filesystem(), ThreadPoolExecutor(
                max_workers=2) as executor:
            futures = tuple(executor.submit(retry, *item)
                            for item in zip(attempts, actions))
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10))
                except Exception as error:  # asserted below by exact type
                    outcomes.append(error)

        self.assertEqual(sum(isinstance(item, EffectRetryRefused)
                             for item in outcomes), 1)
        self.assertEqual(sum(not isinstance(item, Exception)
                             for item in outcomes), 1)
        self.assertEqual(
            self.semantic_reference_count(fixture.root, "verifier-evidence:"), 1)

    def test_pc18_pc20_pc21_public_reload_revalidates_terminal_authority(self):
        """Published evidence and decision reload through their full authority chain."""
        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)

        with self.supported_filesystem():
            reloaded_evidence = reload_verifier_evidence(
                fixture.spec.run_id,
                evidence.attempt_id,
                evidence.action_id,
                start=fixture.root,
            )
            reloaded_decision = reload_integration_decision(
                fixture.spec.run_id,
                decision.attempt_id,
                decision.action_id,
                evidence.action_id,
                start=fixture.root,
            )

        self.assertEqual(reloaded_evidence, evidence)
        self.assertEqual(reloaded_decision, decision)
        with self.supported_filesystem(), self.assertRaises(EvidenceBindingRefusal):
            reload_verifier_evidence(
                fixture.spec.run_id,
                f"{evidence.attempt_id}:different",
                evidence.action_id,
                start=fixture.root,
            )
        with self.supported_filesystem(), self.assertRaises(ApplyBindingRefusal):
            reload_integration_decision(
                fixture.spec.run_id,
                f"{decision.attempt_id}:different",
                decision.action_id,
                evidence.action_id,
                start=fixture.root,
            )

    def test_public_reload_revalidates_closeout_ready_and_completed_authority(self):
        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)

        with self.supported_filesystem(), RunStore.open(fixture.root) as store:
            run = store.get_run(fixture.spec.run_id)
            store.record_transition(
                EntityKind.RUN,
                fixture.spec.run_id,
                expected_version=run.version,
                next_state="running",
                reason=TransitionReason.PROCESS_STARTED,
            )
        with self.supported_filesystem():
            with self.assertRaises(VerificationPlanStateError):
                reload_verifier_evidence(
                    fixture.spec.run_id,
                    evidence.attempt_id,
                    evidence.action_id,
                    start=fixture.root,
                )
            with self.assertRaises(VerificationPlanStateError):
                reload_integration_decision(
                    fixture.spec.run_id,
                    decision.attempt_id,
                    decision.action_id,
                    evidence.action_id,
                    start=fixture.root,
                )

        for state in ("closeout-ready", "completed"):
            with self.supported_filesystem(), RunStore.open(fixture.root) as store:
                run = store.get_run(fixture.spec.run_id)
                store.record_transition(
                    EntityKind.RUN,
                    fixture.spec.run_id,
                    expected_version=run.version,
                    next_state=state,
                    reason=TransitionReason.COMPLETED,
                )
            with self.supported_filesystem():
                self.assertEqual(reload_verifier_evidence(
                    fixture.spec.run_id,
                    evidence.attempt_id,
                    evidence.action_id,
                    start=fixture.root,
                ), evidence)
                self.assertEqual(reload_integration_decision(
                    fixture.spec.run_id,
                    decision.attempt_id,
                    decision.action_id,
                    evidence.action_id,
                    start=fixture.root,
                ), decision)

    def test_operational_dispatch_loader_stays_closed_after_dispatch_ready(self):
        fixture = self.prepare()
        with self.supported_filesystem(), RunStore.open(fixture.root) as store:
            run = store.get_run(fixture.spec.run_id)
            store.record_transition(
                EntityKind.RUN,
                fixture.spec.run_id,
                expected_version=run.version,
                next_state="closeout-ready",
                reason=TransitionReason.COMPLETED,
            )

        with self.supported_filesystem(), self.assertRaisesRegex(
                VerificationPlanStateError, "expected 'dispatch-ready'"):
            load_dispatch_ready(fixture.spec.run_id, start=fixture.root)

    def test_pc20_public_evidence_reload_refuses_tampered_terminal_bytes(self):
        """Reload rehashes published verifier bytes instead of trusting their reference."""
        fixture = self.prepare()
        evidence = self.verify(fixture)
        artifact_path = ArtifactStore(fixture.root).path_for(
            evidence.artifact_reference.digest)
        os.chmod(artifact_path, 0o600)
        artifact_path.write_bytes(b"{}")
        os.chmod(artifact_path, 0o400)

        with self.supported_filesystem(), self.assertRaises(EvidenceBindingRefusal):
            reload_verifier_evidence(
                fixture.spec.run_id,
                evidence.attempt_id,
                evidence.action_id,
                start=fixture.root,
            )

    def test_pc21_public_decision_reload_refuses_tampered_terminal_bytes(self):
        """Reload rehashes the published decision and its verifier authority."""
        fixture = self.prepare()
        evidence, decision = self.verified_decision(fixture)
        artifact_path = ArtifactStore(fixture.root).path_for(
            decision.artifact_reference.digest)
        os.chmod(artifact_path, 0o600)
        artifact_path.write_bytes(b"{}")
        os.chmod(artifact_path, 0o400)

        with self.supported_filesystem(), self.assertRaises(ApplyBindingRefusal):
            reload_integration_decision(
                fixture.spec.run_id,
                decision.attempt_id,
                decision.action_id,
                evidence.action_id,
                start=fixture.root,
            )

    def test_pc20_pc21_retry_requires_new_attempt_and_new_action_identity(self):
        """PC-20/PC-21: verify and decision retries require fresh lineage IDs."""
        fixture = self.prepare()
        old_verify_attempt = "attempt-verify-failed"
        old_verify_action = "action-verify-failed"
        self.create_attempt(fixture, old_verify_attempt)
        with self.supported_filesystem(), self.assertRaises(InvalidVerifierOutput):
            execute_verifier(
                fixture.spec.run_id,
                old_verify_attempt,
                old_verify_action,
                fixture.root,
                fixture.result_ref,
                self.worker.actor_id,
                self.verifier,
                self.check_executor(),
                self.verifier_adapter(
                    fixture, executor=self.verifier_executor(raw_output=b"")),
                start=fixture.root,
            )

        new_verify_attempt = "attempt-verify-retry"
        self.create_attempt(fixture, new_verify_attempt)
        for attempt_id, action_id in (
                (new_verify_attempt, old_verify_action),
                (old_verify_attempt, "action-verify-same-attempt")):
            with self.subTest(
                    phase="verify", attempt=attempt_id, action=action_id), \
                    self.supported_filesystem(), self.assertRaises(EffectRetryRefused):
                execute_verifier(
                    fixture.spec.run_id,
                    attempt_id,
                    action_id,
                    fixture.root,
                    fixture.result_ref,
                    self.worker.actor_id,
                    self.verifier,
                    self.check_executor(),
                    self.verifier_adapter(fixture),
                    retry_of=old_verify_action,
                    start=fixture.root,
                )

        with self.supported_filesystem():
            evidence = execute_verifier(
                fixture.spec.run_id,
                new_verify_attempt,
                "action-verify-retry",
                fixture.root,
                fixture.result_ref,
                self.worker.actor_id,
                self.verifier,
                self.check_executor(),
                self.verifier_adapter(fixture),
                retry_of=old_verify_action,
                start=fixture.root,
            )

        terminal_verify_attempt = "attempt-verify-terminal-retry"
        self.create_attempt(fixture, terminal_verify_attempt)
        with self.supported_filesystem(), self.assertRaises(EffectRetryRefused):
            execute_verifier(
                fixture.spec.run_id,
                terminal_verify_attempt,
                "action-verify-terminal-retry",
                fixture.root,
                fixture.result_ref,
                self.worker.actor_id,
                self.verifier,
                self.check_executor(),
                self.verifier_adapter(fixture),
                retry_of=evidence.action_id,
                start=fixture.root,
            )
        self.assertEqual(
            self.semantic_reference_count(fixture.root, "verifier-evidence:"), 1)

        ancestor_verify_attempt = "attempt-verify-ancestor-retry"
        self.create_attempt(fixture, ancestor_verify_attempt)
        with self.supported_filesystem(), self.assertRaises(EffectRetryRefused):
            execute_verifier(
                fixture.spec.run_id,
                ancestor_verify_attempt,
                "action-verify-ancestor-retry",
                fixture.root,
                fixture.result_ref,
                self.worker.actor_id,
                self.verifier,
                self.check_executor(),
                self.verifier_adapter(fixture),
                retry_of=old_verify_action,
                start=fixture.root,
            )
        self.assertEqual(
            self.semantic_reference_count(fixture.root, "verifier-evidence:"), 1)

        criteria = tuple(item.criterion for item in evidence.criterion_results)
        invalid_decision = self.decision_input(evidence, criteria=criteria[:-1])
        old_decision_action = "action-decision-failed"
        with self.supported_filesystem(), self.assertRaises(MissingCriterionRefusal):
            record_integration_decision(
                fixture.spec.run_id,
                evidence.attempt_id,
                old_decision_action,
                invalid_decision,
                start=fixture.root,
            )

        new_decision_attempt = "attempt-decision-retry"
        self.create_attempt(fixture, new_decision_attempt)
        for attempt_id, action_id in (
                (new_decision_attempt, old_decision_action),
                (evidence.attempt_id, "action-decision-same-attempt")):
            with self.subTest(
                    phase="decision", attempt=attempt_id, action=action_id), \
                    self.supported_filesystem(), self.assertRaises(EffectRetryRefused):
                record_integration_decision(
                    fixture.spec.run_id,
                    attempt_id,
                    action_id,
                    self.decision_input(evidence),
                    retry_of=old_decision_action,
                    start=fixture.root,
                )

        missing_lineage_attempt = "attempt-decision-missing-lineage"
        self.create_attempt(fixture, missing_lineage_attempt)
        with self.supported_filesystem(), self.assertRaises(EffectRetryRefused):
            record_integration_decision(
                fixture.spec.run_id,
                missing_lineage_attempt,
                "action-decision-missing-lineage",
                self.decision_input(evidence),
                start=fixture.root,
            )

        with self.supported_filesystem():
            decision = record_integration_decision(
                fixture.spec.run_id,
                new_decision_attempt,
                "action-decision-retry",
                self.decision_input(evidence),
                retry_of=old_decision_action,
                start=fixture.root,
            )
        self.assertEqual(decision.attempt_id, new_decision_attempt)
        self.assertEqual(decision.action_id, "action-decision-retry")
        self.assertEqual(
            self.semantic_reference_count(
                fixture.root, "integration-decision:"), 1)

        terminal_attempt = "attempt-decision-terminal-retry"
        self.create_attempt(fixture, terminal_attempt)
        with self.supported_filesystem(), self.assertRaises(EffectRetryRefused):
            record_integration_decision(
                fixture.spec.run_id,
                terminal_attempt,
                "action-decision-terminal-retry",
                self.decision_input(evidence),
                retry_of=decision.action_id,
                start=fixture.root,
            )
        ancestor_decision_attempt = "attempt-decision-ancestor-retry"
        self.create_attempt(fixture, ancestor_decision_attempt)
        with self.supported_filesystem(), self.assertRaises(EffectRetryRefused):
            record_integration_decision(
                fixture.spec.run_id,
                ancestor_decision_attempt,
                "action-decision-ancestor-retry",
                self.decision_input(evidence),
                retry_of=old_decision_action,
                start=fixture.root,
            )
        self.assertEqual(
            self.semantic_reference_count(
                fixture.root, "integration-decision:"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
