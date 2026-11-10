"""从已冻结试次及逐步事件构建去敏报告，保留失败分母和上游实际返回的用量。"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median


def read(path):
    return json.loads(path.read_bytes())


def summarize(folder):
    report=read(folder/"summary.json")
    rows=report["rows"]
    expected=report["planned"]
    if expected<=0 or expected%6:
        raise ValueError("计划分母无效")
    keys={(r["case"],r["mode"],r["trial"]) for r in rows}
    plan={(c,m,n) for c in ("追加退货","单项保留") for m in "ABC" for n in range(expected//6)}
    if len(keys)!=len(rows) or keys-plan:
        raise ValueError("存在重复或计划外试次")
    stages=defaultdict(list)
    reasons=Counter()
    for row in rows:
        trace=read(folder/f"{row['case']}-{row['mode']}-{row['trial']}.json")
        if row != trace["result"]:
            raise ValueError("逐试次结果与原始记录不一致")
        if not row["success"]:
            reasons[row.get("error","业务结果未满足判定")]+=1
        for event in trace["events"]:
            if event.get("duration_ms") is not None:
                stages[event["stage"]].append(event["duration_ms"])
            for stage, duration in (event.get("bridge_ms") or {}).items():
                stages["桥接/"+stage].append(duration)
    groups={}
    for mode in "ABC":
        selected=[r for r in rows if r["mode"]==mode]
        measured=[r for r in selected if r["input_tokens"] is not None]
        groups[mode]=dict(planned=expected//3,completed_rows=len(selected),success=sum(r["success"] for r in selected),
            measured_usage=len(measured),input_tokens=sum(r["input_tokens"] for r in measured),
            output_tokens=sum(r["output_tokens"] for r in measured),calls=sum(r["calls"] for r in selected),
            median_seconds=median(r["seconds"] for r in selected) if selected else None,
            missing_item_trials=sum(bool(r["missing_items"]) for r in selected),
            fact_error_trials=sum(r["fact_error"] is True for r in selected),
            unscored_facts=sum(r["fact_error"] is None for r in selected),
            verified_model_trials=sum(r["verified_medium"] for r in selected))
    return dict(scope=report["scope"],runner_sha256=report["runner_sha256"],pack_sha256=report["pack_sha256"],
                source_sha256=hashlib.sha256((folder/"summary.json").read_bytes()).hexdigest(),planned=expected,
                missing=expected-len(rows),failures=dict(reasons),groups=groups,rows=rows,
                stages_ms={k:dict(count=len(v),median=round(median(v),3),p95=round(sorted(v)[math.ceil(.95*len(v))-1],3))
                           for k,v in stages.items()})


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("runs",type=Path,nargs="+")
    p.add_argument("--checks",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    result={"runs":{f.name:summarize(f) for f in a.runs},"service_checks":read(a.checks/"summary.json")}
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

