from __future__ import annotations

from support import *  # noqa: F401,F403

from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import test_run_verify
import test_run_outcome
from test_work_brief import init_project
from waystone.features.review_layout import new_run_id
from waystone.jobs.completion import LifecycleStage
from waystone.jobs.profile import RunAssembly as ProductionRunAssembly
from waystone.jobs.domain import Role
from waystone.project.context import ProjectContext
from waystone.runs.artifacts import (
    ArtifactReference,
    ArtifactReferenceKind,
    ArtifactStore,
)
from waystone.runs.assurance import (
    PromotionLineageRefusal,
    ReviewCycle,
    ReviewerEvidence,
    compile_assurance_plan,
)
from waystone.runs.engine import (
    EngineBindingRefusal,
    StagedRunEngine,
    validate_promotion_evidence,
    validate_promotion_rejection,
)
from waystone.runs.effects import EffectRetryRefused
from waystone.runs.outcome import read_outcome_ledger
from waystone.runs.store import EntityKind, FilesystemInfo, RunStore, TransitionReason
from waystone.runs.verify import (
    ActorIdentity,
    DecisionOutcome,
    IntegrationDecision,
    InvalidVerifierOutput,
    VerifierEvidence,
    VerifierExecutionFailed,
    execute_verifier,
)
from waystone.cli.run_group import _parse_declared_risks_bytes


