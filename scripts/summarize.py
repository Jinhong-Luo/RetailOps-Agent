"""使用上游原始reward汇总；缺失和失败不移出分母。"""
import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from common import ROOT, digest, read_json, write_json


def verify_transport(manifest, simulations):
    """核对桥接逐消息元数据，避免只凭请求配置宣称各强度已经生效。"""
    config = manifest["config"]
    applies = config.get("api_key_env") == "CODEX_BRIDGE_TOKEN" or config.get("local_placeholder_api_key") == "local-codex"
    result = {"required": applies, "checked_messages": 0, "mismatches": 0, "missing_metadata": 0, "instruction_hashes": [], "instruction_fingerprints": []}
    if not applies:
        return result
    hashes = set()
    fingerprints = set()
    for sim in simulations:
        for message in sim.get("messages") or []:
            raw = message.get("raw_data") or {}
            if not raw:
                continue
            meta = raw.get("bridge_metadata")
            if not meta:
                result["missing_metadata"] += 1
                continue
            role = message.get("role")
            model = config["agent_model"] if role == "assistant" else config["user_model"]
            effort = manifest["effort"] if role == "assistant" else config["user_model_args"]["extra_body"]["reasoning_effort"]
            result["checked_messages"] += 1
            if meta.get("actual_model") != model.split("/")[-1] or meta.get("reasoning_effort") != effort:
                result["mismatches"] += 1
            hashes.update((meta.get("instruction_source_sha256") or {}).values())
            fingerprints.add(digest(meta.get("instruction_source_sha256") or {}))
    result["instruction_hashes"] = sorted(hashes)
    result["instruction_fingerprints"] = sorted(fingerprints)
    return result


def summarize(manifest, simulations):
    expected = {(task, trial) for task in manifest["local_task_ids"] for trial in range(manifest["num_trials"])}
    found = {}
    for sim in simulations:
        key = (sim["task_id"], sim["trial"])
        if key not in expected:
            raise ValueError("结果包含计划外任务或试次")
        if key in found:
            raise ValueError("结果包含重复任务试次；不能选择性保留最好结果")
        found[key] = sim
    successes = sum((s.get("reward_info") or {}).get("reward") == 1 for s in found.values())
    scored = sum((s.get("reward_info") or {}).get("reward") is not None for s in found.values())
    reasons = Counter(s.get("termination_reason", "unknown") for s in found.values())
    errors = sum(reasons[r] for r in ("infrastructure_error", "unexpected_error", "agent_error", "user_error"))
    calls = tool_calls = prompt_tokens = completion_tokens = 0
    for sim in found.values():
        for message in sim.get("messages") or []:
            usage = message.get("usage") or {}
            if usage:
                calls += 1
                prompt_tokens += usage.get("prompt_tokens", 0) or 0
                completion_tokens += usage.get("completion_tokens", 0) or 0
            tool_calls += len(message.get("tool_calls") or [])
    durations = [s["duration"] for s in found.values() if s.get("duration") is not None]
    config = dict(manifest["config"])
    comparable = {"config": config, "task_ids": manifest["task_ids"], "trials": manifest["num_trials"], "split_digest": manifest["split_digest"], "upstream_commit": manifest["upstream_commit"], "scope": manifest["scope"], "split": manifest["split"], "environment": manifest.get("environment")}
    return {"effort": manifest["effort"], "candidate_efforts": config.get("efforts", []), "variant": config["variant"], "split": manifest["split"], "scope": manifest["scope"], "comparison_key": digest(comparable), "expected": len(expected), "completed": len(found), "missing": len(expected) - len(found), "scored": scored, "successful": successes, "success_rate": successes / len(expected) if expected else None, "infrastructure_or_generation_errors": errors, "complete_for_summary": scored == len(expected) and not errors and bool(expected), "complete_for_selection": scored == len(expected) and not errors and bool(expected) and manifest["split"] == "dev" and manifest["scope"] == "分组评测", "mean_duration_seconds": statistics.mean(durations) if durations else None, "total_duration_seconds": sum(durations), "observed_message_usage_count": calls, "tool_calls": tool_calls, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "actual_cost": None, "cost_note": "上游旧LiteLLM对未知模型定价失败返回0；本报告不将0当免费。需服务账单核验，消息用量不含所有裁判或失败请求。", "termination_reasons": dict(reasons), "selection_tolerance": config["selection_success_tolerance"]}


