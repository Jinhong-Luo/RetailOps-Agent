"""冻结同题总结材料并在四个独立决策场景比较 low、medium 和关闭。"""
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

from same_case_notes import PROMPT, history
from wiki_build import generate
from wiki_trials import accounts, canonical, make_agent, restore_ecom
from wiki_experiment import (ANSWER, BUILD, Meter, ModelBudget, PositionLimit, bridge, client,
                             ecom, position, read_json, setup, sha, write_json)


ROOT = Path(__file__).resolve().parents[2]
BATCH = ROOT/"RetailOps-BC109-20261128-01"
RUN = ROOT/"RetailOps-笔记档位对照-01"
NEW = PROMPT + """另按字段“触发”“下一步”“推进”“例外”分别给一句具体条件或动作。明确何时换查询源、询问或拟案。
不能把未返回某字段推断为客户没有该资料。没有证据支持时允许字段留空。经验限240字。"""
SCENES = {
    "50正": (50,"请使用当前订单的默认开票资料，先给我看草案。"),
    "50反": (50,"这笔订单的个人发票抬头我已核对，是‘示例甲’，请按这个准备草案给我看。"),
    "32正": (32,"物流问题先放一下。我现在明确想修改这笔订单的收货地址，请先查这笔订单还能不能改。"),
    "32反": (32,"我是在查另一件包裹，还没提供它的运单号。请别改地址，先帮我查证。"),
}


def extra(batch,case):
    rows=history(batch,case)
    folder=position(batch,"ECom-Bench",case)
    with sqlite3.connect((folder/"business.db").as_uri()+"?mode=ro",uri=True) as db:
        msgs=json.loads(db.execute("select history from sessions").fetchone()[0])
    msgs=[m for m in msgs if m["role"] in ("tool","user","assistant")]
    seen={}
    for row,msg in zip(rows,msgs,strict=True):
        if msg["role"]!="tool":
            continue
        value=msg.get("content") or ""
        digest=sha(value.encode())
        row["相同回执首见"]=seen.setdefault(digest,row["序"])
        try:
            obj=json.loads(value)
            if isinstance(obj,str):
                obj=json.loads(obj)
        except (TypeError,ValueError):
            obj=None
        if isinstance(obj,dict):
            row["回包字段"]=list(obj)[:45]
            row["相关字段有值"]={k:bool(v) for k,v in obj.items() if any(w in k for w in ("发票","抬头","姓名","状态","运单","地址","签收","电话"))}
        else:
            row["结果词"]=[w for w in ("失败","成功","已为","转接","请耐心","错误","未找到","确认") if w in value]
    return rows


async def build():
    budget=ModelBudget(RUN/"总结.db",dict(calls=6,tokens=210000,request_tokens=130000,seconds=86400))
    for case in (50,32):
        for key,effort in (("旧","medium"),("新","low"),("新","medium")):
            obj=await generate(RUN,f"{case}-{key}-{effort}",PROMPT if key=="旧" else NEW,
                               history(BATCH,case) if key=="旧" else extra(BATCH,case),budget,effort)
            assert obj["依据"] and all(type(n)==int and 0<=n<len(history(BATCH,case)) for n in obj["依据"])
            if key=="新":
                note="\n".join(f"{k}：{obj[k]}" for k in ("触发","下一步","推进","例外"))
                assert len(note)<900
                wiki=RUN/f"wiki-{case}-{effort}"
                wiki.mkdir(exist_ok=True)
                (wiki/"guide.md").write_text(note)
                write_json(wiki/"manifest.json",dict(pages=dict(guide=dict(适用="当前决策",例外="以本单事实和授权为准",sha256=sha(note.encode())))))


async def replay(scene,arm):
    case,user=SCENES[scene]
    folder=RUN/f"{scene}-{arm}"
    folder.mkdir()
    budget=ModelBudget(RUN/"回放.db",dict(calls=36,tokens=900000,request_tokens=150000,seconds=86400))
    with patch.object(ecom.server,"set_data",lambda data:None):
        store,sid,_=await restore_ecom(case,folder,BATCH)
        state,h=store.snapshot(sid)
        before=accounts(store)
        prefix=sha(json.dumps([state,canonical(h),before,user],sort_keys=True).encode())
        os.environ.update(RETAILOPS_WIKI_MODE="关闭" if arm=="关" else "直给",
                          RETAILOPS_WIKI_DIR=str(RUN/f"wiki-{case}-{arm}"),RETAILOPS_WIKI_PAGE="guide")
        async with client(Meter(budget,folder,ANSWER,"medium",cap=2)) as http:
            try:
                await make_agent(store,http).run(sid,user,state["revision"],"C","回放")
            except PositionLimit:
                pass
        assert before==accounts(store)
        logs=[json.loads(s) for s in (folder/"请求.jsonl").read_text().splitlines()]
        assert logs and all(r["正文"]==(arm!="关") for r in logs)
        write_json(folder/"结果.json",dict(前缀=prefix,动作=[r["动作"] for r in logs],用量=sum(r["用量"]["total_tokens"] for r in logs)))


if __name__=="__main__":
    setup()
    RUN.mkdir(mode=0o700,exist_ok=True)
    if sys.argv[1]=="build":
        with bridge(RUN,BUILD,18882):
            asyncio.run(build())
    else:
        with bridge(RUN,ANSWER,18883):
            for scene in SCENES:
                for arm in ("关","low","medium"):
                    if not (RUN/f"{scene}-{arm}"/"结果.json").exists():
                        asyncio.run(replay(scene,arm))