class PromoteEvidenceContractTests(unittest.TestCase):
    def d(self, value: int) -> str:
        token = format(value, "x")
        return "sha256:" + (token * 64)[:64]

    def plan(self, lineage: str, *, risk: bool = True):
        return compile_assurance_plan(
            "promote",
            evaluation_spec={"digest": self.d(1), "generation": 1},
            promotion_lineage_id=lineage,
            declared_risks=(("trust-surface-store",) if risk else ()),
        )

    def verifier(self, run_id: str, actor_id: str = "verifier") -> VerifierEvidence:
        verifier_ref = ArtifactReference(
            "verifier-evidence:promote", ArtifactReferenceKind.EVIDENCE,
            self.d(10), 1,
        )
        check_ref = ArtifactReference(
            "engine-check-evidence:promote", ArtifactReferenceKind.EVIDENCE,
            self.d(11), 1,
        )
        descriptor_ref = ArtifactReference(
            f"promotion-execution-descriptor:{run_id}",
            ArtifactReferenceKind.EVIDENCE,
            self.d(33),
            1,
        )
        launch_ref = ArtifactReference(
            "verifier-launch:promote",
            ArtifactReferenceKind.EVIDENCE,
            self.d(34),
            1,
        )
        return VerifierEvidence(
            run_id=run_id,
            job_id=f"{run_id}:job",
            attempt_id=f"{run_id}:attempt:1",
            action_id=f"{run_id}:independent-verify",
            worker_actor_id="worker",
            actor=ActorIdentity(actor_id, Role.VERIFIER),
            run_spec_digest=self.d(12),
            verification_plan_digest=self.d(13),
            preflight_evidence_digest=self.d(14),
            engine_checks=SimpleNamespace(artifact_reference=check_ref),
            verifier_binding=SimpleNamespace(role=Role.VERIFIER),
            verifier_sandbox=SimpleNamespace(filesystem="read-only"),
            verifier_capability_digest=self.d(15),
            runner_observation_digest=self.d(16),
            runner_stdout_digest=self.d(17),
            runner_stderr_digest=self.d(18),
            result=SimpleNamespace(result_oid="a" * 40, result_digest=self.d(19)),
            criterion_results=(),
            blockers=(),
            summary="verified exact promotion candidate",
            artifact_reference=verifier_ref,
            execution_descriptor_reference=descriptor_ref,
            verifier_launch_reference=launch_ref,
        )

    def review(
        self, lineage: str, run_spec_digest: str, candidate_digest: str,
        target_result_digest: str, *, actor_id: str = "reviewer",
    ) -> tuple[ReviewCycle, ReviewerEvidence]:
        evidence = ReviewerEvidence(
            promotion_lineage_id=lineage,
            target_run_spec_digest=run_spec_digest,
            candidate_digest=candidate_digest,
            target_result_digest=target_result_digest,
            review_artifact_digest=self.d(20),
            actor={"actor_id": actor_id, "role": "reviewer"},
            finding_digests=(self.d(21),),
        )
        return ReviewCycle(lineage, 1, target_result_digest, evidence.digest, None), evidence

    def decision(
        self, run_id: str, verifier: VerifierEvidence, candidate_digest: str,
        evaluation_digest: str, reviewer_digest: str | None,
        *, actor_id: str = "coordinator",
        outcome: DecisionOutcome = DecisionOutcome.ACCEPT,
    ) -> IntegrationDecision:
        return IntegrationDecision(
            run_id=run_id,
            job_id=f"{run_id}:job",
            attempt_id=f"{run_id}:attempt:1",
            action_id=f"{run_id}:integration-decision",
            actor=ActorIdentity(actor_id, Role.COORDINATOR),
            outcome=outcome,
            criteria=(),
            result_digest=verifier.result.result_digest,
            verifier_reference_id=verifier.artifact_reference.reference_id,
            verifier_artifact_digest=verifier.artifact_reference.digest,
            engine_check_reference_id=verifier.engine_checks.artifact_reference.reference_id,
            engine_check_artifact_digest=verifier.engine_checks.artifact_reference.digest,
            blocker_overrides=(),
            producer_effect_digest=self.d(22),
            candidate_digest=candidate_digest,
            evaluation_evidence_digest=evaluation_digest,
            reviewer_artifact_digests=(
                () if reviewer_digest is None else (reviewer_digest,)
            ),
            artifact_reference=ArtifactReference(
                "integration-decision:promote", ArtifactReferenceKind.DECISION,
                self.d(23), 1,
            ),
        )

    def valid_bundle(self, *, risk: bool = True):
        run_id = new_run_id()
        lineage = new_run_id()
        candidate = self.d(30)
        evaluation = self.d(31)
        target_result = self.d(32)
        verifier = self.verifier(run_id)
        review = self.review(
            lineage, verifier.run_spec_digest, candidate, target_result,
        ) if risk else None
        decision = self.decision(
            run_id, verifier, candidate, evaluation,
            None if review is None else review[1].digest,
        )
        return (
            self.plan(lineage, risk=risk), run_id, candidate, evaluation,
            target_result, verifier, review, decision,
        )

    def test_p1_evaluation_evidence_digest_cannot_complete_independent_verify(self):
        plan, run_id, candidate, evaluation, target, _verifier, review, decision = (
            self.valid_bundle()
        )
        with self.assertRaises(EngineBindingRefusal):
            validate_promotion_evidence(
                plan,
                expected_run_id=run_id,
                expected_run_spec_digest=self.d(12),
                expected_candidate_digest=candidate,
                expected_candidate_oid="a" * 40,
                expected_evaluation_evidence_digest=evaluation,
                expected_target_result_digest=target,
                verifier=evaluation,
                review=review,
                decision=decision,
            )

    def test_p2_decision_must_bind_exact_candidate_evaluation_verifier_and_reviewer(self):
        plan, run_id, candidate, evaluation, target, verifier, review, decision = (
            self.valid_bundle()
        )
        decision = replace(decision, evaluation_evidence_digest=self.d(99))
        with self.assertRaises(EngineBindingRefusal):
            validate_promotion_evidence(
                plan,
                expected_run_id=run_id,
                expected_run_spec_digest=verifier.run_spec_digest,
                expected_candidate_digest=candidate,
                expected_candidate_oid="a" * 40,
                expected_evaluation_evidence_digest=evaluation,
                expected_target_result_digest=target,
                verifier=verifier,
                review=review,
                decision=decision,
            )

    def test_r1_same_actor_or_artifact_cannot_fill_verifier_reviewer_decision_roles(self):
        plan, run_id, candidate, evaluation, target, verifier, review, decision = (
            self.valid_bundle()
        )
        assert review is not None
        cycle, reviewer = review
        reviewer = replace(
            reviewer, actor={"actor_id": "verifier", "role": "reviewer"},
        )
        cycle = replace(cycle, review_digest=reviewer.digest)
        decision = replace(
            decision,
            actor=ActorIdentity("verifier", Role.COORDINATOR),
            reviewer_artifact_digests=(reviewer.digest,),
            artifact_reference=replace(
                decision.artifact_reference,
                digest=verifier.artifact_reference.digest,
            ),
        )
        with self.assertRaises(EngineBindingRefusal):
            validate_promotion_evidence(
                plan,
                expected_run_id=run_id,
                expected_run_spec_digest=verifier.run_spec_digest,
                expected_candidate_digest=candidate,
                expected_candidate_oid="a" * 40,
                expected_evaluation_evidence_digest=evaluation,
                expected_target_result_digest=target,
                verifier=verifier,
                review=(cycle, reviewer),
                decision=decision,
            )

    def test_r2_declared_trust_risk_without_reviewer_artifact_refuses(self):
        plan, run_id, candidate, evaluation, target, verifier, _review, decision = (
            self.valid_bundle()
        )
        decision = replace(decision, reviewer_artifact_digests=())
        with self.assertRaises(EngineBindingRefusal):
            validate_promotion_evidence(
                plan,
                expected_run_id=run_id,
                expected_run_spec_digest=verifier.run_spec_digest,
                expected_candidate_digest=candidate,
                expected_candidate_oid="a" * 40,
                expected_evaluation_evidence_digest=evaluation,
                expected_target_result_digest=target,
                verifier=verifier,
                review=None,
                decision=decision,
            )

    def test_r3_reviewer_artifact_for_other_candidate_or_result_refuses(self):
        plan, run_id, candidate, evaluation, target, verifier, review, decision = (
            self.valid_bundle()
        )
        assert review is not None
        cycle, reviewer = review
        wrong = replace(reviewer, candidate_digest=self.d(98))
        wrong_cycle = replace(
            cycle, target_result_digest=self.d(97), review_digest=wrong.digest,
        )
        decision = replace(decision, reviewer_artifact_digests=(wrong.digest,))
        with self.assertRaises(EngineBindingRefusal):
            validate_promotion_evidence(
                plan,
                expected_run_id=run_id,
                expected_run_spec_digest=verifier.run_spec_digest,
                expected_candidate_digest=candidate,
                expected_candidate_oid="a" * 40,
                expected_evaluation_evidence_digest=evaluation,
                expected_target_result_digest=target,
                verifier=verifier,
                review=(wrong_cycle, wrong),
                decision=decision,
            )

    def test_p4_accepted_risk_lines_activate_public_assurance_review_gate(self):
        risks = _parse_declared_risks_bytes(b"public-contract\ntrust-surface-store\n")
        plan = compile_assurance_plan(
            "promote",
            evaluation_spec={"digest": self.d(1), "generation": 1},
            promotion_lineage_id=new_run_id(),
            declared_risks=risks,
        )
        self.assertTrue(plan.requires("adversarial-review"))
        self.assertEqual(plan.review["reasons"], [
            "public-contract", "trust-surface-store",
        ])
        with self.assertRaises(PromotionLineageRefusal):
            _parse_declared_risks_bytes(b"none\npublic-contract\n")

    def test_reject_uses_separate_exact_tuple_validation_without_weakening_accept(self):
        plan, run_id, candidate, evaluation, target, verifier, review, decision = (
            self.valid_bundle()
        )
        rejected = replace(decision, outcome=DecisionOutcome.REJECT)
        arguments = {
            "expected_run_id": run_id,
            "expected_run_spec_digest": verifier.run_spec_digest,
            "expected_candidate_digest": candidate,
            "expected_candidate_oid": "a" * 40,
            "expected_evaluation_evidence_digest": evaluation,
            "expected_target_result_digest": target,
            "verifier": verifier,
            "review": review,
            "decision": rejected,
        }

        with self.assertRaises(EngineBindingRefusal):
            validate_promotion_evidence(plan, **arguments)
        validate_promotion_rejection(plan, **arguments)

        arguments["decision"] = replace(
            rejected, evaluation_evidence_digest=self.d(99))
        with self.assertRaises(EngineBindingRefusal):
            validate_promotion_rejection(plan, **arguments)

    def test_semantic_reject_refuses_same_candidate_retry_even_with_lineage(self):
        verify_case = test_run_verify.RunVerifyTests("runTest")
        verify_case.setUp()
        self.addCleanup(verify_case.doCleanups)
        fixture = verify_case.prepare()
        failed = fixture.spec.job_input.acceptance_criteria[0]
        rejected = verify_case.verify(
            fixture,
            attempt_id="attempt-promotion-rejected",
            action_id="action-promotion-rejected",
            executor=verify_case.verifier_executor(failed_criteria=(failed,)),
        )
        self.assertFalse(rejected.criterion_results[0].passed)

        retry_attempt = "attempt-promotion-retry"
        verify_case.create_attempt(fixture, retry_attempt)
        with verify_case.supported_filesystem(), self.assertRaises(EffectRetryRefused):
            execute_verifier(
                fixture.spec.run_id,
                retry_attempt,
                "action-promotion-retry",
                fixture.root,
                fixture.result_ref,
                verify_case.worker.actor_id,
                verify_case.verifier,
                verify_case.check_executor(),
                verify_case.verifier_adapter(fixture),
                start=fixture.root,
            )
        with verify_case.supported_filesystem(), self.assertRaisesRegex(
                EffectRetryRefused, "new candidate|owner ruling"):
            execute_verifier(
                fixture.spec.run_id,
                retry_attempt,
                "action-promotion-retry",
                fixture.root,
                fixture.result_ref,
                verify_case.worker.actor_id,
                verify_case.verifier,
                verify_case.check_executor(),
                verify_case.verifier_adapter(fixture),
                retry_of=rejected.action_id,
                start=fixture.root,
            )

        self.assertEqual(
            verify_case.semantic_reference_count(
                fixture.root, "verifier-evidence:"),
            1,
        )

    def test_process_and_malformed_failures_remain_retryable(self):
        cases = (
            ("process", {"raw_output": b"ignored", "returncode": 7},
             VerifierExecutionFailed),
            ("malformed", {"raw_output": {}}, InvalidVerifierOutput),
        )
        for label, executor_args, expected_error in cases:
            with self.subTest(label=label):
                verify_case = test_run_verify.RunVerifyTests("runTest")
                verify_case.setUp()
                self.addCleanup(verify_case.doCleanups)
                fixture = verify_case.prepare()
                failed_attempt = f"attempt-{label}-failed"
                failed_action = f"action-{label}-failed"
                with self.assertRaises(expected_error):
                    verify_case.verify(
                        fixture,
                        attempt_id=failed_attempt,
                        action_id=failed_action,
                        executor=verify_case.verifier_executor(**executor_args),
                    )
                retry_attempt = f"attempt-{label}-retry"
                verify_case.create_attempt(fixture, retry_attempt)
                with verify_case.supported_filesystem():
                    accepted = execute_verifier(
                        fixture.spec.run_id,
                        retry_attempt,
                        f"action-{label}-retry",
                        fixture.root,
                        fixture.result_ref,
                        verify_case.worker.actor_id,
                        verify_case.verifier,
                        verify_case.check_executor(),
                        verify_case.verifier_adapter(fixture),
                        retry_of=failed_action,
                        start=fixture.root,
                    )
                self.assertTrue(all(
                    item.passed for item in accepted.criterion_results))

    def promotion_engine_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "repo"
        root.mkdir()
        init_project(root)
        filesystem = mock.patch(
            "waystone.runs.store._probe_state_filesystem",
            return_value=FilesystemInfo("apfs", Path("/"), writable=True),
        )
        filesystem.start()
        self.addCleanup(filesystem.stop)
        store = RunStore.open(root)
        self.addCleanup(store.close)
        artifacts = ArtifactStore(root)
        run = store.create_run(initial_state="dispatch-ready")
        job_id = f"{run.run_id}:job"
        store.create_job(run.run_id, job_id, initial_state="running")
        attempt_id = f"{run.run_id}:attempt:1"
        store.create_attempt(run.run_id, job_id, attempt_id, initial_state="running")
        lineage = new_run_id()
        plan = self.plan(lineage, risk=False)
        plan_artifact = artifacts.write(plan.canonical_bytes())
        records = tuple(artifacts.write(value) for value in (
            b"regression", b"supported", b"risks",
        ))
        spec = SimpleNamespace(
            run_id=run.run_id,
            job_id=job_id,
            run_spec_digest=self.d(12),
            lifecycle_stage=LifecycleStage.PROMOTE,
            assurance_plan=SimpleNamespace(digest=plan_artifact.digest),
            frame_status_ref=SimpleNamespace(status="committed"),
            promotion_lineage=SimpleNamespace(id=lineage),
            candidate={
                "digest": self.d(30),
                "target_ref": "refs/waystone/candidates/reject",
                "target_oid": "a" * 40,
                "producer_result_digest": self.d(32),
            },
            evaluation={"evidence": {"digest": self.d(31)}},
            result_policy=SimpleNamespace(
                target_ref="refs/heads/main", expected_oid="b" * 40),
            retry=SimpleNamespace(max_attempts_per_job=2, max_total_attempts=2),
        )
        context = ProjectContext(
            "project:promote-reject", root, root, root / ".git",
            "canonical", root / ".waystone" / "state.db",
        )
        assembly = ProductionRunAssembly(
            context, None, {}, store, artifacts, None, None, None, None)
        return root, store, spec, plan, records, StagedRunEngine(assembly)

    def test_unpublished_accept_tuple_is_refused_before_target_apply(self):
        root, store, spec, plan, records, engine = self.promotion_engine_fixture()
        verifier = self.verifier(spec.run_id)
        decision = self.decision(
            spec.run_id,
            verifier,
            spec.candidate["digest"],
            spec.evaluation["evidence"]["digest"],
            None,
            outcome=DecisionOutcome.ACCEPT,
        )
        target = {"oid": spec.result_policy.expected_oid, "applied": False}

        def target_apply():
            target.update(oid=spec.candidate["target_oid"], applied=True)

        handlers = {
            "evaluated-candidate-freeze": lambda: spec.candidate["digest"],
            "independent-verify": lambda: verifier,
            "integration-decision": lambda: decision,
            "target-ref-apply": target_apply,
            "completion": lambda: decision.artifact_reference.digest,
        }
        with mock.patch(
                "waystone.runs.engine.load_run_spec", return_value=spec), \
                mock.patch.object(engine, "_candidate_lineage", return_value=()), \
                mock.patch("waystone.runs.engine.assert_promotion_unblocked"), \
                mock.patch(
                    "waystone.runs.engine.capture_result_snapshot",
                    return_value=SimpleNamespace(digest=self.d(70))), \
                mock.patch(
                    "waystone.runs.engine.git_full_sha",
                    side_effect=lambda _root, ref=None: (
                        spec.candidate["target_oid"]
                        if ref == spec.candidate["target_ref"] else target["oid"]
                    )):
            with self.assertRaisesRegex(
                    EngineBindingRefusal, "published|reload|authority"):
                engine._execute_stage(  # noqa: SLF001 - injection refusal contract
                    spec.run_id,
                    handlers,
                    regression_contract_digest=records[0].digest,
                    supported_scope_digest=records[1].digest,
                    accepted_risks_digest=records[2].digest,
                )
            with self.assertRaisesRegex(
                    EngineBindingRefusal, "published promotion authority"):
                engine._reload_promotion_authority(  # noqa: SLF001
                    spec, decision.attempt_id, plan)

        self.assertFalse(target["applied"])
        self.assertEqual(target["oid"], spec.result_policy.expected_oid)
        self.assertEqual(
            git(root, "rev-parse", "refs/heads/main").stdout.strip(),
            git(root, "rev-parse", "HEAD").stdout.strip(),
        )

    def test_promotion_authority_handlers_have_no_public_injection_surface(self):
        self.assertFalse(hasattr(StagedRunEngine, "execute_stage"))

    def test_semantic_rejection_retry_refuses_new_attempt(self):
        _root, store, spec, _plan, _records, engine = self.promotion_engine_fixture()
        verifier = self.verifier(spec.run_id)
        decision = self.decision(
            spec.run_id,
            verifier,
            spec.candidate["digest"],
            spec.evaluation["evidence"]["digest"],
            None,
            outcome=DecisionOutcome.REJECT,
        )
        attempt = store.get_entity(EntityKind.ATTEMPT, decision.attempt_id)
        store.record_transition(
            EntityKind.ATTEMPT,
            decision.attempt_id,
            expected_version=attempt.version,
            next_state="completed",
            reason=TransitionReason.COMPLETED,
            evidence_digest=decision.artifact_reference.digest,
        )
        for kind, identity in (
                (EntityKind.JOB, spec.job_id),
                (EntityKind.RUN, spec.run_id)):
            entity = (
                store.get_run(identity) if kind is EntityKind.RUN
                else store.get_entity(kind, identity)
            )
            store.record_transition(
                kind,
                identity,
                expected_version=entity.version,
                next_state="failed",
                reason=TransitionReason.PROCESS_FAILED,
                evidence_digest=decision.artifact_reference.digest,
            )
        with mock.patch(
                "waystone.runs.engine.load_run_spec", return_value=spec), \
                mock.patch.object(
                    engine, "_promotion_decision_for_attempt",
                    return_value=decision), \
                mock.patch("waystone.runs.engine.git_full_sha",
                           return_value=spec.result_policy.expected_oid), \
                mock.patch.object(engine, "_ensure_stage_started") as started, \
                self.assertRaisesRegex(
                    EngineBindingRefusal, "new candidate|owner ruling"):
            engine.retry_rejected_promotion(spec.run_id)

        started.assert_not_called()
        with store._connection_lock:  # noqa: SLF001
            attempts = store._connection.execute(  # noqa: SLF001
                "SELECT attempt_id FROM attempts WHERE run_id = ?",
                (spec.run_id,),
            ).fetchall()
        self.assertEqual(
            [row["attempt_id"] for row in attempts],
            [decision.attempt_id],
        )
        self.assertEqual(store.get_run(spec.run_id).state, "failed")
        self.assertEqual(store.get_entity(EntityKind.JOB, spec.job_id).state, "failed")

    def test_rejected_promotion_closes_no_delta_and_preserves_failed_state(self):
        fixture = test_run_outcome.OutcomeFixture(self)
        original, result = fixture.ready_run()
        faux_spec = SimpleNamespace(
            run_id=original.run_id,
            run_spec_digest=original.run_spec_digest,
            lifecycle_stage=LifecycleStage.PROMOTE,
            objective_ref=original.objective_ref,
            job_id=original.job_id,
            job_input=original.job_input,
            assurance_plan=original.assurance_plan,
        )
        verifier_artifact = fixture.assembly.artifact_store.write(b"rejected verifier")
        decision_artifact = fixture.assembly.artifact_store.write(b"reject decision")
        verifier = replace(
            self.verifier(original.run_id),
            artifact_reference=ArtifactReference(
                f"verifier-evidence:{original.run_id}:typed-independent-verify",
                ArtifactReferenceKind.EVIDENCE,
                verifier_artifact.digest,
                verifier_artifact.size,
            ),
        )
        decision = replace(
            self.decision(
                original.run_id, verifier, self.d(30), self.d(31), None,
                outcome=DecisionOutcome.REJECT),
            artifact_reference=ArtifactReference(
                f"integration-decision:{original.run_id}:integration-decision",
                ArtifactReferenceKind.DECISION,
                decision_artifact.digest,
                decision_artifact.size,
            ),
        )
        for kind, identity in (
                (EntityKind.JOB, original.job_id),
                (EntityKind.RUN, original.run_id)):
            entity = (
                fixture.assembly.store.get_run(identity)
                if kind is EntityKind.RUN else
                fixture.assembly.store.get_entity(kind, identity)
            )
            fixture.assembly.store.record_transition(
                kind,
                identity,
                expected_version=entity.version,
                next_state="failed",
                reason=TransitionReason.PROCESS_FAILED,
                evidence_digest=decision.artifact_reference.digest,
            )
        body = yaml.safe_load(fixture.outcome_bytes(original, result))
        body["lifecycle_stage"] = "promote"
        outcome = yaml.safe_dump(body, sort_keys=False).encode()

        with mock.patch(
                "waystone.runs.outcome.load_run_spec", return_value=faux_spec), \
                mock.patch(
                    "waystone.runs.outcome._rejected_promotion_decision",
                    return_value=decision):
            StagedRunEngine(fixture.assembly).close(original.run_id, outcome)

        self.assertEqual(
            fixture.assembly.store.get_run(original.run_id).state,
            "failed",
        )
        entry = read_outcome_ledger(fixture.root)[0]
        self.assertEqual(entry.outcome.kind, "no-objective-delta")
        references = {
            item["reference_id"]: item["digest"]
            for item in entry.closeout.completion_evidence_refs
        }
        self.assertEqual(
            references[decision.verifier_reference_id],
            decision.verifier_artifact_digest,
        )
        self.assertEqual(
            references[decision.artifact_reference.reference_id],
            decision.artifact_reference.digest,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
