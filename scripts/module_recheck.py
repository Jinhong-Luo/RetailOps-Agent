"""在冻结服务代码上，从已采集的同题轨迹重建服务与官方评分的独立复核。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from fcntl import flock, LOCK_EX, LOCK_NB
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from module_manifest import build
from public_modules import load, sha, write


def original(root,row,release=None):
    public=row.startswith(("tau-","ecom-"))
    sub=root/("公开" if public else "共享")/(row+"__业务对话__full")
    matches=list(sub.glob("*/"+("官方评分.json" if public else "已采集.json")))
    if not matches:
        return None
    assert len(matches)==1
    record=load(matches[0])
    if release is not None and record.get("冻结参照")!=release:
        raise ValueError("源轨迹服务参照与要求的冻结提交不符")
    if record.get("来源")=="复用":
        raw=Path(record["原评分文件"])
        if release is not None and load(raw).get("提交")!=release:
            raise ValueError("复用源评分并非指定版本")
        return raw.parent
    return matches[0].parent


def child(args):
    base,folder,src=args.base,args.folder,args.src
    sys.path[:0]=[str(base/"scripts"),str(base),str(base/"runtime"),str(base/"vendor/tau2-bench/src")]
    from common import ROOT
    assert ROOT==base and sha(base)==args.release
    if args.kind=="服务":
        from trace_service import replay
        result=replay(args.row,src,folder,args.condition=="故障")
    elif args.row.startswith("ecom-"):
        from grade_replay import ecom
        primary=load(src/"官方评分.json")
        reference=[primary[k] for k in ("score","action","search","output")]
        value=ecom(src,int(args.row.split("-")[1]))
        result=dict(原分=reference,复核分=value,一致=reference==value)
    else:
        from grade_replay import tau
        from official_service import register
        from customer_button import register_button
        register()
        register_button()
        result=tau(src,False)
        result["一致"]=result["原整题分"]==result["复核分"]

    write(folder/"复核结果.json",dict(题号=args.row,入口=args.kind,条件=args.condition,
                                 服务提交=args.release,复核代码=args.checker,结果=result))


def dispatch(args):
    entries=build(args.source)["矩阵"]
    release=sha(args.base)
    pack=args.root/"轨迹复核"
    pack.mkdir(exist_ok=True)
    pending=[]
    for row,cells in entries.items():
        src=original(args.root,row,release)
        if src is None:
            continue
        options=[]
        if args.phase=="服务与电商评分":
            if cells["多会话服务与恢复"]["执行方式"]=="服务轨迹回放":
                options.extend([("服务","正常"),("服务","故障")])
            if row.startswith("ecom-"):
                options.append(("评分","复核"))
        elif row.startswith("tau-"):
            options.append(("评分","复核"))
        for kind,condition in options:
            place=pack/(row+"__"+kind+"__"+condition)
            results=list(place.glob("*/复核结果.json"))
            if results:
                assert len(results)==1 and load(results[0])["服务提交"]==release
            else:
                pending.append((row,kind,condition,src,place))
    if args.dry:
        print("当前可做复核",len(pending))
        return
    from common import load_env
    load_env()
    environment=os.environ.copy()
    environment["OPENAI_API_BASE"]="http://127.0.0.1:18863/v1"
    environment["OPENAI_API_KEY"]=environment["CODEX_BRIDGE_TOKEN"]
    environment["TAU2_DATA_DIR"]=str(args.base/"vendor/tau2-bench/data")
    judge=load(args.base/"configs/customer_button.json")["judge_override"]
    if args.phase=="零售评分":
        environment["RETAILOPS_JUDGE_OVERRIDE"]=json.dumps(judge)
    else:
        environment.pop("RETAILOPS_JUDGE_OVERRIDE",None)
    environment["PYTHONPATH"]=os.pathsep.join(map(str,[args.base,args.base/"runtime",args.base/"vendor/tau2-bench/src"]))
    interpreter=str(args.base/".venv/bin/python") if args.phase=="零售评分" else sys.executable
    def one(job):
        row,kind,condition,src,place=job
        folder=place/uuid4().hex
        folder.mkdir(parents=True)
        command=[interpreter,__file__,"单项",str(args.base),str(src),str(folder),row,kind,condition,release,args.checker]
        with (folder/"运行.log").open("w") as log:
            proc=subprocess.run(command,env=environment,stdout=log,stderr=subprocess.STDOUT)
        if proc.returncode:
            write(folder/"复核异常.json",dict(题号=row,入口=kind,条件=condition,返回码=proc.returncode))
        return proc.returncode
    with (pack/(args.phase+"锁")).open("a") as lock:
        flock(lock,LOCK_EX|LOCK_NB)
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            codes=list(pool.map(one,pending))
    print("执行位置",len(pending),"运行异常",sum(c!=0 for c in codes),flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    phases=parser.add_subparsers(dest="phase",required=True)
    one=phases.add_parser("单项")
    for name in ("base","src","folder"):
        one.add_argument(name,type=lambda p:Path(p).resolve())
    for name in ("row","kind","condition","release","checker"):
        one.add_argument(name)
    for phase in ("服务与电商评分","零售评分"):
        phase_parser=phases.add_parser(phase)
        for name in ("source","root","base"):
            phase_parser.add_argument(name,type=lambda p:Path(p).resolve())
        phase_parser.add_argument("checker")
        phase_parser.add_argument("--jobs",type=int,default=2)
        phase_parser.add_argument("--dry",action="store_true")
    args=parser.parse_args()
    child(args) if args.phase=="单项" else dispatch(args)
