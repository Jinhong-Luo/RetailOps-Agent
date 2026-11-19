"""官方示例适配版三轮开关实验：逐题完成后整理，独立账本，已有官方零分不重跑。"""
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
from retailops.official_memory import snapshot,validate_wiki

ARMS=("关闭","持续")


def head():
    return subprocess.check_output(["git","-C",str(ROOT),"rev-parse","HEAD"],text=True).strip()


def matrix(ids):
    if len(ids)!=33 or len(set(ids))!=33:
        raise ValueError("正式题单必须恰有三十三个不同题号")
    return [dict(轮次=r,题号=t,条件=a) for r in (1,2,3) for t in ids for a in ARMS]


def manifest(commit):
    from ecom_single import plan
    cfg=plan()
    if cfg["提交"]!=commit:
        raise ValueError("服务器代码与指定冻结提交不符")
    subprocess.run(["git","-C",str(ROOT),"diff","--quiet","HEAD","--"],check=True)
    cfg.update(预检=[1],条件=list(ARMS),轮次=3,记忆="官方示例完整流程，空库开始，题后分析与整体整理，后续会话只读",
               官方示例=read_json(ROOT/"third_party/anthropic_dreaming/来源锁定.json"),
               提炼模型="gpt-6.1-sol",提炼强度="medium",记忆读入="按需文件工具，计入原八步，不预取",
               顺序="正式题号升序，三轮相同",当轮全量重置="会话、订单、余额、回执",
               旧固定笔记="不载入",旧简化记忆="不载入",开发记忆="不进入正式",
               后台每角色保护上限=120,后台整体时限秒=7200,
               迁移评估="第一轮不含本题先前轨迹",重复评估="第二与第三轮允许前轮同题经验")
    cfg["任务清单"]=matrix(cfg["正式"])
    return cfg


def unique_score(base,commit,bank,arm):
    matches=list(base.glob("*/官方评分.json"))
    if len(matches)>1:
        raise ValueError("同一正式位置存在多份官方分")
    if not matches:
        return None
    s=read_json(matches[0])
    if (s["提交"],s["记忆摘要"],s["条件"])!=(commit,bank,arm) or not s["模型强度匹配"] or not s.get("核验完成"):
        raise ValueError("已有官方分与本位置冻结条件不符")
    return matches[0].parent


def worker(task,r,arm,folder,commit,bank):
    from ecom_paired import episode
    if head()!=commit:
        raise ValueError("单题运行的代码提交变化")
    load_env()
    os.environ.pop("RETAILOPS_MEMORY_BANK",None)
    os.environ.pop("RETAILOPS_CONTINUAL_BANK",None)
    os.environ.update(RETAILOPS_ECOM_PRODUCT="1",RETAILOPS_ECOM_REFUND="1",RETAILOPS_ECOM_BOOKING="1",
       RETAILOPS_MODEL="gpt-6.1-sol@medium",RETAILOPS_REFERENCE="官方移植" if arm=="持续" else "无记忆",
       RETAILOPS_OFFICIAL_STORE=str(bank),RETAILOPS_MEMORY_CONTRACT=str(folder/"业务契约.json"),
       RETAILOPS_BOOKING_MODE="轻量",RETAILOPS_ALIGNED_ARM="full",
       OPENAI_API_BASE="http://127.0.0.1:18863/v1",OPENAI_API_KEY=os.environ["CODEX_BRIDGE_TOKEN"])
    before=validate_wiki(bank)
    start=time.monotonic()
    result=asyncio.run(episode(task,0,"button",folder,record_usage=True))
    # 官方分一旦返回立即留档；后续核验失败也不能作为无分位置补跑。
    value=dict(题号=task,轮次=r,条件=arm,提交=commit,记忆摘要=before["digest"],
               记忆条目数=before["index_entries"],用时秒=time.monotonic()-start,
               模型强度匹配=False,核验完成=False,**result)
    write_json(folder/"评分待核验.tmp",value)
    (folder/"评分待核验.tmp").replace(folder/"官方评分.json")
    if snapshot(bank)["digest"]!=before["digest"]:
        raise ValueError("对话期间题前记忆快照被修改")
    usage=[json.loads(line) for line in (folder/"usage.jsonl").read_text().splitlines()]
    matched=bool(usage) and all(x["model"]=="gpt-6.1-sol" and x["effort"]=="medium" for x in usage)
    with sqlite3.connect((folder/"business.db").resolve().as_uri()+"?mode=ro",uri=True) as db:
        events=[json.loads(x[0]) for x in db.execute("select body from events")]
    refs=[e for e in events if e["stage"]=="官方记忆读取"]
    if arm=="关闭" and refs:
        raise ValueError("关闭条件出现记忆读取")
    if any(e["version"]!=before["digest"] or e["step"]>8 for e in refs):
        raise ValueError("记忆读取版本或步骤不符")
    assert all(result[k] in (0,1) for k in ("score","action","search","output"))
    value=dict(题号=task,轮次=r,条件=arm,提交=commit,记忆摘要=before["digest"],
               记忆条目数=before["index_entries"],记忆读取次数=len(refs),
               记忆读取请求数=len({e["request_id"] for e in refs}),
               用时秒=value["用时秒"],模型强度匹配=matched,核验完成=True,**result)
    write_json(folder/"官方评分.json",value)
    if not matched:
        raise ValueError("保留官方分，但模型条件不匹配，禁止重采样")


