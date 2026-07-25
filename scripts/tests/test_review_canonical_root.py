#!/usr/bin/env python3
"""Canonical ProjectContext authority contracts for the public review surface."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import contextlib
import io
import json
from types import SimpleNamespace
from unittest import mock

from test_work_brief import init_project
from waystone.cli import review_group
from waystone.features import review_layout
from waystone.project import tasks_cli
from waystone.project.brief import read_project_frame_at_commit
from waystone.project.context import (
    CanonicalRootIsLinkedWorktree,
    resolve_project_context,
)
from waystone.reviews import findings
from waystone.runs.artifacts import ArtifactStore


class ReviewCanonicalRootTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        _head, self.frame = init_project(self.root)
        (self.root / "tasks.yaml").write_text(
            "version: 1\nproject: demo\ntasks: []\n", encoding="utf-8")
        self.reviews = self.root / "docs/reviews"
        self.run_id = review_layout.new_run_id()
        self.finding_id = review_layout.new_run_id()
        self.lineage_id = review_layout.new_run_id()
        claim = findings.write_claim(self.reviews, self._claim_payload())
        validation = findings.append_validation(
            self.reviews,
            self.run_id,
            self.finding_id,
            self._validation_payload(claim.digest),
            root=self.root,
        )
        disposition = findings.append_disposition(
            self.reviews,
            self.run_id,
            self.finding_id,
            self._disposition_payload(claim.digest, validation.digest),
            root=self.root,
        )
        self.disposition_digest = disposition.digest
        git(self.root, "add", "tasks.yaml", "docs/reviews")
        self.assertEqual(git(self.root, "commit", "-qm", "record review finding").returncode, 0)

        self.linked = self.base / "linked"
        self.assertEqual(
            git(
                self.root,
                "worktree",
                "add",
                "-q",
                "-b",
                "stale-review",
                str(self.linked),
            ).returncode,
            0,
        )
        brief_path = self.root / "PROJECT_BRIEF.md"
        brief_path.write_bytes(brief_path.read_bytes().replace(
            b"Produce the intended result.",
            b"Produce the canonical revised result.",
        ))
        git(self.root, "add", "PROJECT_BRIEF.md")
        self.assertEqual(git(self.root, "commit", "-qm", "revise canonical objective").returncode, 0)

        self.machine = self.base / "machine"
        self.machine.mkdir()
        self.machine.joinpath("projects.json").write_text(json.dumps({"projects": [{
            "project_id": "project:review-canonical-root",
            "name": "demo",
            "path": str(self.root.resolve()),
        }]}), encoding="utf-8")
        self.registry = self.machine / "projects.json"
        self.context = resolve_project_context(self.root, registry=self.registry)
        self.linked_context = resolve_project_context(self.linked, registry=self.registry)
        self.next_disposition = self.base / "next-disposition.yaml"
        self.next_disposition.write_bytes(findings.canonical_bytes(self._disposition_payload(
            claim.digest,
            validation.digest,
            revision=2,
            supersedes_digest=self.disposition_digest,
        )))

    @staticmethod
    def _digest(value: int) -> str:
        token = format(value, "x")
        return "sha256:" + (token * 64)[:64]

    def _claim_payload(self) -> dict:
        return {
            "schema": findings.CLAIM_SCHEMA,
            "finding_id": self.finding_id,
            "review_run_id": self.run_id,
            "target": {
                "run_spec_digest": self._digest(1),
                "result_digest": self._digest(2),
                "review_artifact_digest": self._digest(3),
            },
            "source_finding_id": "WS-GPT-028",
            "claim": "A linked checkout can reuse stale project authority.",
            "evidence": ["linked worktree topology"],
            "reviewer_assessment": {
                "impact": "major",
                "suggested_remediation": "resolve canonical context first",
            },
            "reported_by": {
                "role": "reviewer",
                "binding_digest": self._digest(4),
                "principal": None,
            },
        }

    def _validation_payload(self, claim_digest: str) -> dict:
        return {
            "schema": findings.VALIDATION_SCHEMA,
            "finding_id": self.finding_id,
            "finding_digest": claim_digest,
            "revision": 1,
            "supersedes_digest": None,
            "validity": "confirmed",
            "failure_mechanism": "The public review root follows the linked checkout HEAD.",
            "evidence_refs": [self.frame.fact_ref("commitment/outcome").to_dict()],
            "validated_by": {
                "role": "coordinator",
                "binding_digest": self._digest(5),
                "principal": None,
            },
        }

    def _disposition_payload(
            self,
            claim_digest: str,
            validation_digest: str,
            **changes,
    ) -> dict:
        row = {
            "schema": findings.DISPOSITION_SCHEMA,
            "finding_id": self.finding_id,
            "finding_digest": claim_digest,
            "confirmed_validation_digest": validation_digest,
            "revision": 1,
            "supersedes_digest": None,
            "objective_ref": self.frame.fact_ref("commitment/outcome").to_dict(),
            "lifecycle_stage": "explore",
            "applies_to": {
                "promotion_lineage_id": self.lineage_id,
                "candidate_digest": self._digest(6),
                "result_digest": self._digest(7),
            },
            "impact": "major",
            "exposure": "edge",
            "relevance": "current-objective",
            "disposition": "fix-now",
            "remediation_scope": "local",
            "estimated_cost": "low",
            "rationale": "repair the canonical review authority path",
            "clearance": None,
            "decided_by": {
                "role": "coordinator",
                "binding_digest": self._digest(8),
                "principal": None,
            },
            "materialized_task_id": None,
        }
        row.update(changes)
        return row

    @staticmethod
    def _tracked_review_bytes(root: Path) -> dict[str, bytes]:
        paths = [root / "tasks.yaml"]
        reviews = root / "docs/reviews"
        if reviews.is_dir():
            paths.extend(path for path in reviews.rglob("*") if path.is_file())
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(paths)
        }

    @contextlib.contextmanager
    def _runtime(self, cwd: Path):
        old = Path.cwd()
        stderr = io.StringIO()
        try:
            os.chdir(cwd)
            with mock.patch.dict(os.environ, {"WAYSTONE_HOME": str(self.machine)}), \
                    contextlib.redirect_stderr(stderr):
                yield stderr
        finally:
            os.chdir(old)

    def _assert_linked_refusal(self, argv: list[str], *, cwd: Path | None = None) -> None:
        before = {
            "canonical": self._tracked_review_bytes(self.root),
            "linked": self._tracked_review_bytes(self.linked),
        }
        with self._runtime(self.linked if cwd is None else cwd) as stderr:
            result = review_group.main(argv)
        self.assertEqual(result, 1)
        self.assertIn("canonical_root_is_linked_worktree", stderr.getvalue())
        self.assertEqual(self._tracked_review_bytes(self.root), before["canonical"])
        self.assertEqual(self._tracked_review_bytes(self.linked), before["linked"])

    def test_p1_review_cli_uses_one_project_context_front_door(self):
        source = Path(review_group.__file__).read_text(encoding="utf-8")
        self.assertIn("resolve_project_context", source)
        self.assertNotIn("find_project_root", source)
        self.assertNotIn("Path(value).resolve()", source)
        self.assertEqual(source.count("context = _review_context("), 2)
        commands = (
            ["ingest", self.run_id, "--file", str(self.next_disposition)],
            [
                "validate", self.finding_id, "--run-id", self.run_id,
                "--file", str(self.next_disposition),
            ],
            [
                "disposition", self.finding_id, "--run-id", self.run_id,
                "--file", str(self.next_disposition),
            ],
            ["materialize", self.finding_id, "--run-id", self.run_id],
            ["attach", self.run_id, review_layout.new_run_id()],
        )
        for argv in commands:
            with self.subTest(command=argv[0]):
                self._assert_linked_refusal(argv)

    def test_p2_linked_cwd_refuses_stale_disposition_and_materialize_without_writes(self):
        self._assert_linked_refusal([
            "disposition",
            self.finding_id,
            "--run-id",
            self.run_id,
            "--file",
            str(self.next_disposition),
        ])
        self._assert_linked_refusal([
            "materialize",
            self.finding_id,
            "--run-id",
            self.run_id,
        ])

    def test_p3_explicit_linked_root_is_a_typed_refusal(self):
        self._assert_linked_refusal([
            "materialize",
            self.finding_id,
            "--run-id",
            self.run_id,
            "--root",
            str(self.linked),
        ], cwd=self.root)

    def test_p4_materialize_requires_canonical_project_context_proof(self):
        before = self._tracked_review_bytes(self.linked)
        with mock.patch.object(tasks_cli, "cmd_add") as cmd_add, \
                self.assertRaises(review_group.MaterializationRefused):
            review_group.materialize(self.linked, self.run_id, self.finding_id)
        cmd_add.assert_not_called()
        self.assertEqual(self._tracked_review_bytes(self.linked), before)

    def test_p5_ingest_programmatic_api_requires_canonical_context(self):
        run_id = review_layout.new_run_id()
        source = self.base / "programmatic-feedback.yaml"
        source.write_text(json.dumps({
            "target": {
                "run_spec_digest": self._digest(11),
                "result_digest": self._digest(12),
            },
            "binding_digest": self._digest(13),
            "findings": [{
                "source_finding_id": "WS-GPT-032",
                "claim": "Programmatic review mutation requires canonical proof.",
                "evidence": ["linked worktree context"],
                "impact": "minor",
            }],
        }), encoding="utf-8")

        with self.assertRaises(CanonicalRootIsLinkedWorktree):
            review_group.ingest_feedback(self.linked_context, run_id, source)
        claims = review_group.ingest_feedback(self.context, run_id, source)
        self.assertEqual(len(claims), 1)

    def test_p5_validate_programmatic_api_requires_canonical_context(self):
        payload = self._validation_payload(
            findings.read_claim(self.reviews, self.run_id, self.finding_id).digest)
        payload.update({
            "revision": 2,
            "supersedes_digest": findings.validation_head(
                self.reviews, self.run_id, self.finding_id).digest,
            "failure_mechanism": "Canonical ProjectContext proof closes the programmatic path.",
        })
        source = self.base / "programmatic-validation.yaml"
        source.write_bytes(findings.canonical_bytes(payload))

        with self.assertRaises(CanonicalRootIsLinkedWorktree):
            review_group.validate_file(
                self.linked_context, self.run_id, self.finding_id, source)
        validation = review_group.validate_file(
            self.context, self.run_id, self.finding_id, source)
        self.assertEqual(validation.payload["revision"], 2)

    def test_p5_disposition_programmatic_api_requires_canonical_context(self):
        current_head = git(self.root, "rev-parse", "HEAD").stdout.strip()
        current_frame = read_project_frame_at_commit(self.root, current_head)
        payload = findings.parse_artifact(
            self.next_disposition.read_bytes(), findings.DISPOSITION_SCHEMA)
        payload["objective_ref"] = current_frame.fact_ref("commitment/outcome").to_dict()
        source = self.base / "programmatic-disposition.yaml"
        source.write_bytes(findings.canonical_bytes(payload))

        with self.assertRaises(CanonicalRootIsLinkedWorktree):
            review_group.disposition_file(
                self.linked_context, self.run_id, self.finding_id, source)
        disposition = review_group.disposition_file(
            self.context, self.run_id, self.finding_id, source)
        self.assertEqual(disposition.payload["revision"], 2)

    def test_p5_attach_programmatic_api_requires_canonical_context(self):
        review_run_id = review_layout.new_run_id()
        promotion_run_id = review_layout.new_run_id()
        binding_digest = self._digest(21)
        run_spec_digest = self._digest(22)
        result_digest = self._digest(23)
        candidate_digest = self._digest(24)
        review_layout.publish_markdown(
            self.reviews,
            review_run_id,
            review_layout.FEEDBACK,
            json.dumps({
                "target": {
                    "run_spec_digest": run_spec_digest,
                    "result_digest": result_digest,
                },
                "binding_digest": binding_digest,
                "reported_by": {
                    "role": "reviewer",
                    "binding_digest": binding_digest,
                    "principal": None,
                },
                "findings": [],
            }).encode("utf-8"),
        )
        spec = SimpleNamespace(
            lifecycle_stage=SimpleNamespace(value="promote"),
            promotion_lineage=SimpleNamespace(id=self.lineage_id),
            candidate={
                "digest": candidate_digest,
                "producer_result_digest": result_digest,
            },
            run_spec_digest=run_spec_digest,
        )
        profile = mock.Mock()
        profile.binding_for.return_value = SimpleNamespace(binding_digest=binding_digest)
        assembly = SimpleNamespace(
            context=self.context,
            profile=profile,
            artifact_store=ArtifactStore(self.root),
        )

        with self.assertRaises(CanonicalRootIsLinkedWorktree):
            review_group.attach_review(
                self.linked_context, promotion_run_id, review_run_id)
        with mock.patch(
                "waystone.jobs.profile.assemble_run",
                return_value=contextlib.nullcontext(assembly)), \
                mock.patch("waystone.runs.spec.load_run_spec", return_value=spec), \
                mock.patch("waystone.runs.engine.StagedRunEngine") as engine:
            expected = object()
            engine.return_value.append_review_cycle.return_value = expected
            result = review_group.attach_review(
                self.context, promotion_run_id, review_run_id)
        self.assertIs(result, expected)

    def test_p6_programmatic_apis_refuse_raw_path_without_project_context_proof(self):
        calls = (
            lambda: review_group.ingest_feedback(
                self.root, self.run_id, self.next_disposition),
            lambda: review_group.validate_file(
                self.root, self.run_id, self.finding_id, self.next_disposition),
            lambda: review_group.disposition_file(
                self.root, self.run_id, self.finding_id, self.next_disposition),
            lambda: review_group.attach_review(
                self.root, self.run_id, review_layout.new_run_id()),
        )
        for call in calls:
            with self.subTest(api=call), \
                    self.assertRaises(review_group.ReviewContextRequired):
                call()


if __name__ == "__main__":
    unittest.main(verbosity=2)
