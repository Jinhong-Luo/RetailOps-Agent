"""严格校验后复制开发集检查点到全量新目录，原生续跑只补缺失任务。"""
import argparse
import hashlib
import json
import random
import shutil
from pathlib import Path
from common import ROOT, digest, read_json, write_json
from summarize import load_run, select


def simulations(path):
    source = path / "native" / "zh-retail" / "results.json"
    raw = read_json(source)
    return raw.get("simulations") or [read_json(p) for p in sorted((source.parent / "simulations").glob("*.json"))]


def validate_reuse(source, target, source_sims):
    """校验配置、题目映射、每次试验种子与源结果完整性，不容许挑选成功题。"""
    for key in ("config", "effort", "upstream_commit", "split_digest", "num_trials", "environment"):
        if source.get(key) != target.get(key):
            raise ValueError("复用条件不一致：" + key)
    if source["split"] != "dev" or source["scope"] != "分组评测" or target["split"] != "all" or target["scope"] != "分组评测":
        raise ValueError("只允许完整开发集向完整50题扩展")
    source_map = dict(zip(source["task_ids"], source["local_task_ids"]))
    target_map = dict(zip(target["task_ids"], target["local_task_ids"]))
    if not source_map or any(target_map.get(k) != v for k, v in source_map.items()):
        raise ValueError("目标任务及本地化映射必须包含全部源任务")
    rng = random.Random(source["config"]["seed"])
    seeds = [rng.randint(0, 1000000) for _ in range(source["num_trials"])]
    expected = {(task, trial, seeds[trial]) for task in source["local_task_ids"] for trial in range(source["num_trials"])}
    observed = [(s["task_id"], s["trial"], s["seed"]) for s in source_sims]
    if len(observed) != len(set(observed)) or set(observed) != expected:
        raise ValueError("源结果任务、试次或种子不完整或重复，禁止选择性复用")
    if any((s.get("reward_info") or {}).get("reward") is None or s.get("termination_reason") in {"infrastructure_error", "unexpected_error", "agent_error", "user_error"} for s in source_sims):
        raise ValueError("源结果含未评分或基础设施失败，不能作为完整开发集复用")


def verify_reused_simulations(provenance, target_sims):
    indexed = {(s["task_id"], s["trial"]): s for s in target_sims}
    for record in provenance["simulations"]:
        actual = indexed.get((record["task_id"], record["trial"]))
        if actual is None or digest(actual) != record["sha256"]:
            raise ValueError("已复用轨迹缺失或被改写，禁止形成混合结果报告")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--selection-report", type=Path, required=True)
    args = parser.parse_args()
    source, target = args.source.resolve(), args.target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise SystemExit("源和目标目录必须彼此独立")
    if (target / "native").exists() or (target / "execution.json").exists() or (target / "reuse-provenance.json").exists():
        raise SystemExit("目标已存在原生结果，禁止覆盖；请使用新目录")
    source_manifest = read_json(source / "manifest.json")
    target_manifest = read_json(target / "manifest.json")
    for manifest in (source_manifest, target_manifest):
        unsigned = dict(manifest)
        signature = unsigned.pop("signature")
        if digest(unsigned) != signature:
            raise SystemExit("运行清单签名不匹配")
    split = read_json(ROOT / "data/splits.json")
    if target_manifest["task_ids"] != split["all_task_ids"] or source_manifest["task_ids"] != split["splits"]["dev"]:
        raise SystemExit("必须原样使用冻结的开发集与完整50题集合")
    for manifest in (source_manifest, target_manifest):
        expected_ids = [split["task_features"][i]["local_task_id"] for i in manifest["task_ids"]]
        if manifest["split_digest"] != digest(split) or manifest["local_task_ids"] != expected_ids or len(expected_ids) != len(set(expected_ids)):
            raise SystemExit("冻结分组摘要或本地化任务映射不匹配")
    report = read_json(args.selection_report)
    expected_efforts = {"low", "medium", "high", "xhigh", "max", "ultra"}
    if {r["effort"] for r in report["runs"]} != expected_efforts or set(source_manifest["config"]["efforts"]) != expected_efforts or any(r.get("candidate_efforts") != source_manifest["config"]["efforts"] for r in report["runs"]):
        raise SystemExit("复用要求六档完整报告且各候选配置一致")
    selected = select(report["runs"])
    if not selected["effort"] or selected["effort"] != target_manifest["effort"] or report["selection"]["effort"] != selected["effort"]:
        raise SystemExit("目标档位必须与完整六档开发集选择一致")
    row = load_run(source)
    recorded = next(r for r in report["runs"] if r["effort"] == source_manifest["effort"])
    if row != recorded or not row["complete_for_selection"] or row["wall_seconds"] is None:
        raise SystemExit("源结果与冻结选优报告不符或尚未完整结束")
    execution = read_json(source / "execution.json")
    if execution["exit_code"] != 0 or not execution["actual_model_calls_requested"]:
        raise SystemExit("源运行没有成功结束的真实执行记录")
    source_sims = simulations(source)
    validate_reuse(source_manifest, target_manifest, source_sims)
    provenance = {"schema_version": 1, "source_run": str(source), "source_signature": source_manifest["signature"], "target_signature": target_manifest["signature"], "source_results_sha256": hashlib.sha256((source / "native/zh-retail/results.json").read_bytes()).hexdigest(), "selection_report_sha256": hashlib.sha256(args.selection_report.read_bytes()).hexdigest(), "source_wall_seconds": row["wall_seconds"], "reused_simulations": len(source_sims), "remaining_simulations": len(target_manifest["local_task_ids"]) * target_manifest["num_trials"] - len(source_sims), "simulations": [{"id": s["id"], "task_id": s["task_id"], "trial": s["trial"], "seed": s["seed"], "sha256": digest(s)} for s in source_sims], "note": "所有源轨迹连同失败评分原样保留；原生按task_id/trial/seed跳过，新增运行补齐剩余任务。全50题包含用于选档的开发集，不是独立测试。"}
    provenance["source_artifacts_sha256"] = row["source_artifacts_sha256"]
    shutil.copytree(source / "native", target / "native")
    verify_reused_simulations(provenance, simulations(target))
    write_json(target / "reuse-provenance.json", provenance)
    print(json.dumps({"reused": len(source_sims), "remaining": provenance["remaining_simulations"], "next": "先对目标运行native-dry-run，再用同一命令execute续跑"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