def pair(root,phase,r,task,commit,bank):
    digest=snapshot(bank)["digest"]
    def one(arm):
        base=root/phase/str(r)/arm/str(task)
        done=unique_score(base,commit,digest,arm)
        if done:
            return done
        folder=base/uuid4().hex
        folder.mkdir(parents=True)
        with (folder/"运行.log").open("w") as log:
            proc=subprocess.run([sys.executable,__file__,"单题",str(folder),commit,"--task",str(task),
                                 "--round",str(r),"--arm",arm,"--bank",str(bank)],
                                 cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        if proc.returncode:
            write_json(folder/"首次或后续异常.json",dict(返回码=proc.returncode,
                         已有评分=(folder/"官方评分.json").exists()))
            raise RuntimeError("单题异常；保留已有评分和全部尝试后停止")
        done=unique_score(base,commit,digest,arm)
        if not done:
            write_json(folder/"首次或后续异常.json",dict(返回码=proc.returncode,已有评分=False))
            raise RuntimeError("单题未取得官方分，保留尝试后停止")
        return done
    with ThreadPoolExecutor(max_workers=2) as pool:
        values=list(pool.map(one,ARMS))
    return dict(zip(ARMS,values))


def update(root,r,task,bank,episode):
    from official_dream import dream
    base=root/"整理"/str(r)/str(task)
    completed=list(base.glob("*/完成.json"))
    if len(completed)>1:
        raise ValueError("同轨迹存在多个完成的整理结果")
    if completed:
        return dream(bank,episode,completed[0].parent)
    # 只有未完成整理可恢复；原失败目录一直保留，不替换已发布经验。
    return dream(bank,episode,base/uuid4().hex)


def run(phase,root,commit,probe_bank=None):
    from fcntl import flock,LOCK_EX,LOCK_NB
    root.mkdir(parents=True,exist_ok=True)
    with (root/"运行锁").open("a") as lock:
        flock(lock,LOCK_EX|LOCK_NB)
        cfg=manifest(commit)
        if (root/"配置.json").exists() and read_json(root/"配置.json")!=cfg:
            raise ValueError("实验目录已固定为其他配置")
        write_json(root/"配置.json",cfg)
        initial=root/"初始空库"
        if not initial.exists():
            initial.mkdir()
            (initial/"_index.md").write_text("# 经验索引\n",encoding="utf8")
        if set(snapshot(initial)["files"])!={"_index.md"} or (initial/"_index.md").read_text()!="# 经验索引\n":
            raise ValueError("正式实验初始库不为空")
        if phase=="清单":
            print(json.dumps(dict(提交=commit,正式位置=len(cfg["任务清单"]),每轮每条件=33,轮次=3,条件=list(ARMS)),ensure_ascii=False))
            return
        if phase=="预检":
            if probe_bank is None or validate_wiki(probe_bank)["index_entries"]==0:
                raise ValueError("真实开关预检须提供隔离开发记忆")
            result=pair(root,"预检",0,1,commit,probe_bank)
            scores={a:read_json(f/"官方评分.json") for a,f in result.items()}
            if not scores["持续"]["记忆读取次数"] or scores["关闭"]["记忆读取次数"]:
                raise ValueError("预检未证明真实记忆开关差异")
            write_json(root/"预检完成.json",dict(提交=commit,开关生效=True,开发库不进入正式=True))
            print("真实开关预检完成",flush=True)
            return
        if not (root/"预检完成.json").exists():
            raise ValueError("尚未通过真实开关预检")
        bank=initial
        for r in (1,2,3):
            for task in cfg["正式"]:
                write_json(root/"进度.json",dict(阶段="对话",轮次=r,题号=task,记忆摘要=snapshot(bank)["digest"]))
                result=pair(root,"正式",r,task,commit,bank)
                write_json(root/"进度.json",dict(阶段="整理",轮次=r,题号=task,记忆摘要=snapshot(bank)["digest"]))
                bank=update(root,r,task,bank,result["持续"])
        write_json(root/"完成.json",dict(提交=commit,正式位置=198,轮次=3,最终记忆摘要=snapshot(bank)["digest"]))
        print("三轮全部完成",flush=True)


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase",choices=["清单","预检","正式","单题"])
    p.add_argument("root",type=Path)
    p.add_argument("commit")
    p.add_argument("--task",type=int)
    p.add_argument("--round",type=int)
    p.add_argument("--arm",choices=ARMS)
    p.add_argument("--bank",type=Path)
    a=p.parse_args()
    if a.phase=="单题":
        worker(a.task,a.round,a.arm,a.root.resolve(),a.commit,a.bank.resolve())
    else:
        run(a.phase,a.root.resolve(),a.commit,a.bank.resolve() if a.bank else None)
