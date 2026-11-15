"""按已评分按钮试次的版本，把原工具名称与同版实际文本回执逐项回放。"""
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from ecom_paired import validate_write_result

counts=Counter()
for score in Path("runs/ecom-v1/results").glob("*-button/score.json"):
    # 异常尝试可能有残留账本，只取已产生最终轨迹、对应评分的尝试。
    paths=list(score.parent.glob("*/trajectory.json"))
    if len(paths)!=1:
        raise ValueError(f"{score.parent.name} 的最终轨迹需人工核对")
    with sqlite3.connect(f"file:{paths[0].parent}/business.db?mode=ro",uri=True) as db:
        sid,history=db.execute("SELECT sid,history FROM sessions").fetchone()
        plans={}
        for msg in json.loads(history):
            if msg["role"]=="assistant":
                match=re.fullmatch(r"业务工具已完成第(\d+)版方案：(.*)",msg.get("content") or "",flags=re.S)
                if match:
                    plans[int(match[1])]=json.loads(match[2])
        for version,body in db.execute("SELECT version,body FROM receipts WHERE sid=?",(sid,)):
            for action,result in zip(plans[version],json.loads(body)["results"],strict=True):
                validate_write_result(action["tool"],result)
                counts[action["tool"]]+=1
print("按原工具名称通过的历史回执",dict(counts),"合计",sum(counts.values()))
