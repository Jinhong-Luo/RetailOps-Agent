"""防止分母偏差、跨组泄漏与不公平档位选择。"""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import ROOT, read_json, agent_args
from prepare_splits import build_splits
from summarize import summarize, select


class EvaluationTests(unittest.TestCase):
    def manifest(self):
        return {"local_task_ids": ["1_zh", "2_zh"], "num_trials": 1, "task_ids": ["1", "2"], "effort": "low", "split": "dev", "scope": "分组评测", "split_digest": "fixed", "upstream_commit": "fixed", "config": {"variant": "A0", "selection_success_tolerance": .02}}

    def simulation(self, task, reward=1):
        return {"task_id": task, "trial": 0, "reward_info": {"reward": reward}, "duration": 1, "termination_reason": "user_stop"}

    def test_missing_not_dropped_from_denominator(self):
        result = summarize(self.manifest(), [self.simulation("1_zh")])
        self.assertEqual(result["expected"], 2)
        self.assertEqual(result["success_rate"], .5)
        self.assertFalse(result["complete_for_selection"])
        self.assertIsNone(result["actual_cost"])

    def test_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            summarize(self.manifest(), [self.simulation("1_zh"), self.simulation("1_zh")])

    def test_partial_matrix_no_selection(self):
        complete = summarize(self.manifest(), [self.simulation("1_zh"), self.simulation("2_zh")])
        partial = summarize(self.manifest(), [])
        self.assertIsNone(select([complete, partial])["effort"])

    def test_heldout_cannot_select(self):
        manifest = self.manifest()
        manifest["split"] = "heldout"
        row = summarize(manifest, [self.simulation("1_zh"), self.simulation("2_zh")])
        self.assertIsNone(select([row])["effort"])

    def test_duplicate_effort_cannot_select(self):
        manifest = self.manifest()
        manifest["config"]["efforts"] = ["low", "high"]
        low = summarize(manifest, [self.simulation("1_zh"), self.simulation("2_zh")])
        manifest["effort"] = "high"
        high = summarize(manifest, [self.simulation("1_zh"), self.simulation("2_zh")])
        low["wall_seconds_per_planned_task"] = 3
        high["wall_seconds_per_planned_task"] = 4
        self.assertEqual(select([low, high])["effort"], "low")
        self.assertIsNone(select([low, high, dict(low)])["effort"])

    def test_smoke_is_complete_but_not_selectable(self):
        manifest = self.manifest()
        manifest["scope"] = "小样本连通测试"
        row = summarize(manifest, [self.simulation("1_zh"), self.simulation("2_zh")])
        self.assertTrue(row["complete_for_summary"])
        self.assertFalse(row["complete_for_selection"])

    def test_shared_entity_transitive_group(self):
        def task(i, entities):
            return {"id": i, "evaluation_criteria": {"actions": [{"name": "test", "arguments": entities}]}, "user_scenario": {"instructions": {"reason_for_call": "scenario_" + i}}}
        tasks = [task("a", {"user_id": "u"}), task("b", {"user_id": "u", "order_id": "o"}), task("c", {"order_id": "o"})]
        result = build_splits(tasks, {i: {"canonical_task_id": i} for i in "abc"}, list("abc"))
        self.assertEqual(len(result["groups"]), 1)

    def test_frozen_split_no_overlap(self):
        split = read_json(ROOT / "data" / "splits.json")
        ids = [x for values in split["splits"].values() for x in values]
        self.assertEqual(len(ids), 50)
        self.assertEqual(len(set(ids)), 50)
        self.assertEqual(set(ids), set(split["all_task_ids"]))
        entities = {}
        for name, values in split["splits"].items():
            for task in values:
                for entity in split["task_features"][task]["entities"]:
                    self.assertIn(entities.setdefault(entity, name), [name])

    def test_effort_transmitted_without_litellm_filter(self):
        config = read_json(ROOT / "configs" / "codex_bridge.json")
        for effort in config["efforts"]:
            self.assertEqual(agent_args(config, effort)["extra_body"]["reasoning_effort"], effort)


if __name__ == "__main__":
    unittest.main()
