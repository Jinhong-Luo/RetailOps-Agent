"""从 6.2 实验原始回包与账本核对真实档位、费用、经验送达和整题授权。"""
import json
from pathlib import Path
import sqlite3
import sys

from same_case_audit import behavior


def audit(run):
    jobs=json.loads((run/"排期.json").read_text())
    totals={}
    result={}
    for stage in ("总结","短试","整题"):
        folders=(sorted(run.glob("总结-*-*")) if stage=="总结" else
                 [run/stage/(scene+"-"+arm) for scene,arm,kind in jobs if kind==stage])
        calls={}
        for folder in folders:
            rows=[json.loads(s) for s in (folder/"请求.jsonl").read_text().splitlines()]
            expected=("gpt-6.2-astra","low") if stage=="总结" else ("gpt-6.2-sol","medium")
            assert rows and all((r["模型"],r["档位"])==expected for r in rows)
            for r in rows:
                assert r["序"] not in calls
                calls[r["序"]]=r["用量"]["total_tokens"]
            if stage=="总结":
                item={"笔记":json.loads((folder/"结构.json").read_text())}
            else:
                item=json.loads((folder/"结果.json").read_text())
                assert item["用量"]==sum(r["用量"]["total_tokens"] for r in rows)
                if stage=="整题":
                    item.update(behavior(folder,False))
                assert all(not r["正文"] for r in rows if r["角色"]=="顾客")
                item["按角色用量"]={role:sum(r["用量"]["total_tokens"] for r in rows if r["角色"]==role)
                                    for role in sorted({r["角色"] for r in rows})}
            result[folder.name]=item
        with sqlite3.connect((run/(stage+".db")).as_uri()+"?mode=ro",uri=True) as db:
            paid=dict(db.execute("select id,used from calls where used is not null"))
            assert calls==paid
            pending,=db.execute("select coalesce(sum(reserved),0) from calls where used is null").fetchone()
        totals[stage]=dict(已返回请求=len(calls),用量=sum(calls.values()),未结预留=pending)
    for s in {scene for scene,_,stage in jobs if stage=="短试"}:
        assert len({result[scene+"-"+arm]["前缀"] for scene,arm,k in jobs if k=="短试" and scene==s})==1
    return dict(台账=totals,位置=result)


if __name__=="__main__":
    print(json.dumps(audit(Path(sys.argv[1]).resolve()),ensure_ascii=False,indent=2))
