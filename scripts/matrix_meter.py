"""分别统计有效采集、无分尝试、退款展示盲读和复用轨迹的可核实模型用量。"""
from collections import Counter, defaultdict
import json
from pathlib import Path

from module_evidence import meter, objects, read
from module_matrix import save


def measured_attempt(folder, suite):
    if suite=="τ":
        result=folder/"native/zh-retail/results.json"
        if not result.exists():
            return None
        sims=read(result)["simulations"]
        if len(sims)!=1:
            raise ValueError("单一位置落盘了多条轨迹")
        rows=[m["usage"] for m in sims[0]["messages"] if m.get("usage")]
        return dict(**meter(rows),位置耗时秒=sims[0].get("duration"))
    if suite=="ECom":
        path=folder/"usage.jsonl"
        return meter([r.get("usage") for r in objects(path)]) if path.is_file() else None
    path=folder/"模型输入.jsonl"
    if not path.exists():
        return None
    responses=folder/"模型回执.jsonl"
    rows=objects(responses) if responses.is_file() else []
    return dict(**meter([r["用量"] for r in rows]),已记录请求=len(objects(path)),
                响应耗时合计秒=round(sum(r["秒"] for r in rows),2))


def grouped(rows):
    keys=("已记录响应","有用量响应","输入令牌","输出令牌","已记录请求","位置耗时秒","响应耗时合计秒")
    result={}
    for name,data in rows.items():
        result[name]=dict(位置或尝试数=len(data),完全缺少计量的位置=sum(v is None for v in data))
        for k in keys:
            values=[v[k] for v in data if v and v.get(k) is not None]
            if values:
                result[name][k]=round(sum(values),2)
        if "输入令牌" in result[name]:
            result[name]["已记录总令牌"]=result[name]["输入令牌"]+result[name]["输出令牌"]
    return result


def audit(root,business,prior):
    records=defaultdict(list)
    for key,v in business.items():
        row,kind,arm=key.split("__")
        suite="τ" if row.startswith("tau-") else "ECom" if row.startswith("ecom-") else "共享"
        data=dict(v["计量"])
        if suite=="共享":
            folder=next((root/"共享"/key).glob("*/已采集.json")).parent
            data.update(已记录请求=len(objects(folder/"模型输入.jsonl")))
        if v.get("位置用时秒") is not None:
            data["位置耗时秒"]=v["位置用时秒"]
        records[f"{suite}业务有效位置·{v['来源']}"].append(data)
    for part in ("公开","共享"):
        for marker in (root/part).glob("*/*/异常.json"):
            folder=marker.parent
            if (folder/"官方评分.json").exists() or (folder/"已采集.json").exists():
                continue
            row=folder.parent.name.split("__")[0]
            suite="τ" if row.startswith("tau-") else "ECom" if row.startswith("ecom-") else "共享"
            records[suite+"无分尝试"].append(measured_attempt(folder,suite))
    for marker in (root/"共享").glob("D16__*/*/已采集.json"):
        blind=read(marker.parent/"盲读原始记录.json")
        records["展示盲读实跑"].append(dict(**meter([blind["用量"]]),位置耗时秒=blind["用时秒"]))
    for r in prior["各次"]:
        if r["阶段"]=="正式" and r["官方成绩"] is None:
            records["复用ECom冻结批次原有无分尝试"].append(dict(已记录响应=r["成功响应数"],有用量响应=r["有用量响应数"],
                                                输入令牌=r["输入令牌"],输出令牌=r["输出令牌"],位置耗时秒=r["尝试历时秒"]))
    checks=Counter()
    for marker in (root/"轨迹复核").glob("*/*/复核结果.json"):
        item=read(marker)
        checks[item["入口"]+"有效复核"]+=1
    checks["内部检索入口补正位置"]=len(list((root/"服务入口补正").glob("*/*/复核结果.json")))
    checks["复核运行器无结果异常"]=len(list((root/"轨迹复核").glob("*/*/复核异常.json")))
    return dict(分类用量=grouped(records),独立复核次数=dict(checks),
                说明=["τ 以官方轨迹中有用量的客服及顾客消息计数；实际请求未提供，独立语义裁判未返回单独用量。",
                    "ECom 记录客服与顾客的成功桥接响应。共享客服另统计已发请求，覆盖发出后无成功响应的情况。",
                    "轨迹缺少用量或耗时的尝试保留原位置。没有记录的模型往返不能推断为零。",
                    "业务用时为独立位置累计，存在并发。共享对话的响应时间仅含成功模型往返。",
                    "复用 ECom 有效轨迹及该批次原有无分尝试单列，计算新增开销时排除。固定回放与服务故障使用录制响应。"])


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    for k in ("root","business","prior","output"):
        parser.add_argument(k,type=Path)
    args=parser.parse_args()
    data=audit(args.root,read(args.business),read(args.prior))
    save(args.output,data)
    print("用量类别",len(data["分类用量"]),"保留复核异常",data["独立复核次数"]["复核运行器无结果异常"])
