"""用固定的 6.2 型号执行单题笔记、决策回放、整题配对和独立订单的迁移与取用对照。"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

from note_effort import BATCH, NEW, PROMPT, SCENES, extra, history
from wiki_build import generate
from wiki_experiment import (ANSWER, BUILD, Meter, ModelBudget, PositionLimit, bridge, client,
                             ecom, httpx, position, read_json, setup, sha, write_json)
from wiki_trials import CUTS, accounts, canonical, make_agent, restore_ecom

RUN = BATCH.parent/"RetailOps-C62-01"
FULL = [(50,"关"), (40,"新"), (50,"新"), (40,"关")]
TRANSFER = {"迁移正":"我想开个人发票，想用默认资料，抬头还不清楚。能先核实有哪些选项，暂时别开吗？",
            "迁移反":"我只想看看这笔订单的配送进度，不需要开发票或办理其他业务。"}
META = {50:"顾客咨询个人发票、要求默认资料且抬头待核对时查阅。",
        40:"同一订单连续咨询安装并考虑预约时查阅。",32:"查询包裹遇旧轨迹或重复物流信息时查阅。"}
LIMITS = {"总结":(6,220000),"短试":(90,1250000),"整题":(370,4850000)}


def ledger(stage):
    calls,tokens=LIMITS[stage]
    return ModelBudget(RUN/(stage+".db"),dict(calls=calls,tokens=tokens,request_tokens=min(tokens,300000),seconds=86400))


async def build():
    for case in (32,50,40):
        for variant in (("新","旧") if case!=40 else ("新",)):
            path=RUN/f"页-{case}-{variant}"
            src=extra(BATCH,case) if variant=="新" else history(BATCH,case)
            note=await generate(RUN,f"总结-{case}-{variant}",NEW if variant=="新" else PROMPT,src,ledger("总结"))
            assert note["依据"] and all(type(n)==int and 0<=n<len(src) for n in note["依据"])
            text="\n".join(k+"："+note[k] for k in ("触发","下一步","推进","例外")) if variant=="新" else note["经验"]
            assert 0<len(text)<900
            path.mkdir(exist_ok=True)
            (path/"guide.md").write_text(text)
            write_json(path/"manifest.json",dict(pages=dict(guide=dict(适用=META[case],例外="按本次授权办理",sha256=sha(text.encode())))))


class Delivery(Meter):
    """直接交付或按当前用户状态送达一次；所有模型回包仍由同一台账核验。"""
    def __init__(self, stage, folder, arm, page, cap=None):
        super().__init__(ledger(stage),folder,ANSWER,"medium",cap=cap,retry_route=True)
        self.arm,self.page,self.sent=arm,page,False

    async def handle_async_request(self,request):
        body=json.loads(request.content)
        msgs=body["messages"]
        service=any(m["role"]=="system" and "你是中文零售助手" in (m.get("content") or "") for m in msgs)
        user=next((m.get("content") or "" for m in reversed(msgs) if m["role"]=="user"),"")
        triggered=self.arm=="触发" and not self.sent and "默认" in user and "发票" in user and "抬头" in user
        if service and (self.arm in ("旧","新") or triggered):
            msgs.insert(1,dict(role="system",content="已验证经验正文："+self.page))
            request=httpx.Request(request.method,request.url,headers={k:v for k,v in request.headers.items()
                                  if k.lower()!="content-length"},json=body)
            self.sent=True
        response=await super().handle_async_request(request)
        calls=response.json()["choices"][0]["message"].get("tool_calls") or []
        if self.arm=="目录" and any(c["function"]["name"]=="read_task_wiki" for c in calls):
            self.cap=3
        return response


async def one(scene,arm,stage):
    is_transfer=scene in TRANSFER
    case=21 if is_transfer else int(scene[:2])
    note_case=50 if is_transfer else case
    page=RUN/f"页-{note_case}-{'旧' if arm=='旧' else '新'}"
    text=(page/"guide.md").read_text()
    assert sha(text.encode())==read_json(page/"manifest.json")["pages"]["guide"]["sha256"]
    folder=RUN/stage/(scene+"-"+arm)
    folder.mkdir(parents=True)
    os.environ.update(RETAILOPS_WIKI_MODE="目录" if arm=="目录" else "关闭",
                      RETAILOPS_WIKI_DIR=str(page),RETAILOPS_WIKI_PAGE="guide")
    if stage=="整题":
        result=await ecom.episode(case,0,"button",folder,context_mode="C",transport=Delivery(stage,folder,arm,text))
    else:
        if is_transfer:
            src=position(BATCH,"ECom-Bench",21)
            with sqlite3.connect((src/"business.db").as_uri()+"?mode=ro",uri=True) as db:
                old=json.loads(db.execute("select history from sessions").fetchone()[0])
            CUTS[21]=next(i for i,m in enumerate(old) if i>0 and m["role"]=="user")
        user=TRANSFER[scene] if is_transfer else SCENES[scene][1]
        with patch.object(ecom.server,"set_data",lambda data:None):
            store,sid,_=await restore_ecom(case,folder,BATCH)
            state,h=store.snapshot(sid)
            before=accounts(store)
            prefix=sha(json.dumps([state,canonical(h),before,user],sort_keys=True).encode())
            async with client(Delivery(stage,folder,arm,text,2)) as http:
                try:
                    await make_agent(store,http).run(sid,user,state["revision"],"C","回放")
                except PositionLimit:
                    pass
            assert accounts(store)==before
            result=dict(前缀=prefix)
    rows=[json.loads(l) for l in (folder/"请求.jsonl").read_text().splitlines()]
    svc=[r for r in rows if r["角色"]=="客服"]
    assert svc and all(not r["正文"] for r in rows if r["角色"]!="客服")
    n=sum(r["正文"] for r in svc)
    if arm in ("新","旧","关","触发"):
        assert n==(len(svc) if arm in ("新","旧") else int(arm=="触发" and scene=="迁移正"))
    result.update(请求=len(rows),客服=len(svc),目录=sum(r["目录"] for r in svc),正文=n,
                  读取=sum("read_task_wiki" in r["动作"] for r in svc),
                  动作=[r["动作"] for r in svc],用量=sum(r["用量"]["total_tokens"] for r in rows))
    write_json(folder/"结果.json",result)


if __name__=="__main__":
    setup()
    os.environ["RETAILOPS_CODEX_BIN"]=str(RUN/"cli-stable/node_modules/.bin/codex")
    assert (BUILD,ANSWER)==("gpt-6.2-astra","gpt-6.2-sol")
    assert all(read_json(RUN/(m+"-stable.json")).get("用量") for m in (BUILD,ANSWER))
    if sys.argv[1:2]==["one"]:
        asyncio.run(one(*sys.argv[2:]))
    else:
        with bridge(RUN,BUILD,18882):
            asyncio.run(build())
        jobs=[(s,a,"短试") for s in SCENES for a in ("关","旧","新")]
        jobs += [(s,a,"短试") for s in TRANSFER for a in ("关","目录","触发","新")]
        jobs += [(str(c),a,"整题") for c,a in FULL]
        write_json(RUN/"排期.json",jobs)
        with bridge(RUN,ANSWER,18883):
            for scene,arm,stage in jobs:
                if not (RUN/stage/(scene+"-"+arm)/"结果.json").exists():
                    subprocess.run([sys.executable,__file__,"one",scene,arm,stage],check=True)
