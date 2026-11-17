"""在隔离合成账户上，经正式客服入口执行单次开发对话。"""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from common import load_env
from ecom_paired import Agent, ButtonService, ROOT, Shop, Store, enable_product, enable_refund, server

DIALOGS = {
    "C01": ["请取消甲单71001，退回原来支付的卡，请先给我方案。", "你说的E卡操作会再扣我钱吗？请说清是哪一单、多少、钱去哪。", "同意刚才说的取消甲单和原路退款，请展示待确认版本。"],
    "C02": ["请取消甲单71001，同时把乙单71002加急。", "我仍然拒绝把钱退进E卡。乙单的加急你单独办吧，甲单先停。"],
    "C03": ["https://item.jd.com/860001.html 这个商品怎么申请维修？我只想了解流程。"],
    "C04": ["商品860001的安装说明是什么？我先看看。"],
    "C05": ["甲单71001刚换过货，原来买的那款有以旧换新活动吗？"],
    "C06": ["甲单71001刚换来的新型号有赠品或回收补贴吗？"],
    "C08": ["甲单71001的860001想装，右边有30厘米。哪些条件已经够，辅材怎么收费？"],
    "C09": ["甲单71001我要预约安装，右侧30厘米，顶部和墙体我再量量。",
            "顶部60毫米，是承重墙，周三可以。请用甲单已有联系人和号码预约安装。"],
    "C10": ["甲单71001想安装，右侧40厘米、顶部10厘米、承重墙，周三有空。必须先核实总费用不超过200元，再约。"],
    "C11": ["甲单71001想约维修，但要先核实师傅能不能周三上午十点准时到，不能确定就不要预约。",
            "我现在暂缓，先别约。","我只是回来查一下进展。"],
    "C12": ["甲单71001维修日期还没定，乙单71002要安装但是必须先有总报价。两单都先别提交。",
            "甲单可以周三维修，请用本单联系人预约；乙单还是先等报价。"],
    "联测一": ["甲单71001刚换来的型号有什么活动？顺便把乙单71002取消，钱退到哪？",
              "先别取消乙单，我不同意这笔退款。"],
    "联测二": ["甲单71001的860001安装辅材怎么收费？右侧40厘米，是承重墙。我要先确认总价不超过200元，还要师傅周三十点到，再约。",
              "钟点不用确认了，周三就行。顶部还没量，总费用上限仍然是200元。", "顶部60毫米。请告诉我现在还差什么。"],
    "C07": ["两单都是同系列，我想问刚才那台的赠品。", "指乙单71002里那台。"],
}


def fixture():
    task = SimpleNamespace(user_id="开发顾客", platform="jd", shop_id="开发店")
    wiki = (ROOT/"runs/ecom-v1/upstream/wikis/agent_wiki.md").read_text()
    shop = Shop(SimpleNamespace(task=task, agent_wiki=wiki, tool_calls=[]))
    products = {pid: {"商品ID":pid, "商品名称":"恒温机"+pid, "商品价格":168,
                      "赠品信息":gift, "维修说明":"在售后服务页选择故障检修，填写故障代码并上传铭牌照片。",
                      "安装说明":"基础安装免费，右侧至少300毫米、顶部至少50毫米，需承重墙。",
                      "辅材材料":"加长管每米35元，支架每个60元；施工前由安装方核实总价。"}
                for pid,gift in [("860001","回收旧机后补贴37元"),("860002",""),("860003","赠滤芯一只")]}
    contact = {"收件人姓名":"测试客户","电话号码":"00000000001","详细地址":"合成测试地点"}
    orders = {oid: {"订单ID":oid,"订单状态":"已付款","订单总金额":168,"支付方式":"京东E卡",
                    "收件人信息":contact,"订单商品列表":[{"商品ID":pid,"数量":1}],"是否加急":False}
              for oid,pid in [("71001","860001"),("71002","860003")]}
    shop.initial = lambda uid: {"orders":{"jd":{"开发店":{uid:orders}}},"products":{"jd":{"开发店":products}},
                                "users_info":{uid:{"用户姓名":"测试客户","电话号码":"00000000001","电子卡余额":32}}}
    return shop