def select(rows):
    if not rows:
        return {"effort": None, "reason": "没有结果"}
    if any(not r["complete_for_selection"] for r in rows):
        return {"effort": None, "reason": "至少一档未完成或不是完整开发集，不能正式选优"}
    candidates = set(rows[0].get("candidate_efforts", []))
    observed = {r["effort"] for r in rows}
    if len(rows) != len(observed):
        return {"effort": None, "reason": "同一思考强度出现多份结果，禁止选择性比较重复运行"}
    if len(rows) < 2 or not candidates or observed != candidates or len(rows) != len(candidates):
        return {"effort": None, "reason": "必须提供配置中的全部候选强度，不能遗漏失败档位"}
    if len({r["comparison_key"] for r in rows}) != 1:
        return {"effort": None, "reason": "配置、任务或试次数不一致"}
    if any(r["split"] != "dev" or r["scope"] != "分组评测" for r in rows):
        return {"effort": None, "reason": "只允许使用完整开发集选择，连通测试和保留集不参与"}
    best = max(r["success_rate"] for r in rows)
    quality_eligible = [r for r in rows if r["success_rate"] >= best - r["selection_tolerance"]]
    if len(quality_eligible) == 1:
        return {"effort": quality_eligible[0]["effort"], "reason": "完整开发集中仅该档达到预设质量阈值；选择依据为质量，未宣称耗时最优", "actual_cost_known": False}
    if any(not r.get("timing_comparable", True) for r in quality_eligible):
        return {"effort": None, "reason": "质量候选并列且至少一档耗时受外部负载干扰；需补跑可比耗时，不能依据拥塞时长选优"}
    eligible = [r for r in quality_eligible if r.get("wall_seconds_per_planned_task") is not None]
    if not eligible:
        return {"effort": None, "reason": "缺少可比较的耗时记录"}
    selected = min(eligible, key=lambda r: r["wall_seconds_per_planned_task"])
    return {"effort": selected["effort"], "reason": "在成功率距最佳不超过预设阈值的档位中，累计实际运行时间除以计划任务数最低，包含本脚本记录的重试和续跑时间；需冻结后单独评测保留集", "actual_cost_known": False}


def load_run(path):
    manifest = read_json(path / "manifest.json")
    source = path / "native" / "zh-retail" / "results.json"
    simulations = []
    if source.exists():
        raw = read_json(source)
        simulations = raw.get("simulations") or []
        if not simulations and (source.parent / "simulations").is_dir():
            simulations = [read_json(p) for p in sorted((source.parent / "simulations").glob("*.json"))]
    result = summarize(manifest, simulations)
    transport = verify_transport(manifest, simulations)
    result["transport_verification"] = transport
    if transport["required"]:
        result["comparison_key"] = digest({"experiment": result["comparison_key"], "instruction_fingerprints": transport["instruction_fingerprints"]})
        if transport["mismatches"] or transport["missing_metadata"] or not transport["checked_messages"] or len(transport["instruction_fingerprints"]) != 1:
            result["complete_for_selection"] = False
    history = path / "execution-history.jsonl"
    if history.exists():
        executions = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif (path / "execution.json").exists():
        executions = [read_json(path / "execution.json")]
    else:
        executions = []
    result["wall_seconds"] = sum(e["elapsed_seconds"] for e in executions) if executions else None
    result["recorded_invocations"] = len(executions)
    result["wall_seconds_per_planned_task"] = result["wall_seconds"] / result["expected"] if executions and result["expected"] else None
    result["duration_note"] = "mean_duration_seconds仅为最终保留轨迹的平均时长；wall_seconds累计本脚本记录的实际运行时间，含启动、重试和续跑"
    observation_path = path / "runtime-observations.json"
    observations = read_json(observation_path) if observation_path.exists() else {}
    result["timing_comparable"] = observations.get("timing_comparable", True)
    result["runtime_observations"] = observations
    reuse_path = path / "reuse-provenance.json"
    if reuse_path.exists():
        from prepare_reuse import verify_reused_simulations
        provenance = read_json(reuse_path)
        if provenance["target_signature"] != manifest["signature"]:
            raise ValueError("复用来源记录与当前清单不符")
        verify_reused_simulations(provenance, simulations)
        result["reused_simulations"] = provenance["reused_simulations"]
        result["new_completed_simulations"] = result["completed"] - provenance["reused_simulations"]
        result["new_execution_wall_seconds"] = result["wall_seconds"]
        result["reused_source_wall_seconds"] = provenance["source_wall_seconds"]
        result["wall_seconds"] = (result["wall_seconds"] or 0) + provenance["source_wall_seconds"]
        result["wall_seconds_per_planned_task"] = result["wall_seconds"] / result["expected"]
        result["duration_note"] += "；本结果复用完整开发集，wall_seconds另加其历史实际运行时间，新增37题耗时单列"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "baseline-summary.json")
    args = parser.parse_args()
    rows = [load_run(p) for p in args.run_dirs]
    report = {"runs": rows, "selection": select(rows), "description": "官方中文零售50题中的指定集合；不代表整个tau基准。完整运行前没有成绩结论。"}
    write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
