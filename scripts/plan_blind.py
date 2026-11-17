"""在互不相见的独立会话中，请模拟顾客自由复述同一份退款草案。"""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time

from ecom_development import fixture
from ecom_paired import ButtonService, Store, enable_refund, server, dump
from common import load_env
import httpx


def display(folder, amount, balance, include_direction):
    shop = fixture()
    data = shop.initial(shop.uid)
    orders = data["orders"]["jd"]["开发店"][shop.uid]
    only = orders.pop("71001")
    only.update(订单ID="甲单", 订单总金额=amount)
    data["orders"]["jd"]["开发店"][shop.uid] = {"甲单": only}
    data["users_info"][shop.uid]["电子卡余额"] = balance
    shop.initial = lambda uid: data
    enable_refund(shop)
    store = Store(folder/"账本.db", shop)
    sid = store.identifier(store.open(shop.uid))
    common = dict(platform="jd",shop_id="开发店",user_id=shop.uid,order_id="甲单")
    actions = [dict(tool="manage_order_tool",args=dict(common,action="取消")),
               dict(tool="manage_ecard_tool",args=dict(common,action="退款",refund_reason="取消",amount=amount))]
    v = store.propose(sid, 0, actions)["version"]
    notice = "请核对以下完整方案，并按版本确认。"
    if include_direction:
        notice += "\n" + shop.describe_plan(actions)
    screen = notice + dump(actions) + ButtonService.confirmation_prompt(SimpleNamespace(version=v))
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    return screen


async def run(folder, purpose, include_direction):
    load_env()
    server.set_data = lambda data: None
    screen = display(folder, 239, 61, include_direction)
    messages = [{"role":"system","content":"你是正在使用商家售后界面的顾客。根据你的目的和界面内容，用自己的话说说你认为接下来会发生什么，再决定同意、追问或拒绝。"},
                {"role":"user","content":purpose + "\n你看到的界面如下。\n" + screen}]
    start = time.monotonic()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:18863/v1/", timeout=300, trust_env=False,
            headers={"Authorization":"Bearer "+os.environ["CODEX_BRIDGE_TOKEN"]}) as client:
        response = await client.post("chat/completions", json=dict(model="gpt-6.1-sol@medium",seed=300,messages=messages))
        response.raise_for_status()
    body = response.json()
    record = dict(顾客目的=purpose,界面=screen,自由复述=body["choices"][0]["message"]["content"],
                  用时秒=time.monotonic()-start,用量=body.get("usage"),模型=body.get("bridge_metadata"))
    (folder/"盲读原始记录.json").write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf8")
    return record
