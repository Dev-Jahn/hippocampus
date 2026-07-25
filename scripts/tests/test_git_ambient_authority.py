#!/usr/bin/env python3
"""Engine-owned Git operations ignore ambient repository authority."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import contextlib
import os
import subprocess
from unittest import mock

from test_work_brief import init_project
from waystone.adapters import git as git_adapter
from waystone.features.review_layout import new_run_id
from waystone.jobs import completion
from waystone.reviews import findings
from waystone.runs import effects as effects_module
from waystone.runs import outcome as outcome_module


class GitAmbientAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "A"
        self.unrelated = self.base / "B"
        self.root.mkdir()
        self.unrelated.mkdir()
        self.root_head, self.frame = init_project(self.root)
        init_repo(self.unrelated)
        (self.unrelated / "f.txt").write_text("unrelated\n", encoding="utf-8")
        self._raw_git(self.unrelated, "add", "f.txt")
        self._raw_git(self.unrelated, "commit", "-qm", "distinguish unrelated")
        self.unrelated_head = self._raw_git(self.unrelated, "rev-parse", "HEAD")

    @staticmethod
    def _raw_git(root: Path, *args: str, input_bytes: bytes | None = None) -> str:
        environment = {
            name: value for name, value in os.environ.items()
            if not name.startswith("GIT_")
        }
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            input=input_bytes,
            capture_output=True,
            env=environment,
            check=True,
        )
        return result.stdout.decode().strip()

    @staticmethod
    def _tree_bytes(root: Path, relative: str) -> dict[str, bytes]:
        directory = root / relative
        if not directory.exists():
            return {}
        return {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }

    @contextlib.contextmanager
    def _ambient(self, values: dict[str, str]):
        git_names = [name for name in os.environ if name.startswith("GIT_")]
        previous = {name: os.environ[name] for name in git_names}
        for name in git_names:
            os.environ.pop(name)
        try:
            with mock.patch.dict(os.environ, values):
                yield
        finally:
            for name in tuple(os.environ):
                if name.startswith("GIT_"):
                    os.environ.pop(name)
            os.environ.update(previous)

    def test_p1_adapter_reads_and_ref_mutations_stay_in_canonical_repository(self):
        scenarios = (
            ("absolute", {"GIT_DIR": str(self.unrelated / ".git")}),
            ("relative", {"GIT_DIR": "../B/.git"}),
            ("dir-and-work-tree", {
                "GIT_DIR": str(self.unrelated / ".git"),
                "GIT_WORK_TREE": str(self.unrelated),
            }),
        )
        unrelated_before = {
            "refs": self._tree_bytes(self.unrelated, ".git/refs"),
            "objects": self._tree_bytes(self.unrelated, ".git/objects"),
            "tracked": (self.unrelated / "f.txt").read_bytes(),
        }
        for label, ambient in scenarios:
            ref = f"refs/waystone/ambient/{label}"
            with self.subTest(label=label):
                with self._ambient(ambient):
                    self.assertEqual(
                        git_adapter.git_full_sha(self.root), self.root_head)
                    self.assertEqual(
                        git_adapter.git_read_bytes(self.root, "show", "HEAD:src.py"),
                        b"baseline = False\n",
                    )
                    self.assertEqual(
                        git_adapter.git(self.root, "rev-parse", "HEAD"),
                        self.root_head,
                    )
                    self.assertIn(
                        b"schema: waystone-project-brief-1",
                        completion._git_bytes(  # noqa: SLF001
                            self.root,
                            self.root_head,
                            "PROJECT_BRIEF.md",
                            "project brief",
                        ),
                    )
                    rc, _stdout, stderr = git_adapter.git_rc(
                        self.root, "update-ref", ref, self.root_head)
                    self.assertEqual(rc, 0, stderr)
                self.assertEqual(
                    self._raw_git(self.root, "rev-parse", ref), self.root_head)
                self.assertEqual(
                    self._raw_git(
                        self.unrelated, "show-ref", "--verify", "--hash", ref
                    ) if (self.unrelated / ".git" / ref).exists() else "",
                    "",
                )
                self.assertEqual(
                    {
                        "refs": self._tree_bytes(self.unrelated, ".git/refs"),
                        "objects": self._tree_bytes(self.unrelated, ".git/objects"),
                        "tracked": (self.unrelated / "f.txt").read_bytes(),
                    },
                    unrelated_before,
                )

    def test_p2_outcome_objects_and_ref_stay_in_canonical_repository(self):
        run_id = new_run_id()
        unrelated_before = {
            "refs": self._tree_bytes(self.unrelated, ".git/refs"),
            "objects": self._tree_bytes(self.unrelated, ".git/objects"),
        }
        ambient = {
            "GIT_DIR": str(self.unrelated / ".git"),
            "GIT_WORK_TREE": str(self.unrelated),
        }
        with self._ambient(ambient):
            commit = outcome_module._prepare_ledger_commit(  # noqa: SLF001
                self.root,
                None,
                run_id,
                b"closeout\n",
                b"outcome\n",
            )
            rc, _stdout, stderr = effects_module._git_rc(  # noqa: SLF001
                self.root,
                "update-ref",
                outcome_module.OUTCOME_LEDGER_REF,
                commit,
            )
            self.assertEqual(rc, 0, stderr)

        self.assertEqual(
            self._raw_git(
                self.root, "rev-parse", outcome_module.OUTCOME_LEDGER_REF),
            commit,
        )
        self._raw_git(self.root, "cat-file", "-e", f"{commit}^{{commit}}")
        self.assertEqual(
            {
                "refs": self._tree_bytes(self.unrelated, ".git/refs"),
                "objects": self._tree_bytes(self.unrelated, ".git/objects"),
            },
            unrelated_before,
        )

    def test_p3_stale_linked_gitdir_cannot_revive_superseded_objective(self):
        objective_head = self.root_head
        linked = self.base / "stale"
        self._raw_git(
            self.root, "worktree", "add", "-q", "-b", "stale-objective", str(linked))
        brief_path = self.root / "PROJECT_BRIEF.md"
        brief_path.write_bytes(brief_path.read_bytes().replace(
            b"Produce the intended result.",
            b"Produce the revised canonical result.",
        ))
        self._raw_git(self.root, "add", "PROJECT_BRIEF.md")
        self._raw_git(self.root, "commit", "-qm", "revise canonical objective")
        stale_gitdir = self._raw_git(linked, "rev-parse", "--absolute-git-dir")
        payload = self._disposition_payload(
            self.frame.fact_ref("commitment/outcome").to_dict())

        with self._ambient({
            "GIT_DIR": stale_gitdir,
            "GIT_WORK_TREE": str(linked),
        }), self.assertRaises(findings.ObjectiveSuperseded):
            findings.validate_disposition_authority(self.root, payload)

        self.assertNotEqual(
            self._raw_git(self.root, "rev-parse", "HEAD"), objective_head)

    def test_p4_work_tree_only_is_ignored(self):
        (self.unrelated / "f.txt").write_text("dirty unrelated\n", encoding="utf-8")
        with self._ambient({"GIT_WORK_TREE": str(self.unrelated)}):
            self.assertEqual(
                git_adapter.git_full_sha(self.root), self.root_head)
            self.assertEqual(
                git_adapter.git_read_bytes(
                    self.root, "status", "--porcelain=v1"),
                b"",
            )

    def test_p4_effects_delegate_is_sanitized(self):
        ref = "refs/waystone/ambient/effects-delegation"
        unrelated_before = self._tree_bytes(self.unrelated, ".git/refs")
        with self._ambient({"GIT_DIR": str(self.unrelated / ".git")}), \
                mock.patch.object(
                    effects_module.git_adapter,
                    "git_rc",
                    wraps=effects_module.git_adapter.git_rc,
                ) as delegated:
            rc, _stdout, stderr = effects_module._git_rc(  # noqa: SLF001
                self.root, "update-ref", ref, self.root_head)
        self.assertEqual(rc, 0, stderr)
        delegated.assert_called_once_with(
            self.root, "update-ref", ref, self.root_head)
        self.assertEqual(self._raw_git(self.root, "rev-parse", ref), self.root_head)
        self.assertEqual(
            self._tree_bytes(self.unrelated, ".git/refs"), unrelated_before)

    def test_central_environment_rejects_repository_selection_overrides(self):
        with self._ambient({
            "GIT_DIR": "/ambient/repository",
            "GIT_CONFIG_COUNT": "1",
        }):
            environment = git_adapter.build_git_environment(
                overrides={"GIT_INDEX_FILE": "/operation/index"})
        self.assertFalse(any(
            name.startswith("GIT_")
            for name in environment
            if name not in {"GIT_INDEX_FILE", "GIT_PAGER"}
        ))
        self.assertEqual(environment["GIT_INDEX_FILE"], "/operation/index")
        self.assertEqual(environment["GIT_PAGER"], "cat")
        self.assertEqual(environment["LC_ALL"], "C")
        completed = subprocess.CompletedProcess(
            ["git"], 0, stdout="", stderr="")
        with mock.patch.object(
                git_adapter.subprocess, "run", return_value=completed) as invoked:
            git_adapter.git_rc(self.root, "rev-parse", "HEAD")
            read_environment = invoked.call_args.kwargs["env"]
            git_adapter.git_rc(
                self.root, "update-ref", "refs/heads/probe", self.root_head)
            mutation_environment = invoked.call_args.kwargs["env"]
        self.assertEqual(read_environment["GIT_OPTIONAL_LOCKS"], "0")
        self.assertNotIn("GIT_OPTIONAL_LOCKS", mutation_environment)
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                git_adapter.build_git_environment(overrides={name: "/forged"})

    @staticmethod
    def _disposition_payload(objective_ref: dict) -> dict:
        digest = "sha256:" + "a" * 64
        return {
            "schema": findings.DISPOSITION_SCHEMA,
            "finding_id": new_run_id(),
            "finding_digest": digest,
            "confirmed_validation_digest": digest,
            "revision": 1,
            "supersedes_digest": None,
            "objective_ref": objective_ref,
            "lifecycle_stage": "explore",
            "applies_to": {
                "promotion_lineage_id": new_run_id(),
                "candidate_digest": digest,
                "result_digest": digest,
            },
            "impact": "major",
            "exposure": "edge",
            "relevance": "current-objective",
            "disposition": "fix-now",
            "remediation_scope": "local",
            "estimated_cost": "low",
            "rationale": "ambient Git authority must not revive stale objectives",
            "clearance": None,
            "decided_by": {
                "role": "coordinator",
                "binding_digest": digest,
                "principal": None,
            },
            "materialized_task_id": None,
        }


if __name__ == "__main__":
    unittest.main()
