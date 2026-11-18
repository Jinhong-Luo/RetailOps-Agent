"""对照两个冻结版本的正式单次成绩，只导出脱敏字段。"""
import argparse
from collections import Counter
import hashlib
import json
import sqlite3
from pathlib import Path

def read(p):
    return json.loads(p.read_bytes())

def tool(item):
    f=item["function"]
    return f["name"],json.loads(f["arguments"]) if isinstance(f["arguments"],str) else f["arguments"]

def first_tool(a,b):
    for i in range(max(len(a),len(b))):
        x=tool(a[i]) if i<len(a) else ("结束",{})
        y=tool(b[i]) if i<len(b) else ("结束",{})
        if x!=y:
            return dict(调用序号=i+1,旧工具=x[0],新工具=y[0],
                        不同参数字段=sorted(k for k in set(x[1])|set(y[1]) if x[1].get(k)!=y[1].get(k)) if x[0]==y[0] else [])
    return None

def writes(trace):
    return [t for t in trace["tools"] if tool(t)[0].startswith(("manage_","schedule_","register_"))
            and tool(t)[1].get("action") not in {"查询","余额查询","信息查询"}]

def score(root,task,commit):
    paths=list((root/"正式"/str(task)).glob("*/官方评分.json"))
    assert len(paths)==1,(task,"缺分或重复")
    value=read(paths[0])
    assert value["题号"]==task and value["提交"]==commit
    assert all(value[k] in (0,1) for k in ("score","action","search","output"))
    return paths[0].parent,value


def business(folder):
    with sqlite3.connect((folder/"business.db").resolve().as_uri()+"?mode=ro",uri=True) as db:
        phase,version,body=db.execute("select phase,version,history from sessions").fetchone()
        history=json.loads(body)
        receipts=[json.loads(r[0]) for r in db.execute("select body from receipts")]
        waits=[dict(服务=s,条件=k,来源=p,已解除=bool(v)) for s,k,p,v in
               db.execute("select service,kind,provider,resolved from scoped_wait")]
    calls=Counter(c["function"]["name"] for m in history for c in m.get("tool_calls") or [])
    reasons=("需要已生效的办理授权","需要登记之后的新依据","转接或查进度不能解除安装方待核条件或暂缓",
             "本单仍有待补资料或授权，先逐项核实","需要顾客明确变更原条件或重新同意办理")
    errors={reason:sum(reason in (m.get("content") or "") for m in history if m["role"]=="tool") for reason in reasons}
    return dict(最终阶段=phase,草案版本数=version,历史工具次数=dict(calls),等待恢复错误=errors,等待=waits,
                写回执工具=[i["tool"] for r in receipts for i in r["items"]])

def meter(paths):
    values=[]
    for p in paths:
        for line in p.read_text().splitlines():
            values.append(json.loads(line))
    totals=Counter()
    for v in values:
        for k in ("prompt_tokens","completion_tokens","total_tokens"):
            totals[k]+=int((v.get("usage") or {}).get(k) or 0)
    return dict(成功模型响应数=len(values),全部回执模型强度匹配=bool(values) and all(
        v.get("model")=="gpt-6.1-sol" and v.get("effort")=="medium" for v in values),**totals)

