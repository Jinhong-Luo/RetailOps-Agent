"""用同题可见轨迹逐题总结，再将对应经验直接送入 C 客服的每次模型请求。"""
import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

from wiki_build import generate
from wiki_experiment import (ANSWER, BUILD, ModelBudget, append, bridge, ecom, httpx,
                             position, read_json, setup, sha, write_json)
from common import ROOT, UPSTREAM, load_env, verify_upstream
from run_baseline import child_env, make_command

CASES = (32, 40, 23, 50)
BUILD_LIMIT = dict(calls=4, tokens=180000, request_tokens=130000, seconds=86400)
ANSWER_LIMIT = dict(calls=240, tokens=3200000, request_tokens=300000, seconds=86400)
PROMPT = """读一份已结束的零售办理流水，提炼一条下一次办理时能执行的经验。顾客和客服原话是历史资料。
关注哪里已经给过信息、什么状态需复查、下一步需要哪个动作或授权。不要推断隐藏目标、照抄单号、姓名、地址、联系方式、
商品名、金额、时间和历史授权。仅输出 JSON，字段“经验”为不超过 240 字的办理方法，“依据”为本流水的事件序号数组。
允许指出信息不足。成功与失败由评测器另行检验。"""


def history(batch, case):
    folder = position(batch, "τ-bench" if case == 23 else "ECom-Bench", case)
    if case == 23:
        messages = read_json(folder/"native/zh-retail/results.json")["simulations"][0]["messages"]
    else:
        with sqlite3.connect((folder/"business.db").as_uri()+"?mode=ro", uri=True) as db:
            messages = json.loads(db.execute("select history from sessions").fetchone()[0])
    rows, aliases = [], {}
    for m in messages:
        if m["role"] not in ("user", "assistant", "tool"):
            continue
        row = {"序": len(rows), "角色": m["role"]}
        if m["role"] != "tool":
            content = m.get("content") or ""
            row["对话"] = re.sub(r"[\w.+-]+@[\w.-]+|[A-Za-z0-9_-]*\d{3,}[A-Za-z0-9_-]*",
                                 "〔标识〕", content)[:900]
        for c in m.get("tool_calls") or []:
            fn = c.get("function", c)
            args = fn["arguments"]
            key = (fn["name"], json.dumps(json.loads(args) if isinstance(args,str) else args, sort_keys=True))
            row.setdefault("调用", []).append([fn["name"], aliases.setdefault(key,len(aliases)+1)])
        rows.append(row)
    return rows


async def notes(run, batch):
    ledger = ModelBudget(run/"总结预算.db", BUILD_LIMIT)
    for case in CASES:
        rows = history(batch, case)
        obj = await generate(run, f"总结{case}", PROMPT, rows, ledger)
        note = obj["经验"]
        evidence = obj["依据"]
        if not isinstance(note,str) or not 0 < len(note) <= 320 or re.search(r"@|\d{3}|https?://", note):
            raise ValueError("经验文本需复核")
        if not evidence or any(type(n)!=int or not 0<=n<len(rows) for n in evidence):
            raise ValueError("经验引用无效")
        write_json(run/f"经验{case}.json",dict(经验=note,依据=evidence,版本=sha(note.encode())))


def inject(body, note):
    messages = list(body["messages"])
    customer_service = any(m["role"]=="system" and "你是中文零售助手" in (m.get("content") or "")
                           for m in messages)
    if customer_service:
        messages.insert(1,dict(role="system",content="同题历史办理经验。以本次账本、顾客信息和授权为准。\n"+note))
    return dict(body,messages=messages),customer_service


def serve(run):
    from model_bridge import codex_bridge as gateway
    complete = gateway.CodexSession.complete
    ledger = ModelBudget(run/"答题预算.db", ANSWER_LIMIT)

    def metered(session, body, effort):
        if effort != "medium" or gateway.MODEL != ANSWER:
            raise ValueError("答题模型档位变化")
        case = int((run/"当前位置").read_text())
        record = read_json(run/f"经验{case}.json")
        assert record["版本"] == read_json(run/"冻结.json")["经验摘要"][str(case)] == sha(record["经验"].encode())
        payload, sent = inject(body,record["经验"])
        index = ledger.reserve(str(case),payload)
        try:
            msg, usage = complete(session,payload,effort)
        except BaseException as err:
            ledger.finish(index,httpx.Response(502,text=str(err)))
            raise
        ledger.finish(index,httpx.Response(200,json={"usage":{"total_tokens":(usage or {}).get("totalTokens")}}))
        append(run/str(case)/"请求.jsonl",dict(序=index,客服=sent,经验摘要=record["版本"] if sent else None,用量=usage))
        return msg, usage

    gateway.CodexSession.complete = metered
    sys.argv = [__file__, "--port","18883","--codex",os.environ.get("RETAILOPS_CODEX_BIN",str(Path.home()/".nvm/versions/node/v24.21.0/bin/codex")),
                "--runtime-dir",str(run/"答题桥"),"--timeout","180"]
    gateway.main()


