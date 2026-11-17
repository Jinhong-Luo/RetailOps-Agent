"""复核历史归档指纹，分别汇总两个公开基准、各模块配对和共享工程结果。"""
import argparse
from collections import Counter
from hashlib import sha256
from pathlib import Path
import json

from module_manifest import BUSINESS, build
from module_matrix import read, save


def validate_archive(archive, source):
    fingerprint=read(source/"冻结清单.json")
    if read(archive/"清单.json")!=fingerprint:
        raise ValueError("历史参照的任务或指纹清单与封存方案不同")
    for name,expected in fingerprint["sha256"].items():
        if sha256((archive/name).read_bytes()).hexdigest()!=expected:
            raise ValueError("历史归档字节与封存摘要不符："+name)
    return {f"{suite}-{case}-{arm}" for suite,case,arm in fingerprint["jobs"]}


def historical(archive, keys, row, arm):
    key=f"{row}-{arm}"
    if key not in keys:
        raise ValueError("历史参照清单缺少对应位置")
    records=[read(path) for sub in ("结果","补跑") if (path:=archive/sub/key/"result.json").is_file()]
    valid=[r for r in records if r.get("score") is not None]
    if len(valid)!=1:
        raise ValueError("历史参照没有唯一官方评分："+key)
    return valid[0]


def group(row):
    if row.startswith("tau-"):
        return "τ-bench 中文零售"
    if row.startswith("ecom-"):
        return "ECom-Bench 中文文本"
    return "共享中文业务场景"


def differences(paired):
    return {name:[row for row,a,b in paired if cond(a,b)] for name,cond in (
        ("改善",lambda a,b:a>b),("退化",lambda a,b:a<b),("持平",lambda a,b:a==b))}


def totals(rows):
    count=Counter()
    for v in rows:
        count.update({k:v["计量"][k] for k in ("已记录响应","有用量响应","输入令牌","输出令牌") if v["计量"].get(k) is not None})
    return dict(位置=len(rows),用量完全缺失位置=sum(v["计量"].get("用量完全缺失",False) for v in rows),整题通过=sum(v["整题分"] for v in rows),**count,
                有位置用时的位置=sum(v.get("位置用时秒") is not None for v in rows),
                位置用时累计秒=round(sum(v.get("位置用时秒") or 0 for v in rows),2),
                有响应计时的位置=sum("响应耗时合计秒" in v["计量"] for v in rows),
                响应耗时累计秒=round(sum(v["计量"].get("响应耗时合计秒",0) for v in rows),2))


