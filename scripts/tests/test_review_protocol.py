#!/usr/bin/env python3
"""Focused review protocol contracts; legacy round gates are retired."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import contextlib
import io
from types import SimpleNamespace
from unittest import mock

from waystone.cli import review_group
from waystone.features.review_layout import new_run_id
from waystone.reviews import findings


class ReviewProtocolTests(unittest.TestCase):
    def test_review_group_exposes_claim_validation_disposition_materialization_and_attach(self):
        source = Path(review_group.__file__).read_text(encoding="utf-8")
        for command in ("ingest", "validate", "disposition", "materialize", "attach"):
            self.assertIn(f'add_parser("{command}")', source)
        self.assertNotIn('add_parser("triage")', source)
        self.assertNotIn('add_parser("freeze")', source)

    def test_finding_schema_keeps_claim_validation_and_disposition_separate(self):
        self.assertNotEqual(findings.CLAIM_SCHEMA, findings.VALIDATION_SCHEMA)
        self.assertNotEqual(findings.VALIDATION_SCHEMA, findings.DISPOSITION_SCHEMA)

    def test_p5_review_attach_is_a_public_minimal_promotion_lineage_bridge(self):
        promotion_run_id = new_run_id()
        review_run_id = new_run_id()
        context = SimpleNamespace()
        context.canonical_root = Path("/unused")
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    review_group,
                    "_review_context",
                    return_value=context,
                ), \
                mock.patch.object(
                    review_group, "attach_review",
                    return_value=SimpleNamespace(cycle=1),
                ) as attach, contextlib.redirect_stdout(io.StringIO()):
            result = review_group.main([
                "attach", promotion_run_id, review_run_id, "--root", directory,
            ])
        self.assertEqual(result, 0)
        attach.assert_called_once_with(
            context, promotion_run_id, review_run_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
