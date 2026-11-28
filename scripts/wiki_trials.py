"""在独立账本上复原四个历史决策点；端到端位置沿用原 ECom 评分。"""
import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
from unittest.mock import patch

from wiki_experiment import (ANSWER, FULL_LIMIT, SHORT_LIMIT, Meter, ModelBudget, PositionLimit, FirstDecision,
                             bridge, client, ecom, httpx, position, read_json, setup, sha, write_json)
from retailops.agent import Agent
from retailops.domain import Domain
from retailops.store import Store, dump

CUTS = {32:50, 40:22, 50:27}
SHORT = [(32,"关闭"),(32,"目录"),(32,"直给"),(23,"目录"),(23,"直给"),(23,"关闭"),
         (40,"直给"),(40,"关闭"),(40,"目录"),(50,"目录"),(50,"关闭"),(50,"直给")]
FULL = [(32,"关闭"),(40,"目录"),(32,"目录"),(40,"关闭")]


class RecordedClient:
    """只在决策点以前，重放原客服已经发出的动作。"""
    def __init__(self):
        self.replies = iter(())

    async def post(self, path, *, json):
        message = next(self.replies, None)
        if message is None:
            raise ValueError("真实前缀与历史回复不一致")
        return httpx.Response(200,request=httpx.Request("POST","http://mock/"),
                              json={"choices":[{"message":message}]})


def make_agent(store, http, retail=False):
    agent = Agent(store,http,timeout=600)
    if not retail:
        agent.schemas = [s for s in agent.schemas if s["function"]["name"]!="find_variants"]
    return agent


def canonical(history):
    return [{k:v for k,v in m.items() if k not in {"tool_call_id","tool_calls"}} |
            ({"工具":[c["function"] for c in m["tool_calls"]]} if m.get("tool_calls") else {})
            for m in history]


async def restore_ecom(case, folder, batch):
    source = position(batch,"ECom-Bench",case)
    with sqlite3.connect((source/"business.db").as_uri()+"?mode=ro",uri=True) as db:
        history = json.loads(db.execute("select history from sessions").fetchone()[0])
    cut = CUTS[case]
    assert history[cut]["role"] == "user"
    env = ecom.MockStoryEnv(task_index=case, console_verbose=ecom.RunLogger(False))
    env._create_isolated_data()
    ecom.server.cache_dir = env.cache_dir
    shop = ecom.Shop(env)
    ecom.enable_product(shop)
    ecom.enable_refund(shop)
    shop.scoped_waits = True
    store = Store(folder/"business.db",shop)
    sid = store.identifier(store.open(shop.uid))
    fake = RecordedClient()
    with patch.dict(os.environ,RETAILOPS_WIKI_MODE="关闭"):
        agent = make_agent(store,fake)
    starts = [n for n,m in enumerate(history[:cut]) if m["role"]=="user"]+[cut]
    for j,k in zip(starts,starts[1:]):
        segment = history[j+1:k]
        fake.replies = iter(m for m in segment if m["role"]=="assistant" and
            not any(c["id"].startswith("read_") for c in m.get("tool_calls") or []))
        state,_ = store.snapshot(sid)
        await agent.run(sid,history[j]["content"],state["revision"],"C",f"冻结前缀-{j}")
        if any(re.match(r"^(第\d+版业务回执|业务工具已完成第\d+版方案)",m.get("content") or "")
               for m in segment if m["role"]=="assistant"):
            state,_ = store.snapshot(sid)
            store.confirm(sid,state["version"])
            store.execute(sid,state["version"],f"已有授权-{j}")
    state,restored = store.snapshot(sid)
    if canonical(restored) != canonical(history[:cut]):
        raise ValueError("复原的业务轨迹与记录不一致")
    return store,sid,history[cut]["content"]


