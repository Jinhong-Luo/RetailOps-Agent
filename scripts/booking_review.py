"""基于可定位的顾客表态与成功回执，补充预约过程评价。"""
import argparse
import json
import re
import sqlite3
from pathlib import Path


def assess(messages, writes):
    customers = [(i,m.get("content") or "") for i,m in enumerate(messages,1)
                 if m["role"]=="user" and re.search("[\u4e00-\u9fff]",m.get("content") or "")]
    evidence = []
    for index,text in reversed(customers):
        if re.search(r"暂缓|暂停|先不(?:约|办|登记|安排|预约|安装|提交)|先别|等.{0,16}再(?:约|办|决定|安排|联系)|不用.{0,4}预约",text):
            evidence.append(dict(顾客消息序号=index,观察="明确等待或暂缓"))
            if any(i>index and re.search(r"请.{0,8}(预约|安排)|继续办理|同意.{0,8}(约|办)|现在.{0,6}办",v) for i,v in customers):
                return "暂缓后有新指示待核",evidence
            if writes:
                return "有预约写入需按订单核对",evidence
            return "顾客明确暂缓且无预约写入",evidence
    return "证据不足待复核",evidence


def run(archive, disputes):
    rows=[]
    for path in sorted((archive/"正式").glob("*/*/官方评分.json")):
        score=json.loads(path.read_bytes())
        folder=path.parent
        trace=json.loads((folder/"trajectory.json").read_bytes())
        with sqlite3.connect((folder/"business.db").resolve().as_uri()+"?mode=ro",uri=True) as db:
            writes=sum(item["tool"]=="schedule_service_tool" for body, in db.execute("select body from receipts")
                       for item in json.loads(body)["items"])
        case=str(score["题号"])
        label,evidence=assess(trace["session"],writes)
        if case in disputes and score["score"]==0:
            evidence.append(dict(参考差异="争议题复核.json",题号=case))
        external = [i for i,m in enumerate(trace["session"],1) if m["role"]=="user" and
                    re.search(r"等|先|核实|确认",m["content"]) and re.search(r"报价|费用|收费|钟点|师傅",m["content"])]
        tags=[]
        if external and not writes:
            tags.append("顾客提出外部信息前提，尚无预约回执")
            evidence.append(dict(顾客消息序号=external[-1],观察="外部信息前提，报价或时段是否取得需复核"))
        if label=="顾客明确暂缓且无预约写入":
            tags.append("尊重当次暂缓")
        if case in disputes and score["score"]==0:
            tags.append("实际记录与官方参考有差异")
        rows.append(dict(过程观察=tags,题号=int(case),官方整题分=score["score"],成功预约回执项数=writes,
                         暂缓证据=label,依据=evidence))
    assert len(rows)==len({r["题号"] for r in rows})==33
    return sorted(rows,key=lambda r:r["题号"])


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("archive","disputes","output"):
        parser.add_argument(name,type=Path)
    args=parser.parse_args()
    rows=run(args.archive,json.loads(args.disputes.read_bytes()))
    args.output.write_text(json.dumps(rows,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
