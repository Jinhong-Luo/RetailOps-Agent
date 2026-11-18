"""固定业务代码，按完成顺序更新经验，比较三遍全题集的记忆开关。"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4
from common import ROOT,load_env,read_json,write_json
from retailops.continual_memory import empty_bank,digest,validate_bank

ARMS=("关闭","持续")
def head():
    return subprocess.check_output(["git","-C",str(ROOT),"rev-parse","HEAD"],text=True).strip()

def matrix(ids,rounds=3):
    if len(ids)!=len(set(ids)) or rounds!=3:
        raise ValueError("清单题号重复或轮数不符")
    return [dict(轮次=r,题号=t,条件=a) for r in range(1,rounds+1) for t in ids for a in ARMS]

def manifest(commit):
    from ecom_single import plan
    p=plan()
    if p["提交"]!=commit:
        raise ValueError("运行版本不符")
    subprocess.run(["git","-C",str(ROOT),"diff","--quiet","HEAD","--"],check=True)
    p.update(条件=list(ARMS),记忆="空库开始，题后更新，下一题只读",轮次=3,初始记忆=digest(empty_bank()),
             提炼模型="gpt-6.1-sol",提炼强度="medium",更新上限=3,注入上限字节=800,检索上限条数=3,
             顺序="正式题号升序，三轮相同",当轮全量重置="会话、订单、余额、回执",旧固定笔记="不载入",
             迁移评估="第一轮；答题前记忆库不含本题先前轨迹",重复评估="第二与第三轮；允许本题前轮经验")
    p["任务清单"]=matrix(p["正式"])
    assert len(p["任务清单"])==198
    return p

def unique_score(base,commit,bank_hash,arm):
    matches=list(base.glob("*/官方评分.json"))
    if len(matches)>1:
        raise ValueError("同位置重复官方评分")
    if matches:
        value=read_json(matches[0])
        if (value["提交"],value["记忆摘要"],value["条件"])!=(commit,bank_hash,arm):
            raise ValueError("已有官方评分与配置不符")
        return matches[0].parent
    return None

def worker(task,round_id,arm,folder,commit,bank):
    from ecom_paired import episode
    if head()!=commit:
        raise ValueError("实际工作树提交变化")
    load_env()
    os.environ.pop("RETAILOPS_MEMORY_BANK",None)
    os.environ.update(RETAILOPS_ECOM_PRODUCT="1",RETAILOPS_ECOM_REFUND="1",RETAILOPS_ECOM_BOOKING="1",
        RETAILOPS_MODEL="gpt-6.1-sol@medium",RETAILOPS_REFERENCE="持续记忆" if arm=="持续" else "无记忆",
        RETAILOPS_CONTINUAL_BANK=str(bank),RETAILOPS_BOOKING_MODE="轻量",RETAILOPS_ALIGNED_ARM="full",
        OPENAI_API_BASE="http://127.0.0.1:18863/v1",OPENAI_API_KEY=os.environ["CODEX_BRIDGE_TOKEN"])
    snapshot=validate_bank(read_json(bank))
    started=time.monotonic()
    value=asyncio.run(episode(task,0,"button",folder,record_usage=True))
    usage=[json.loads(l) for l in (folder/"usage.jsonl").read_text().splitlines()]
    matched=bool(usage) and all(u["model"]=="gpt-6.1-sol" and u["effort"]=="medium" for u in usage)
    with sqlite3.connect((folder/"business.db").resolve().as_uri()+"?mode=ro",uri=True) as db:
        events=[json.loads(x[0]) for x in db.execute("select body from events")]
    refs=[e for e in events if e["stage"]=="跨会话参考"]
    if arm=="关闭" and refs:
        raise ValueError("关闭组出现经验注入")
    if any(e["bytes"]>800 or len(e["hits"])>3 or e["version"]!=digest(snapshot) for e in refs):
        raise ValueError("实际注入与冻结记忆不符")
    assert all(value[k] in (0,1) for k in ("score","action","search","output"))
    result=dict(题号=task,轮次=round_id,条件=arm,提交=commit,记忆摘要=digest(snapshot),记忆版本=snapshot["revision"],
        记忆条目数=len(snapshot["entries"]),用时秒=time.monotonic()-started,模型强度匹配=matched,
        注入轮数=len(refs),最大注入字节=max((e["bytes"] for e in refs),default=0),**value)
    write_json(folder/"官方评分.json",result)
    if not matched:
        raise ValueError("已保留官方分但模型强度不匹配，禁止继续或重采样")

def pair(root,phase,round_id,task,commit,bank):
    expected=digest(read_json(bank))
    def one(arm):
        base=root/phase/str(round_id)/arm/str(task)
        scored=unique_score(base,commit,expected,arm)
        if scored:
            if not read_json(scored/"官方评分.json")["模型强度匹配"]:
                raise ValueError("历史位置不可比")
            return scored
        folder=base/uuid4().hex
        folder.mkdir(parents=True)
        with (folder/"运行.log").open("w") as log:
            proc=subprocess.run([sys.executable,__file__,"单题",str(folder),commit,"--task",str(task),
                "--round",str(round_id),"--arm",arm,"--bank",str(bank)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        scored=unique_score(base,commit,expected,arm)
        if proc.returncode:
            write_json(folder/"首次或后续异常.json",dict(返回码=proc.returncode,已有评分=scored is not None))
            raise RuntimeError("单题未正常结束，保留评分和首次异常后停止")
        if not scored:
            raise RuntimeError("单题未取得官方评分")
        return scored
    with ThreadPoolExecutor(max_workers=2) as pool:
        values=list(pool.map(one,ARMS))
    return dict(zip(ARMS,values))

def update_memory(root,round_id,task,bank,folder):
    from memory_dream import dream
    base=root/"整理"/str(round_id)/str(task)
    finished=list(base.glob("*/更新.json"))
    if len(finished)>1:
        raise ValueError("同轨迹重复整理")
    if finished:
        return dream(bank,folder,finished[0].parent)
    target=base/uuid4().hex
    try:
        return dream(bank,folder,target)
    except Exception as exc:
        # 异常正文可能含模型文本，只在私有日志保留；清单只记录固定类型。
        write_json(target/"提炼异常.json",dict(类型=type(exc).__name__))
        raise

def run(phase,root,commit,probe_bank=None):
    from fcntl import flock,LOCK_EX,LOCK_NB
    root.mkdir(parents=True,exist_ok=True)
    with (root/"运行锁").open("a") as lock:
        flock(lock,LOCK_EX|LOCK_NB)
        config=manifest(commit)
        if (root/"配置.json").exists() and read_json(root/"配置.json")!=config:
            raise ValueError("结果目录已有其他配置")
        write_json(root/"配置.json",config)
        initial=root/"初始空记忆.json"
        if initial.exists() and read_json(initial)!=empty_bank():
            raise ValueError("初始记忆不是空库")
        write_json(initial,empty_bank())
        if phase=="清单":
            print(json.dumps(dict(提交=commit,位置数=len(config["任务清单"]),轮次=3,每轮每组=33,条件=ARMS),ensure_ascii=False))
            return
        if phase=="预检":
            if probe_bank is None:
                raise ValueError("预检必须给出隔离开发记忆")
            data=validate_bank(read_json(probe_bank))
            if not data["entries"]:
                raise ValueError("预检经验为空")
            copied=root/"预检专用记忆.json"
            if copied.exists() and read_json(copied)!=data:
                raise ValueError("预检经验改变")
            write_json(copied,data)
            results=pair(root,"预检",0,1,commit,copied)
            checks={arm:read_json(p/"官方评分.json") for arm,p in results.items()}
            if not checks["持续"]["注入轮数"] or checks["关闭"]["注入轮数"]:
                raise ValueError("真实入口没有证明记忆开关差异")
            write_json(root/"预检完成.json",dict(提交=commit,开关生效=True,开发记忆不进入正式=True))
            print("预检完成",flush=True)
            return
        if not (root/"预检完成.json").exists():
            raise ValueError("真实入口开关预检尚未完成")
        bank=initial
        for r in range(1,4):
            for task in config["正式"]:
                write_json(root/"进度.json",dict(阶段="对话",轮次=r,题号=task,记忆版本=read_json(bank)["revision"]))
                result=pair(root,"正式",r,task,commit,bank)
                write_json(root/"进度.json",dict(阶段="整理",轮次=r,题号=task,记忆版本=read_json(bank)["revision"]))
                bank=update_memory(root,r,task,bank,result["持续"])
        write_json(root/"完成.json",dict(提交=commit,正式位置=198,已完成轮次=3,最终记忆摘要=digest(read_json(bank))))
        print("三轮全部完成",flush=True)

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase",choices=["清单","预检","正式","单题"])
    parser.add_argument("root",type=Path)
    parser.add_argument("commit")
    parser.add_argument("--task",type=int)
    parser.add_argument("--round",type=int)
    parser.add_argument("--arm",choices=ARMS)
    parser.add_argument("--bank",type=Path)
    args=parser.parse_args()
    if args.phase=="单题":
        worker(args.task,args.round,args.arm,args.root.resolve(),args.commit,args.bank.resolve())
    else:
        run(args.phase,args.root.resolve(),args.commit,args.bank.resolve() if args.bank else None)
