"""对缺少内部检索入口的服务回放补正，原结果和复核版本继续保留。"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from module_recheck import original
from public_modules import sha, write


def run(args):
    release=sha(args.base)
    checker=sha(args.runtime)
    candidates=[]
    for p in (args.root/"轨迹复核").glob("*__服务__*/*/复核结果.json"):
        value=json.loads(p.read_bytes())
        if value["服务提交"]!=release:
            raise ValueError("原回放使用了其他冻结版本")
        if all(v["状态"]=="未触发" and v.get("原因")=="没有足以重建已核实用户只读请求的证据" for v in value["结果"].values()):
            candidates.append((p,value))
    for p,value in candidates:
        src=original(args.root,value["题号"],release)
        assert src is not None
        key=p.parent.parent.name
        path=args.root/"服务入口补正"/key
        if list(path.glob("*/复核结果.json")):
            continue
        folder=path/uuid4().hex
        folder.mkdir(parents=True)
        cmd=[sys.executable,str(args.runtime/"scripts/module_recheck.py"),"单项",str(args.base),str(src),str(folder),
             value["题号"],"服务",value["条件"],release,checker]
        with (folder/"运行.log").open("w") as log:
            status=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT).returncode
        if status:
            write(folder/"复核异常.json",dict(返回码=status,原位置=key))
            raise RuntimeError("检索服务补正未取得结果")
        result=json.loads((folder/"复核结果.json").read_bytes())
        assert result["服务提交"]==release and result["复核代码"]==checker
        write(folder/"补正说明.json",dict(原位置=key,原复核代码=value["复核代码"],原状态="未触发",
                    原因="回放提取器遗漏内部检索入口",新的复核代码=checker))
        print(value["题号"],value["条件"],{k:v["状态"] for k,v in result["结果"].items()},flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for k in ("root","base","runtime"):
        parser.add_argument(k,type=lambda p:Path(p).resolve())
    run(parser.parse_args())