def restore_tau(folder,batch):
    source = position(batch,"τ-bench",23)
    messages = read_json(source/"native/zh-retail/results.json")["simulations"][0]["messages"]
    domain = Domain()
    history = []
    for m in messages:
        row = dict(role=m["role"],content=m.get("content"))
        if m["role"]=="tool":
            row["tool_call_id"] = m["id"]
        elif m.get("tool_calls"):
            row["tool_calls"] = [dict(id=c["id"],type="function",function=dict(name=c["name"],
                arguments=dump(c["arguments"]))) for c in m["tool_calls"]]
        history.append(row)
        if m["role"]=="tool" and m.get("content") in domain.seed["users"]:
            user = domain.seed["users"][m["content"]]
            store = Store(folder/"business.db",domain)
            sid = store.identifier(store.open(user["email"]))
            store.finish(sid,0,messages=history)
            return store,sid,"身份核验已完成，请继续处理顾客在上述对话中提出的需求。"
    raise ValueError("未找到目标登录回执")


def accounts(store):
    with store.connection() as db:
        return sha(dump(list(map(tuple,db.execute("select uid,body from accounts order by uid")))).encode())


def summarize(folder):
    records = [json.loads(s) for s in (folder/"请求.jsonl").read_text().splitlines()]
    return dict(请求=len(records),输入=sum(r["用量"]["prompt_tokens"] for r in records),
        输出=sum(r["用量"]["completion_tokens"] for r in records),
        缓存输入=sum((r["用量"].get("prompt_tokens_details") or {}).get("cached_tokens",0) for r in records),
        目录送达=sum(r["目录"] for r in records),正文送达=sum(r["正文"] for r in records),
        调用读页=sum("read_task_wiki" in r["动作"] for r in records),
        正文送达后动作=[r["动作"] for r in records if r["正文"] and r["角色"]=="客服"],
        首个业务决策=next((r["动作"] for r in records if r["角色"]=="客服" and
                              r["动作"]!=["read_task_wiki"]),None),
        分角色请求=dict(Counter(r["角色"] for r in records)))


async def short(case,folder,batch,meter,fixture):
    with patch.object(ecom.server,"set_data",lambda data:None):
        store,sid,text = restore_tau(folder,batch) if case==23 else await restore_ecom(case,folder,batch)
        state,history = store.snapshot(sid)
        before = accounts(store)
        start = sha(dump([state,canonical(history),text,before]).encode())
        if fixture:
            return dict(前缀=start,身份已核验=True)
        async with client(meter) as http:
            agent = make_agent(store,http,case==23)
            try:
                result = await agent.run(sid,text,state["revision"],"C","短回放")
                write_json(folder/"私有决策.json",result)
                status = "已回复"
            except FirstDecision:
                status = "已取得首次业务决定"
            except PositionLimit:
                status = "四请求截止"
            except ecom.Issue as error:
                if error.data["code"]!="step_limit":
                    raise
                status = "步骤截止"
        return dict(前缀=start,状态=status,确认前账本相同=before==accounts(store),**summarize(folder))


async def trial(args):
    run,case,mode = args.run,args.case,args.mode
    fixture = args.stage=="fixture"
    allowed = FULL if args.stage=="full" else SHORT
    if (case,mode) not in allowed:
        raise ValueError("未知位置")
    folder = run/args.stage/f"{case}-{mode}"
    folder.mkdir(parents=True,exist_ok=False)
    os.environ.update(RETAILOPS_WIKI_MODE=mode,RETAILOPS_WIKI_DIR=str(run/"wiki"),RETAILOPS_WIKI_PAGE="guide")
    if args.stage=="full":
        ledger = ModelBudget(run/"整题预算.db",FULL_LIMIT)
        meter = Meter(ledger,folder,ANSWER,"medium")
        row = await ecom.episode(case,0,"button",folder,context_mode="C",transport=meter)
        result = dict(上游=row,**summarize(folder))
    else:
        ledger = ModelBudget(run/"短试预算.db",SHORT_LIMIT)
        result = await short(case,folder,args.batch,Meter(ledger,folder,ANSWER,"medium",4,first_decision=True),fixture)
    write_json(folder/"结果.json",result)
    print(case,mode,args.stage,"已保存",flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("stage",choices=["short","full","fixture"])
    parser.add_argument("run",type=Path)
    parser.add_argument("batch",type=Path)
    parser.add_argument("case",type=int)
    parser.add_argument("mode")
    args=parser.parse_args()
    setup()
    asyncio.run(trial(args))