def run(old,new,out):
    a,b=read(old/"配置.json"),read(new/"配置.json")
    assert {k:v for k,v in a.items() if k!="提交"}=={k:v for k,v in b.items() if k!="提交"}
    assert len(b["正式"])==len(set(b["正式"]))==33 and b["重复次数"]==1
    rows=[]
    fingerprints=[]
    scored_folders=[]
    for i in b["正式"]:
        p,x=score(old,i,a["提交"])
        q,y=score(new,i,b["提交"])
        scored_folders.append(q)
        before,after=read(p/"trajectory.json"),read(q/"trajectory.json")
        n=next((j+1 for j,(u,v) in enumerate(zip(before["session"],after["session"])) if u!=v),None)
        if n is None and len(before["session"])!=len(after["session"]):
            n=min(len(before["session"]),len(after["session"]))+1
        rows.append(dict(题号=i,旧版=[x[k] for k in ("score","action","search","output")],
            新版=[y[k] for k in ("score","action","search","output")],
            首处消息差异=n,首处上游调用差异=first_tool(before["tools"],after["tools"]),
            首处写入调用差异=first_tool(writes(before),writes(after)),
            旧写入工具=[tool(t)[0] for t in writes(before)],新写入工具=[tool(t)[0] for t in writes(after)],
            旧轮数=x["turns"],新轮数=y["turns"],旧结束=x["termination"],新结束=y["termination"],
            旧业务事实=business(p),新业务事实=business(q)))
        for label,folder in (("旧版",p),("新版",q)):
            for name in ("官方评分.json","trajectory.json","business.db","usage.jsonl"):
                file=folder/name
                if file.exists():
                    fingerprints.append(dict(版本=label,题号=i,文件=name,摘要=hashlib.sha256(file.read_bytes()).hexdigest()))
    oldtotal=[sum(r["旧版"][i] for r in rows) for i in range(4)]
    newtotal=[sum(r["新版"][i] for r in rows) for i in range(4)]
    assert oldtotal==[16,16,31,33]
    improved=[r["题号"] for r in rows if r["旧版"][0]<r["新版"][0]]
    regressed=[r["题号"] for r in rows if r["旧版"][0]>r["新版"][0]]
    extras=[p for p in (new/"正式").glob("*/*") if p.is_dir() and p not in scored_folders]
    usage=dict(正式已评分=meter([p/"usage.jsonl" for p in scored_folders]),
               正式无评分尝试=meter([p/"usage.jsonl" for p in extras if (p/"usage.jsonl").exists()]),
               预检=meter(list((new/"预检").glob("*/*/usage.jsonl"))),
               无评分尝试数=len(extras),正式位置数=33,
               单题累计耗时秒=sum(read(p/"官方评分.json")["用时秒"] for p in scored_folders))
    result=dict(旧配置=a,新配置=b,成绩顺序=["整题","终态","指定查询","输出"],
                旧版汇总=oldtotal,新版汇总=newtotal,改善=improved,退化=regressed,逐题=rows,计量=usage,文件指纹=fingerprints)
    out.mkdir(parents=True,exist_ok=True)
    (out/"正式逐题对照.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    lines=["# ECom 33 题正式单次对照","",
        f'旧冻结提交：`{a["提交"]}`；新冻结提交：`{b["提交"]}`。',
        "",f'整题成功：{oldtotal[0]}/33 → {newtotal[0]}/33；改善：{improved}；退化：{regressed}。',
        "","四项顺序为整题 / 终态 / 指定查询 / 输出；每题每版一次。旧成绩复用同配置归档，官方 0 分原样保留。",
        "","| 题号 | 旧版 | 新版 | 变化 |","|---|---|---|---|"]
    for r in rows:
        label="改善" if r["题号"] in improved else "退化" if r["题号"] in regressed else "持平"
        lines.append(f'| {r["题号"]} | {"/".join(map(str,r["旧版"]))} | {"/".join(map(str,r["新版"]))} | {label} |')
    lines.extend(["","相同配置不能保证相同模拟顾客措辞。两版存在多项代码差异，本次结果不能单独归因于 S08 修复，也不能由单次成绩判断稳定成功率。",
        "","模型使用原服务器 Codex 登录桥接，并非直连 API；回执核验模型和强度。种子为请求值，不宣称服务端保证确定性。token 为桥接成功响应统计，不等于实付费用。",
        "","原始对话、账本和模型明细只留原服务器。JSON 附四项成绩、首次消息/调用分歧、计量及归档摘要；首次差异本身不等于业务失败原因。"])
    (out/"正式逐题对照.md").write_text("\n".join(lines)+"\n",encoding="utf8")
    print(json.dumps(dict(旧版=oldtotal,新版=newtotal,改善=improved,退化=regressed,计量=usage),ensure_ascii=False))

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("old","new","out"):
        parser.add_argument(name,type=Path)
    args=parser.parse_args()
    run(args.old,args.new,args.out)
