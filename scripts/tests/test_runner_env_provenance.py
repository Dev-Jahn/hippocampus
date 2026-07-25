from __future__ import annotations

from support import *  # noqa: F401,F403

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

from waystone.jobs.domain import ExecutionCategory, Role, RoleBinding
from waystone.runs.artifacts import (
    ArtifactReference,
    ArtifactReferenceKind,
    ArtifactStore,
)
from waystone.runs.engine import StagedRunEngine
from waystone.runs.environment import (
    RunnerExecutionDescriptor,
    build_runner_environment,
    freeze_runner_execution_descriptor,
)
from waystone.runs.effects import RunnerLaunchIntent
from waystone.runs.preflight import SandboxContract
from waystone.runs.store import RecordNotFoundError
from waystone.runs.supervisor import (
    RunnerCandidateContext,
    RunnerInvocation,
    Supervisor,
)


class RunnerEnvironmentProvenanceTests(unittest.TestCase):
    def test_p1_redirect_environment_cannot_override_candidate_git_authority(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        candidate = base / "candidate"
        integration = base / "integration"
        for root, verdict in ((candidate, "fail"), (integration, "pass")):
            root.mkdir()
            git(root, "init", "-q")
            git(root, "config", "user.email", "fixture@example.com")
            git(root, "config", "user.name", "Fixture")
            root.joinpath("verdict").write_text(verdict + "\n", encoding="utf-8")
            git(root, "add", "verdict")
            self.assertEqual(git(root, "commit", "-qm", verdict).returncode, 0)

        source = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "GIT_DIR": str(integration / ".git"),
            "GIT_WORK_TREE": str(integration),
            "UV_WORKING_DIRECTORY": str(integration),
        }
        environment = build_runner_environment(source)

        self.assertNotIn("GIT_DIR", environment.values)
        self.assertNotIn("GIT_WORK_TREE", environment.values)
        self.assertNotIn("UV_WORKING_DIRECTORY", environment.values)
        completed = subprocess.run(
            ["git", "show", "HEAD:verdict"],
            cwd=candidate,
            env=environment.as_dict(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "fail\n")

    def test_p1_detached_launch_passes_and_records_only_the_frozen_environment(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        environment = build_runner_environment({
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "LANG": "C.UTF-8",
            "GIT_DIR": "/integration/.git",
            "GIT_WORK_TREE": "/integration",
            "UV_WORKING_DIRECTORY": "/integration",
        })
        invocation = RunnerInvocation(
            (str(Path(sys.executable).resolve()), "-c", "pass"),
            root,
            environment=environment,
        )
        digest = StagedRunEngine._invocation_digest(invocation)  # noqa: SLF001
        supervisor = object.__new__(Supervisor)
        supervisor.project_root = root
        supervisor.directory = root / ".waystone" / "supervisors"
        supervisor._invocations = {digest: invocation}  # noqa: SLF001
        supervisor._heartbeat_interval = 1.0  # noqa: SLF001
        supervisor._lease_ttl = 5.0  # noqa: SLF001
        supervisor._leases = SimpleNamespace(  # noqa: SLF001
            guard_effect_start=lambda _principal, effect: effect())
        action_id = "evaluate-action"
        intent = RunnerLaunchIntent(
            "run",
            "job",
            action_id,
            "owner",
            1,
            digest,
            "launch",
            supervisor._marker_path(action_id),  # noqa: SLF001
        )
        process = SimpleNamespace(pid=12345, wait=lambda: 0)

        with mock.patch.object(
                supervisor, "_principal_for_intent",
                return_value=SimpleNamespace(entity_version=1)), \
                mock.patch.object(
                    supervisor, "_worker_result_binding", return_value=None), \
                mock.patch(
                    "waystone.runs.supervisor.subprocess.Popen",
                    return_value=process) as popen:
            handle = supervisor.launch(intent)

        payload = json.loads(handle.launch_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["environment_digest"], environment.digest)
        self.assertEqual(payload["schema"], "waystone-supervisor-launch-2")
        self.assertEqual(popen.call_args.kwargs["env"], environment.as_dict())
        self.assertNotIn("GIT_DIR", popen.call_args.kwargs["env"])
        self.assertNotIn("GIT_WORK_TREE", popen.call_args.kwargs["env"])
        self.assertNotIn("UV_WORKING_DIRECTORY", popen.call_args.kwargs["env"])

    def test_p2_environment_digest_is_part_of_invocation_authority(self):
        base = {
            "PATH": "/runner/bin",
            "HOME": "/home/runner",
            "LANG": "C.UTF-8",
            "GIT_DIR": "/integration/a.git",
        }

        def invocation(source):
            with mock.patch.dict(os.environ, source, clear=True):
                return RunnerInvocation(("runner", "--check"), Path("/candidate"))

        original = invocation(base)
        reordered = invocation(dict(reversed(tuple(base.items()))))
        allowed_changed = invocation({**base, "LANG": "ko_KR.UTF-8"})
        stripped_changed = invocation({**base, "GIT_DIR": "/integration/b.git"})

        self.assertEqual(original.environment.digest, reordered.environment.digest)
        self.assertNotEqual(
            original.environment.digest,
            allowed_changed.environment.digest,
        )
        self.assertNotEqual(
            StagedRunEngine._invocation_digest(original),  # noqa: SLF001
            StagedRunEngine._invocation_digest(allowed_changed),  # noqa: SLF001
        )
        self.assertEqual(
            original.environment.digest,
            stripped_changed.environment.digest,
        )
        self.assertEqual(
            StagedRunEngine._invocation_digest(original),  # noqa: SLF001
            StagedRunEngine._invocation_digest(stripped_changed),  # noqa: SLF001
        )

    def test_p2_promotion_launch_evidence_exposes_environment_digest(self):
        environment = build_runner_environment({
            "PATH": "/runner/bin",
            "HOME": "/home/runner",
            "LANG": "C.UTF-8",
        })
        context = RunnerCandidateContext(
            "a" * 40,
            "sha256:" + "b" * 64,
            "sha256:" + "c" * 64,
        )
        invocation = RunnerInvocation(
            ("/runner/bin/codex", "exec"),
            Path("/candidate"),
            context,
            environment,
        )
        artifact_store = mock.Mock(spec=ArtifactStore)
        captured: list[bytes] = []

        def write(content):
            captured.append(content)
            return SimpleNamespace(digest="sha256:" + "d" * 64, size=len(content))

        artifact_store.write.side_effect = write
        store = mock.Mock()
        store.get_artifact_reference.side_effect = RecordNotFoundError(
            "artifact", "verifier-launch:run:typed-independent-verify")
        store.get_entity.return_value = SimpleNamespace(version=3, state="running")
        engine = object.__new__(StagedRunEngine)
        engine.assembly = SimpleNamespace(
            artifact_store=artifact_store,
            store=store,
        )
        spec = SimpleNamespace(run_id="run", job_id="job")
        descriptor = RunnerExecutionDescriptor(
            environment.digest,
            invocation.argv[0],
            "sha256:" + "e" * 64,
            "codex:verifier",
            "sha256:" + "f" * 64,
        )
        descriptor_reference = ArtifactReference(
            "promotion-execution-descriptor:run",
            ArtifactReferenceKind.EVIDENCE,
            descriptor.digest,
            len(descriptor.canonical_bytes()),
        )

        engine._record_promotion_verifier_launch(  # noqa: SLF001
            spec,
            "run:attempt:1",
            "run:typed-independent-verify",
            invocation,
            descriptor,
            descriptor_reference,
        )

        self.assertEqual(len(captured), 1)
        payload = json.loads(captured[0])
        self.assertEqual(payload["environment_digest"], environment.digest)
        self.assertEqual(payload["execution_descriptor_digest"], descriptor.digest)
        self.assertEqual(
            payload["executable_content_digest"],
            descriptor.executable_content_digest)

    def test_p3_promote_verifier_receives_the_frozen_builder_environment(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        review_root = Path(temporary.name).resolve()
        executable = review_root / "bin" / "codex"
        executable.parent.mkdir()
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        environment = build_runner_environment({
            "PATH": str(executable.parent),
            "HOME": "/home/runner",
            "LANG": "C.UTF-8",
            "GIT_DIR": "/integration/.git",
            "GIT_WORK_TREE": "/integration",
            "UV_WORKING_DIRECTORY": "/integration",
        })
        context = RunnerCandidateContext(
            "a" * 40,
            "sha256:" + "b" * 64,
            "sha256:" + "c" * 64,
        )
        invocation = RunnerInvocation(
            (str(executable), "exec"),
            review_root,
            context,
            environment,
        )
        verifier_binding = RoleBinding(
            Role.VERIFIER,
            ExecutionCategory.EXTERNAL,
            "codex:verifier",
        )
        sandbox = SandboxContract("read-only", "isolated", "denied")

        def binding_for(role):
            binding = verifier_binding if role is Role.VERIFIER else RoleBinding(
                Role.WORKER,
                ExecutionCategory.EXTERNAL,
                "codex:worker",
            )
            return SimpleNamespace(
                binding=binding,
                binding_digest="sha256:" + (
                    "a" if role is Role.VERIFIER else "b") * 64,
            )

        engine = object.__new__(StagedRunEngine)
        engine.root = review_root
        engine.assembly = SimpleNamespace(
            profile=SimpleNamespace(binding_for=binding_for),
        )
        descriptor = freeze_runner_execution_descriptor(
            environment,
            executable="codex",
            cwd=review_root,
            verifier_backend=verifier_binding.backend,
            verifier_binding_digest="sha256:" + "a" * 64,
        )
        descriptor_reference = ArtifactReference(
            "promotion-execution-descriptor:run",
            ArtifactReferenceKind.EVIDENCE,
            descriptor.digest,
            len(descriptor.canonical_bytes()),
        )
        spec = SimpleNamespace(
            run_id="run",
            job_id="job",
            run_spec_digest=context.run_spec_digest,
            candidate={
                "target_oid": context.candidate_oid,
                "target_ref": "refs/waystone/candidates/run",
            },
        )
        request = SimpleNamespace(
            result=SimpleNamespace(result_oid=context.candidate_oid),
            review_root=review_root,
            review_root_fingerprint=context.root_fingerprint,
        )

        def execute_verifier(*args, **_kwargs):
            return args[8].executor(request)

        completed = SimpleNamespace(returncode=7, stderr=b"fixture failure")
        with mock.patch.object(
                engine, "_load_promotion_execution_descriptor",
                return_value=(
                    descriptor, descriptor_reference, environment)), \
                mock.patch.object(
                engine, "_promotion_verifier_result_schema",
                return_value=SimpleNamespace()), \
                mock.patch.object(
                    engine, "_stage_invocation", return_value=invocation), \
                mock.patch.object(engine, "_record_promotion_verifier_launch"), \
                mock.patch(
                    "waystone.runs.engine.fingerprint_materialized_root",
                    return_value=context.root_fingerprint), \
                mock.patch(
                    "waystone.runs.engine.load_verification_plan",
                    return_value=SimpleNamespace(
                        binding_for=lambda _role: SimpleNamespace(
                            binding=verifier_binding),
                        verifier_sandbox=sandbox,
                    )), \
                mock.patch(
                    "waystone.runs.engine.execute_verifier",
                    side_effect=execute_verifier), \
                mock.patch(
                    "waystone.runs.engine.subprocess.run",
                    return_value=completed) as run:
            result = engine._execute_promotion_verifier(  # noqa: SLF001
                spec,
                "run:attempt:1",
                SimpleNamespace(),
            )

        self.assertEqual(result.returncode, 7)
        self.assertEqual(run.call_args.kwargs["env"], environment.as_dict())
        self.assertNotIn("GIT_DIR", run.call_args.kwargs["env"])
        self.assertNotIn("GIT_WORK_TREE", run.call_args.kwargs["env"])
        self.assertNotIn("UV_WORKING_DIRECTORY", run.call_args.kwargs["env"])

    def test_p4_pythonpath_is_neither_inherited_nor_injected(self):
        environment = build_runner_environment({
            "PATH": "/runner/bin",
            "HOME": "/home/runner",
            "PYTHONPATH": "/integration/python",
            "PYTHONHOME": "/integration/python-home",
            "PYTHONSTARTUP": "/integration/startup.py",
        })

        self.assertNotIn("PYTHONPATH", environment.values)
        self.assertNotIn("PYTHONHOME", environment.values)
        self.assertNotIn("PYTHONSTARTUP", environment.values)
        self.assertFalse(hasattr(Supervisor, "_supervisor_environment"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
