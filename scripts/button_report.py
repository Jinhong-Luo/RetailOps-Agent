"""核验顾客按钮试次，与冻结的两版官方轨迹逐题配对。"""
import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from official_pairs import index, load


def build(run, reference):
    actual = load(run / "manifest.json")
    baseline = load(reference / "old/manifest.json")
    for key in ("task_ids", "local_task_ids", "split_digest", "upstream_commit", "num_trials", "effort", "environment"):
        if actual[key] != baseline[key]:
            raise ValueError("清单条件不一致：" + key)
    changed = {"agent", "user", "variant", "baseline_label", "api_base"}
    for key in set(actual["config"]) | set(baseline["config"]):
        if key not in changed and actual["config"].get(key) != baseline["config"].get(key):
            raise ValueError("运行配置不一致：" + key)
    path = Path("native/zh-retail/results.json")
    paths = [reference / arm / path for arm in ("old", "new")] + [run / path]
    groups = [index(p, 3) for p in paths]
    previous = load(Path(__file__).resolve().parents[1] / "reports/official-paired-3.json")
    historical = {r["task"]: r["historic"] for r in previous["rows"]}
    expected = {(t, n) for t in actual["local_task_ids"] for n in range(3)}
    if (len(expected) != 150 or set(historical) != set(actual["local_task_ids"])
            or any(set(g) != expected for g in groups)):
        raise ValueError("缺少完整的 150 个配对")
    rows = []
    for task, trial in sorted(expected):
        sims = [g[task, trial] for g in groups]
        if len({s["seed"] for s in sims}) != 1:
            raise ValueError("试次种子不同")
        latest = sims[-1]
        clicks = {c["id"] for m in latest["messages"] if m["role"] == "user"
                  for c in m.get("tool_calls") or [] if c["name"] == "click_confirm"}
        accepted = sum(m["role"] == "tool" and m.get("id") in clicks and not m.get("error")
                       for m in latest["messages"])
        if any(s["termination_reason"] == "infrastructure_error"
               or (s.get("reward_info") or {}).get("reward") is None for s in sims):
            raise ValueError(f"{task} 第 {trial} 轮缺少有效的业务评分")
        scores = [int(s["reward_info"]["reward"] == 1) for s in sims]
        rows.append(dict(task=task, trial=trial, seed=latest["seed"], historic=historical[task], old=scores[0], text=scores[1],
                         button=scores[2], clicks=len(clicks), accepted=accepted, termination=latest["termination_reason"]))
    worker_logs = sorted((paths[-1].parent / "workers").glob("worker_*.log"))
    if not worker_logs:
        raise ValueError("缺少工作进程重试日志")
    retries = Counter()
    requeued = 0
    for worker_log in worker_logs:
        log = worker_log.read_text(encoding="utf-8", errors="replace")
        for reason in re.findall(r"failed \(attempt \d+/\d+\): ([^:\n]+)", log):
            category = {"litellm.BadGatewayError": "模型网关报错",
                        "达到单轮工具调用上限": "达到单轮工具调用上限"}.get(reason, "其他错误")
            retries[category] += 1
        requeued += len(re.findall(r"Controller requeued [^\n]*\(infrastructure_error result\)", log))
    return dict(planned=150, hashes=[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths],
                retry_reasons=dict(retries), infrastructure_requeues=requeued, rows=rows)


def description(result):
    rows = result["rows"]
    passed = {k:sum(r[k] for r in rows) for k in ("old", "text", "button")}
    lines = ["# 顾客确认按钮与完整历史的官方复测", "",
             "50 道官方中文零售任务，每题 3 次。按钮版为本次新运行，两个对照组复用上次的全部同题同种子轨迹。",
             "本次同时引入顾客侧按钮、带按钮提示的模拟顾客与完整历史加业务状态。模型、官方评分器和任务预算与原对照一致。", "",
             "| 版本 | 成功次数 | 成功率 |","| --- | ---: | ---: |"]
    for k, name in (("old","原生旧版"), ("text","文字确认加裁剪历史"), ("button","顾客按钮加完整历史")):
        lines.append(f"| {name} | {passed[k]}/150 | {passed[k]/150:.1%} |")
    lines.append("")
    for k, name in (("old","原生旧版"), ("text","文字确认版")):
        gains = sum(r["button"] > r[k] for r in rows)
        losses = sum(r["button"] < r[k] for r in rows)
        lines.append(f"与{name}配对，按钮版多办成 {gains} 次，少办成 {losses} 次，净差 {gains-losses:+d} 次。")
    terms = dict(Counter(r["termination"] for r in rows))
    tasks = sorted({r["task"] for r in rows}, key=lambda t:int(t.split("_")[0]))
    totals = {t: {k: sum(r[k] for r in rows if r["task"] == t)
                  for k in ("old", "text", "button", "historic")} for t in tasks}
    historical_wins = [s for s in totals.values() if s["historic"]]
    def names(selected):
        return "、".join(t.split("_")[0] for t in selected) or "无"
    improvements = names(t for t,s in totals.items() if not s["historic"] and s["button"] > s["old"])
    regressions = names(t for t,s in totals.items() if s["button"] < s["old"])
    lines += [f"顾客调用按钮 {sum(r['clicks'] for r in rows)} 次，工具成功返回 {sum(r['accepted'] for r in rows)} 次。终止原因计数为 {terms}。", "",
              f"按最早的单次测试分组，历史失败题中，相比原生旧版三试次成功数增加的题号为 {improvements}。",
              f"历史成功的 {len(historical_wins)} 题中，按钮版三次全过 {sum(s['button']==3 for s in historical_wins)} 题，至少两次通过 {sum(s['button']>=2 for s in historical_wins)} 题。",
              f"全部 50 题中，相比原生旧版三试次成功数下降的题号为 {regressions}。",
              f"按钮版工作进程记录的首次失败后重试原因为 {result['retry_reasons']}。基础设施错误导致重新排队 {result['infrastructure_requeues']} 次。最终 150 个按钮版试次均取得业务评分。",
              "这批题此前已用于分析和修改。按钮入口和上下文一起变动，每题只有三次试验。", "",
              "| 官方题号 | 原生旧版 | 文字确认版 | 按钮版 |", "| --- | ---: | ---: | ---: |"]
    for task in tasks:
        scores = [totals[task][k] for k in ("old","text","button")]
        lines.append(f"| {task} | {scores[0]}/3 | {scores[1]}/3 | {scores[2]}/3 |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("run", "reference", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    result = build(args.run, args.reference)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",encoding="utf-8")
    args.output.with_suffix(".md").write_text(description(result), encoding="utf-8")
