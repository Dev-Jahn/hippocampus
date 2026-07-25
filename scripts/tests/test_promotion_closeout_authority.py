#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Stage-aware final-result authority contracts for durable run closeout."""
from __future__ import annotations

from support import *  # noqa: F401,F403

from dataclasses import replace

import test_run_outcome
from waystone.jobs.completion import LifecycleStage
from waystone.runs import outcome as outcome_module
from waystone.runs.outcome import OutcomeBindingRefusal


class PromotionCloseoutAuthorityTests(unittest.TestCase):
    def test_p3_explore_keeps_completed_worker_result_authority(self):
        fixture = test_run_outcome.OutcomeFixture(self)
        spec, result = fixture.ready_run()

        authority = outcome_module._final_result_authority(  # noqa: SLF001
            fixture.assembly,
            spec,
        )

        self.assertEqual(authority.result_digest, result.digest)
        self.assertEqual(
            authority.completion_references[0],
            {
                "reference_id": f"worker-result:{spec.run_id}:attempt:1",
                "digest": result.digest,
            },
        )

    def test_p2_promote_refuses_any_final_worker_result(self):
        fixture = test_run_outcome.OutcomeFixture(self)
        spec, _result = fixture.ready_run()
        promote = replace(spec, lifecycle_stage=LifecycleStage.PROMOTE)

        with self.assertRaisesRegex(
                OutcomeBindingRefusal,
                "promotion final attempt must not have a worker result"):
            outcome_module._final_result_authority(  # noqa: SLF001
                fixture.assembly,
                promote,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
