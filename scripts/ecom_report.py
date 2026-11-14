"""逐题比较三组 ECom-Bench 成绩，仅导出匿名统计。"""
import argparse
import json
from collections import Counter
from pathlib import Path


def compare(run):
    plan=json.loads((run/"manifest.json").read_bytes())
    rows=[]
    pairs={}
    for task in plan["formal"]:
        for trial in range(plan["trials"]):
            scores={}
            for arm in plan["arms"]:
                path=run/"results"/f"{task}-{trial}-{arm}"/"score.json"
                result=json.loads(path.read_bytes()) if path.exists() else {}
                if result and (result["task"],result["trial"],result["arm"])!=(task,trial,arm):
                    raise ValueError("试次标识不一致")
                scores[arm]=result.get("score")
            rows.append(dict(task=task,trial=trial,**scores))
    for reference in ("old","text"):
        counts=Counter()
        for row in rows:
            a,b=row[reference],row["button"]
            if a is None or b is None:
                counts["缺失"]+=1
            else:
                counts[f"{int(a)}→{int(b)}"]+=1
        pairs[reference]=dict(counts)
    return dict(plan=plan,planned_per_arm=len(rows),pairs=pairs,rows=rows)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run",type=Path)
    parser.add_argument("output",type=Path)
    args=parser.parse_args()
    report=compare(args.run)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
