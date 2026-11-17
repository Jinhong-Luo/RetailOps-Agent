"""在原有冻结公开运行器之外调度进程，恢复已输出的官方评分并按桥接剩余席位投放。"""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from fcntl import flock, LOCK_EX, LOCK_NB
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from public_modules import archived, positions, load, sha, write


def native_score(folder, row, arm, release, harness):
    raw = folder/"native/zh-retail/results.json"
    if not raw.exists():
        return None
    records = load(raw)["simulations"]
    if not records:
        return None
    if len(records) != 1 or records[0]["task_id"].split("_")[0] != row.split("-")[1]:
        raise ValueError("上游轨迹数量或题号不符")
    sim = records[0]
    reward = sim.get("reward_info")
    if reward is None or reward.get("reward") is None:
        return None
    from aligned_trial import usage
    calls = []
    for message in sim["messages"]:
        if message.get("usage"):
            meta=(message.get("raw_data") or {}).get("bridge_metadata",{})
            calls.append(dict(usage=message["usage"], model=meta.get("actual_model"), effort=meta.get("reasoning_effort")))
    return dict(题号=row,条件=arm,服务提交=release if arm=="full" else harness,冻结参照=release,运行器提交=harness,
                来源="恢复上游已落盘评分",用时秒=sim.get("duration"),score=reward["reward"],
                termination=sim["termination_reason"],seed=sim["seed"], **usage(calls))


def occupied_by_shared(target):
    count=0
    for proc in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args=proc.read_bytes().split(b"\0")
        except (FileNotFoundError,PermissionError,ProcessLookupError):
            continue
        if len(args)>3 and args[1].endswith(b"/shared_modules.py") and args[2]=="单项".encode():
            if any(str(target).encode() in arg for arg in args):
                count+=1
    return count


def public_capacity(shared, jobs, shared_slots):
    reserve=0 if (shared/"共享全部采集完毕").is_file() else shared_slots
    return max(1,jobs-max(reserve,occupied_by_shared(shared)))


def reconcile(folder, row, arm, release, harness):
    valid=[]
    for attempt in folder.glob("*"):
        if (attempt/"采集排除.json").exists():
            continue
        if not attempt.is_dir():
            continue
        score=attempt/"官方评分.json"
        if not score.exists() and row.startswith("tau-"):
            found=native_score(attempt,row,arm,release,harness)
            if found is not None:
                write(score,found)
        if score.exists():
            value=load(score)
            if value["冻结参照"]!=release or value["运行器提交"]!=harness:
                raise ValueError("结果版本不匹配")
            valid.append(score)
        elif not (attempt/"异常.json").exists():
            write(attempt/"异常.json",dict(题号=row,条件=arm,原因="上次调度退出，未落盘有效官方评分"))
    if len(valid)>1:
        raise ValueError("同位置存在多个官方评分")
    return bool(valid)


def run(args):
    entries=positions(args.source)
    output=args.output
    output.mkdir(parents=True,exist_ok=True)
    release,harness=sha(args.base),sha(args.runtime)
    config=load(output/"公开位置.json")
    if config != dict(冻结参照=release,运行器=harness,位置=entries):
        raise ValueError("排期与已有数据版本不符")
    env=os.environ.copy()
    env["PYTHONPATH"]=os.pathsep.join(str(args.runtime/p) for p in (".","scripts","runtime","vendor/tau2-bench/src"))
    env["TAU2_DATA_DIR"]=str(args.runtime/"vendor/tau2-bench/data")
    env["LITELLM_LOCAL_MODEL_COST_MAP"]="True"
    with (output/"公开运行锁").open("a") as lock:
        flock(lock,LOCK_EX|LOCK_NB)
        pending=[]
        for key,spec in entries.items():
            row,arm=spec["题号"],spec["条件"]
            base=output/key
            if reconcile(base,row,arm,release,harness):
                continue
            if row.startswith("ecom-") and arm=="full":
                original,result=archived(args.archive,row.split("-")[1],release)
                attempt=base/uuid4().hex
                attempt.mkdir(parents=True)
                write(attempt/"官方评分.json",dict(**result,条件=arm,冻结参照=release,服务提交=release,
                                             运行器提交=harness,来源="复用",原评分文件=str(original)))
            else:
                pending.append(key)

        def one(key):
            spec=entries[key]
            row,arm=spec["题号"],spec["条件"]
            folder=output/key/uuid4().hex
            folder.mkdir(parents=True)
            with (folder/"运行.log").open("w") as log:
                process=subprocess.run([sys.executable,str(args.runtime/"scripts/public_modules.py"),"单题",
                        str(args.base),release,harness,row,arm,str(folder)],env=env,cwd=args.runtime,
                        stdout=log,stderr=subprocess.STDOUT)
            scored=reconcile(output/key,row,arm,release,harness)
            if not scored:
                write(folder/"异常.json",dict(题号=row,条件=arm,返回码=process.returncode,原因="没有有效官方评分"))
            return key,scored

        active=set()
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            while pending or active:
                capacity=public_capacity(output.parent/"共享",args.jobs,args.shared_slots)
                while pending and len(active)<capacity:
                    active.add(pool.submit(one,pending.pop(0)))
                if active:
                    done,active=wait(active,timeout=20,return_when=FIRST_COMPLETED)
                    for event in done:
                        key,ok=event.result()
                        print(key,"已取得官方评分" if ok else "保留无分异常",flush=True)
        missing=[key for key in entries if not list((output/key).glob("*/官方评分.json"))]
        print("本轮后缺分",missing,flush=True)
        return int(bool(missing))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("source","base","runtime","archive","output"):
        parser.add_argument(name,type=lambda p:Path(p).resolve())
    parser.add_argument("--jobs",type=int,default=3)
    parser.add_argument("--shared-slots",type=int,default=2)
    sys.exit(run(parser.parse_args()))