def summarize(args):
    manifest=build(args.source)
    business=read(args.business)
    if args.完整 and (len(business)!=632 or set(business)!={k for k,v in manifest["位置"].items() if v["入口"]=="业务对话"}):
        raise ValueError("未齐的业务矩阵不能发布为最终结果")
    if any(v["服务参照"]!=args.release for v in business.values()):
        raise ValueError("存在其他冻结版本")
    keys=validate_archive(args.archive,args.source)
    benchmark={}
    for row in manifest["矩阵"]:
        if row.startswith(("D","S")):
            continue
        old=historical(args.archive,keys,row,"old")
        previous=historical(args.archive,keys,row,"full")
        full=business.get(row+"__业务对话__full")
        benchmark[row]=dict(历史原生=old["score"],历史完整版=previous["score"],冻结完整版=full["整题分"] if full else None)
        if row.startswith("ecom-"):
            names=("action","search","output")
            benchmark[row]["历史原生分量"]=[old[k] for k in names]
            benchmark[row]["历史完整版分量"]=[previous[k] for k in names]
            benchmark[row]["冻结分量"]=list(full["官方分量"].values()) if full else None
    arms,modules,versions={},{},{}
    groups=list(dict.fromkeys(group(row) for row in manifest["矩阵"]))
    for name in groups:
        sample=[r for r in business.values() if group(r["题号"])==name]
        arms[name]={arm:totals([r for r in sample if r["条件"]==arm]) for arm in sorted({r["条件"] for r in sample})}
        modules[name]={}
        for module,off in BUSINESS.items():
            paired=[]
            for row,cells in manifest["矩阵"].items():
                places=cells[module]["位置"]
                if group(row)!=name or len(places)!=2 or not places[0].endswith("__业务对话__full") or not all(p in business for p in places):
                    continue
                ref,control=[business[p] for p in places]
                if control["条件"]!=off:
                    raise ValueError("关闭组配置与矩阵不符")
                paired.append((row,ref["整题分"],control["整题分"]))
            if paired:
                modules[name][module]=dict(配对数=len(paired),完整版通过=sum(a for _,a,_ in paired),
                                           关闭组通过=sum(b for _,_,b in paired),**differences(paired))
        rows={r:d for r,d in benchmark.items() if group(r)==name}
        if rows:
            versions[name]={}
            for other in ("历史原生","历史完整版"):
                paired=[(r,d["冻结完整版"],d[other]) for r,d in rows.items() if d["冻结完整版"] is not None]
                versions[name][other]=dict(冻结得分数=len(paired),历史全套通过=sum(d[other] for d in rows.values()),**differences(paired))
    args.out.mkdir(parents=True,exist_ok=True)
    save(args.out/"基准和消融逐题统计.json",dict(冻结提交=args.release,历史核验摘要数=57,公开逐题=benchmark,历史对照=versions,
            完整版对关闭组=modules,各条件计量=arms,计量范围="各条件仅含取得业务分数的有效轨迹。并行位置用时是各位置用时之和。共享用时只计客服模型响应。"))
    def ids(rows):
        return "、".join(r.split("-",1)[-1] for r in rows) or "无"
    lines=["# 两套公开基准与模块对照","",f"冻结服务 `{args.release}`。历史原生和旧完整版来自同一批归档，214 个原任务位置、57 份代码与配置摘要已和封存清单核对。原生和旧完整版是历史参照，各关闭组与冻结完整版按相同题号、每条件一次比较。",""]
    for name in groups:
        lines+=["## "+name,""]
        if name in versions:
            rows={r:d for r,d in benchmark.items() if group(r)==name}
            full=[v["冻结完整版"] for v in rows.values() if v["冻结完整版"] is not None]
            lines.append(f"历史原生 {int(sum(d['历史原生'] for d in rows.values()))}/{len(rows)}，历史完整版 {int(sum(d['历史完整版'] for d in rows.values()))}/{len(rows)}，冻结完整版 {int(sum(full))}/{len(full)}。")
            for other,data in versions[name].items():
                lines+=["",f"相对{other}，改善题为 {ids(data['改善'])}，退化题为 {ids(data['退化'])}，持平 {len(data['持平'])} 题。"]
            lines+=["","| 题号 | 历史原生 | 历史完整版 | 冻结完整版 |","|---|---:|---:|---:|"]
            for row,d in rows.items():
                score=d["冻结完整版"]
                lines.append(f"| {row} | {int(d['历史原生'])} | {int(d['历史完整版'])} | {int(score) if score is not None else '未取得'} |")
            lines.append("")
        lines+=["| 模块 | 同题配对数 | 完整版通过 | 关闭组通过 | 开启后改善 | 开启后退化 | 持平数 |","|---|---:|---:|---:|---|---|---:|"]
        for module,d in modules[name].items():
            lines.append(f"| {module} | {d['配对数']} | {int(d['完整版通过'])} | {int(d['关闭组通过'])} | {ids(d['改善'])} | {ids(d['退化'])} | {len(d['持平'])} |")
        lines.append("")
    lines+=["上下文列共同切换状态卡、政策与商品检索。草案列保留相同确认按钮，ECom 还比较资金展示与联动预检，τ 的可观测差异集中在执行回执。每题每条件仅运行一次，顾客和客服的生成轨迹都会变化。","",
            "ECom 每题的终态、指定查询、输出和首次分歧见[冻结复盘](../../ecom33/b2cf403/复盘.md)。各模块的实跑、复用及未触发事件见[113 题矩阵](113题八模块矩阵.md)。"]
    (args.out/"公开基准与模块对照.md").write_text("\n".join(lines)+"\n",encoding="utf8")
    print("公开题号",len(benchmark),"逐条件统计",sum(len(t) for t in arms.values()))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for k in ("source","archive","business","out"):
        parser.add_argument(k,type=Path)
    parser.add_argument("release")
    parser.add_argument("--完整",action="store_true")
    summarize(parser.parse_args())
