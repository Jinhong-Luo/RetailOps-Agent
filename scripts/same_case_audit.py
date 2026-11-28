"""核对同题直给的冻结笔记、逐请求用量、重复查询及执行前授权。"""
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
from hashlib import sha256


def read(path):
    return json.loads(path.read_bytes())


def lines(path):
    return [json.loads(s) for s in path.read_bytes().splitlines()]


def counts(rows):
    usage = [r["用量"] for r in rows]
    def total(native, standard):
        return sum(v[native] if native in v else v[standard] for v in usage)
    result = dict(请求=len(rows),输入=total("inputTokens","prompt_tokens"),
                  输出=total("outputTokens","completion_tokens"),合计=total("totalTokens","total_tokens"))
    assert result["输入"] + result["输出"] == result["合计"]
    return result


def behavior(folder, tau):
    if tau:
        sim, = read(folder/"native/zh-retail/results.json")["simulations"]
        messages = sim["messages"]
        calls = [(c["name"], c["arguments"]) for m in messages if m["role"]=="assistant"
                 for c in m.get("tool_calls") or []]
        events = read(folder/"events.json")
    else:
        trace = read(folder/"trajectory.json")
        messages = trace["session"]
        calls = [(t["function"]["name"],json.loads(t["function"]["arguments"])) for t in trace["tools"]]
        with sqlite3.connect((folder/"business.db").as_uri()+"?mode=ro",uri=True) as db:
            events=[json.loads(body) for body, in db.execute("select body from events order by id")]
    queries = Counter((name,json.dumps(args,sort_keys=True,ensure_ascii=False)) for name,args in calls
                      if name.startswith(("get_","search_","compare_")))
    seen = set()
    executions = 0
    for event in events:
        if event["stage"]=="用户确认":
            seen.add(event["version"])
        elif event["stage"]=="业务工具执行":
            assert event["version"] in seen
            executions+=1
    return dict(顾客发言数=sum(m["role"]=="user" for m in messages),工具频数=dict(Counter(n for n,_ in calls)),
                同参重复查询=sum(n-1 for n in queries.values()),已确认执行次数=executions)


def audit(run):
    baseline=read(run.parent/"RetailOps-BC109-20261128-01/本批进度.json")["位置"]
    frozen=read(run/"冻结.json")
    result={}
    tracked={"总结":{}, "答题":{}}
    for case in frozen["题号"]:
        tau=case==23
        original,=[r for r in baseline if str(r["题号"])==str(case) and r.get("上下文")=="C"
                   and r["题集"]==("τ-bench" if tau else "ECom-Bench") and r["类型"]=="公开正式"]
        old=Path(original["证据"]).parent
        target=run/str(case)
        note=read(run/f"经验{case}.json")
        version=sha256(note["经验"].encode()).hexdigest()
        assert version==frozen["经验摘要"][str(case)]==note["版本"]
        logs=lines(target/"请求.jsonl")
        assert all(row["经验摘要"]==(version if row["客服"] else None) for row in logs)
        score=read(target/"结果.json")
        assert score["正文送达"]==sum(r["客服"] for r in logs)>0
        source=read(old/"结果.json")
        before=({"score":source["原生评分"]["reward"],"termination":source["终止原因"]}
                if tau else source["上游评分"])
        build=lines(run/f"总结{case}/请求.jsonl")
        for stage,rows in (("总结",build),("答题",logs)):
            for row in rows:
                assert row["序"] not in tracked[stage]
                tracked[stage][row["序"]]=counts([row])["合计"]
        result[case]=dict(经验=note["经验"],总结=counts(build),历史=dict(评分=before,
            **counts(lines(old/"请求.jsonl")),**behavior(old,tau)),直给=dict(评分=score["评分"],
            正文送达=score["正文送达"],**counts(logs),**behavior(target,tau)))
    for stage,known in tracked.items():
        with sqlite3.connect((run/(stage+"预算.db")).as_uri()+"?mode=ro",uri=True) as db:
            assert dict(db.execute("select id,used from calls"))==known
    return result


if __name__=="__main__":
    print(json.dumps(audit(Path(sys.argv[1]).resolve()),ensure_ascii=False,indent=2))
