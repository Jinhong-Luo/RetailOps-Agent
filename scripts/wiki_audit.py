"""只读重算建库、短回放和整题的消耗及动作；输出脱敏结果。"""
from collections import Counter
import json
from pathlib import Path
import re
import sqlite3
import sys


def load(path):
    return json.loads(path.read_text(encoding="utf8"))


def details(folder):
    rows = [json.loads(s) for s in (folder/"请求.jsonl").read_text().splitlines()]
    replies = [json.loads(s) for s in (folder/"私有回复.jsonl").read_text().splitlines()]
    assert len(rows)==len(replies)
    return rows,replies


def metered(rows):
    return dict(请求=len(rows),输入=sum(r["用量"]["prompt_tokens"] for r in rows),
        输出=sum(r["用量"]["completion_tokens"] for r in rows),
        缓存输入=sum((r["用量"].get("prompt_tokens_details") or {}).get("cached_tokens",0) for r in rows),
        总量=sum(r["用量"]["total_tokens"] for r in rows))


def examine(folder,mode):
    result=load(folder/"结果.json")
    rows,replies=details(folder)
    stats=metered(rows)
    assert stats["总量"]==stats["输入"]+stats["输出"] and stats["请求"]==result["请求"]
    assert all((r["模型"],r["档位"])==("gpt-6.1-sol","medium") for r in rows)
    service=[r for r in rows if r["角色"]=="客服"]
    assert service and all(r["目录"]==(mode!="关闭") for r in service)
    assert all(not r["目录"] and not r["正文"] for r in rows if r["角色"]=="顾客")
    sent=[i for i,r in enumerate(rows) if r["正文"]]
    if mode=="关闭":
        assert not sent and not any("read_task_wiki" in r["动作"] for r in service)
    if mode=="直给":
        assert len(sent)==len(service)
    with sqlite3.connect((folder/"business.db").as_uri()+"?mode=ro",uri=True) as db:
        events=[json.loads(v) for (v,) in db.execute("select body from events order by id")]
    success_reads=sum(e.get("stage")=="读取经验页" for e in events)
    if mode=="目录" and sent:
        assert success_reads and any("read_task_wiki" in r["动作"] for r in rows[:sent[0]])
    authorized=set()
    executions=0
    for e in events:
        if e["stage"]=="用户确认":
            authorized.add(e["version"])
        if e["stage"]=="业务工具执行":
            assert e["version"] in authorized
            executions+=1
    facts=dict(读取成功=success_reads,正文送达=len(sent),首次送达后动作=rows[sent[0]]["动作"] if sent else [],
        首个业务决策=result["首个业务决策"],按角色={role:metered([r for r in rows if r["角色"]==role])
            for role in sorted({r["角色"] for r in rows})})
    if "上游" in result:
        trace=load(folder/"trajectory.json")
        counts=Counter()
        for t in trace["tools"]:
            f=t["function"]
            if f["name"].startswith(("get_","compare_","search_")):
                arg=f["arguments"]
                counts[(f["name"],json.dumps(json.loads(arg) if isinstance(arg,str) else arg,sort_keys=True))]+=1
        # 每条回复只计一次；标准草案确认、问候排除，无法仅凭文本判定追问是否必要。
        questions=[]
        for i,m in enumerate(trace["session"][1:],1):
            s=m.get("content") or ""
            if m["role"]!="assistant" or "确认按钮" in s or "请核对以下完整方案" in s:
                continue
            if re.search(r"[？?]|(?:请|麻烦)(?:您|再|帮忙)?(?:提供|补充|告诉|告知|明确|问|确认是否)",s):
                questions.append(i)
        facts.update(上游=result["上游"],疑似询问消息序号=questions,
            重复同参只读次数=sum(n-1 for n in counts.values()),有事前授权执行事件=executions)
    else:
        facts.update(前缀=result["前缀"],状态=result["状态"],确认前账本相同=result["确认前账本相同"])
    return facts|stats


def audit(run):
    unique={}
    out={}
    for stage in ("建库","short","full"):
        if stage=="建库":
            folders=sorted([*run.glob("归并[1-3]"),*run.glob("单题[1-3]")])
        else:
            frozen=load(run/(stage+"-冻结.json"))
            folders=[run/stage/f"{i}-{m}" for i,m in frozen["位置"]]
        for folder in folders:
            rows,_=details(folder)
            key="整题" if stage=="full" else "短试"
            for row in rows:
                k=(key,row["序"])
                if k in unique: raise ValueError("同一预算出现重复用量")
                unique[k]=row
            if stage=="建库":
                assert all((r["模型"],r["档位"])==("gpt-6-astra","low") for r in rows)
            facts=metered(rows) if stage=="建库" else examine(folder,folder.name.split("-")[1])
            out.setdefault(stage,{})[folder.name]=facts
    budgets={}
    for label,stage_key in (("修复前短回放","短试"),("中断整题","整题")):
        prior=run/label
        if not prior.is_dir():
            continue
        for f in prior.glob("*/请求.jsonl"):
            for line in f.read_text().splitlines():
                row=json.loads(line)
                key=(stage_key,row["序"])
                if key in unique: raise ValueError("修复前台账重复")
                unique[key]=row
    for key in ("短试","整题"):
        with sqlite3.connect((run/(key+"预算.db")).as_uri()+"?mode=ro",uri=True) as db:
            calls=db.execute("select id,used,reserved from calls").fetchall()
        for i,used,_ in calls:
            if used is not None:
                assert unique[(key,i)]["用量"]["total_tokens"]==used
        budgets[key]=dict(计入请求=len(calls),已计用量=sum(used or 0 for _,used,_ in calls),
            未结预留=sum(r for _,used,r in calls if used is None))
    for i in (32,23,40,50):
        assert len({out["short"][f"{i}-{m}"]["前缀"] for m in ("关闭","目录","直给")})==1
    return dict(预算=budgets,位置=out)


if __name__=="__main__":
    print(json.dumps(audit(Path(sys.argv[1]).resolve()),ensure_ascii=False,indent=2))
