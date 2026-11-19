"""统计已留存官方分和整理进度，不读取或输出原始顾客对话。"""
import argparse
from collections import Counter
from pathlib import Path
import json

def status(root):
    root=Path(root)
    config=json.loads((root/"配置.json").read_text()) if (root/"配置.json").exists() else {}
    scores=[]
    for path in root.glob("正式/*/*/*/*/官方评分.json"):
        row=json.loads(path.read_text())
        scores.append(row)
    counts=Counter((x["轮次"],x["条件"]) for x in scores)
    totals=Counter()
    for row in scores:
        for key in ("score","action","search","output"):
            totals[(row["轮次"],row["条件"],key)]+=row[key]
    progress=json.loads((root/"进度.json").read_text()) if (root/"进度.json").exists() else {}
    phases=[]
    for r in (1,2,3):
        for arm in ("关闭","持续"):
            phases.append(dict(轮次=r,条件=arm,已评分=counts[(r,arm)],
                  分量={k:totals[(r,arm,k)] for k in ("score","action","search","output")}))
    dream_done=list(root.glob("整理/*/*/*/完成.json"))
    failures=list(root.glob("整理/*/*/*/异常.json"))
    return dict(提交=config.get("提交"),已评分=len(scores),目标=198,已完成整理=len(dream_done),整理目标=99,
                正式全部完成=(root/"完成.json").exists(),当前=progress,各轮=phases,整理异常次数=len(failures),
                无分或核验异常次数=len(list(root.glob("正式/*/*/*/*/首次或后续异常.json"))))

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("root",type=Path)
    a=p.parse_args()
    print(json.dumps(status(a.root),ensure_ascii=False,indent=2))
