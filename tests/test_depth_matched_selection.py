"""Depth target checks use synthetic metadata only."""

import copy
import unittest

from scripts.select_depth_matched_baselines import METHODS, SEEDS, reference_rule


class DepthTargetTests(unittest.TestCase):
    def fixture(self):
        return {
            "experiment_id": "search-replication-20261109",
            "finalist_selection_frozen": True,
            "runs": [
                {
                    "method": method,
                    "seed": seed,
                    "run_id": f"{method}-{seed}",
                    "status": "failed" if seed == 103 else "completed",
                    "finalist": {
                        "state_id": f"{method}-{seed}",
                        "genes_deleted": 1500 + i,
                    },
                }
                for method in METHODS
                for i, seed in enumerate(SEEDS)
            ],
        }

    def test_all_runs_enter_mean_independent_of_whole_cell_outcomes(self):
        reference = self.fixture()
        rule = reference_rule(reference, "a" * 64)
        self.assertEqual(rule["maximum_total_deletions"], 1502)
        self.assertEqual(rule["reference_count"], 15)
        changed = copy.deepcopy(reference)
        for row in changed["runs"]:
            row["whole_cell_result"] = "failed"
        self.assertEqual(rule, reference_rule(changed, "a" * 64))

    def test_omitted_duplicate_and_missing_finalist_rejected(self):
        ref = self.fixture()
        variants = [copy.deepcopy(ref) for _ in range(3)]
        variants[0]["runs"].pop()
        variants[1]["runs"][-1] = variants[1]["runs"][0]
        variants[2]["runs"][0]["finalist"] = None
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                reference_rule(variant, "a" * 64)


if __name__ == "__main__":
    unittest.main()
