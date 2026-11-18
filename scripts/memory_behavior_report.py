"""从只读轨迹提取可公开的行为计数和经验来源，不输出对话与记忆正文。"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
from common import read_json,write_json
from retailops.continual_memory import digest

def summarize_events(events,bank,origins,task,round_id):
    entries={e["id"]:e for e in bank["entries"]}
    hits=Counter(h for e in events if e["stage"]=="跨会话参考" for h in e["hits"])
    if any(h not in entries for h in hits):
        raise ValueError("实际命中不在题前快照中")
    sources=[]
    for h,count in sorted(hits.items()):
        used=[origins[x["source"]] for x in entries[h]["sources"]]
        if any(r>round_id or (r==round_id and t>=task) for r,t in used):
            raise ValueError("命中经验含尚未结束的题目")
        sources.append(dict(记忆编号=h,注入次数=count,来源=[dict(轮次=r,题号=t) for r,t in used],
                            包含本题前轮经验=any(t==task and r<round_id for r,t in used)))
    return dict(查询工具=dict(Counter(e["tool"] for e in events if e["stage"]=="查询工具")),
        办理环节=dict(Counter(e["stage"] for e in events if e["stage"] in
                    ("执行前检查","等待确认","用户确认","业务工具执行","完成"))),
        经验来源=sources,读取本题前轮经验=any(s["包含本题前轮经验"] for s in sources))

def audit(root,out):
    if not (root/"完成.json").exists():
        raise ValueError("全部正式位置尚未完成")
    snapshots={digest(b:=read_json(root/"初始空记忆.json")):b}
    for p in root.glob("整理/*/*/*/记忆.json"):
        b=read_json(p)
        snapshots[digest(b)]=b
    paths=list(root.glob("正式/*/*/*/*/官方评分.json"))
    origins={digest(s:=read_json(p)):(s["轮次"],s["题号"]) for p in paths if read_json(p)["条件"]=="持续"}
    result=[]
    for p in paths:
        s=read_json(p)
        with sqlite3.connect((p.parent/"business.db").resolve().as_uri()+"?mode=ro",uri=True) as db:
            events=[json.loads(x[0]) for x in db.execute("select body from events")]
        value=summarize_events(events,snapshots[s["记忆摘要"]],origins,s["题号"],s["轮次"])
        result.append(dict(轮次=s["轮次"],题号=s["题号"],条件=s["条件"],score=s["score"],
            turns=s["turns"],clicks=s["clicks"],**value))
    result.sort(key=lambda x:(x["轮次"],x["题号"],x["条件"]))
    output=dict(逐位置=result,限制="命中表示文本已注入，不等于模型采用；同题来源不等于正确经验；办理计数不替代账本与官方评分。")
    write_json(out,output)
    print(json.dumps(dict(位置数=len(result),逐轮同题经验命中=[
        dict(轮次=r,题数=sum(x["读取本题前轮经验"] for x in result if x["条件"]=="持续" and x["轮次"]==r))
        for r in (1,2,3)]),ensure_ascii=False))

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("root",type=Path)
    p.add_argument("out",type=Path)
    args=p.parse_args()
    audit(args.root,args.out)
