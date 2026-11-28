"""按冻结次序运行短回放及两题整题配对，已出分位置可安全跳过。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import nullcontext

from wiki_trials import FULL, SHORT
from wiki_experiment import ANSWER, FULL_LIMIT, SHORT_LIMIT, bridge, read_json, setup, sha, write_json


def main(stage,run,batch):
    here = Path(__file__).resolve().parent
    todo = FULL if stage=="full" else SHORT if stage=="short" else [(i,"关闭") for i in (32,23,40,50)]
    if stage=="full":
        for i,m in SHORT:
            r=read_json(run/"short"/f"{i}-{m}"/"结果.json")
            if not r["确认前账本相同"]:
                raise RuntimeError("短试出现未授权写入")
    if stage!="fixture":
        version = read_json(run/"建库完成.json")["版本"]
        if sha((run/"wiki/manifest.json").read_bytes())!=version:
            raise ValueError("Wiki 版本变化")
    else:
        version=None
    manifest=dict(位置=todo,库版本=version,上下文="C",建库来源=[8,21,27],
        配置=FULL_LIMIT if stage=="full" else SHORT_LIMIT,模型=ANSWER+"@medium",
        源码={p.name:sha(p.read_bytes()) for p in [Path(__file__),here/"wiki_trials.py",
            here/"wiki_experiment.py",here/"ecom_paired.py",here.parent/"retailops/agent.py"]})
    frozen = run/(stage+"-冻结.json")
    normalized = json.loads(json.dumps(manifest))
    if frozen.exists() and read_json(frozen)!=normalized:
        raise ValueError("已冻结配置变化")
    write_json(frozen,manifest)
    with nullcontext() if stage=="fixture" else bridge(run,ANSWER,18872):
        for case,mode in todo:
            folder=run/stage/f"{case}-{mode}"
            if (folder/"结果.json").exists():
                continue
            subprocess.run([sys.executable,str(here/"wiki_trials.py"),stage,str(run),str(batch),
                str(case),mode],check=True)
            if stage=="short":
                result=read_json(folder/"结果.json")
                if result["前缀"]!=read_json(run/"fixture"/f"{case}-关闭"/"结果.json")["前缀"]:
                    raise ValueError("实际短回放起点偏离冻结前缀")
                if result["调用读页"] and not result["正文送达"] and result["状态"]=="已回复":
                    raise ValueError("客服读页后未收到正文")
                if not result["确认前账本相同"] or (mode=="关闭" and result["目录送达"]) or (
                    mode!="关闭" and not result["目录送达"]) or (mode=="直给" and not result["正文送达"]):
                    raise ValueError("短回放接线未通过")


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("stage",choices=["fixture","short","full"])
    parser.add_argument("run",type=Path)
    parser.add_argument("batch",type=Path)
    args=parser.parse_args()
    setup()
    main(args.stage,args.run.resolve(),args.batch.resolve())
