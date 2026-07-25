#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Focused production run CLI ingress and ProjectContext ordering contracts."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from unittest import mock

import yaml

from test_work_brief import init_project, item, payload
from waystone.cli import review_group, run_group
from waystone.features.review_layout import new_run_id
from waystone.jobs import completion, work_brief
from waystone.jobs.completion import LifecycleStage
from waystone.jobs.domain import Role
from waystone.jobs.profile import assemble_run, read_profile
from waystone.jobs.run_scaffold import (
    RunScaffoldRefusal,
    scaffold_outcome_delta,
    scaffold_work_brief,
)
from waystone.project.brief import read_project_frame_at_commit
from waystone.project.context import resolve_project_context
from waystone.runs.artifacts import ArtifactStore
from waystone.runs.engine import EngineBindingRefusal, StagedRunEngine
from waystone.runs.outcome import read_outcome_ledger
from waystone.runs.preflight import EnvironmentPreparationUnavailableError
from waystone.runs.spec import load_run_spec
from waystone.runs.store import (
    EngineOwnedPathUnverifiableError, EntityKind, FilesystemInfo, RunStore)
from waystone.runs.supervisor import RunnerCandidateContext
from waystone.runs.transport import ActionPlanRefusal


class RunCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.head, self.frame = init_project(self.root)
        (self.root / "tasks.yaml").write_text(
            "version: 1\nproject: demo\ntasks:\n"
            "  - id: feat/semantic-brief\n"
            "    title: Compare candidate approaches\n"
            "    status: pending\n"
            "    scope: [src.py]\n"
            "    deps: []\n",
            encoding="utf-8",
        )
        git(self.root, "add", "tasks.yaml")
        self.assertEqual(git(self.root, "commit", "-qm", "task").returncode, 0)
        state = self.root / ".waystone"
        state.mkdir()
        state.joinpath("profile.yml").write_text(
            "schema: waystone-profile-2\nbindings:\n"
            "  coordinator: {execution: in-session, backend: 'host:current'}\n"
            "  worker: {execution: external, backend: 'codex:worker'}\n"
            "  verifier: {execution: external, backend: 'codex:verifier'}\n"
            "  reviewer: {execution: external, backend: 'codex:reviewer'}\n",
            encoding="utf-8",
        )
        self.machine = self.base / "machine"
        self.machine.mkdir()
        self.machine.joinpath("projects.json").write_text(json.dumps({"projects": [{
            "project_id": "project:run-cli",
            "name": "demo",
            "path": str(self.root.resolve()),
        }]}), encoding="utf-8")
        self.context = resolve_project_context(
            self.root, registry=self.machine / "projects.json")
        self.brief_path = self.base / "work-brief.json"
        self.brief_path.write_bytes(completion.canonical_json(
            payload(self.head, self.frame, new_run_id())))

    def install_fixture_codex(self) -> Path:
        binary = self.base / "bin" / "codex"
        binary.parent.mkdir()
        binary.write_text(
            f"#!{sys.executable}\n"
            "import json, subprocess, sys\n"
            "from pathlib import Path\n"
            "if Path(__file__).with_name('fixture-codex-http-400').exists():\n"
            "  sys.stderr.write(\"HTTP 400: invalid_json_schema for attempt_id\\n\")\n"
            "  raise SystemExit(1)\n"
            "args = sys.argv[1:]\n"
            "schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())\n"
            "properties = schema['properties']\n"
            "assert properties['attempt_id']['type'] == 'string'\n"
            "promotion = 'criterion_results' in properties\n"
            "summary = None if promotion else properties['result_summary']['anyOf'][0]\n"
            "evaluation = promotion or 'enum' in summary\n"
            "if not evaluation:\n"
            "  Path('candidate.txt').write_text('fixture candidate\\n', encoding='utf-8')\n"
            "  paths = ['candidate.txt']\n"
            "  for verdict_path in ('evaluate-verdict.txt', 'promote-verdict.txt'):\n"
            "    if Path(verdict_path).exists():\n"
            "      paths.append(verdict_path)\n"
            "  subprocess.run(['git', 'add', *paths], check=True)\n"
            "  subprocess.run(['git', 'commit', '-qm', 'fixture candidate'], check=True)\n"
            "else:\n"
            "  verdict_path = 'promote-verdict.txt' if promotion else 'evaluate-verdict.txt'\n"
            "  verdict = Path(verdict_path).read_text(encoding='utf-8').strip()\n"
            "  assert verdict in {'pass', 'fail'}\n"
            "if promotion:\n"
            "  result = {\n"
            "    'schema': properties['schema']['const'],\n"
            "    'run_spec_digest': properties['run_spec_digest']['const'],\n"
            "    'attempt_id': properties['attempt_id']['const'],\n"
            "    'candidate_oid': properties['candidate_oid']['const'],\n"
            "    'root_fingerprint': properties['root_fingerprint']['const'],\n"
            "    'criterion_results': [{\n"
            "      'criterion': criterion,\n"
            "      'passed': verdict == 'pass',\n"
            "      'evidence_digests': [],\n"
            "    } for criterion in properties['criterion_results']['items']['properties']"
            "['criterion']['enum']],\n"
            "    'blockers': [] if verdict == 'pass' else [{\n"
            "      'blocker_id': 'promotion-verifier-rejected',\n"
            "      'detail': 'candidate verdict is fail',\n"
            "    }],\n"
            "    'summary': f'candidate verdict: {verdict}',\n"
            "  }\n"
            "else:\n"
            "  result = {\n"
            "    'schema': properties['schema']['const'],\n"
            "    'status': 'completed',\n"
            "    'run_spec_digest': properties['run_spec_digest']['const'],\n"
            "    'attempt_id': properties['attempt_id']['const'],\n"
            "    'result_summary': verdict if evaluation else 'Candidate explored.',\n"
            "    'evidence_refs': [],\n"
            "    'context_request': None,\n"
            "  }\n"
            "Path(args[args.index('-o') + 1]).write_text(json.dumps(result), encoding='utf-8')\n",
            encoding="utf-8",
        )
        binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return binary

    @contextmanager
    def runtime(self, cwd: Path | None = None):
        old = Path.cwd()
        target = self.root if cwd is None else cwd
        output = io.StringIO()
        try:
            os.chdir(target)
            with mock.patch.dict(os.environ, {"WAYSTONE_HOME": str(self.machine)}), mock.patch(
                    "waystone.runs.store._probe_state_filesystem",
                    return_value=FilesystemInfo(
                        filesystem="apfs", mount_point=Path("/"), writable=True)), \
                    contextlib.redirect_stdout(output):
                yield output
        finally:
            os.chdir(old)

    def semantic_draft(self, stage: str, objective_fact_id: str) -> dict:
        return {
            "lifecycle_stage": stage,
            "objective_fact_id": objective_fact_id,
            "desired_delta": "Compare the candidate approaches.",
            "why_now": "The current uncertainty blocks the first supported result.",
            "current_state": ["The baseline does not yet contain a candidate."],
            "decisions": {
                "fixed": ["Preserve the intended result."],
                "worker_may_choose": ["Choose the smallest sound comparison."],
                "requires_escalation": ["Escalate before widening the task scope."],
            },
            "constraints": ["Keep the public seam stable."],
            "non_goals": ["Do not build a distributed scheduler."],
            "known_failures": [],
            "evidence_expected": [{
                "criterion_id": "candidate-produced",
                "kind": "candidate",
                "text": "Produce a reviewable candidate.",
            }],
            "references": [{
                "path": "src.py",
                "anchor": "baseline",
                "purpose": "Current implementation seam.",
            }],
            "open_questions": ["Which candidate is the smallest sound option?"],
        }

    def test_start_uses_production_assembly_and_freezes_typed_ingress(self):
        binary = self.install_fixture_codex()
        with self.runtime() as output, mock.patch.dict(
                os.environ, {"PATH": f"{binary.parent}{os.pathsep}{os.environ['PATH']}"}):
            result = run_group.main([
                "start",
                "feat/semantic-brief",
                "--work-brief",
                str(self.brief_path),
                "--stage",
                "explore",
            ])
            run_id = output.getvalue().split()[1]
            deadline = time.monotonic() + 10
            marker = self.root / ".waystone" / "runner-completions"
            while time.monotonic() < deadline and not tuple(marker.glob("*.json")):
                time.sleep(0.02)

        self.assertEqual(result, 0, output.getvalue())
        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)):
            spec = load_run_spec(run_id, start=self.root)
            with RunStore.open(self.root) as store:
                self.assertEqual(store.get_run(run_id).state, "dispatch-ready")
                self.assertEqual(
                    store.get_entity(
                        EntityKind.ATTEMPT, f"{run_id}:attempt:1").state,
                    "running",
                )
        self.assertEqual(spec.revision, 1)
        self.assertEqual(spec.lifecycle_stage.value, "explore")

    def test_e2e5_public_explore_completes_and_closes_published_worker_result(self):
        binary = self.install_fixture_codex()
        with self.runtime() as output, mock.patch.dict(
                os.environ, {"PATH": f"{binary.parent}{os.pathsep}{os.environ['PATH']}"}):
            result = run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(self.brief_path),
                "--stage", "explore",
            ])
            self.assertEqual(result, 0, output.getvalue())
            run_id = output.getvalue().split()[1]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                output.seek(0)
                output.truncate(0)
                resumed = run_group.main(["resume", run_id])
                if resumed == 0 and "run_closeout_ready" in output.getvalue():
                    break
                time.sleep(0.02)
            else:
                self.fail(f"public explore did not reach closeout-ready: {output.getvalue()}")

            spec = load_run_spec(run_id, start=self.root)
            with mock.patch(
                    "waystone.runs.store._probe_state_filesystem",
                    return_value=FilesystemInfo(
                        filesystem="apfs", mount_point=Path("/"), writable=True)), \
                    RunStore.open(self.root) as store:
                attempt_id = f"{run_id}:attempt:1"
                result_ref = store.get_artifact_reference(f"worker-result:{attempt_id}")
                self.assertEqual(store.get_run(run_id).state, "closeout-ready")
                self.assertEqual(
                    store.get_entity(EntityKind.ATTEMPT, attempt_id).state, "completed")
                self.assertEqual(
                    store.get_entity(EntityKind.JOB, spec.job_id).state, "completed")
            candidate_oid = git(
                self.root, "rev-parse", f"refs/waystone/candidates/{run_id}").stdout.strip()
            self.assertEqual(candidate_oid, git(self.root, "rev-parse", "HEAD").stdout.strip())

            binding = read_profile(
                self.root / ".waystone" / "profile.yml").binding_for(
                    Role.COORDINATOR).binding_digest
            outcome = {
                "schema": "waystone-outcome-delta-1",
                "run_id": run_id,
                "run_spec_digest": spec.run_spec_digest,
                "lifecycle_stage": "explore",
                "objective_ref": spec.objective_ref.to_dict(),
                "kind": "no-objective-delta",
                "summary": "The public explore path published a candidate.",
                "result_digest": result_ref.digest,
                "evidence_refs": [],
                "finding_refs": [],
                "recorded_by": {
                    "role": "coordinator", "binding_digest": binding, "principal": None,
                },
                "rationale": "The candidate ref is locally reachable and immutable.",
            }
            outcome_path = self.base / "outcome.yaml"
            outcome_path.write_bytes(yaml.safe_dump(outcome, sort_keys=False).encode())
            output.seek(0)
            output.truncate(0)
            self.assertEqual(
                run_group.main(["close", run_id, "--outcome", str(outcome_path)]),
                0,
                output.getvalue(),
            )

        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            self.assertEqual(store.get_run(run_id).state, "completed")

    def test_scaffold_e2e_explore_starts_and_closes_from_semantic_drafts(self):
        brief_draft = self.base / "work-brief-draft.yaml"
        brief_draft.write_text(yaml.safe_dump(
            self.semantic_draft("explore", "hypothesis/solver"), sort_keys=False,
        ), encoding="utf-8")
        binary = self.install_fixture_codex()
        start_head = git(self.root, "rev-parse", "HEAD").stdout.strip()
        start_frame = read_project_frame_at_commit(self.root, start_head)

        with self.runtime() as output, mock.patch.dict(
                os.environ, {"PATH": f"{binary.parent}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(run_group.main([
                "start", "feat/semantic-brief",
                "--work-brief-draft", str(brief_draft),
                "--stage", "explore",
            ]), 0, output.getvalue())
            run_id = output.getvalue().split()[1]
            self._resume_until_closeout(output, run_id)
            spec = load_run_spec(run_id, start=self.root)
            brief_bytes = ArtifactStore(self.root).read(spec.work_brief.digest)
            brief = work_brief.parse_work_brief_bytes(brief_bytes)
            self.assertEqual(brief_bytes, brief.canonical_bytes())
            self.assertEqual(brief.task_id, "feat/semantic-brief")
            self.assertEqual(brief.revision, 1)
            self.assertEqual(brief.objective.ref.to_dict(), start_frame.fact_ref(
                "hypothesis/solver").to_dict())
            self.assertEqual(brief.objective.desired_delta, "Compare the candidate approaches.")
            self.assertEqual(brief.current_state[0].text,
                             "The baseline does not yet contain a candidate.")
            self.assertEqual(brief.references[0].digest,
                             "sha256:" + hashlib.sha256(b"baseline = False\n").hexdigest())

            outcome_draft = self.base / "outcome-draft.yaml"
            outcome_draft.write_text(yaml.safe_dump({
                "kind": "no-objective-delta",
                "summary": "The explore run produced a candidate for evaluation.",
                "evidence_refs": [],
                "finding_refs": [],
                "rationale": "Evaluation is required before claiming objective progress.",
            }, sort_keys=False), encoding="utf-8")
            output.seek(0)
            output.truncate(0)
            self.assertEqual(run_group.main([
                "close", run_id, "--outcome-draft", str(outcome_draft),
            ]), 0, output.getvalue())

        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            self.assertEqual(store.get_run(run_id).state, "completed")

    def test_scaffold_refuses_missing_semantic_fields_without_creating_a_run(self):
        draft = self.base / "incomplete-draft.yaml"
        draft.write_text(yaml.safe_dump({
            "lifecycle_stage": "explore",
            "objective_fact_id": "hypothesis/solver",
            "desired_delta": "Compare candidates.",
        }, sort_keys=False), encoding="utf-8")

        with self.runtime() as output:
            context = resolve_project_context(Path.cwd(), require_run_input=True)
            with assemble_run(context) as assembly, self.assertRaisesRegex(
                    RunScaffoldRefusal, "missing .*why_now"):
                scaffold_work_brief(
                    assembly, "feat/semantic-brief", draft.read_bytes())
            output.seek(0)
            output.truncate(0)
            result = run_group.main([
                "start", "feat/semantic-brief", "--work-brief-draft", str(draft),
            ])

        self.assertEqual(result, 2, output.getvalue())
        failure = json.loads(output.getvalue())
        self.assertEqual(failure["code"], "action_plan_invalid")
        self.assertIn(
            "WorkBrief semantic draft fields are not exact: missing",
            failure["detail"],
        )
        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            count = store._connection.execute(  # noqa: SLF001
                "SELECT count(*) FROM runs").fetchone()[0]
        self.assertEqual(count, 0)

    def test_start_refusal_exposes_actionable_work_brief_reason(self):
        invalid = self.base / "invalid-work-brief.json"
        body = json.loads(self.brief_path.read_bytes())
        body["lifecycle_stage"] = "deploy"
        invalid.write_bytes(completion.canonical_json(body))

        with self.runtime() as output:
            result = run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(invalid),
            ])

        self.assertEqual(result, 2, output.getvalue())
        failure = json.loads(output.getvalue())
        self.assertEqual(failure["code"], "action_plan_invalid")
        self.assertIn("work_brief_schema_refusal", failure["detail"])
        self.assertIn(
            "lifecycle_stage must be explore, evaluate, or promote",
            failure["detail"],
        )

    def test_failure_codes_distinguish_authority_preflight_and_internal(self):
        cases = (
            (
                ActionPlanRefusal("WorkBrief lifecycle_stage must be corrected"),
                2,
                "action_plan_invalid",
                "WorkBrief lifecycle_stage must be corrected",
            ),
            (
                EnvironmentPreparationUnavailableError(
                    "frozen toolchain is unavailable"),
                2,
                "preflight_failed",
                "frozen toolchain is unavailable",
            ),
            (
                RuntimeError(
                    "token=secret-value /private/runtime/path\n"
                    "Traceback (most recent call last): ..."),
                1,
                "unclassified",
                "RuntimeError",
            ),
        )
        for error, expected_exit, expected_code, expected_detail in cases:
            with self.subTest(code=expected_code), contextlib.redirect_stdout(
                    io.StringIO()) as output:
                result = run_group._failure(error)  # noqa: SLF001
                failure = json.loads(output.getvalue())
                self.assertEqual(result, expected_exit)
                self.assertEqual(failure["code"], expected_code)
                self.assertEqual(failure["detail"], expected_detail)
                if expected_code == "unclassified":
                    self.assertNotIn("secret-value", failure["detail"])
                    self.assertNotIn("/private/runtime/path", failure["detail"])
                    self.assertNotIn("Traceback", failure["detail"])

    def test_scaffold_derives_candidate_evaluation_and_promotion_lineage(self):
        binary = self.install_fixture_codex()
        path = f"{binary.parent}{os.pathsep}{os.environ['PATH']}"
        for name in ("evaluate-verdict.txt", "promote-verdict.txt"):
            (self.root / name).write_text("pass\n", encoding="utf-8")
        explore_draft = self.base / "explore-draft.yaml"
        explore_draft.write_text(yaml.safe_dump(
            self.semantic_draft("explore", "hypothesis/solver"), sort_keys=False,
        ), encoding="utf-8")
        with self.runtime() as output, mock.patch.dict(os.environ, {"PATH": path}):
            self.assertEqual(run_group.main([
                "start", "feat/semantic-brief", "--work-brief-draft", str(explore_draft),
            ]), 0, output.getvalue())
            explore_id = output.getvalue().split()[1]
            self._resume_until_closeout(output, explore_id)
            explore_outcome = self.base / "explore-outcome-draft.yaml"
            explore_outcome.write_text(yaml.safe_dump({
                "kind": "no-objective-delta",
                "summary": "The candidate is ready for evaluation.",
                "evidence_refs": [],
                "finding_refs": [],
                "rationale": "Evaluation remains outstanding.",
            }, sort_keys=False), encoding="utf-8")
            output.seek(0)
            output.truncate(0)
            self.assertEqual(run_group.main([
                "close", explore_id, "--outcome-draft", str(explore_outcome),
            ]), 0, output.getvalue())

        head = git(self.root, "rev-parse", "HEAD").stdout.strip()
        frame = read_project_frame_at_commit(self.root, head)
        evaluation_body = {
            "schema": "waystone-evaluation-spec-1",
            "evaluation_id": new_run_id(),
            "generation": 1,
            "objective_ref": frame.fact_ref("commitment/outcome").to_dict(),
            "criteria": [{
                "id": "representative", "metric": "exact-match",
                "operator": "gte", "threshold": 1,
            }],
            "datasets": [{
                "id": "fixture", "artifact_reference_id": "dataset:fixture",
                "digest": "sha256:" + "d" * 64, "visibility": "harness-only",
            }],
            "seed": 7,
            "supersedes_spec_digest": None,
        }
        evaluation_path = self.root / "docs/evaluations/scaffold/spec.yaml"
        evaluation_path.parent.mkdir(parents=True)
        evaluation_path.write_text(
            yaml.safe_dump(evaluation_body, sort_keys=False), encoding="utf-8")
        git(self.root, "add", str(evaluation_path.relative_to(self.root)))
        self.assertEqual(git(self.root, "commit", "-qm", "scaffold evaluation").returncode, 0)
        evaluate = self.semantic_draft("evaluate", "commitment/outcome")
        evaluate["evaluation_spec_path"] = str(evaluation_path.relative_to(self.root))
        evaluate["evidence_expected"] = [{
            "criterion_id": "representative",
            "kind": "evaluation-evidence",
            "text": "Measure the frozen candidate on the representative fixture.",
        }]
        evaluate_draft = self.base / "evaluate-draft.yaml"
        evaluate_draft.write_text(
            yaml.safe_dump(evaluate, sort_keys=False), encoding="utf-8")

        with self.runtime() as output, mock.patch.dict(os.environ, {"PATH": path}):
            self.assertEqual(run_group.main([
                "start", "feat/semantic-brief", "--work-brief-draft", str(evaluate_draft),
            ]), 0, output.getvalue())
            evaluate_id = output.getvalue().split()[1]
            evaluate_spec = load_run_spec(evaluate_id, start=self.root)
            self.assertEqual(
                evaluate_spec.candidate["reference_id"], f"candidate:{explore_id}")
            self._resume_until_closeout(output, evaluate_id)
            evaluate_outcome = self.base / "evaluate-outcome-draft.yaml"
            evaluate_outcome.write_text(yaml.safe_dump({
                "kind": "no-objective-delta",
                "summary": "The representative evaluation is frozen for promotion.",
                "evidence_refs": [],
                "finding_refs": [],
                "rationale": "Promotion has not yet established objective progress.",
            }, sort_keys=False), encoding="utf-8")
            output.seek(0)
            output.truncate(0)
            self.assertEqual(run_group.main([
                "close", evaluate_id, "--outcome-draft", str(evaluate_outcome),
            ]), 0, output.getvalue())

        records = self.root / "docs/promotion/scaffold"
        records.mkdir(parents=True)
        records.joinpath("regression.txt").write_text(
            "representative regression\n", encoding="utf-8")
        records.joinpath("scope.txt").write_text("candidate.txt\n", encoding="utf-8")
        records.joinpath("risks.txt").write_text("none\n", encoding="utf-8")
        git(self.root, "add", "docs/promotion/scaffold/regression.txt",
            "docs/promotion/scaffold/scope.txt", "docs/promotion/scaffold/risks.txt")
        self.assertEqual(git(
            self.root, "commit", "-qm", "scaffold promotion records").returncode, 0)
        promote = self.semantic_draft("promote", "commitment/outcome")
        promote["promotion_records"] = {
            "regression_contract": "docs/promotion/scaffold/regression.txt",
            "supported_scope": "docs/promotion/scaffold/scope.txt",
            "accepted_risks": "docs/promotion/scaffold/risks.txt",
        }
        promote["evidence_expected"] = [{
            "criterion_id": "representative",
            "kind": "regression-contract",
            "text": "Promote only the passed representative generation.",
        }]
        with self.runtime():
            context = resolve_project_context(Path.cwd(), require_run_input=True)
            with assemble_run(context) as assembly:
                content = scaffold_work_brief(
                    assembly, "feat/semantic-brief",
                    yaml.safe_dump(promote, sort_keys=False).encode("utf-8"),
                )
        brief = work_brief.parse_work_brief_bytes(content)
        lineage_sources = {
            source.payload["reference_id"]
            for source in brief.current_state[-1].sources
        }
        self.assertIn(f"candidate:{explore_id}", lineage_sources)
        self.assertIn(f"evaluation-evidence:{evaluate_id}", lineage_sources)
        self.assertIn("regression-contract:scaffold", lineage_sources)
        self.assertIn("supported-scope:scaffold", lineage_sources)
        self.assertIn("accepted-risks:scaffold", lineage_sources)
        promote_draft = self.base / "promote-draft.yaml"
        promote_draft.write_text(
            yaml.safe_dump(promote, sort_keys=False), encoding="utf-8")
        with self.runtime() as output, mock.patch.dict(os.environ, {"PATH": path}):
            self.assertEqual(run_group.main([
                "start", "feat/semantic-brief", "--work-brief-draft", str(promote_draft),
            ]), 0, output.getvalue())
            promote_id = output.getvalue().split()[1]
            promote_spec = load_run_spec(promote_id, start=self.root)
            self.assertEqual(
                promote_spec.candidate["reference_id"], f"candidate:{explore_id}")
            self.assertEqual(
                promote_spec.evaluation["evidence"]["reference_id"],
                f"evaluation-evidence:{evaluate_id}",
            )

    def test_e2e6_public_evaluate_then_promote_executes_frozen_full_chain(self):
        evaluation_body = {
            "schema": "waystone-evaluation-spec-1",
            "evaluation_id": new_run_id(),
            "generation": 1,
            "objective_ref": self.frame.fact_ref("commitment/outcome").to_dict(),
            "criteria": [{
                "id": "representative", "metric": "exact-match",
                "operator": "gte", "threshold": 1,
            }],
            "datasets": [{
                "id": "fixture", "artifact_reference_id": "dataset:fixture",
                "digest": "sha256:" + "d" * 64, "visibility": "harness-only",
            }],
            "seed": 7,
            "supersedes_spec_digest": None,
        }
        evaluation_bytes = yaml.safe_dump(evaluation_body, sort_keys=True).encode()
        evaluation_path = self.root / "docs/evaluations/fixture/spec.yaml"
        evaluation_path.parent.mkdir(parents=True)
        evaluation_path.write_bytes(evaluation_bytes)
        git(self.root, "add", str(evaluation_path.relative_to(self.root)))
        self.assertEqual(git(self.root, "commit", "-qm", "evaluation fixture").returncode, 0)
        evaluation_commit = git(self.root, "rev-parse", "HEAD").stdout.strip()
        evaluation_source = {
            "kind": "evaluation-spec",
            "commit": evaluation_commit,
            "path": str(evaluation_path.relative_to(self.root)),
            "digest": "sha256:" + hashlib.sha256(evaluation_bytes).hexdigest(),
            "generation": 1,
        }
        for name in ("evaluate-verdict.txt", "promote-verdict.txt"):
            (self.root / name).write_text("fail\n", encoding="utf-8")
        git(self.root, "add", "evaluate-verdict.txt", "promote-verdict.txt")
        self.assertEqual(git(
            self.root, "commit", "-qm", "integration verdict fails").returncode, 0)
        candidate_worktree = self.base / "candidate-worktree"
        self.assertEqual(git(
            self.root, "worktree", "add", "-q", "-b", "fixture-candidate",
            str(candidate_worktree)).returncode, 0)
        for name in ("evaluate-verdict.txt", "promote-verdict.txt"):
            (candidate_worktree / name).write_text("pass\n", encoding="utf-8")
        binary = self.install_fixture_codex()
        path = f"{binary.parent}{os.pathsep}{os.environ['PATH']}"

        with self.runtime(candidate_worktree) as output, mock.patch.dict(
                os.environ, {"PATH": path}):
            self.assertEqual(run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(self.brief_path),
                "--stage", "explore", "--from-worktree", str(candidate_worktree),
            ]), 0, output.getvalue())
            explore_id = output.getvalue().split()[1]
            self._resume_until_closeout(output, explore_id)

        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            candidate_ref = store.get_artifact_reference(f"candidate:{explore_id}")

        evaluate_payload = payload(self.head, self.frame, new_run_id())
        evaluate_payload["lifecycle_stage"] = "evaluate"
        evaluate_payload["objective"]["ref"] = self.frame.fact_ref(
            "commitment/outcome").to_dict()
        evaluate_payload["current_state"].append(item(
            "The explore run published the frozen candidate.",
            "harness-observation",
            source={
                "kind": "evidence", "reference_id": f"candidate:{explore_id}",
                "digest": candidate_ref.digest,
            },
        ))
        evaluate_payload["evidence_expected"] = [{
            "criterion_id": "representative",
            "kind": "evaluation-evidence",
            "text": "The candidate passes the representative fixture.",
            "source": evaluation_source,
        }]
        evaluate_path = self.base / "evaluate-brief.json"
        evaluate_path.write_bytes(completion.canonical_json(evaluate_payload))
        with self.runtime() as output, mock.patch.dict(os.environ, {"PATH": path}):
            self.assertEqual(run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(evaluate_path),
                "--stage", "evaluate",
            ]), 0, output.getvalue())
            evaluate_id = output.getvalue().split()[1]
            self._resume_until_closeout(output, evaluate_id)

        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            evidence_ref = store.get_artifact_reference(
                f"evaluation-evidence:{evaluate_id}")
        evaluation = json.loads(
            ArtifactStore(self.root).read_reference(evidence_ref).decode("utf-8"))
        self.assertEqual(evaluation["result"], "pass")
        self.assertEqual(
            (self.root / "evaluate-verdict.txt").read_text(encoding="utf-8"),
            "fail\n",
        )
        evaluate_spec = load_run_spec(evaluate_id, start=self.root)
        evaluate_action = f"{evaluate_id}:attempt:1:read-only-evaluator"
        launch_path = (
            self.root / ".waystone" / "supervisors"
            / f"{hashlib.sha256(evaluate_action.encode()).hexdigest()}.launch.json"
        )
        launch = json.loads(launch_path.read_text(encoding="utf-8"))
        frozen_candidate_oid = git(
            self.root, "rev-parse", f"refs/waystone/candidates/{explore_id}").stdout.strip()
        self.assertEqual(launch["candidate_context"], {
            "candidate_oid": frozen_candidate_oid,
            "root_fingerprint": launch["candidate_context"]["root_fingerprint"],
            "run_spec_digest": evaluate_spec.run_spec_digest,
        })
        self.assertRegex(
            launch["candidate_context"]["root_fingerprint"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(
            (Path(launch["cwd"]) / "evaluate-verdict.txt").read_text(encoding="utf-8"),
            "pass\n",
        )
        wrong_context = RunnerCandidateContext(
            frozen_candidate_oid,
            launch["candidate_context"]["root_fingerprint"],
            "sha256:" + "f" * 64,
        )
        with self.runtime():
            context = resolve_project_context(Path.cwd())
            with assemble_run(context) as assembly, self.assertRaisesRegex(
                    EngineBindingRefusal, "RunSpec"):
                StagedRunEngine(assembly)._stage_invocation(  # noqa: SLF001
                    evaluate_spec,
                    f"{evaluate_id}:attempt:1",
                    Role.VERIFIER,
                    candidate_root=Path(launch["cwd"]),
                    candidate_context=wrong_context,
                )
        evidence_source = {
            "kind": "evaluation-evidence",
            "reference_id": f"evaluation-evidence:{evaluate_id}",
            "candidate_digest": candidate_ref.digest,
            "generation": 1,
            "digest": evidence_ref.digest,
        }
        artifacts = ArtifactStore(self.root)
        records = {
            "regression-contract:fixture": artifacts.write(b"representative regression\n"),
            "supported-scope:fixture": artifacts.write(b"candidate.txt\n"),
            "accepted-risks:fixture": artifacts.write(b"public-contract\n"),
        }
        promote_payload = payload(self.head, self.frame, new_run_id())
        promote_payload["lifecycle_stage"] = "promote"
        promote_payload["objective"]["ref"] = self.frame.fact_ref(
            "commitment/outcome").to_dict()
        promote_payload["current_state"].append(item(
            "The candidate and passed evaluation generation are frozen with promotion records.",
            "harness-observation",
            sources=[
                {"kind": "evidence", "reference_id": f"candidate:{explore_id}",
                 "digest": candidate_ref.digest},
                evidence_source,
                *[
                    {"kind": "evidence", "reference_id": reference_id,
                     "digest": artifact.digest}
                    for reference_id, artifact in records.items()
                ],
            ],
        ))
        promote_payload["evidence_expected"] = [{
            "criterion_id": "representative",
            "kind": "regression-contract",
            "text": "Promote only the passed representative generation.",
            "source": evidence_source,
        }]
        promote_path = self.base / "promote-brief.json"
        promote_path.write_bytes(completion.canonical_json(promote_payload))
        integration_base = git(self.root, "rev-parse", "HEAD").stdout.strip()
        candidate_oid = git(
            self.root, "rev-parse", f"refs/waystone/candidates/{explore_id}").stdout.strip()
        self.assertNotEqual(integration_base, candidate_oid)

        with self.runtime() as output, mock.patch.dict(os.environ, {"PATH": path}):
            self.assertEqual(run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(promote_path),
                "--stage", "promote",
            ]), 0, output.getvalue())
            promote_id = output.getvalue().split()[1]
            promote_spec = load_run_spec(promote_id, start=self.root)
            private_ref = (
                f"refs/waystone/integration/{promote_spec.promotion_lineage.id}")
            self.assertEqual(promote_spec.result_policy.target_ref, private_ref)
            self.assertEqual(
                git(self.root, "rev-parse", private_ref).stdout.strip(),
                integration_base,
            )
            reviewer_binding = read_profile(
                self.root / ".waystone" / "profile.yml").binding_for(
                    Role.REVIEWER).binding_digest
            review_run_id = new_run_id()
            feedback_path = self.base / "promotion-review.yaml"
            feedback_path.write_text(yaml.safe_dump({
                "target": {
                    "run_spec_digest": promote_spec.run_spec_digest,
                    "result_digest": promote_spec.candidate["producer_result_digest"],
                },
                "binding_digest": reviewer_binding,
                "reported_by": {
                    "role": "reviewer",
                    "binding_digest": reviewer_binding,
                    "principal": None,
                },
                "findings": [],
            }, sort_keys=False), encoding="utf-8")
            review_group.ingest_feedback(
                self.context, review_run_id, feedback_path,
                binding_digest=reviewer_binding,
            )
            review_group.attach_review(self.context, promote_id, review_run_id)
            git(self.root, "add", "docs/reviews")
            self.assertEqual(git(
                self.root, "commit", "-qm", "promotion review fixture").returncode, 0)
            public_ref = git(self.root, "symbolic-ref", "HEAD").stdout.strip()
            public_oid = git(self.root, "rev-parse", "HEAD").stdout.strip()
            public_bytes = {
                name: (self.root / name).read_bytes()
                for name in ("evaluate-verdict.txt", "promote-verdict.txt")
            }
            public_index = git(self.root, "write-tree").stdout.strip()
            public_status = git(
                self.root, "status", "--porcelain=v1").stdout
            self.assertEqual(public_status, "")
            self._resume_until_closeout(output, promote_id)

        self.assertIn(private_ref, output.getvalue())
        self.assertIn("Live-tree delivery is not performed", output.getvalue())
        self.assertEqual(git(self.root, "symbolic-ref", "HEAD").stdout.strip(), public_ref)
        self.assertEqual(git(self.root, "rev-parse", "HEAD").stdout.strip(), public_oid)
        self.assertEqual(
            git(self.root, "rev-parse", public_ref).stdout.strip(), public_oid)
        self.assertEqual(
            git(self.root, "rev-parse", private_ref).stdout.strip(), candidate_oid)
        self.assertEqual(
            {
                name: (self.root / name).read_bytes()
                for name in ("evaluate-verdict.txt", "promote-verdict.txt")
            },
            public_bytes,
        )
        self.assertFalse((self.root / "candidate.txt").exists())
        self.assertEqual(git(self.root, "write-tree").stdout.strip(), public_index)
        self.assertEqual(
            git(self.root, "status", "--porcelain=v1").stdout, public_status)

        outcome_draft = self.base / "promote-outcome-draft.yaml"
        outcome_draft.write_text(yaml.safe_dump({
            "kind": "no-objective-delta",
            "summary": "The approved candidate was published to the private integration ref.",
            "evidence_refs": [],
            "finding_refs": [],
            "rationale": "Public delivery remains outside this bounded promotion run.",
        }, sort_keys=False), encoding="utf-8")
        with self.runtime():
            context = resolve_project_context(Path.cwd())
            with assemble_run(context) as assembly:
                completed_outcome = scaffold_outcome_delta(
                    assembly,
                    promote_id,
                    outcome_draft.read_bytes(),
                )
        completed_body = yaml.safe_load(completed_outcome)
        self.assertNotEqual(
            completed_body["result_digest"],
            promote_spec.candidate["producer_result_digest"],
        )
        completed_outcome_path = self.base / "promote-outcome.yaml"
        completed_outcome_path.write_bytes(completed_outcome)
        mismatched = yaml.safe_load(completed_outcome)
        mismatched["result_digest"] = "sha256:" + "0" * 64
        mismatched_path = self.base / "promote-outcome-mismatched.yaml"
        mismatched_path.write_text(
            yaml.safe_dump(mismatched, sort_keys=False), encoding="utf-8")
        with self.runtime() as close_output:
            self.assertNotEqual(run_group.main([
                "close", promote_id, "--outcome", str(mismatched_path),
            ]), 0)
        refusal = json.loads(close_output.getvalue())
        self.assertIn("approved GitResultTriple", refusal["detail"])

        with self.runtime() as close_output:
            self.assertEqual(run_group.main([
                "close", promote_id, "--outcome-draft", str(outcome_draft),
            ]), 0, close_output.getvalue())
        with self.runtime() as close_output:
            self.assertEqual(run_group.main([
                "close", promote_id, "--outcome", str(completed_outcome_path),
            ]), 0, close_output.getvalue())

        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            self.assertEqual(store.get_run(promote_id).state, "completed")
        ledger = read_outcome_ledger(self.root)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0].outcome.run_id, promote_id)
        completion_refs = {
            item["reference_id"]: item["digest"]
            for item in ledger[0].closeout.completion_evidence_refs
        }
        self.assertIn(
            f"verifier-evidence:{promote_id}:typed-independent-verify",
            completion_refs,
        )
        self.assertIn(
            f"integration-decision:{promote_id}:integration-decision",
            completion_refs,
        )
        self.assertIn(
            f"review-cycle:{promote_spec.promotion_lineage.id}:1",
            completion_refs,
        )
        apply_action = f"{promote_id}:target-ref-apply"
        self.assertIn(f"effect-plan:{apply_action}", completion_refs)
        self.assertEqual(
            len([
                reference_id for reference_id in completion_refs
                if reference_id.startswith(f"effect-observation:{apply_action}:")
            ]),
            1,
        )

        reload_code = """
import json
from pathlib import Path
from unittest import mock
from waystone.jobs.profile import assemble_run
from waystone.project.context import resolve_project_context
from waystone.runs import outcome as outcome_module
from waystone.runs.outcome import read_outcome_ledger
from waystone.runs.spec import load_run_spec
from waystone.runs.store import FilesystemInfo

root = Path.cwd()
with mock.patch(
        "waystone.runs.store._probe_state_filesystem",
        return_value=FilesystemInfo("apfs", Path("/"), writable=True)):
    context = resolve_project_context(root)
    with assemble_run(context) as assembly:
        entry = read_outcome_ledger(root)[-1]
        spec = load_run_spec(entry.outcome.run_id, start=root)
        authority = outcome_module._final_result_authority(assembly, spec)
        print(json.dumps({
            "result_digest": authority.result_digest,
            "completion_refs": list(authority.completion_references),
        }, sort_keys=True))
"""
        reloaded = subprocess.run(
            [sys.executable, "-c", reload_code],
            cwd=self.root,
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                "WAYSTONE_HOME": str(self.machine),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(reloaded.returncode, 0, reloaded.stderr)
        fresh = json.loads(reloaded.stdout)
        self.assertEqual(fresh["result_digest"], ledger[0].outcome.result_digest)
        self.assertEqual(
            {
                item["reference_id"]: item["digest"]
                for item in fresh["completion_refs"]
            },
            completion_refs,
        )
        self.assertEqual(
            git(self.root, "rev-parse", private_ref).stdout.strip(), candidate_oid)
        self.assertEqual(git(self.root, "symbolic-ref", "HEAD").stdout.strip(), public_ref)
        self.assertEqual(git(self.root, "rev-parse", "HEAD").stdout.strip(), public_oid)
        self.assertEqual(git(self.root, "write-tree").stdout.strip(), public_index)
        self.assertEqual(
            git(self.root, "status", "--porcelain=v1").stdout, public_status)
        for name in ("evaluate-verdict.txt", "promote-verdict.txt"):
            (self.root / name).write_text("pass\n", encoding="utf-8")
        fail_worktree = self.base / "fail-candidate-worktree"
        self.assertEqual(git(
            self.root, "worktree", "add", "-q", "-b", "fixture-fail-candidate",
            str(fail_worktree), candidate_oid).returncode, 0)
        for name in ("evaluate-verdict.txt", "promote-verdict.txt"):
            (fail_worktree / name).write_text("fail\n", encoding="utf-8")
        git(fail_worktree, "add", "evaluate-verdict.txt", "promote-verdict.txt")
        self.assertEqual(git(
            fail_worktree, "commit", "-qm", "candidate verdict fails").returncode, 0)
        fail_oid = git(fail_worktree, "rev-parse", "HEAD").stdout.strip()
        fail_candidate = {
            **promote_spec.candidate,
            "target_ref": "refs/heads/fixture-fail-candidate",
            "target_oid": fail_oid,
            "code_sha": fail_oid,
        }
        self.assertEqual(
            (self.root / "evaluate-verdict.txt").read_text(encoding="utf-8"),
            "pass\n",
        )
        self.assertEqual(
            (self.root / "promote-verdict.txt").read_text(encoding="utf-8"),
            "pass\n",
        )
        with self.runtime(), mock.patch.dict(
                os.environ, {"PATH": path}), assemble_run(
                    resolve_project_context(Path.cwd())) as assembly:
            engine = StagedRunEngine(assembly)
            fail_evaluate = replace(evaluate_spec, candidate=fail_candidate)
            evaluate_attempt = f"{evaluate_id}:attempt:candidate-fail"
            evaluate_invocation = engine._stage_invocation(  # noqa: SLF001
                fail_evaluate, evaluate_attempt, Role.VERIFIER)
            evaluated = subprocess.run(
                evaluate_invocation.argv,
                cwd=evaluate_invocation.cwd,
                capture_output=True,
                check=False,
            )
            self.assertEqual(evaluated.returncode, 0, evaluated.stderr)
            evaluate_result = json.loads(
                (evaluate_invocation.cwd / "WAYSTONE_RESULT.yaml").read_text(
                    encoding="utf-8"))
            self.assertEqual(evaluate_result["run_spec_digest"], fail_evaluate.run_spec_digest)
            self.assertEqual(evaluate_result["attempt_id"], evaluate_attempt)
            self.assertEqual(evaluate_result["result_summary"], "fail")

            fail_promote = replace(promote_spec, candidate=fail_candidate)
            promote_invocation_spec = replace(
                fail_promote, lifecycle_stage=LifecycleStage.EVALUATE)
            promote_attempt = f"{promote_id}:attempt:candidate-fail"
            promote_root, promote_context = engine._candidate_stage_root(  # noqa: SLF001
                promote_invocation_spec, promote_attempt)
            promote_schema = engine._promotion_verifier_result_schema(  # noqa: SLF001
                fail_promote,
                promote_attempt,
                fail_oid,
                promote_context.root_fingerprint,
            )
            promote_invocation = engine._stage_invocation(  # noqa: SLF001
                promote_invocation_spec,
                promote_attempt,
                Role.VERIFIER,
                candidate_root=promote_root,
                candidate_context=promote_context,
                output_schema=promote_schema,
            )
            promoted = subprocess.run(
                promote_invocation.argv,
                cwd=promote_invocation.cwd,
                capture_output=True,
                check=False,
            )
            self.assertEqual(promoted.returncode, 0, promoted.stderr)
            promote_result = json.loads(
                (promote_invocation.cwd / "WAYSTONE_RESULT.yaml").read_text(
                    encoding="utf-8"))
            self.assertTrue(promote_result["criterion_results"])
            self.assertTrue(all(
                not item["passed"] for item in promote_result["criterion_results"]))

        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            self.assertEqual(store.get_run(promote_id).state, "completed")
            verifier_ref = store.get_artifact_reference(
                f"verifier-evidence:{promote_id}:typed-independent-verify")
            launch_ref = store.get_artifact_reference(
                f"verifier-launch:{promote_id}:typed-independent-verify")
            decision_ref = store.get_artifact_reference(
                f"integration-decision:{promote_id}:integration-decision")
            review_ref = store.get_artifact_reference(
                f"review-cycle:{promote_spec.promotion_lineage.id}:1")
            self.assertEqual(len({
                verifier_ref.digest, decision_ref.digest, review_ref.digest,
            }), 3)
            verifier_launch = json.loads(ArtifactStore(self.root).read_reference(
                launch_ref).decode("utf-8"))
            self.assertEqual(verifier_launch["candidate_oid"], candidate_oid)
            self.assertEqual(
                verifier_launch["run_spec_digest"], promote_spec.run_spec_digest)
            self.assertRegex(
                verifier_launch["root_fingerprint"], r"^sha256:[0-9a-f]{64}$")
            self.assertNotEqual(Path(verifier_launch["cwd"]), self.root)
            decision = json.loads(ArtifactStore(self.root).read_reference(
                decision_ref).decode("utf-8"))
            self.assertEqual(
                decision["candidate_digest"], promote_spec.candidate["digest"])
            self.assertEqual(
                decision["evaluation_evidence_digest"],
                promote_spec.evaluation["evidence"]["digest"],
            )
            self.assertEqual(len(decision["reviewer_artifact_digests"]), 1)
            attempts = store._connection.execute(  # noqa: SLF001
                "SELECT attempt_id, state FROM attempts WHERE run_id = ?", (promote_id,)
            ).fetchall()
            self.assertEqual([(row["attempt_id"], row["state"]) for row in attempts], [
                (f"{promote_id}:attempt:1", "completed"),
            ])

    def test_promote_has_no_post_hoc_worker_result_replay_publisher(self):
        self.assertFalse(hasattr(
            StagedRunEngine,
            "_publish_promotion_verifier",
        ))

    def test_g01304_http_400_child_failure_terminalizes_marker_and_run_state(self):
        binary = self.install_fixture_codex()
        binary.with_name("fixture-codex-http-400").write_text(
            "enabled\n", encoding="utf-8")
        environment = {
            "PATH": f"{binary.parent}{os.pathsep}{os.environ['PATH']}",
        }
        with self.runtime() as output, mock.patch.dict(os.environ, environment):
            self.assertEqual(run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(self.brief_path),
                "--stage", "explore",
            ]), 0, output.getvalue())
            run_id = output.getvalue().split()[1]
            # A transient typed refusal (e.g. lease contention with the detached
            # supervisor) also returns a nonzero envelope, so the loop must key on
            # the run reaching its terminal state rather than on the first nonzero rc.
            deadline = time.monotonic() + 10
            result = 0
            state = None
            with mock.patch(
                    "waystone.runs.store._probe_state_filesystem",
                    return_value=FilesystemInfo(
                        filesystem="apfs", mount_point=Path("/"), writable=True)):
                while time.monotonic() < deadline:
                    output.seek(0)
                    output.truncate(0)
                    result = run_group.main(["resume", run_id])
                    try:
                        with RunStore.open(self.root) as store:
                            state = store.get_run(run_id).state
                    except EngineOwnedPathUnverifiableError:
                        # WAL sidecar verification can race with the detached
                        # supervisor's connection teardown; retry within the deadline.
                        time.sleep(0.02)
                        continue
                    if state == "failed":
                        break
                    time.sleep(0.02)
            self.assertEqual(state, "failed", output.getvalue())
            self.assertEqual(result, 2, output.getvalue())

        marker_paths = tuple((
            self.root / ".waystone" / "runner-completions").glob("*.json"))
        self.assertEqual(len(marker_paths), 1)
        marker = json.loads(marker_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(marker["schema"], "waystone-runner-completion-1")
        self.assertEqual(marker["returncode"], 1)
        self.assertNotIn("worker_result_digest", marker)
        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            attempt_id = f"{run_id}:attempt:1"
            self.assertEqual(store.get_run(run_id).state, "failed")
            self.assertEqual(
                store.get_entity(EntityKind.JOB, f"{run_id}:job").state, "failed")
            self.assertEqual(
                store.get_entity(EntityKind.ATTEMPT, attempt_id).state, "failed")
            self.assertEqual(
                store.get_entity(
                    EntityKind.ACTION, f"{attempt_id}:worker").state,
                "completed",
            )
            failure_ref = store.get_artifact_reference(f"runner-failure:{attempt_id}")
        failure = json.loads(
            ArtifactStore(self.root).read(failure_ref.digest).decode("utf-8"))
        self.assertEqual(failure["failure_class"], "runner-exit-nonzero")
        stderr = ArtifactStore(self.root).read(failure["stderr_artifact_digest"])
        self.assertIn(b"HTTP 400", stderr)

    def _resume_until_closeout(self, output: io.StringIO, run_id: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            output.seek(0)
            output.truncate(0)
            result = run_group.main(["resume", run_id])
            if result == 0 and "run_closeout_ready" in output.getvalue():
                return
            time.sleep(0.02)
        self.fail(f"run {run_id} did not reach closeout-ready: {output.getvalue()}")

    def test_stage_is_only_an_assertion_and_mismatch_creates_no_run(self):
        with self.runtime() as output:
            result = run_group.main([
                "start",
                "feat/semantic-brief",
                "--work-brief",
                str(self.brief_path),
                "--stage",
                "promote",
            ])

        self.assertEqual(result, 2, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["code"], "action_plan_invalid")
        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            count = store._connection.execute("SELECT count(*) FROM runs").fetchone()[0]  # noqa: SLF001
        self.assertEqual(count, 0)

    def test_linked_start_without_explicit_selector_refuses_before_ingress_or_db_open(self):
        linked = self.base / "linked"
        self.assertEqual(
            git(self.root, "worktree", "add", "-q", "-b", "cli-linked", str(linked)).returncode,
            0,
        )
        missing = self.base / "must-not-be-read.json"
        with self.runtime(linked) as output:
            result = run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(missing),
            ])

        self.assertEqual(result, 2, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["code"], "action_plan_invalid")
        self.assertFalse((self.root / ".waystone" / "state.db").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
