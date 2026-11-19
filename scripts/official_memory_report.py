"""核验完整198个评分及99次文件记忆来源链，脱敏输出三轮对照和用量。"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
from common import read_json,write_json
from memory_round_report import metrics,usage
from retailops.continual_memory import digest
from retailops.official_memory import snapshot,validate_wiki,TRANSCRIPTS

KEYS=("score","action","search","output")

def collect(root,config):
    rows=[];paths={}
    for p in root.glob("正式/*/*/*/*/官方评分.json"):
        row=read_json(p);key=(row["轮次"],row["题号"],row["条件"])
        if key in paths or row["提交"]!=config["提交"] or not row["模型强度匹配"] or not row.get("核验完成"):
            raise ValueError("评分重复、未核验或冻结条件不符")
        if any(row[k] not in (0,1) for k in KEYS):
            raise ValueError("官方分量不合法")
        paths[key]=p.parent;rows.append(row)
    expected={(r,t,a) for r in (1,2,3) for t in config["正式"] for a in ("关闭","持续")}
    if set(paths)!=expected:
        raise ValueError("存在缺分或清单外位置，不能给出完整结论")
    return rows,paths

def audit_chain(root,config,paths):
    initial=root/"初始空库"
    if set(snapshot(initial)["files"])!={"_index.md"} or (initial/"_index.md").read_text()!="# 经验索引\n":
        raise ValueError("正式初始记忆不为空")
    before=snapshot(initial)["digest"];chain=[]
    for r in (1,2,3):
        for task in config["正式"]:
            for arm in ("关闭","持续"):
                row=read_json(paths[(r,task,arm)]/"官方评分.json")
                if row["记忆摘要"]!=before or arm=="关闭" and row["记忆读取次数"]:
                    raise ValueError("两条件题前快照不同或关闭条件污染")
            receipts=list((root/"整理"/str(r)/str(task)).glob("*/完成.json"))
            if len(receipts)!=1:
                raise ValueError("整理完成记录缺失或重复")
            folder=receipts[0].parent;receipt=read_json(receipts[0])
            after=validate_wiki(folder/"store")
            source=digest(read_json(paths[(r,task,"持续")]/"官方评分.json"))
            if (receipt["输入摘要"],receipt["输出摘要"],receipt["来源摘要"],receipt["来源提交"])!=(before,after["digest"],source,config["提交"]):
                raise ValueError("记忆来源链或冻结提交不一致")
            if receipt["官方上游"]!=config["官方示例"]["提交"] or receipt["文件摘要"]!=after["files"]:
                raise ValueError("官方版本或记忆文件指纹不一致")
            flow=receipt["流程"]
            if flow["inputs_processed"]!=1 or flow["inputs_expected"]!=1 or flow["failures"] or not flow["organized"]:
                raise ValueError("整理流程未完整通过")
            audit=read_json(folder/"工具核验.json")
            if any(x["未读回"] for x in audit):
                raise ValueError("存在未读回写入")
            transcript=folder/"transcripts"/(source+".txt")
            target=TRANSCRIPTS+"/"+transcript.name
            lines=transcript.read_text().splitlines()
            ranges=set()
            for role in audit:
                if role["角色"]=="分析":
                    for x in role["工具"]:
                        if x["tool"]=="read" and x["path"]==target:
                            ranges.update(range(x["offset"],x["offset"]+x["lines"]))
            if not set(range(len(lines))).issubset(ranges):
                raise ValueError("轨迹并未完整阅读")
            counts=Counter(x["tool"] for role in audit for x in role["工具"])
            chain.append(dict(轮次=r,题号=task,输入摘要=before,输出摘要=after["digest"],来源摘要=source,
                条目数=after["index_entries"],退役数=after["retired"],工具次数=dict(counts),
                整理变更说明数量=len(flow["changes"]),全部输入已读=True,全部写入已读回=True,
                用时秒=receipt["用时秒"]))
            before=after["digest"]
    if read_json(root/"完成.json")["最终记忆摘要"]!=before:
        raise ValueError("最终记忆与完成标记不符")
    return chain

def background_meter(root):
    total=Counter()
    for p in root.glob("整理/*/*/*/模型记录/用量.jsonl"):
        for line in p.read_text().splitlines():
            row=json.loads(line)
            if (row["模型"],row["强度"])!=("gpt-6.1-sol","medium"):
                raise ValueError("后台模型或强度不符")
            total["返回次数"]+=1
            for k in ("prompt_tokens","completion_tokens","total_tokens"):
                total[k]+=int((row.get("用量") or {}).get(k) or 0)
    total["已记录异常调用"]=len(list(root.glob("整理/*/*/*/模型记录/异常-*.json")))
    return dict(total)

def report(root,out):
    config=read_json(root/"配置.json");complete=read_json(root/"完成.json")
    if complete["提交"]!=config["提交"] or complete["正式位置"]!=198 or len(config["正式"])!=33:
        raise ValueError("三轮正式实验尚未完成")
    rows,paths=collect(root,config)
    if len(rows)!=198:
        raise ValueError("正式位置不是198个")
    chain=audit_chain(root,config,paths)
    totals,switches=metrics([dict(s,注入轮数=s["记忆读取请求数"]) for s in rows],config["正式"])
    for item in totals:
        item["调用记忆工具题数"]=item.pop("发生经验注入的题数")
    fingers=[];observations=[];first={}
    for key,folder in paths.items():
        row=read_json(folder/"官方评分.json")
        session=read_json(folder/"trajectory.json")["session"]
        first[key]=next((x.get("content") for x in session if x["role"]=="user"),None)
        with sqlite3.connect((folder/"business.db").resolve().as_uri()+"?mode=ro",uri=True) as db:
            events=[json.loads(x[0]) for x in db.execute("select body from events")]
        refs=[e for e in events if e["stage"]=="官方记忆读取"]
        if len(refs)!=row["记忆读取次数"] or any(e["version"]!=row["记忆摘要"] or e["step"]>8 for e in refs):
            raise ValueError("实际读取与结果回执不符")
        observations.append(dict(轮次=key[0],题号=key[1],条件=key[2],记忆工具调用次数=len(refs),
            索引读取次数=sum(e["tool"]=="memory_read" and str(e.get("path","")).endswith("/_index.md") for e in refs),
            正文读取次数=sum(e["tool"]=="memory_read" and not str(e.get("path","")).endswith("/_index.md") for e in refs),
            目录浏览次数=sum(e["tool"]=="memory_glob" for e in refs),
            片段检索次数=sum(e["tool"]=="memory_grep" for e in refs),
            记忆调用被拦截次数=sum(e["stage"]=="工具被拦截" and e.get("tool") in {"memory_read","memory_glob","memory_grep"} for e in events),
            草案拦截次数=sum(e["stage"]=="工具被拦截" and e.get("tool")=="propose_plan" for e in events)))
        for name in ("官方评分.json","trajectory.json","business.db","usage.jsonl","客服实际调用.jsonl","业务契约.json"):
            p=folder/name
            fingers.append(dict(轮次=key[0],题号=key[1],条件=key[2],文件=name,摘要=hashlib.sha256(p.read_bytes()).hexdigest()))
    paired=[]
    for task in config["正式"]:
        paired.append(dict(题号=task,**{a:[[read_json(paths[(r,task,a)]/"官方评分.json")[k] for k in KEYS] for r in (1,2,3)] for a in ("关闭","持续")}))
    compared=[dict(轮次=r,开启改善=[x["题号"] for x in paired if x["关闭"][r-1][0]<x["持续"][r-1][0]],
        开启退化=[x["题号"] for x in paired if x["关闭"][r-1][0]>x["持续"][r-1][0]],
        首条顾客话术相同题数=sum(first[(r,t,"关闭")]==first[(r,t,"持续")] for t in config["正式"])) for r in (1,2,3)]
    unscored=[p for p in root.glob("正式/*/*/*/*") if p.is_dir() and not (p/"官方评分.json").exists()]
    meter={a:usage([p/"usage.jsonl" for key,p in paths.items() if key[2]==a]) for a in ("关闭","持续")}
    meter.update(后台整理=background_meter(root),预检=usage(list(root.glob("预检/*/*/*/*/usage.jsonl"))),
        无分尝试已返用量=usage([p/"usage.jsonl" for p in unscored if (p/"usage.jsonl").exists()]))
    value=dict(配置=config,逐轮=totals,逐题四项=paired,四项逐位置=rows,同轮开关差异=compared,
        轮间变化=switches,读取与草案观测=observations,计量=meter,无分尝试数=len(unscored),
        未完成整理尝试数=sum(not (p/"完成.json").exists() for p in root.glob("整理/*/*/*") if p.is_dir()),
        记忆来源链=chain,文件指纹=fingers,
        限制="原始对话与笔记不公开；读取不等于采用，完整阅读与结构核验不证明笔记语义正确；种子不保证确定性，顾客话术独立；重复33题不能当作99道独立题；token不等于实际费用，异常未返用量不填零。")
    out.mkdir(parents=True,exist_ok=True)
    write_json(out/"三轮逐题结果.json",value)
    text=["# 官方文件记忆三轮结果","",f"冻结提交：{config['提交']}。198个正式位置与99次完整整理均已核验，官方零分保留。","",
          "第一轮从空经验库逐题迁移，第二与第三轮允许此前同题经验积累。四项依次为整题、终态、指定查询、输出。",
          "","|轮次|条件|整题|终态|指定查询|输出|累计曾成功|调用记忆工具题数|","|---|---|---|---|---|---|---|---|"]
    for s in sorted(totals,key=lambda x:(x["轮次"],x["条件"])):
        text.append("|"+str(s["轮次"])+"|"+s["条件"]+"|"+"|".join(str(int(v))+"/33" for v in s["四项"])+f"|{s['累计曾成功']}/33|{s['调用记忆工具题数']}/33|")
    text+=["","|题号|关闭第1轮|记忆第1轮|关闭第2轮|记忆第2轮|关闭第3轮|记忆第3轮|","|---|---|---|---|---|---|---|"]
    for row in paired:
        text.append("|"+str(row["题号"])+"|"+"|".join("/".join(str(int(v)) for v in row[a][r-1]) for r in (1,2,3) for a in ("关闭","持续"))+"|")
    for row in compared:
        text+=["",f"第{row['轮次']}轮，开启组改善题号：{row['开启改善']}；退化题号：{row['开启退化']}。首条顾客话术相同：{row['首条顾客话术相同题数']}/33。"]
    text+=["",value["限制"],"","完整计量、逐位置结果和99次来源链见[三轮逐题结果.json](三轮逐题结果.json)。"]
    (out/"三轮逐题结果.md").write_text("\n".join(text)+"\n",encoding="utf8")
    print(json.dumps(dict(逐轮=totals,同轮开关差异=compared,计量=meter),ensure_ascii=False))

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root",type=Path);parser.add_argument("out",type=Path)
    args=parser.parse_args();report(args.root,args.out)
