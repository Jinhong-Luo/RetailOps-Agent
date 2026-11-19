"""固定合成旁例与记忆快照，每个条件只跑一次，不挑选成功轨迹。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import sys
from common import ROOT,read_json,write_json
from official_memory_development import CASES
from retailops.official_memory import snapshot,validate_wiki

def run(source,root):
    root.mkdir(parents=True,exist_ok=False)
    before=validate_wiki(source)
    bank=root/"冻结开发库"
    shutil.copytree(source,bank)
    tasks=[(case,arm) for case in CASES for arm in ("关闭","持续")]
    write_json(root/"清单.json",dict(位置=len(tasks),初始库摘要=before["digest"],任务=[dict(案例=c,条件=a) for c,a in tasks],
        提交=subprocess.check_output(["git","rev-parse","HEAD"],text=True,cwd=ROOT).strip(),
        用途="开发验证；经验不进入正式实验"))
    def one(pair):
        case,arm=pair
        folder=root/(case+"-"+arm)
        with (root/(case+"-"+arm+".log")).open("w") as log:
            p=subprocess.run([sys.executable,str(ROOT/"scripts/official_memory_development.py"),
                  case,arm,str(bank),str(folder)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        if not (folder/"结果.json").exists():
            return dict(案例=case,条件=arm,通过=False,异常="未生成结果",返回码=p.returncode)
        return read_json(folder/"结果.json")
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(one,tasks))
    hashes={case:{x.get("初始账本摘要") for x in rows if x["案例"]==case} for case in CASES}
    paired=all(len(v)==1 and None not in v for v in hashes.values())
    unchanged=snapshot(bank)["digest"]==before["digest"]
    reading=any(x.get("记忆读取次数",0)>0 for x in rows if x["条件"]=="持续")
    result=dict(结果=rows,起始事实一致=paired,冻结库未变=unchanged,开启组存在真实读取=reading,
                通过=paired and unchanged and reading and all(x["通过"] for x in rows))
    write_json(root/"汇总.json",result)
    print("开发位置完成：",sum(x["通过"] for x in rows),"/",len(rows),flush=True)
    return result

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path)
    parser.add_argument("root",type=Path)
    args=parser.parse_args()
    run(args.source.resolve(),args.root.resolve())
