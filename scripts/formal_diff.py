"""按正式清单核对三个版本，保留缺分和首次异常。"""
import json
import sys
from collections import Counter
from pathlib import Path

root,archive,output=map(Path,sys.argv[1:])
config=json.loads((root/"配置.json").read_bytes())
ids=config["正式"]
def read(p):
    return json.loads(p.read_bytes())

def historical(task,arm):
    p=archive/f"结果/ecom-{task}-{arm}"
    if "score" not in read(p/"result.json"):
        p=archive/f"补跑/ecom-{task}-{arm}"
    return read(p/"result.json"),read(p/"trajectory.json")

def first_tool(a,b):
    def normalized(t):
        f=t["function"]
        args=f["arguments"]
        if isinstance(args,str):
            args=json.loads(args)
        return f["name"],args
    for i in range(max(len(a),len(b))):
        x=normalized(a[i]) if i<len(a) else ("结束",{})
        y=normalized(b[i]) if i<len(b) else ("结束",{})
        if x!=y:
            if x[0]==y[0]:
                fields=sorted(k for k in set(x[1])|set(y[1]) if x[1].get(k)!=y[1].get(k))
                return f"第{i+1}次 {y[0]} 的参数不同（{'、'.join(fields)}）"
            return f"第{i+1}次，旧版 {x[0]}，候选 {y[0]}"
    return "上游工具序列相同"

def writes(trace):
    result = []
    for item in trace["tools"]:
        f = item["function"]
        args = json.loads(f["arguments"]) if isinstance(f["arguments"],str) else f["arguments"]
        if f["name"].startswith(("manage_","schedule_","register_")) and args.get("action") not in {"查询","余额查询","信息查询"}:
            result.append(item)
    return result

def names(items):
    return {item["function"]["name"] for item in items}

keys=("score","action","search","output")
def scores(d):
    return [d[k] for k in keys]
rows=[]
for task in ids:
    old,_=historical(task,"old")
    full,trace=historical(task,"full")
    matches=list((root/"正式"/str(task)).glob("*/官方评分.json"))
    if len(matches)!=1:
        raise ValueError(f"题{task}缺分或重复")
    p=matches[0]
    candidate=read(p)
    assert candidate["提交"]==config["提交"] and candidate["题号"]==task
    current=read(p.parent/"trajectory.json")
    first=next((i+1 for i,(a,b) in enumerate(zip(trace["session"],current["session"])) if a!=b),None)
    if first is None and len(trace["session"])!=len(current["session"]):
        first=min(len(trace["session"]),len(current["session"]))+1
    rows.append(dict(题号=task,原生=scores(old),旧完整版=scores(full),候选=scores(candidate),
                     旧版至候选首处消息差异=first,首次上游调用分歧=first_tool(trace["tools"],current["tools"]),
                     结束=candidate["termination"],顾客轮数=candidate["turns"],
                     已提交写操作首差=first_tool(writes(trace), writes(current)),
                     仅候选使用的写工具=sorted(names(writes(current))-names(writes(trace)))))
assert len(rows)==33 and sum(r["原生"][0] for r in rows)==11 and sum(r["旧完整版"][0] for r in rows)==9
output.mkdir(exist_ok=True,parents=True)
(output/"正式逐题.json").write_text(json.dumps(dict(配置=config,成绩顺序=["整题","终态","指定查询","输出"],逐题=rows),ensure_ascii=False,indent=2)+"\n")
def group(sign):
    return [r["题号"] for r in rows if r["候选"][0]-r["旧完整版"][0]==sign]
lines=["# ECom 中文文本正式单次回归","",f'运行提交 `{config["提交"]}`。每题每版一次，种子 300。原生和旧完整版使用同条件历史归档，第 31 题原生首次网关异常后取得的官方 0 分保留。',
       "",f'原生 11/33，旧完整版 9/33，本候选 {sum(r["候选"][0] for r in rows)}/33。相对旧完整版改善题号 {group(1)}，退化题号 {group(-1)}。',
       "","每格依次给出整题、终态、指定查询、输出四个官方分量。消息序号从开场问候起计，工具分歧按实际记录比较。同一顾客模拟器也可能生成不同措辞，首次差异不能单独确定失分原因。",
       "","| 题号 | 原生 | 旧完整版 | 候选 | 首个不同消息序号 | 首个不同上游调用 | 已提交写入首差 | 结束方式 |","|---|---|---|---|---|---|---|---|"]
for r in rows:
    fields=["/".join(map(str,r[k])) for k in ("原生","旧完整版","候选")]
    lines.append(f'| {r["题号"]} | {" | ".join(fields)} | {r["旧版至候选首处消息差异"] or "一致"} | {r["首次上游调用分歧"]} | {r["已提交写操作首差"]} | {r["结束"]} |')
(output/"正式逐题.md").write_text("\n".join(lines)+"\n")
