#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Run the focused 0.13 contract suite; retired legacy modules are not imported."""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_ROOT))
sys.path.insert(0, str(TEST_ROOT.parent.parent))

MODULES = (
    "test_completion_contract",
    "test_project_brief",
    "test_project_context",
    "test_project",
    "test_work_brief",
    "test_worker_result",
    "test_run_assurance",
    "test_promote_evidence",
    "test_run_cancel",
    "test_run_domain",
    "test_run_prompt",
    "test_run_effects",
    "test_run_lease",
    "test_run_store",
    "test_run_supervisor",
    "test_runner_env_provenance",
    "test_run_transport",
    "test_run_verify",
    "test_run_spec",
    "test_run_outcome",
    "test_run_preflight",
    "test_run_observe",
    "test_run_cli",
    "test_promotion_integration_ref",
    "test_review_runs_layout",
    "test_review_findings",
    "test_review_protocol",
    "test_review_canonical_root",
    "test_review_settlement",
    "test_improve",
    "test_overlay",
    "test_policy",
    "test_release",
    "test_hooks",
    "test_git_ambient_authority",
)


def suite() -> unittest.TestSuite:
    loader = unittest.defaultTestLoader
    result = unittest.TestSuite()
    for name in MODULES:
        result.addTests(loader.loadTestsFromModule(importlib.import_module(name)))
    return result


if __name__ == "__main__":
    raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite()).wasSuccessful() else 1)
