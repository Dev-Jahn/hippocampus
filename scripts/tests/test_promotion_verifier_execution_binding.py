#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Promotion verifier execution descriptors are terminal authority."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import hashlib
import json
import stat

import test_run_verify
from waystone.jobs.domain import Role
from waystone.runs import environment as environment_module
from waystone.runs.artifacts import (
    ArtifactReference,
    ArtifactReferenceKind,
    ArtifactStore,
)
from waystone.runs.store import EntityKind, RunStore, TransitionReason
from waystone.runs.verify import (
    ApplyBindingRefusal,
    EvidenceBindingRefusal,
    derive_git_result,
    execute_verifier,
    fingerprint_materialized_root,
    reload_integration_decision,
    reload_verifier_evidence,
)


def sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class PromotionVerifierExecutionBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def executable(self, content: bytes = b"#!/bin/sh\nexit 0\n") -> Path:
        path = self.base / "bin" / "codex"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return path

    @staticmethod
    def binding_digest(binding) -> str:
        return sha256(json.dumps({
            "backend": binding.backend,
            "execution_category": binding.execution_category.value,
            "role": binding.role.value,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def environment(self, *, lang: str = "C.UTF-8"):
        return environment_module.build_runner_environment({
            "HOME": str(self.base / "home"),
            "LANG": lang,
            "PATH": str(self.base / "bin"),
        })

    def descriptor(self, *, binding=None):
        executable = self.executable()
        backend = "codex:verifier" if binding is None else binding.backend
        binding_digest = (
            sha256(b"verifier-binding")
            if binding is None else self.binding_digest(binding)
        )
        return environment_module.freeze_runner_execution_descriptor(
            self.environment(),
            executable="codex",
            cwd=self.base,
            verifier_backend=backend,
            verifier_binding_digest=binding_digest,
        )

    def test_p1_start_descriptor_rejects_resume_environment_drift_and_accepts_exact_match(self):
        descriptor = self.descriptor()
        descriptor_bytes = descriptor.canonical_bytes()
        self.assertNotIn(b"C.UTF-8", descriptor_bytes)
        self.assertNotIn(str(self.base / "home").encode(), descriptor_bytes)

        environment_module.require_runner_execution_match(
            descriptor,
            self.environment(),
            executable="codex",
            cwd=self.base,
            verifier_backend=descriptor.verifier_backend,
            verifier_binding_digest=descriptor.verifier_binding_digest,
        )
        with self.assertRaisesRegex(
                environment_module.RunnerExecutionDescriptorRefusal,
                "environment digest"):
            environment_module.require_runner_execution_match(
                descriptor,
                self.environment(lang="ko_KR.UTF-8"),
                executable="codex",
                cwd=self.base,
                verifier_backend=descriptor.verifier_backend,
                verifier_binding_digest=descriptor.verifier_binding_digest,
            )

    def test_p2_start_descriptor_rejects_executable_bytes_changed_before_spawn(self):
        descriptor = self.descriptor()
        self.executable(b"#!/bin/sh\nexit 7\n")

        with self.assertRaisesRegex(
                environment_module.RunnerExecutionDescriptorRefusal,
                "executable content"):
            environment_module.require_runner_execution_match(
                descriptor,
                self.environment(),
                executable="codex",
                cwd=self.base,
                verifier_backend=descriptor.verifier_backend,
                verifier_binding_digest=descriptor.verifier_binding_digest,
            )

    def verified_with_launch(self):
        case = test_run_verify.RunVerifyTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        fixture = case.prepare()
        binding = fixture.plan.binding_for(Role.VERIFIER).binding
        descriptor = self.descriptor(binding=binding)
        artifacts = ArtifactStore(fixture.root)
        stored_descriptor = artifacts.write(descriptor.canonical_bytes())
        descriptor_ref = ArtifactReference(
            f"promotion-execution-descriptor:{fixture.spec.run_id}",
            ArtifactReferenceKind.EVIDENCE,
            stored_descriptor.digest,
            stored_descriptor.size,
        )
        attempt_id = "attempt-promotion-execution-binding"
        action_id = "action-promotion-execution-binding"
        case.create_attempt(fixture, attempt_id)
        result = derive_git_result(
            fixture.root, fixture.spec.base_snapshot.head, fixture.result_ref)
        launch_bytes = json.dumps({
            "action_id": action_id,
            "attempt_id": attempt_id,
            "candidate_oid": result.result_oid,
            "cwd": str(fixture.result_worktree.resolve()),
            "environment_digest": descriptor.environment_digest,
            "executable_content_digest": descriptor.executable_content_digest,
            "execution_descriptor_digest": descriptor.digest,
            "execution_descriptor_reference_id": descriptor_ref.reference_id,
            "invocation_digest": sha256(b"promotion invocation"),
            "job_id": fixture.spec.job_id,
            "resolved_executable": descriptor.resolved_executable,
            "root_fingerprint": fingerprint_materialized_root(
                fixture.result_worktree),
            "run_id": fixture.spec.run_id,
            "run_spec_digest": fixture.spec.run_spec_digest,
            "schema": "waystone-promotion-verifier-launch-3",
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        stored_launch = artifacts.write(launch_bytes)
        launch_ref = ArtifactReference(
            f"verifier-launch:{action_id}",
            ArtifactReferenceKind.EVIDENCE,
            stored_launch.digest,
            stored_launch.size,
        )
        with case.supported_filesystem(), RunStore.open(fixture.root) as store:
            run = store.get_run(fixture.spec.run_id)
            store.record_transition(
                EntityKind.RUN,
                fixture.spec.run_id,
                expected_version=run.version,
                next_state=run.state,
                reason=TransitionReason.PLANNED,
                evidence_digest=descriptor_ref.digest,
                artifact_references=(descriptor_ref,),
            )
            attempt = store.get_entity(EntityKind.ATTEMPT, attempt_id)
            store.record_transition(
                EntityKind.ATTEMPT,
                attempt_id,
                expected_version=attempt.version,
                next_state=attempt.state,
                reason=TransitionReason.EFFECT_OBSERVED,
                evidence_digest=launch_ref.digest,
                artifact_references=(launch_ref,),
            )
        with case.supported_filesystem():
            evidence = execute_verifier(
                fixture.spec.run_id,
                attempt_id,
                action_id,
                fixture.root,
                fixture.result_ref,
                case.worker.actor_id,
                case.verifier,
                case.check_executor(),
                case.verifier_adapter(fixture),
                start=fixture.root,
                execution_descriptor_reference_id=descriptor_ref.reference_id,
                verifier_launch_reference_id=launch_ref.reference_id,
            )
        return case, fixture, evidence, descriptor_ref, launch_ref

    def test_p3_reload_and_decision_reload_require_bound_launch_artifact(self):
        case, fixture, evidence, _descriptor_ref, launch_ref = (
            self.verified_with_launch())
        decision = case.decide(fixture, evidence)
        ArtifactStore(fixture.root).path_for(launch_ref.digest).unlink()

        with case.supported_filesystem(), self.assertRaises(EvidenceBindingRefusal):
            reload_verifier_evidence(
                fixture.spec.run_id,
                evidence.attempt_id,
                evidence.action_id,
                start=fixture.root,
            )
        with case.supported_filesystem(), self.assertRaises(ApplyBindingRefusal):
            reload_integration_decision(
                fixture.spec.run_id,
                decision.attempt_id,
                decision.action_id,
                evidence.action_id,
                start=fixture.root,
            )

    def test_p4_terminal_evidence_round_trips_launch_and_descriptor_binding(self):
        case, fixture, evidence, descriptor_ref, launch_ref = (
            self.verified_with_launch())

        with case.supported_filesystem():
            reloaded = reload_verifier_evidence(
                fixture.spec.run_id,
                evidence.attempt_id,
                evidence.action_id,
                start=fixture.root,
            )
        self.assertEqual(reloaded, evidence)
        self.assertEqual(evidence.execution_descriptor_reference, descriptor_ref)
        self.assertEqual(evidence.verifier_launch_reference, launch_ref)
        launch = json.loads(
            ArtifactStore(fixture.root).read_reference(launch_ref).decode("utf-8"))
        self.assertEqual(
            launch["execution_descriptor_digest"], descriptor_ref.digest)
        self.assertEqual(
            launch["executable_content_digest"],
            environment_module.parse_runner_execution_descriptor(
                ArtifactStore(fixture.root).read_reference(descriptor_ref),
                expected_digest=descriptor_ref.digest,
            ).executable_content_digest,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
