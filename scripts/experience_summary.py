"""汇总冻结试次；只导出计数、时间和 token，不导出对话与业务字段。"""
import argparse
import json
import statistics
from pathlib import Path

ARMS = ("无记忆","人工提示","历史检索","提炼记忆")


def first_invoice_evidence(history):
    # 只检查第一次被服务器接受的开票草案之前，本轮到当时真正可见的字段来源。
    users = []
    reads = []
    calls = {}
    for msg in history:
        if msg["role"] == "user":
            users.append(msg["content"])
        if msg["role"] == "assistant":
            calls.update({c["id"]:c["function"] for c in (msg.get("tool_calls") or [])})
        if msg["role"] == "tool":
            call = calls.get(msg["tool_call_id"], {})
            if call.get("name") == "get_user_info_tool":
                reads.append(msg["content"])
            if call.get("name") == "propose_plan" and msg["content"] == "方案已提交供用户核对":
                args = json.loads(call["arguments"])
                for action in args["actions"]:
                    if action["tool"] == "manage_invoice_tool":
                        values = action["args"]
                        return all(any(values[key] in u for u in users) for key in ("order_id","title")) and any(
                            values["phone_number"] in r for r in reads)
    return None


def summarize(folder):
    rows=[]
    for index in range(6):
        for repeat in range(2):
            for arm in ARMS:
                pair=dict(index=index,repeat=repeat,arm=arm)
                path=folder/f"{index}-{repeat}-{arm}"
                if not (path/"score.json").exists():
                    rows.append(dict(**pair,first=False,final=False,error="缺失"))
                    continue
                result=json.loads((path/"score.json").read_text())
                assert all(result[k]==v for k,v in pair.items())
                details=json.loads((path/"private.json").read_text())
                references=[e for e in details["events"] if e["stage"]=="跨会话参考"]
                first_plan=next((i for i,r in enumerate(details["rows"],1)
                    if r["body"].get("state",{}).get("phase")=="待确认"),None)
                reference_by_request={e["request_id"]:e["bytes"] for e in references}
                transmitted=sum(reference_by_request.get(e["request_id"],0) for e in details["events"]
                                if e["stage"]=="模型往返")
                result.update(transmitted_reference_bytes=transmitted,first_invoice_evidence=first_invoice_evidence(details["history"]),first_plan_turn=first_plan,reference_bytes=sum(e["bytes"] for e in references),
                              hits=[e["hits"] for e in references],versions=sorted({e["version"] for e in references}))
                rows.append(result)
    metrics={}
    for arm in ARMS:
        group=[r for r in rows if r["arm"]==arm]
        observed=[r for r in group if "calls" in r]
        totals={k:sum(r[k] for r in observed) for k in (
            "calls","first","final","corrections","input","output","wrong_write","duplicate_queries","turns","seconds")}
        totals.update(failures=sum(bool(r["error"]) for r in group),missing=12-len(observed),
                      median_seconds=statistics.median(r["seconds"] for r in observed) if observed else None)
        for k in ("input","output","seconds"):
            totals["mean_"+k]=round(totals[k]/len(observed),2) if observed else None
            totals["per_success_"+k]=round(totals[k]/totals["final"],2) if totals["final"] else None
        metrics[arm]=totals
    return dict(planned=48,metrics=metrics,trials=rows)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder",type=Path)
    parser.add_argument("output",type=Path)
    args=parser.parse_args()
    args.output.write_text(json.dumps(summarize(args.folder),ensure_ascii=False,indent=2)+"\n",encoding="utf8")
