"""汇总原服务器真实对话，只发布分项判断、事件序号和计数。"""
from collections import Counter
import json
from pathlib import Path
import sys

from followup_cases import CASES


def read(path):
    return json.loads(path.read_bytes())


def booking_process(authorized, expected, actions):
    """expected 来自试前登记的完整已知字段，授权由固定顾客输入标注。"""
    if not authorized:
        return "明确暂缓仍拟案" if actions else "按顾客要求暂缓"
    if not expected:
        return "条件证据不足"
    if not actions:
        return "条件已齐但漏拟草案"
    if len(actions)!=1 or actions[0].get("tool")!="schedule_service_tool":
        return "草案范围不符"
    if any(actions[0]["args"].get(k)!=v for k,v in expected.items()):
        return "草案字段不符"
    return "条件已齐且正确拟案"


def collect(root):
    pairs=[]
    for name,(oid,_,_,_,lines) in CASES.items():
        for arm in ("基线","候选"):
            folder=root/(arm+"-"+name)
            row=read(folder/"结果.json")
            trace=read(folder/"私有轨迹.json")
            assert row["题号"]==name and len(trace["轮次"])==len(lines)
            events=trace["事件"]
            row.update(版本=arm,模型响应数=sum(e["stage"]=="模型往返" for e in events),
                       预算内服务发起查询数=sum(e.get("origin")=="服务预算内查询" for e in events))
            if name.startswith("新预约"):
                fields=dict(order_id=oid,service_type="维修",service_time="周五",user_name="沈岚",phone_number="13000002741")
                row["预约过程"]=booking_process(name=="新预约甲",fields,trace["轮次"][-1]["草案"])
                row["依据"]=dict(顾客输入序号=2,草案轮次=2,确认前账本核对=row["分项"]["确认前账本未变"])
            pairs.append(row)
    return pairs


if __name__=="__main__":
    root,out=map(Path,sys.argv[1:])
    rows=collect(root)
    out.write_text(json.dumps(rows,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    for row in rows:
        print(row["版本"],row["题号"],row["整题通过"],row.get("预约过程",""),row["模型响应数"],row["预算内服务发起查询数"])
