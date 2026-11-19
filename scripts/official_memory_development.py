"""原服务器合成旁例：同起始事实比较记忆开关，经真实客服入口核对草案、账本和回执。"""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

import httpx
from common import load_env,write_json
from ecom_development import fixture
from ecom_paired import Agent,Store,enable_product,enable_refund,server
from retailops.domain import Issue
from retailops.official_memory import snapshot

CASES={
    "个人发票办理":"请给林泉741开个人发票，抬头用已查到的测试客户，联系电话用该订单预留号码，我确认这些资料。请先给我完整草案，确认前不要开票。",
    "换货草案":"林泉741要把原商品860001换成可换列表中的860002，请核对后给我完整换货草案，确认前不要执行。",
    "退货退款办理":"林泉741的商品已签收，请办理退货并将本单应退款原路退回京东E卡。我同意关联退款，但仍需先看完整草案再确认。",
    "退款暂缓":"林泉741我要退货，但退款金额还没向我说明，我暂不同意退款，也不要拆开提交退货。请说明已核实的订单金额和原支付方式，先别办理。"
}

def account(store,sid):
    with store.connection() as db:
        row=store.row(db,sid)
        return deepcopy(store.toolkit(db,row["uid"]).db)

async def run(case,arm,bank,folder):
    folder.mkdir(parents=True,exist_ok=False)
    load_env()
    os.environ.update(RETAILOPS_CAPTURE_MEMORY_INPUT="1",RETAILOPS_MODEL="gpt-6.1-sol@medium",RETAILOPS_REFERENCE="官方移植" if arm=="持续" else "无记忆",
        RETAILOPS_OFFICIAL_STORE=str(bank),RETAILOPS_MEMORY_CONTRACT=str(folder/"业务契约.json"),
        RETAILOPS_ALIGNED_ARM="full",RETAILOPS_BOOKING_MODE="轻量")
    os.environ.pop("RETAILOPS_MEMORY_BANK",None)
    shop=fixture()
    seed=deepcopy(shop.initial(shop.uid))
    orders=seed["orders"]["jd"]["开发店"][shop.uid]
    order=orders.pop("71001");order["订单ID"]="林泉741";orders["林泉741"]=order
    order["订单状态"]="已签收" if case.startswith(("退货","退款")) else "已付款"
    products=seed["products"]["jd"]["开发店"]
    products["860001"].update(商品状态="已上架",可换货商品=["860002"],库存=5)
    products["860002"].update(商品状态="已上架",可换货商品=["860001"],库存=5)
    shop.initial=lambda uid:deepcopy(seed)
    enable_product(shop);enable_refund(shop);shop.scoped_waits=True
    server.set_data=lambda data:None
    store=Store(folder/"business.db",shop)
    sid=store.identifier(store.open(shop.uid))
    before=account(store,sid)
    calls=[]
    class Client(httpx.AsyncClient):
        async def post(self,path,*,json):
            started=time.monotonic()
            from memory_capture import start,finish
            handle=start(folder,dict(json,seed=300))
            response=await super().post(path,json=dict(json,seed=300))
            finish(handle,response)
            response.raise_for_status()
            body=response.json();meta=body.get("bridge_metadata",{})
            if (meta.get("actual_model"),meta.get("reasoning_effort"))!=("gpt-6.1-sol","medium"):
                raise ValueError("实际模型不匹配")
            calls.append(dict(回复=body["choices"][0]["message"],用量=body.get("usage"),秒=time.monotonic()-started,模型=meta))
            return response
    checks={}
    reply=None;error=None;result=None
    initial_bank=snapshot(bank)["digest"]
    try:
        async with Client(base_url="http://127.0.0.1:18863/v1/",timeout=300,trust_env=False,
               headers={"Authorization":"Bearer "+os.environ["CODEX_BRIDGE_TOKEN"]}) as client:
            agent=Agent(store,client,timeout=600)
            agent.schemas=[x for x in agent.schemas if x["function"]["name"]!="find_variants"]
            result=await agent.run(sid,CASES[case],0,"B","首次")
            reply=result["reply"];state=result["state"]
            checks["确认前账本未变"]=account(store,sid)==before
            actions=state["actions"] if state["phase"]=="待确认" else []
            if case=="退款暂缓":
                checks["暂缓未拟案"]=not actions
                checks["没有付款或退货"]=account(store,sid)==before
            else:
                names=[a["tool"] for a in actions]
                if case=="个人发票办理":
                    wanted=names==["manage_invoice_tool"] and actions[0]["args"].get("invoice_type")=="个人发票"
                    if wanted:
                        wanted=actions[0]["args"].get("title")=="测试客户" and actions[0]["args"].get("phone_number")=="00000000001"
                elif case=="换货草案":
                    wanted=names==["manage_exchange_tool"] and actions[0]["args"].get("exchange_product_id")=="860002"
                else:
                    wanted=names==["manage_return_tool","manage_ecard_tool"] and actions[1]["args"].get("amount")==168
                wanted=bool(wanted and all(a["args"].get("order_id")=="林泉741" for a in actions))
                checks["正确草案"]=wanted
                if wanted and checks["确认前账本未变"]:
                    store.confirm(sid,state["version"])
                    receipt=store.execute(sid,state["version"],"合成顾客确认")
                    after=account(store,sid)
                    checks["确认后成功回执"]=len(receipt["items"])==len(actions)
                    if case=="个人发票办理":
                        checks["发票终态"]=after["invoices"]["林泉741"]["发票类型"]=="个人发票"
                    elif case=="换货草案":
                        checks["换货终态"]=after["orders"]["jd"]["开发店"][shop.uid]["林泉741"]["订单商品列表"][0]["商品ID"]=="860002"
                    else:
                        checks["退货终态"]=after["orders"]["jd"]["开发店"][shop.uid]["林泉741"]["订单状态"]=="已退货"
                        checks["余额准确"]=after["users_info"][shop.uid]["电子卡余额"]==200
    except Exception as exc:
        error=exc.data["code"] if isinstance(exc,Issue) else type(exc).__name__
        checks["运行无异常"]=False
    events=store.events(sid,0)
    refs=[e for e in events if e["stage"]=="官方记忆读取"]
    checks["确认前账本未变"]=checks.get("确认前账本未变",account(store,sid)==before)
    checks["开启时真实读取经验"]=arm!="持续" or bool(refs)
    checks["预算内"]=max((e.get("step",0) for e in events),default=0)<=8 and error!="step_limit"
    checks["记忆快照未变"]=snapshot(bank)["digest"]==initial_bank
    checks["关闭条件无记忆读取"]=arm!="关闭" or not refs
    write_json(folder/"私有轨迹.json",dict(输入=CASES[case],回复=reply,结果=result,事件=events,调用=calls))
    row=dict(案例=case,条件=arm,分项=checks,通过=all(checks.values()),异常=error,
             初始账本摘要=hashlib.sha256(json.dumps(before,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
             记忆摘要=initial_bank,记忆读取次数=len(refs),读取路径=[e.get("path") for e in refs],模型调用=len(calls),
             token=sum((x["用量"] or {}).get("total_tokens",0) for x in calls))
    write_json(folder/"结果.json",row)
    print(json.dumps(row,ensure_ascii=False),flush=True)
    return row

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case",choices=CASES)
    parser.add_argument("arm",choices=["关闭","持续"])
    parser.add_argument("bank",type=Path)
    parser.add_argument("folder",type=Path)
    args=parser.parse_args()
    asyncio.run(run(args.case,args.arm,args.bank.resolve(),args.folder.resolve()))
