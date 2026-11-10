"""对相同官方题号、试次和种子逐对比较，保留历史成功与失败分组。"""
import argparse
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_bytes())


def index(path, repeats):
    rows = {}
    for sim in load(path)["simulations"]:
        key = (sim["task_id"], sim["trial"])
        if key in rows or not 0 <= key[1] < repeats:
            raise ValueError("重复或计划外试次")
        rows[key] = sim
    return rows


def compare(root, history):
    base = index(history, 1)
    manifests = [load(root / side / "manifest.json") for side in ("old", "new")]
    keys = ("local_task_ids", "effort", "num_trials", "upstream_commit", "split_digest")
    if any(manifests[0][k] != manifests[1][k] for k in keys):
        raise ValueError("配对清单不一致")
    configs = [{k:v for k,v in m["config"].items() if k not in {"agent","variant"}} for m in manifests]
    if configs[0] != configs[1]:
        raise ValueError("运行条件不一致")
    groups = [index(root / side / "native/zh-retail/results.json",3) for side in ("old","new")]
    ids = manifests[0]["local_task_ids"]
    expected = {(t,n) for t in ids for n in range(3)}
    if len(ids)!=50 or set(base)!={(t,0) for t in ids} or any(set(g)!=expected for g in groups):
        raise ValueError("尚未齐备 50 道历史成绩及 300 条配对结果")
    rows = []
    for t in ids:
        counts=[0,0]
        trials=[]
        for n in range(3):
            a,b = [g[t,n] for g in groups]
            if a["seed"]!=b["seed"]:
                raise ValueError("同题试次种子不一致")
            passed=[int((s.get("reward_info") or {}).get("reward")==1) for s in (a,b)]
            counts=[v+x for v,x in zip(counts,passed)]
            trials.append(dict(trial=n,seed=a["seed"],old=passed[0],new=passed[1],
                               termination=[a["termination_reason"],b["termination_reason"]]))
        rows.append(dict(task=t,historic=base[t,0]["reward_info"]["reward"],old=counts[0],
                         new=counts[1],delta=counts[1]-counts[0],trials=trials))
    return dict(planned_per_version=150,old_success=sum(r["old"] for r in rows),
                new_success=sum(r["new"] for r in rows),rows=rows)


def report(result):
    rows = result["rows"]
    historic_win = [r for r in rows if r["historic"] == 1]
    historic_loss = [r for r in rows if r["historic"] != 1]
    def names(selected):
        return "、".join(r["task"].split("_")[0] for r in selected) or "无"
    invalid = sum("infrastructure_error" in t["termination"] for r in rows for t in r["trials"])
    improved = [r for r in historic_loss if r["delta"] > 0]
    regressed = [r for r in rows if r["delta"] < 0]
    stable = [r for r in historic_win if r["new"] == 3]
    majority = [r for r in historic_win if r["new"] >= 2]
    lines = ["# 官方中文零售 50 题端到端配对复测", "",
             f"每题每版三次，共 300 次。旧版成功 {result['old_success']}/150，新版成功 {result['new_success']}/150。",
             f"含基础设施终止的配对共有 {invalid}/150。历史成绩来自此前每题一次的基线。", "",
             f"历史失败的 11 题中，新版相对本轮旧版成功次数增加 {len(improved)} 题（{names(improved)}）。",
             f"历史成功的 39 题中，新版三次全过 {len(stable)} 题，至少两次通过 {len(majority)} 题。",
             f"全部 50 题中，新版成功次数减少 {len(regressed)} 题（{names(regressed)}）。",
             "每题只有三个配对试次，增减反映本轮观测。", "",
             "| 官方题号 | 历史单次 | 本轮旧版 | 本轮新版 | 成功次数差 |",
             "| --- | --- | ---: | ---: | ---: |"]
    for r in rows:
        lines.append(f"| {r['task']} | {'通过' if r['historic'] else '失败'} | {r['old']}/3 | {r['new']}/3 | {r['delta']:+d} |")
    return "\n".join(lines)+"\n"


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("root",type=Path)
    p.add_argument("history",type=Path)
    p.add_argument("output",type=Path)
    a=p.parse_args()
    result=compare(a.root,a.history)
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    a.output.with_suffix(".md").write_text(report(result),encoding="utf-8")
