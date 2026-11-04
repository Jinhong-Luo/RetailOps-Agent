"""导出失败轨迹的可观察结束事件，不输出业务正文，也不自动判定原因。"""
import argparse
from pathlib import Path
from common import read_json, write_json
from prepare_reuse import simulations
from summarize import load_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = load_run(args.run)
    execution_path = args.run / "execution.json"
    if not summary["complete_for_summary"] or not execution_path.exists() or read_json(execution_path)["exit_code"] != 0:
        raise SystemExit("只导出已完整结束、无基础设施错误的正式基线失败观察")
    manifest = read_json(args.run / "manifest.json")
    canonical = dict(zip(manifest["local_task_ids"], manifest["task_ids"]))
    rows = []
    for sim in simulations(args.run):
        reward = sim.get("reward_info") or {}
        if reward.get("reward") == 1:
            continue
        messages = sim.get("messages") or []
        tools = [call["name"] for msg in messages for call in msg.get("tool_calls") or []]
        user_messages = [msg.get("content") or "" for msg in messages if msg["role"] == "user"]
        end = user_messages[-1] if user_messages else ""
        markers = [marker for marker in ("###TRANSFER###", "###STOP###", "OUT-OF-SCOPE") if marker in end]
        rows.append({"canonical_task_id": canonical[sim["task_id"]], "task_id": sim["task_id"], "trial": sim["trial"], "reward": reward.get("reward"), "reward_breakdown": reward.get("reward_breakdown"), "termination_reason": sim.get("termination_reason"), "tool_call_count": len(tools), "last_tool_names": tools[-4:], "user_end_markers": markers, "human_transfer_observed": "transfer_to_human_agents" in tools, "known_audit_note": "题意保留24MP，但金标换30MP；分母及评分保持原样" if canonical[sim["task_id"]] == "52" else None})
    result = {"expected": summary["expected"], "successful": summary["successful"], "failed": len(rows), "observations": rows, "source_artifacts_sha256": summary["source_artifacts_sha256"], "note": "仅记录工具名、用户终止标记和原生评分，不输出对话正文。转人工后结束不直接等同于策略错误或数据错误；原因需另核对政策、题意和重复运行。该导出不改变任务输入、评分或分母。"}
    write_json(args.output, result)
    print({"failed": len(rows), "output": str(args.output)})


if __name__ == "__main__":
    main()
