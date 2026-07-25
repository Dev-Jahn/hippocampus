from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from test_work_brief import init_project
from waystone.cli import review_group
from waystone.features import review_layout
from waystone.project.context import resolve_project_context
from waystone.reviews import findings
from waystone.runs.artifacts import ArtifactStore


class FindingChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.head, self.frame = init_project(self.root)
        self.reviews = self.root / "docs/reviews"
        self.run_id = review_layout.new_run_id()
        self.finding_id = review_layout.new_run_id()
        self.lineage_id = review_layout.new_run_id()
        self.evidence = ArtifactStore(self.root).write(b"reproduced code observation")
        self.machine = self.root / "machine"
        self.machine.mkdir()
        self.registry = self.machine / "projects.json"
        self.registry.write_text(json.dumps({"projects": [{
            "project_id": "project:review-findings",
            "name": "demo",
            "path": str(self.root.resolve()),
        }]}), encoding="utf-8")
        self.context = resolve_project_context(self.root, registry=self.registry)

    def tearDown(self):
        self.tmp.cleanup()

    def d(self, value: int) -> str:
        token = format(value, "x")
        return "sha256:" + (token * 64)[:64]

    def claim_payload(self):
        return {
            "schema": findings.CLAIM_SCHEMA, "finding_id": self.finding_id,
            "review_run_id": self.run_id,
            "target": {"run_spec_digest": self.d(1), "result_digest": self.d(2),
                        "review_artifact_digest": self.d(3)},
            "source_finding_id": "WS-GPT-001",
            "claim": "The bound result permits the failure mechanism.",
            "evidence": ["code observation"],
            "reviewer_assessment": {"impact": "major", "suggested_remediation": "repair local"},
            "reported_by": {"role": "reviewer", "binding_digest": self.d(4), "principal": None},
        }

    def validation_payload(self, claim_digest, **changes):
        row = {
            "schema": findings.VALIDATION_SCHEMA, "finding_id": self.finding_id,
            "finding_digest": claim_digest, "revision": 1, "supersedes_digest": None,
            "validity": "confirmed", "failure_mechanism": "X breaks because Y is reachable",
            "evidence_refs": [{"kind": "code", "digest": self.evidence.digest}],
            "validated_by": {"role": "coordinator", "binding_digest": self.d(6), "principal": None},
        }
        row.update(changes)
        return row

    def disposition_payload(self, claim_digest, validation_digest, **changes):
        row = {
            "schema": findings.DISPOSITION_SCHEMA, "finding_id": self.finding_id,
            "finding_digest": claim_digest, "confirmed_validation_digest": validation_digest,
            "revision": 1, "supersedes_digest": None,
            "objective_ref": self.frame.fact_ref("commitment/outcome").to_dict(),
            "lifecycle_stage": "explore",
            "applies_to": {"promotion_lineage_id": self.lineage_id,
                            "candidate_digest": self.d(8), "result_digest": self.d(9)},
            "impact": "major", "exposure": "edge", "relevance": "current-objective",
            "disposition": "fix-now", "remediation_scope": "local", "estimated_cost": "low",
            "rationale": "test ruling", "clearance": None,
            "decided_by": {"role": "coordinator", "binding_digest": self.d(10), "principal": None},
            "materialized_task_id": None,
        }
        row.update(changes)
        return row

    def commit(self, message: str, *paths: str) -> None:
        subprocess.run(["git", "add", *paths], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", message], cwd=self.root, check=True)

    def test_immutable_claim_and_digest_bound_validation(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        with self.assertRaises(findings.ImmutableArtifactConflict):
            findings.write_claim(self.reviews, dict(self.claim_payload(), claim="changed"))
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id, self.validation_payload(claim.digest),
            root=self.root)
        self.assertEqual(findings.validation_head(
            self.reviews, self.run_id, self.finding_id).digest, validation.digest)
        with self.assertRaises(findings.ChainConflict):
            findings.append_validation(
                self.reviews, self.run_id, self.finding_id,
                self.validation_payload(self.d(99), revision=2, supersedes_digest=validation.digest),
                root=self.root)

    def test_divergent_heads_are_typed_conflict(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        first = findings.append_validation(
            self.reviews, self.run_id, self.finding_id, self.validation_payload(claim.digest),
            root=self.root)
        second = self.validation_payload(claim.digest, revision=2,
                                          supersedes_digest=first.digest,
                                          failure_mechanism="second head")
        review_layout.publish_finding_yaml(
            self.reviews, self.run_id, self.finding_id, review_layout.FINDING_VALIDATION, 2,
            findings.canonical_bytes(second))
        third = dict(second, failure_mechanism="third head")
        review_layout.publish_finding_yaml(
            self.reviews, self.run_id, self.finding_id, review_layout.FINDING_VALIDATION, 3,
            findings.canonical_bytes(third))
        with self.assertRaises(findings.DivergentHeadConflict):
            findings.validation_head(self.reviews, self.run_id, self.finding_id)

    def test_confirmed_major_accept_risk_does_not_materialize(self):
        (self.root / ".waystone.yml").write_text("version: 1\nproject: test\n")
        (self.root / "tasks.yaml").write_text("version: 1\nproject: test\ntasks: []\n")
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id, self.validation_payload(claim.digest),
            root=self.root)
        findings.append_disposition(
            self.reviews, self.run_id, self.finding_id,
            self.disposition_payload(claim.digest, validation.digest,
                                      disposition="accept-risk", relevance="future"),
            root=self.root)
        with self.assertRaises(review_group.MaterializationRefused):
            review_group.materialize(self.context, self.run_id, self.finding_id)

    def test_q3_owner_only_boundaries(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id, self.validation_payload(claim.digest),
            root=self.root)
        for changes in ({"disposition": "accept-risk"}, {"remediation_scope": "architectural"}):
            with self.subTest(changes=changes), self.assertRaises(findings.OwnerDecisionRequired):
                findings.append_disposition(
                    self.reviews, self.run_id, self.finding_id,
                    self.disposition_payload(claim.digest, validation.digest, **changes),
                    root=self.root)

    def test_promotion_clearance_is_structured_and_stale_disposition_cannot_materialize(self):
        (self.root / ".waystone.yml").write_text("version: 1\nproject: test\n")
        (self.root / "tasks.yaml").write_text("version: 1\nproject: test\ntasks: []\n")
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id, self.validation_payload(claim.digest),
            root=self.root)
        initial = self.disposition_payload(
            claim.digest, validation.digest, disposition="fix-before-promotion",
            relevance="promotion-bound")
        first_disposition = findings.append_disposition(
            self.reviews, self.run_id, self.finding_id, initial, root=self.root)
        cleared = dict(initial)
        cleared.update({
            "revision": 2, "supersedes_digest": first_disposition.digest,
            "clearance": {"candidate_digest": self.d(14),
                           "supersedes_candidate_digest": self.d(15),
                           "verification_evidence_digest": self.d(16)},
        })
        findings.append_disposition(
            self.reviews, self.run_id, self.finding_id, cleared, root=self.root)
        newer = self.validation_payload(
            claim.digest, revision=2, supersedes_digest=validation.digest,
            failure_mechanism="new validation head")
        findings.append_validation(
            self.reviews, self.run_id, self.finding_id, newer, root=self.root)
        with self.assertRaises(findings.StaleDisposition):
            review_group.materialize(self.context, self.run_id, self.finding_id)

    def test_ingest_validate_disposition_and_materialize(self):
        (self.root / ".waystone.yml").write_text("version: 1\nproject: test\n")
        (self.root / "tasks.yaml").write_text("version: 1\nproject: test\ntasks: []\n")
        source = self.root / "feedback.yaml"
        source.write_bytes(yaml.safe_dump({
            "target": {"run_spec_digest": self.d(11), "result_digest": self.d(12)},
            "binding_digest": self.d(13),
            "findings": [{"source_finding_id": "WS-GPT-002",
                          "claim": "A confirmed failure mechanism", "evidence": ["reproduction"],
                          "impact": "major"}],
        }).encode())
        claim = review_group.ingest_feedback(self.context, self.run_id, source)[0]
        self.finding_id = claim.payload["finding_id"]
        validation_file = self.root / "validation.yaml"
        validation_file.write_bytes(findings.canonical_bytes(self.validation_payload(claim.digest)))
        validation = review_group.validate_file(
            self.context, self.run_id, claim.payload["finding_id"], validation_file)
        disposition_file = self.root / "disposition.yaml"
        disposition_file.write_bytes(findings.canonical_bytes(
            self.disposition_payload(claim.digest, validation.digest,
                                     disposition="fix-before-promotion", relevance="promotion-bound")))
        review_group.disposition_file(
            self.context, self.run_id, claim.payload["finding_id"], disposition_file)
        task_id = review_group.materialize(
            self.context, self.run_id, claim.payload["finding_id"])
        registry = yaml.safe_load((self.root / "tasks.yaml").read_text())
        self.assertEqual(registry["tasks"][0]["id"], task_id)
        head = findings.disposition_head(self.reviews, self.run_id, claim.payload["finding_id"])
        self.assertEqual(head.payload["materialized_task_id"], task_id)

    def test_disposition_refuses_nonexistent_commit_before_append(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id,
            self.validation_payload(claim.digest), root=self.root)
        payload = self.disposition_payload(claim.digest, validation.digest)
        payload["objective_ref"] = dict(payload["objective_ref"], commit="a" * 40)

        with self.assertRaises(findings.AuthorityValidationRefusal):
            findings.append_disposition(
                self.reviews, self.run_id, self.finding_id, payload, root=self.root)
        self.assertIsNone(findings.disposition_head(
            self.reviews, self.run_id, self.finding_id))

    def test_disposition_objective_ref_requires_project_fact_variant(self):
        payload = self.disposition_payload(self.d(30), self.d(31))
        payload["objective_ref"] = {
            "kind": "owner-request",
            "artifact_reference_id": "owner-request:test",
            "digest": self.evidence.digest,
            "binding": "binding",
        }

        with self.assertRaises(findings.ArtifactValidationError):
            findings.validate_disposition(payload)

    def test_disposition_refuses_missing_fact_before_append(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id,
            self.validation_payload(claim.digest), root=self.root)
        payload = self.disposition_payload(claim.digest, validation.digest)
        payload["objective_ref"] = dict(
            payload["objective_ref"], fact_id="commitment/not-present")

        with self.assertRaises(findings.AuthorityValidationRefusal):
            findings.append_disposition(
                self.reviews, self.run_id, self.finding_id, payload, root=self.root)
        self.assertIsNone(findings.disposition_head(
            self.reviews, self.run_id, self.finding_id))

    def test_disposition_refuses_stale_fact_digest_after_brief_revision(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id,
            self.validation_payload(claim.digest), root=self.root)
        old_ref = self.frame.fact_ref("commitment/outcome")
        brief_path = self.root / "PROJECT_BRIEF.md"
        brief_path.write_bytes(brief_path.read_bytes().replace(
            b"Produce the intended result.", b"Produce the revised intended result."))
        subprocess.run(["git", "add", "PROJECT_BRIEF.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "revise brief"], cwd=self.root, check=True)
        revised_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, check=True,
            stdout=subprocess.PIPE, text=True,
        ).stdout.strip()
        payload = self.disposition_payload(claim.digest, validation.digest)
        payload["objective_ref"] = dict(old_ref.to_dict(), commit=revised_head)

        with self.assertRaises(findings.AuthorityValidationRefusal):
            findings.append_disposition(
                self.reviews, self.run_id, self.finding_id, payload, root=self.root)
        self.assertIsNone(findings.disposition_head(
            self.reviews, self.run_id, self.finding_id))

    def test_disposition_refuses_superseded_objective_before_append(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id,
            self.validation_payload(claim.digest), root=self.root)
        old_ref = self.frame.fact_ref("commitment/outcome")
        brief_path = self.root / "PROJECT_BRIEF.md"
        brief_path.write_bytes(brief_path.read_bytes().replace(
            b"Produce the intended result.", b"Produce the realigned intended result."))
        self.commit("realign brief", "PROJECT_BRIEF.md")
        payload = self.disposition_payload(claim.digest, validation.digest)
        payload["objective_ref"] = old_ref.to_dict()

        with self.assertRaises(findings.FindingError) as raised:
            findings.append_disposition(
                self.reviews, self.run_id, self.finding_id, payload, root=self.root)

        self.assertEqual(raised.exception.code, "objective-superseded")
        self.assertIsNone(findings.disposition_head(
            self.reviews, self.run_id, self.finding_id))

    def test_materialize_refuses_disposition_after_objective_is_superseded(self):
        (self.root / ".waystone.yml").write_text("version: 1\nproject: test\n")
        (self.root / "tasks.yaml").write_text("version: 1\nproject: test\ntasks: []\n")
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id,
            self.validation_payload(claim.digest), root=self.root)
        findings.append_disposition(
            self.reviews, self.run_id, self.finding_id,
            self.disposition_payload(claim.digest, validation.digest), root=self.root)
        brief_path = self.root / "PROJECT_BRIEF.md"
        brief_path.write_bytes(brief_path.read_bytes().replace(
            b"Produce the intended result.", b"Produce the realigned intended result."))
        self.commit("realign brief", "PROJECT_BRIEF.md")

        with self.assertRaises(findings.FindingError) as raised:
            review_group.materialize(self.context, self.run_id, self.finding_id)

        self.assertEqual(raised.exception.code, "objective-superseded")
        self.assertEqual(yaml.safe_load((self.root / "tasks.yaml").read_text())["tasks"], [])

    def test_disposition_accepts_unchanged_objective_after_unrelated_commit(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id,
            self.validation_payload(claim.digest), root=self.root)
        old_ref = self.frame.fact_ref("commitment/outcome")
        (self.root / "unrelated.txt").write_text("unrelated\n")
        self.commit("unrelated change", "unrelated.txt")
        payload = self.disposition_payload(claim.digest, validation.digest)
        payload["objective_ref"] = old_ref.to_dict()

        disposition = findings.append_disposition(
            self.reviews, self.run_id, self.finding_id, payload, root=self.root)

        self.assertEqual(disposition.payload["objective_ref"], old_ref.to_dict())

    def test_disposition_refuses_objective_deleted_from_current_brief(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id,
            self.validation_payload(claim.digest), root=self.root)
        old_ref = self.frame.fact_ref("commitment/outcome")
        brief_path = self.root / "PROJECT_BRIEF.md"
        brief_path.write_bytes(brief_path.read_bytes().replace(
            b"- [commitment/outcome] Produce the intended result.\n",
            b"- [commitment/replacement] Produce the replacement result.\n"))
        self.commit("replace objective", "PROJECT_BRIEF.md")
        payload = self.disposition_payload(claim.digest, validation.digest)
        payload["objective_ref"] = old_ref.to_dict()

        with self.assertRaises(findings.FindingError) as raised:
            findings.append_disposition(
                self.reviews, self.run_id, self.finding_id, payload, root=self.root)

        self.assertEqual(raised.exception.code, "objective-superseded")

    def test_disposition_refuses_objective_binding_changed_in_current_brief(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id,
            self.validation_payload(claim.digest), root=self.root)
        old_ref = self.frame.fact_ref("commitment/outcome")
        brief_path = self.root / "PROJECT_BRIEF.md"
        brief_path.write_bytes(brief_path.read_bytes().replace(
            b"status: committed", b"status: provisional"))
        self.commit("make brief provisional", "PROJECT_BRIEF.md")
        payload = self.disposition_payload(claim.digest, validation.digest)
        payload["objective_ref"] = old_ref.to_dict()

        with self.assertRaises(findings.FindingError) as raised:
            findings.append_disposition(
                self.reviews, self.run_id, self.finding_id, payload, root=self.root)

        self.assertEqual(raised.exception.code, "objective-superseded")

    def test_disposition_refuses_objective_from_non_ancestor_commit(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id,
            self.validation_payload(claim.digest), root=self.root)
        non_ancestor = subprocess.run(
            ["git", "commit-tree", f"{self.head}^{{tree}}", "-m", "abandoned root"],
            cwd=self.root, check=True, stdout=subprocess.PIPE, text=True,
        ).stdout.strip()
        payload = self.disposition_payload(claim.digest, validation.digest)
        payload["objective_ref"] = dict(
            self.frame.fact_ref("commitment/outcome").to_dict(), commit=non_ancestor)

        with self.assertRaises(findings.FindingError) as raised:
            findings.append_disposition(
                self.reviews, self.run_id, self.finding_id, payload, root=self.root)

        self.assertEqual(raised.exception.code, "objective-superseded")

    def test_disposition_refuses_fact_binding_mismatch_before_append(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id,
            self.validation_payload(claim.digest), root=self.root)
        payload = self.disposition_payload(claim.digest, validation.digest)
        payload["objective_ref"] = dict(
            payload["objective_ref"], binding="nonbinding")

        with self.assertRaises(findings.AuthorityValidationRefusal):
            findings.append_disposition(
                self.reviews, self.run_id, self.finding_id, payload, root=self.root)
        self.assertIsNone(findings.disposition_head(
            self.reviews, self.run_id, self.finding_id))

    def test_validation_refuses_missing_cas_evidence_before_append(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        payload = self.validation_payload(
            claim.digest, evidence_refs=[{"kind": "code", "digest": self.d(99)}])

        with self.assertRaises(findings.AuthorityValidationRefusal):
            findings.append_validation(
                self.reviews, self.run_id, self.finding_id, payload, root=self.root)
        self.assertIsNone(findings.validation_head(
            self.reviews, self.run_id, self.finding_id))

    def test_validation_resolves_git_evidence_through_typed_authority_ref(self):
        adr_path = self.root / "docs/adr/0001-test.md"
        adr_path.parent.mkdir(parents=True)
        adr_bytes = b"# ADR 0001\n\nStatus: accepted\n"
        adr_path.write_bytes(adr_bytes)
        subprocess.run(["git", "add", "docs/adr/0001-test.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "add accepted ADR"], cwd=self.root, check=True)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, check=True,
            stdout=subprocess.PIPE, text=True,
        ).stdout.strip()
        claim = findings.write_claim(self.reviews, self.claim_payload())
        payload = self.validation_payload(claim.digest, evidence_refs=[{
            "kind": "accepted-adr",
            "commit": commit,
            "path": "docs/adr/0001-test.md",
            "decision_id": "ADR-0001",
            "digest": findings.artifact_digest(adr_bytes),
        }])

        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id, payload, root=self.root)

        self.assertEqual(findings.validation_head(
            self.reviews, self.run_id, self.finding_id).digest, validation.digest)

    def test_real_cas_evidence_and_exact_project_fact_are_accepted(self):
        claim = findings.write_claim(self.reviews, self.claim_payload())
        validation = findings.append_validation(
            self.reviews, self.run_id, self.finding_id,
            self.validation_payload(claim.digest), root=self.root)
        disposition = findings.append_disposition(
            self.reviews, self.run_id, self.finding_id,
            self.disposition_payload(claim.digest, validation.digest), root=self.root)

        self.assertEqual(
            findings.validation_head(self.reviews, self.run_id, self.finding_id).digest,
            validation.digest)
        self.assertEqual(
            findings.disposition_head(self.reviews, self.run_id, self.finding_id).digest,
            disposition.digest)


if __name__ == "__main__":
    unittest.main()
