"""对冻结候选运行 ECom 中文文本单次清单，单题进程和账本独立。"""
import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from common import ROOT, load_env, read_json, verify_upstream, write_json


def revision(directory):
    return subprocess.check_output(["git","-C",directory,"rev-parse","HEAD"],text=True).strip()


def plan():
    from ecom_paired import UP, UPSTREAM, partition
    assert revision(UP) == UPSTREAM
    subprocess.run(["git","-C",UP,"diff","--quiet","HEAD","--"],check=True)
    verify_upstream()
    fixed = read_json(ROOT/"reports/ecom-text-plan.json")
    official = partition()
    assert fixed["formal"] == official["formal"] and len(set(official["formal"])) == 33
    assert official["pilot"] == [1,3]
    return dict(提交=revision(ROOT),上游=UPSTREAM,客服模型="gpt-6.1-sol",顾客模型="gpt-6.1-sol",
                强度="medium",种子=300,每轮步数=8,整单轮数=20,重复次数=1,条件=["三项全开"],
                记忆="提炼记忆",授权="按钮",等待="轻量",预检=official["pilot"],正式=official["formal"])


def worker(task,folder,commit):
    from ecom_paired import episode
    assert revision(ROOT)==commit
    load_env()
    os.environ.update(RETAILOPS_ECOM_PRODUCT="1",RETAILOPS_ECOM_REFUND="1",RETAILOPS_ECOM_BOOKING="1",
                      RETAILOPS_MODEL="gpt-6.1-sol@medium",RETAILOPS_REFERENCE="提炼记忆",
                      RETAILOPS_BOOKING_MODE="轻量",RETAILOPS_ALIGNED_ARM="full",
                      OPENAI_API_BASE="http://127.0.0.1:18863/v1",OPENAI_API_KEY=os.environ["CODEX_BRIDGE_TOKEN"])
    started=time.monotonic()
    result=asyncio.run(episode(task,0,"button",folder,record_usage=True))
    assert all(result[key] in (0,1) for key in ("score","action","search","output"))
    write_json(folder/"得分.tmp",dict(题号=task,提交=commit,用时秒=time.monotonic()-started,**result))
    (folder/"得分.tmp").replace(folder/"官方评分.json")


def run(phase,root,commit):
    from fcntl import LOCK_EX, LOCK_NB, flock
    root.mkdir(parents=True,exist_ok=True)
    with (root/"运行锁").open("a") as lock:
        flock(lock,LOCK_EX|LOCK_NB)
        config=plan()
        if config["提交"]!=commit:
            raise ValueError("服务器工作树提交不符")
        manifest=root/"配置.json"
        if manifest.exists() and read_json(manifest)!=config:
            raise ValueError("实验目录已有其他冻结配置")
        write_json(manifest,config)
        if phase=="清单":
            print(json.dumps(config,ensure_ascii=False))
            return
        def scored(base):
            return list(base.glob("*/官方评分.json"))
        if phase=="正式" and any(len(scored(root/"预检"/str(i)))!=1 for i in config["预检"]):
            raise ValueError("两个连通预检尚未取得官方评分")
        def one(task):
            base=root/phase/str(task)
            finished=scored(base)
            if finished:
                assert len(finished)==1 and read_json(finished[0])["提交"]==commit
                return
            folder=base/uuid4().hex
            folder.mkdir(parents=True)
            with (folder/"运行.log").open("w") as log:
                proc=subprocess.run([sys.executable,__file__,"单题",str(folder),commit,"--task",str(task)],
                                    cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
            if proc.returncode and not scored(base):
                write_json(folder/"首次或后续异常.json",dict(返回码=proc.returncode,题号=task))
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(one,config[phase]))
        missing=[i for i in config[phase] if not scored(root/phase/str(i))]
        print("未评分",missing,flush=True)
        if missing:
            sys.exit(1)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase",choices=["清单","预检","正式","单题"])
    parser.add_argument("root",type=Path)
    parser.add_argument("commit")
    parser.add_argument("--task",type=int)
    args=parser.parse_args()
    if args.phase=="单题":
        worker(args.task,args.root.resolve(),args.commit)
    else:
        run(args.phase,args.root.resolve(),args.commit)
