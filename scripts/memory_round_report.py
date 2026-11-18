"""核验全部位置和记忆来源链，分别报告当轮成功率与累计覆盖。"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from common import read_json,write_json
from retailops.continual_memory import digest,validate_bank

def metrics(rows,ids):
    output=[]
    switches=[]
    for arm in ("关闭","持续"):
        ever=set()
        for r in (1,2,3):
            current={s["题号"]:s for s in rows if s["轮次"]==r and s["条件"]==arm}
            if set(current)!=set(ids) or len([s for s in rows if s["轮次"]==r and s["条件"]==arm])!=len(ids):
                raise ValueError("轮次缺分或重复")
            passed={i for i,s in current.items() if s["score"]==1}
            ever|=passed
            output.append(dict(条件=arm,轮次=r,分母=len(ids),当轮成功=len(passed),累计曾成功=len(ever),
                四项=[sum(s[k] for s in current.values()) for k in ("score","action","search","output")],
                发生经验注入的题数=sum(s.get("注入轮数",0)>0 for s in current.values())))
            if r>1:
                before={s["题号"]:s["score"] for s in rows if s["轮次"]==r-1 and s["条件"]==arm}
                switches.append(dict(条件=arm,之前轮次=r-1,之后轮次=r,
                    改善=[i for i in ids if before[i]==0 and current[i]["score"]==1],
                    退化=[i for i in ids if before[i]==1 and current[i]["score"]==0]))
    return output,switches

def usage(paths):
    total=Counter()
    bad=0
    for p in paths:
        for line in p.read_text().splitlines():
            try: row=json.loads(line)
            except json.JSONDecodeError:
                bad+=1
                continue
            total["成功响应"]+=1
            for k in ("prompt_tokens","completion_tokens","total_tokens"):
                total[k]+=int((row.get("usage") or {}).get(k) or 0)
    return dict(**total,不可解析行数=bad)

def report(root,out):
    config=read_json(root/"配置.json")
    complete=read_json(root/"完成.json")
    if complete["提交"]!=config["提交"] or complete["正式位置"]!=198:
        raise ValueError("运行未完整完成")
    ids=config["正式"]
    rows=[]
    paths={}
    fingerprints=[]
    for p in root.glob("正式/*/*/*/*/官方评分.json"):
        s=read_json(p)
        key=(s["轮次"],s["题号"],s["条件"])
        if key in paths or s["提交"]!=config["提交"] or not s["模型强度匹配"]:
            raise ValueError("位置重复或配置不匹配")
        paths[key]=p.parent
        rows.append(s)
        for name in ("官方评分.json","trajectory.json","business.db","usage.jsonl"):
            file=p.parent/name
            fingerprints.append(dict(轮次=s["轮次"],题号=s["题号"],条件=s["条件"],文件=name,
                摘要=hashlib.sha256(file.read_bytes()).hexdigest()))
    if len(rows)!=198:
        raise ValueError("不允许对部分结果给出完整结论")
    totals,switches=metrics(rows,ids)
    bank=validate_bank(read_json(root/"初始空记忆.json"))
    if bank["entries"] or bank["processed"]:
        raise ValueError("不是空库起步")
    chain=[]
    for r in (1,2,3):
        for task in ids:
            for arm in ("关闭","持续"):
                score=read_json(paths[(r,task,arm)]/"官方评分.json")
                if score["记忆摘要"]!=digest(bank):
                    raise ValueError("题前经验不一致")
                if arm=="关闭" and score["注入轮数"]:
                    raise ValueError("关闭条件污染")
            results=list((root/"整理"/str(r)/str(task)).glob("*/更新.json"))
            if len(results)!=1:
                raise ValueError("整理记录缺失或重复")
            u=read_json(results[0])
            after=validate_bank(read_json(results[0].parent/"记忆.json"))
            source=digest(read_json(paths[(r,task,"持续")]/"官方评分.json"))
            if (u["输入记忆摘要"],u["输出记忆摘要"],u["轨迹摘要"])!=(digest(bank),digest(after),source):
                raise ValueError("记忆来源链不一致")
            if after["parent"]!=digest(bank) or after["processed"]!=bank["processed"]+[source] or after["revision"]!=bank["revision"]+1:
                raise ValueError("未来经验或重复处理混入")
            if any(origin["source"] not in after["processed"] for entry in after["entries"] for origin in entry["sources"]):
                raise ValueError("经验来源不属于已结束轨迹")
            chain.append(dict(轮次=r,题号=task,版本=u["版本"],条目数=u["条目数"],变更=u["变更"],
                提炼拒绝=u["格式或字段拒绝"],输入摘要=u["输入记忆摘要"],输出摘要=u["输出记忆摘要"],
                用量=u["用量"],用时秒=u.get("用时秒")))
            bank=after
    if digest(bank)!=complete["最终记忆摘要"]:
        raise ValueError("最终经验与完成标记不同")
    paired=[]
    for task in ids:
        item=dict(题号=task)
        for arm in ("关闭","持续"):
            item[arm]=[read_json(paths[(r,task,arm)]/"官方评分.json")["score"] for r in (1,2,3)]
        paired.append(item)
    compared=[]
    for r in (1,2,3):
        compared.append(dict(轮次=r,开启改善=[x["题号"] for x in paired if x["关闭"][r-1]<x["持续"][r-1]],
                            开启退化=[x["题号"] for x in paired if x["关闭"][r-1]>x["持续"][r-1]]))
    meter={}
    for arm in ("关闭","持续"):
        meter[arm]=usage([p/"usage.jsonl" for key,p in paths.items() if key[2]==arm])
    meter["整理"]=dict(响应数=len(chain),total_tokens=sum((u["用量"] or {}).get("total_tokens",0) for u in chain),
        用时秒=sum(u.get("用时秒") or 0 for u in chain))
    missing_attempts=[p for p in root.glob("正式/*/*/*/*") if p.is_dir() and not (p/"官方评分.json").exists()]
    meter["无分异常尝试"]=usage([p/"usage.jsonl" for p in missing_attempts if (p/"usage.jsonl").exists()])
    result=dict(配置=config,逐轮=totals,逐题=paired,轮间变化=switches,同轮开关差异=compared,
        四项逐位置=rows,计量=meter,无分尝试数=len(missing_attempts),记忆来源链=chain,文件指纹=fingerprints)
    out.mkdir(parents=True,exist_ok=True)
    write_json(out/"三轮逐题结果.json",result)
    lines=["# 持续记忆三轮结果","",f'冻结提交：`{config["提交"]}`。每轮两组各三十三题；全部位置完成，官方零分保留。',
        "","第一轮为相对空库的跨题迁移，第二、三轮允许同题的前轮经验。累计曾成功仅作辅助指标。",
        "","| 轮次 | 条件 | 当轮整题成功 | 终态 | 指定查询 | 输出 | 累计曾成功 |",
        "|---|---|---|---|---|---|---|"]
    for s in sorted(totals,key=lambda s:(s["轮次"],s["条件"])):
        lines.append(f'| {s["轮次"]} | {s["条件"]} | {s["当轮成功"]}/33 | {s["四项"][1]}/33 | {s["四项"][2]}/33 | {s["四项"][3]}/33 | {s["累计曾成功"]}/33 |')
    lines+=["","| 题号 | 关闭记忆三轮 | 持续记忆三轮 |","|---|---|---|"]
    for s in paired:
        lines.append(f'| {s["题号"]} | {"/".join(map(str,s["关闭"]))} | {"/".join(map(str,s["持续"]))} |')
    lines+=["","同一个题序、三轮样本不能保证稳定提升；模型请求的种子不保证确定性。记忆内容跨轮变化，不把连续成功比例称为独立同分布的 pass^k。",
        "","原始对话、账本和记忆正文留服务器，公开结果保存来源摘要与变更计数。提炼和业务推理分别计量，token 不等于实际费用。"]
    (out/"三轮逐题结果.md").write_text("\n".join(lines)+"\n",encoding="utf8")
    print(json.dumps(dict(逐轮=totals,同轮开关差异=compared,计量=meter),ensure_ascii=False))

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("root",type=Path)
    p.add_argument("out",type=Path)
    args=p.parse_args()
    report(args.root,args.out)