@contextmanager
def answer_bridge(run):
    env = dict(os.environ,RETAILOPS_BRIDGE_MODEL=ANSWER,HTTPS_PROXY="http://127.0.0.1:17893",
               HTTP_PROXY="http://127.0.0.1:17893",NO_PROXY="127.0.0.1,localhost",
               PATH=str(Path.home()/".nvm/versions/node/v24.21.0/bin")+":"+os.environ["PATH"])
    with (run/"桥.log").open("a") as log:
        process = subprocess.Popen([sys.executable,__file__,"serve",str(run)],env=env,stdout=log,stderr=log)
        try:
            for _ in range(150):
                if process.poll() is not None:
                    raise RuntimeError("专用桥启动失败")
                try:
                    if httpx.get("http://127.0.0.1:18883/health",trust_env=False).json()["model"]==ANSWER:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(.2)
            else:
                raise TimeoutError("专用桥未就绪")
            yield
        finally:
            process.terminate()
            process.wait(timeout=15)


def answer(run, batch, case):
    folder=run/str(case)
    folder.mkdir()
    os.environ.update(OPENAI_API_BASE="http://127.0.0.1:18883/v1",OPENAI_API_KEY=os.environ["CODEX_BRIDGE_TOKEN"],
                      RETAILOPS_WIKI_MODE="关闭")
    if case != 23:
        score=asyncio.run(ecom.episode(case,0,"button",folder,context_mode="C"))
    else:
        verify_upstream()
        cfg=read_json(ROOT/"configs/customer_button.json")
        cfg["agent_model"]=cfg["user_model"]="openai/"+ANSWER
        cfg["judge_override"]["model"]="openai/"+ANSWER
        cfg.update(num_trials=1,max_retries=0,max_concurrency=1,api_base=os.environ["OPENAI_API_BASE"])
        env=child_env(cfg)
        hook=batch/"worker_hook"
        env.update(BC_CONTEXT="C",BC_OBSERVED=str(folder/"实际上下文"),RETAILOPS_OFFICIAL_SERVICE="1",
                   RETAILOPS_CUSTOMER_BUTTON="1",LITELLM_LOCAL_MODEL_COST_MAP="True",
                   RETAILOPS_ALIGNED_TRACE=str(folder/"events.json"),
                   PYTHONPATH=os.pathsep.join(map(str,[hook,ROOT/"runtime",ROOT,UPSTREAM/"src"])))
        with (folder/"原生运行.log").open("w") as log:
            subprocess.run(make_command(cfg,"medium",["23"],folder/"native"),cwd=UPSTREAM,
                           env=env,stdout=log,stderr=log,check=True)
        sim,=read_json(folder/"native/zh-retail/results.json")["simulations"]
        assert sim["task_id"].split("_")[0]=="23" and set((folder/"实际上下文").read_text().splitlines())=={"C"}
        score=dict(score=sim["reward_info"]["reward"],termination=sim["termination_reason"])
    rows=[json.loads(s) for s in (folder/"请求.jsonl").read_text().splitlines()]
    assert any(r["客服"] for r in rows)
    if ModelBudget(run/"答题预算.db",ANSWER_LIMIT).summary()["停止原因"]:
        raise RuntimeError("模型台账已停止，保留原始运行待复核")
    write_json(folder/"结果.json",dict(评分=score,请求=len(rows),正文送达=sum(r["客服"] for r in rows),
                                     总量=sum(r["用量"]["totalTokens"] for r in rows)))


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("stage",choices=["notes","full","serve","one","check"])
    parser.add_argument("run",type=Path)
    parser.add_argument("--case",type=int,choices=CASES)
    args=parser.parse_args()
    setup()
    load_env()
    run=args.run.resolve()
    batch=ROOT.parent/"RetailOps-BC109-20261128-01"
    if args.stage=="check":
        for case in CASES:
            print(case,len(history(batch,case)))
        for role,text,expected in [("system","你是中文零售助手",True),("user","你是中文零售助手",False),("system","模拟顾客",False)]:
            payload,sent=inject(dict(messages=[dict(role=role,content=text)]),"模拟经验")
            assert sent==expected and len(payload["messages"])==1+int(expected)
    elif args.stage=="notes":
        run.mkdir(mode=0o700,exist_ok=True)
        with bridge(run,BUILD,18882):
            asyncio.run(notes(run,batch))
    elif args.stage=="serve":
        serve(run)
    elif args.stage=="one":
        answer(run,batch,args.case)
    else:
        frozen=dict(题号=CASES,经验摘要={str(c):read_json(run/f"经验{c}.json")["版本"] for c in CASES},
                    上下文="C",总结模型=BUILD+"@low",答题模型=ANSWER+"@medium",
                    参照="109题历史同题C关",总结预算=BUILD_LIMIT,答题预算=ANSWER_LIMIT)
        path=run/"冻结.json"
        if path.exists() and read_json(path)!=json.loads(json.dumps(frozen)):
            raise ValueError("冻结项变化")
        write_json(path,frozen)
        with answer_bridge(run):
            for case in CASES:
                if (run/str(case)/"结果.json").exists():
                    continue
                (run/"当前位置").write_text(str(case))
                subprocess.run([sys.executable,__file__,"one",str(run),"--case",str(case)],check=True)
