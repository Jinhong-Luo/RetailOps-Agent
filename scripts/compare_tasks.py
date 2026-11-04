"""逐任务展示各档原始评分，缺失结果单独标注，不作自动原因推断。"""
import argparse
from pathlib import Path
from common import ROOT, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "dev-task-comparison.md")
    args = parser.parse_args()
    runs = []
    task_ids = None
    for path in args.run_dirs:
        manifest = read_json(path / "manifest.json")
        if task_ids is None:
            task_ids = manifest["task_ids"]
        elif task_ids != manifest["task_ids"]:
            raise SystemExit("任务集合或顺序不一致，不能生成同题对照")
        source = path / "native" / "zh-retail" / "results.json"
        simulations = (read_json(source).get("simulations") or []) if source.exists() else []
        mapping = dict(zip(manifest["local_task_ids"], manifest["task_ids"]))
        records = {}
        for sim in simulations:
            canonical = mapping[sim["task_id"]]
            key = (canonical, sim["trial"])
            if key in records:
                raise SystemExit("出现重复任务试次，停止输出")
            records[key] = sim
        runs.append((manifest, records))
    efforts = [m["effort"] for m, _ in runs]
    if len(set(efforts)) != len(efforts):
        raise SystemExit("同一思考强度不能重复出现在对照列")
    lines = ["# 开发任务逐题原生评分", "", "每格为成功试次数/计划试次数；成功仅指原生reward等于1。缺失试次明确标注，不能当作已经运行失败。此表用于检查跨档现象，不代替完整配置和耗时可比性校验。", "", "| 规范任务ID | " + " | ".join(efforts) + " |", "|---|" + "---:|" * len(efforts)]
    for task in task_ids:
        cells = []
        for manifest, records in runs:
            trials = manifest["num_trials"]
            found = [records[(task, i)] for i in range(trials) if (task, i) in records]
            successful = sum((s.get("reward_info") or {}).get("reward") == 1 for s in found)
            missing = trials - len(found)
            cell = f"{successful}/{trials}"
            if missing:
                cell += f"（缺失{missing}）"
            elif any((s.get("reward_info") or {}).get("reward") is None for s in found):
                cell += "（有未评分）"
            cells.append(cell)
        lines.append("| " + task + " | " + " | ".join(cells) + " |")
    lines += ["", "52题的规格与金标动作冲突、68题的时间信息不足及104题的过时说明元数据，详见[数据一致性审计](../docs/dev数据一致性审计.md)。所有题仍保留在官方原始分母中。", "", "单次试验受用户模拟器随机波动影响；不得将单道题的跨档差异直接解释为统计显著能力变化。"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
