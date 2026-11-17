"""逐位置隔离执行专题、复合及共享业务矩阵，保留已有结果与每次无结果异常。"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fcntl import flock, LOCK_EX, LOCK_NB
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from module_manifest import build, frozen_input
from public_modules import HERE, load, sha, write


def list_batch(plan, phase):
    jobs = plan["位置"]
    preferred = list(dict.fromkeys(plan["专题"].values()))
    if phase == "离线":
        return [k for k in preferred if jobs[k]["入口"]=="离线回放"]
    if phase == "专题":
        return [k for k in preferred if jobs[k]["入口"]!="离线回放" and k.startswith("D")]
    if phase == "复合":
        return [k for k in preferred if k.startswith("S")]
    if phase == "全量":
        return [k for k,v in jobs.items() if k.startswith(("D","S")) and v["入口"]=="业务对话" and k not in preferred]
    raise ValueError(phase)


def work(spec, scene, folder, archive):
    from ecom_paired import server
    case, kind, arm = spec["题号"], spec["入口"], spec["条件"]
    if kind=="离线回放":
        from topic_replays import evaluate
        value = evaluate(case,int(arm.split("-")[-1]),folder/"隔离输入",archive)
        return dict(类型=kind,诊断=value)
    if kind=="展示盲评":
        from plan_blind import run
        value=asyncio.run(run(folder,scene["顾客首句"],arm=="D16-2"))
        return dict(类型=kind,已收顾客复述=bool(value["自由复述"].strip()))
    from shared_dialog import run
    data = asyncio.run(run(scene,arm,folder))
    return dict(类型=kind,轮数=len(data["轮次"]),成功回执数=len(data["成功回执"]),
                已保存初终态=bool(data["终态"]))


def execute(args):
    plan=build(args.source)
    batches={phase:list_batch(plan,phase) for phase in ("离线","专题","复合","全量")}
    assert [len(v) for v in batches.values()]==[16,41,6,106]
    tasks=batches[args.phase]
    if args.dry:
        print("共享执行清单", {k:len(v) for k,v in batches.items()})
        return
    assert sha(HERE)==args.harness
    args.out.mkdir(parents=True,exist_ok=True)
    with (args.out/"共享运行锁").open("a") as lock:
        flock(lock,LOCK_EX|LOCK_NB)
        cfg=dict(运行器提交=args.harness,冻结参照=args.release,题单摘要=plan["输入摘要"])
        config=args.out/"共享配置.json"
        if config.exists() and cfg!=load(config):
            raise ValueError("结果目录内已有其他配置")
        write(config,cfg)

        def one(key):
            base=args.out/key
            scores=list(base.glob("*/已采集.json"))
            if scores:
                assert len(scores)==1 and load(scores[0])["运行器提交"]==args.harness
                return
            folder=base/uuid4().hex
            folder.mkdir(parents=True)
            with (folder/"运行.log").open("w") as log:
                child=subprocess.run([sys.executable,__file__,"单项",str(args.source),str(args.archive),
                    str(folder),key,args.harness,args.release],stdout=log,stderr=subprocess.STDOUT)
            if child.returncode and not (folder/"已采集.json").exists():
                write(folder/"异常.json",dict(返回码=child.returncode,位置=key))

        # 同题条件依照排期顺序逐一运行；不同题各有独立进程、目录和账本。
        grouped={}
        for key in tasks:
            grouped.setdefault(plan["位置"][key]["题号"],[]).append(key)
        def group(keys):
            for key in keys:
                one(key)
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            list(pool.map(group,grouped.values()))
        missing=[k for k in tasks if not list((args.out/k).glob("*/已采集.json"))]
        print("尚无结果位置",missing,flush=True)
        return int(bool(missing))


if __name__=="__main__":
    if sys.argv[1:2]==["单项"]:
        _,_,src,archive,folder,key,harness,release=sys.argv
        assert sha(HERE)==harness
        source=Path(src)
        scenes={s["编号"]:s for s in frozen_input(source,"共享中文客服场景.json")["场景"]}
        spec=build(source)["位置"][key]
        result=work(spec,scenes[spec["题号"]],Path(folder),Path(archive))
        write(Path(folder)/"已采集.json",dict(位置=key,冻结参照=release,运行器提交=harness,**result))
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument("phase",choices=["离线","专题","复合","全量"])
        for name in ("source","archive","out"):
            parser.add_argument(name,type=lambda p:Path(p).resolve())
        parser.add_argument("release")
        parser.add_argument("harness")
        parser.add_argument("--jobs",type=int,default=3)
        parser.add_argument("--dry",action="store_true")
        sys.exit(execute(parser.parse_args()))