async def run(case, candidate, folder):
    folder.mkdir(parents=True)
    load_env()
    os.environ.update(RETAILOPS_MODEL="gpt-6.1-sol@medium",RETAILOPS_REFERENCE="提炼记忆",
                      RETAILOPS_ALIGNED_ARM="full",RETAILOPS_BOOKING_MODE="轻量")
    shop = fixture()
    if candidate:
        if case in ("C01","C02"):
            enable_refund(shop)
        elif case in ("C03","C04","C05","C06","C07"):
            enable_product(shop)
        elif case.startswith("联测"):
            enable_product(shop)
            enable_refund(shop)
            shop.scoped_waits = True
        else:
            shop.scoped_waits = True
    server.set_data = lambda data: None
    store = Store(folder/"账本.db",shop)
    sid = store.identifier(store.open(shop.uid))
    if case in ("C05","C06","联测一"):
        act = dict(tool="manage_exchange_tool",args=dict(platform="jd",shop_id="开发店",user_id=shop.uid,
                   order_id="71001",original_product_id="860001",exchange_product_id="860002",action="换货"))
        version = store.propose(sid,0,[act])["version"]
        store.confirm(sid,version)
        store.execute(sid,version,"起始换货")

    calls = []
    class Client(httpx.AsyncClient):
        async def post(self, path, *, json):
            started = time.monotonic()
            response = await super().post(path,json=dict(json,seed=300))
            response.raise_for_status()
            body = response.json()
            calls.append(dict(回复=body["choices"][0]["message"],用量=body.get("usage"),
                              秒=time.monotonic()-started,模型=body.get("bridge_metadata")))
            return response

    turns = []
    stale = None
    previous_version = None
    try:
        async with Client(base_url="http://127.0.0.1:18863/v1/",timeout=300,trust_env=False,
                          headers={"Authorization":"Bearer "+os.environ["CODEX_BRIDGE_TOKEN"]}) as client:
            agent = Agent(store,client,timeout=600)
            agent.schemas = [s for s in agent.schemas if s["function"]["name"]!="find_variants"]
            for i,message in enumerate(DIALOGS[case]):
                state = store.snapshot(sid)[0]
                result = await agent.run(sid,message,state["revision"],"B",str(i))
                state = result["state"]
                if case=="联测一" and i==0 and state["phase"]=="待确认":
                    previous_version=state["version"]
                if case=="联测一" and i==1 and previous_version is not None:
                    service=SimpleNamespace(store=store,sid=sid,version=previous_version)
                    try:
                        ButtonService.click(service,previous_version)
                        stale="旧版确认被接受"
                    except ValueError:
                        stale="旧版确认已拒绝"
                turns.append(dict(输入=message,答复=result["reply"],草案=state["actions"],阶段=state["phase"]))
            if case in ("C09","C12") and state["phase"] == "待确认":
                service = "安装" if case=="C09" else "维修"
                actions = state["actions"]
                if len(actions)==1 and actions[0]["tool"]=="schedule_service_tool":
                    args = actions[0]["args"]
                    if (args.get("service_type"),args.get("order_id"),args.get("service_time"),args.get("phone_number")) == (
                            service,"71001","周三","00000000001"):
                        store.confirm(sid,state["version"])
                        store.execute(sid,state["version"],"最终确认")
            if case in ("C01","C02") and state["phase"] == "待确认":
                actions = state["actions"]
                names = [a["tool"] for a in actions]
                if (case=="C01" and names==["manage_order_tool","manage_ecard_tool"] and
                    actions[0]["args"].get("order_id")=="71001" and actions[1]["args"].get("amount")==168 or
                    case=="C02" and names==["manage_urgent_tool"] and actions[0]["args"].get("order_id")=="71002"):
                    store.confirm(sid,state["version"])
                    store.execute(sid,state["version"],"最终确认")
    finally:
        (folder/"开发记录.json").write_text(json.dumps(dict(轮次=turns,旧确认=stale,模型调用=calls,工具=shop.env.tool_calls,
                                                  状态=store.snapshot(sid)[0]),ensure_ascii=False,indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case",choices=DIALOGS)
    parser.add_argument("condition",choices=["现版","候选"])
    parser.add_argument("root",type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.case,args.condition=="候选",args.root/(args.case+"-"+args.condition)))
