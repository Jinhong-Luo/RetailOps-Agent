"""用未用于前述修改的合成事实，按相同输入比较两个固定服务版本。"""
import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys

CASES = {
    "新咨询甲": ("星河793", "804617901", 17, 26, ["商品编号804617901，顶部要留几厘米，延长管辅材每米多少钱？我只问一下。"]),
    "新咨询乙": ("山海294", "804617952", 13, 37, ["我这笔山海294的商品，管材怎么收费？机器右侧应该留多少厘米？请两项都告诉我。"]),
    "新预约甲": ("青砚465", "804617963", 14, 28, ["青砚465要预约周五维修，联系人不用账户里的，我稍后给名字号码，先别提交。","联系沈岚，电话13000002741，按刚才那单继续办理。"]),
    "新预约乙": ("青柚538", "804617984", 19, 34, ["青柚538周五需要维修，联系人我来补。先别安排。","电话13000002741，名字沈岚。我只是补资料，这单先别约。"]),
}

async def run(args):
    base=args.base.resolve()
    sha=subprocess.check_output(["git","-C",base,"rev-parse","HEAD"],text=True).strip()
    subprocess.run(["git","-C",base,"diff","--quiet","HEAD","--"],check=True)
    if sha!=args.commit:
        raise ValueError("工作树与指定提交不同")
    sys.path[:0]=[str(base/"scripts"),str(base),str(base/"runtime"),str(base/"vendor/tau2-bench/src")]
    from common import load_env
    from shared_fixture import ecom_case, OLD
    from shared_dialog import read_account, fits
    from ecom_paired import Agent, Store, server
    from aligned_trial import configure
    import httpx
    load_env()
    configure("full")
    os.environ["RETAILOPS_ALIGNED_ARM"]="full"
    oid,pid,margin,fee,lines=CASES[args.case]
    folder=args.out
    folder.mkdir(parents=True,exist_ok=False)
    shop=ecom_case("D08","full")
    data=shop.initial(shop.uid)
    catalog=data["products"]["jd"]["合成店"]
    item=deepcopy(catalog[OLD])
    item.update(商品ID=pid,安装说明=f"顶部与右侧均至少留{margin}厘米。",辅材材料=[f"延长管每米{fee}元，按实用量收费。"])
    catalog[pid]=item
    bucket=data["orders"]["jd"]["合成店"][shop.uid]
    order=bucket.pop("松墨61")
    bucket[oid]=order
    order["订单ID"]=oid
    order["订单商品列表"][0]["商品ID"]=pid
    logistics=data["logistics"]["jd"]["合成店"][shop.uid]
    logistics[oid]=logistics.pop("松墨61")
    shop.initial=lambda uid:deepcopy(data)
    server.set_data=lambda data:None
    store=Store(folder/"账本.db",shop)
    sid=store.identifier(store.open(shop.uid))
    initial=read_account(store,sid)
    class Client(httpx.AsyncClient):
        async def post(self,path,*,json):
            response=await super().post(path,json=dict(json,seed=300))
            response.raise_for_status()
            body=response.json()
            meta=body["bridge_metadata"]
            if (meta.get("actual_model"),meta.get("reasoning_effort"))!=("gpt-6.1-sol","medium"):
                raise ValueError("响应模型或强度不匹配")
            return response

    turns=[]
    async with Client(base_url="http://127.0.0.1:18863/v1/",headers={"Authorization":"Bearer "+os.environ["CODEX_BRIDGE_TOKEN"]},timeout=300,trust_env=False) as client:
        agent=Agent(store,client,timeout=600)
        agent.schemas=[s for s in agent.schemas if s["function"]["name"]!="find_variants"]
        for index,line in enumerate(lines):
            from retailops.domain import Issue
            try:
                result=await agent.run(sid,line,store.snapshot(sid)[0]["revision"],"B",str(index))
            except Issue as exc:
                if exc.data["code"]!="step_limit":
                    raise
                turns.append(dict(答复="",草案=[],超步=True))
                break
            state=result["state"]
            turns.append(dict(答复=result["reply"],草案=state["actions"] if state["phase"]=="待确认" else [],超步=False))
    events=store.events(sid,0)
    tools=shop.env.tool_calls
    (folder/"私有轨迹.json").write_text(json.dumps(dict(轮次=turns,事件=events,工具=tools),ensure_ascii=False))
    last=turns[-1]
    checks=dict(确认前账本未变=read_account(store,sid)==initial,预算内=not any(t["超步"] for t in turns))
    if args.case.startswith("新咨询"):
        expected={"get_installation_service_info_tool","get_auxiliary_materials_info_tool"}
        seen=set()
        for t in tools:
            fn=t["function"]
            if fn["name"] in expected and json.loads(fn["arguments"]).get("product_id")==pid:
                seen.add(fn["name"])
        checks.update(两项均查对型号=seen==expected,答出留距=bool(re.search(fr"{margin}\s*(?:厘米|公分|cm)",last["答复"],re.I)),
                      答出材料价格=bool(re.search(fr"{fee}\s*元",last["答复"])),纯咨询无草案=not last["草案"])
    elif args.case=="新预约甲":
        fields=dict(order_id=oid,service_type="维修",service_time="周五",user_name="沈岚",phone_number="13000002741")
        checks.update(首轮未拟案=not turns[0]["草案"],本轮生成正确草案=fits(last["草案"],[("schedule_service_tool",fields)]))
    else:
        checks.update(两轮均未拟案=all(not t["草案"] for t in turns))
    counts=[max((e.get("step",0) for e in events if e.get("request_id")==str(i)),default=0) for i in range(len(lines))]
    output=dict(题号=args.case,提交=sha,逐轮步数=counts,分项=checks,整题通过=all(checks.values()))
    (folder/"结果.json").write_text(json.dumps(output,ensure_ascii=False,indent=2))
    print(json.dumps(output,ensure_ascii=False))

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base",type=Path)
    parser.add_argument("commit")
    parser.add_argument("case",choices=CASES)
    parser.add_argument("out",type=Path)
    asyncio.run(run(parser.parse_args()))
