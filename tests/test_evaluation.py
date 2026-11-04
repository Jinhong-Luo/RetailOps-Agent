"""防止分母偏差、跨组泄漏与不公平档位选择。"""
import sys
import random
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import ROOT, read_json, agent_args
from prepare_splits import build_splits
from summarize import summarize, select, verify_transport, load_run
from prepare_reuse import validate_reuse, verify_reused_simulations
from common import digest, write_json


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

    def test_network_contention_blocks_latency_tiebreak(self):
        manifest = self.manifest()
        manifest["config"]["efforts"] = ["low", "high"]
        low = summarize(manifest, [self.simulation("1_zh"), self.simulation("2_zh")])
        manifest["effort"] = "high"
        high = summarize(manifest, [self.simulation("1_zh"), self.simulation("2_zh")])
        low.update(wall_seconds_per_planned_task=100, timing_comparable=False)
        high.update(wall_seconds_per_planned_task=5, timing_comparable=True)
        self.assertIsNone(select([low, high])["effort"])

    def test_actual_effort_must_match_requested_effort(self):
        manifest = self.manifest()
        manifest["config"] = read_json(ROOT / "configs" / "codex_bridge.json")
        message = {"role": "assistant", "raw_data": {"bridge_metadata": {"actual_model": "gpt-6.1-sol", "reasoning_effort": "high", "instruction_source_sha256": {"instructions": "fixed"}}}}
        result = verify_transport(manifest, [{"messages": [message]}])
        self.assertEqual(result["mismatches"], 1)
        message["raw_data"]["bridge_metadata"]["reasoning_effort"] = "low"
        self.assertEqual(verify_transport(manifest, [{"messages": [message]}])["mismatches"], 0)

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

    def test_reuse_rejects_changed_seed_and_dropped_failure(self):
        source = self.manifest()
        source["config"]["seed"] = 300
        target = dict(source, split="all")
        seed = random.Random(300).randint(0, 1000000)
        sims = [dict(self.simulation("1_zh"), seed=seed), dict(self.simulation("2_zh", 0), seed=seed)]
        validate_reuse(source, target, sims)
        with self.assertRaises(ValueError):
            validate_reuse(source, target, sims[:1])
        sims[1]["seed"] += 1
        with self.assertRaises(ValueError):
            validate_reuse(source, target, sims)

    def test_reuse_requires_original_trajectory(self):
        sim = self.simulation("1_zh", 0)
        provenance = {"simulations": [{"task_id": "1_zh", "trial": 0, "sha256": digest(sim)}]}
        verify_reused_simulations(provenance, [sim])
        with self.assertRaises(ValueError):
            verify_reused_simulations(provenance, [self.simulation("1_zh", 1)])

    def test_equal_quality_missing_latency_cannot_select(self):
        manifest = self.manifest()
        manifest["config"]["efforts"] = ["low", "high"]
        low = summarize(manifest, [self.simulation("1_zh"), self.simulation("2_zh")])
        high = dict(low, effort="high", wall_seconds_per_planned_task=5)
        for value in (None, float("nan"), float("inf"), 0, -1):
            low["wall_seconds_per_planned_task"] = value
            self.assertIsNone(select([low, high])["effort"])

    def test_report_binds_source_content_even_if_score_unchanged(self):
        with tempfile.TemporaryDirectory(prefix=".evaluation-test-", dir=ROOT) as folder:
            path = Path(folder)
            write_json(path / "manifest.json", self.manifest())
            result_path = path / "native/zh-retail/results.json"
            sim = self.simulation("1_zh")
            write_json(result_path, {"simulations": [sim]})
            before = load_run(path)
            sim["messages"] = [{"role": "assistant", "content": "轨迹内容改变，分数不变"}]
            write_json(result_path, {"simulations": [sim]})
            after = load_run(path)
            self.assertEqual(before["success_rate"], after["success_rate"])
            self.assertNotEqual(before["source_artifacts_sha256"], after["source_artifacts_sha256"])

    def test_full_report_groups_have_no_invented_wall_time(self):
        split = read_json(ROOT / "data/splits.json")
        manifest = self.manifest()
        manifest.update(split="all", split_digest=digest(split), task_ids=split["all_task_ids"], local_task_ids=[split["task_features"][i]["local_task_id"] for i in split["all_task_ids"]])
        with tempfile.TemporaryDirectory(prefix=".evaluation-test-", dir=ROOT) as folder:
            path = Path(folder)
            write_json(path / "manifest.json", manifest)
            write_json(path / "native/zh-retail/results.json", {"simulations": [self.simulation(t) for t in manifest["local_task_ids"]]})
            result = load_run(path)
            self.assertEqual({k: v["expected"] for k, v in result["group_breakdown"].items()}, {"tune": 25, "dev": 13, "heldout": 12})
            self.assertTrue(all(v["wall_seconds"] is None for v in result["group_breakdown"].values()))

    def test_separate_simulation_file_is_bound_to_report(self):
        with tempfile.TemporaryDirectory(prefix=".evaluation-test-", dir=ROOT) as folder:
            path = Path(folder)
            write_json(path / "manifest.json", self.manifest())
            write_json(path / "native/zh-retail/results.json", {"simulations": []})
            sim_path = path / "native/zh-retail/simulations/a.json"
            sim = self.simulation("1_zh")
            write_json(sim_path, sim)
            before = load_run(path)
            sim["messages"] = [{"role": "assistant", "content": "仅分离文件发生改变"}]
            write_json(sim_path, sim)
            after = load_run(path)
            key = "native/zh-retail/results.json"
            self.assertEqual(before["source_artifacts_sha256"][key], after["source_artifacts_sha256"][key])
            self.assertNotEqual(before["source_artifacts_sha256"], after["source_artifacts_sha256"])


if __name__ == "__main__":
    unittest.main()
