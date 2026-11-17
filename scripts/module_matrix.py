"""合并真实业务成绩、固定专题、双版本服务回放及独立评分，逐格保留覆盖状态。"""
import argparse
from collections import Counter
import json
from pathlib import Path

from module_manifest import build


def read(path):
    return json.loads(path.read_bytes())


def save(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf8")


def replay(root, row, kind, condition, release):
    place=root/"轨迹复核"/f"{row}__{kind}__{condition}"
    paths=list(place.glob("*/复核结果.json"))
    if kind=="服务":
        corrected=list((root/"服务入口补正"/place.name).glob("*/复核结果.json"))
        if corrected:
            if len(paths)!=1 or len(corrected)!=1:
                raise ValueError("入口补正必须能唯一追溯原结果")
            previous=read(paths[0])
            note=read(corrected[0].parent/"补正说明.json")
            if note["原位置"]!=place.name or note["原复核代码"]!=previous["复核代码"]:
                raise ValueError("入口补正没有对应原记录")
            paths=corrected
    if not paths:
        return None
    if len(paths)!=1:
        raise ValueError("同位置取得多个复核结果")
    doc=read(paths[0])
    if doc["服务提交"]!=release or (doc["题号"],doc["入口"],doc["条件"])!=(row,kind,condition):
        raise ValueError("复核输入与矩阵位置不符")
    result=doc["结果"]
    if kind=="服务":
        result["_核验来源"]=dict(复核代码=doc["复核代码"],入口补正=paths[0].parents[2].name=="服务入口补正")
    return result


DEDICATED={"多需求登记":["register_needs"],"同轮咨询":["带咨询答复的草案调用"],
           "预约等待与恢复":["record_booking_wait","reply_with_booking_wait","resolve_booking_wait"],
           "上下文与检索":["search_policy","find_variants"],"跨会话记忆":["参考内容注入"],
           "草案展示、授权和执行":["提交草案调用","执行前检查","用户确认","完成"]}


def business_cell(module, keys, business):
    if not all(key in business for key in keys):
        return dict(状态="待取得结果")
    values=[business[key] for key in keys]
    for key,v in zip(keys,values):
        if (v["题号"],v["条件"])!=(key.split("__")[0],key.split("__")[2]):
            raise ValueError("两个配对位置的题号或条件不符")
    evidence={k:values[0]["观测"].get(k,0) for k in DEDICATED[module]}
    trace="专用事件或工具已触发" if any(evidence.values()) else "未观测到专用事件或工具"
    if module=="上下文与检索":
        trace+="；状态卡与检索共同切换"
    if module=="同轮咨询":
        trace+="；开票提示和文字咨询的单独作用未验证"
    return dict(状态="已配对",来源=[v["来源"] for v in values],条件=[v["条件"] for v in values],
                整题分=[v["整题分"] for v in values],冻结组观测=evidence,触发说明=trace)


def service_cell(row, root, release):
    cases={}
    for arm in ("正常","故障"):
        result=replay(root,row,"服务",arm,release)
        if result is None:
            return dict(状态="待取得结果")
        cases[arm]={"核验来源":result["_核验来源"]}
        for version in ("冻结服务","早期按钮服务"):
            original=result[version]
            allowed=("状态","通过","组数","原终态相同","查询值相符","失败时回滚","席位释放","账本不变","原因")
            facts={k:original[k] for k in allowed if k in original}
            if "逐组" in original:
                facts["各组回滚且幂等"]=all(g["半执行回滚"] and g["重启重试幂等"] and g["仅一份回执"] for g in original["逐组"])
            cases[arm][version]=facts
    return dict(状态="实跑服务回放",条件=cases)


def assemble(manifest, business, root, topic_rows, shared, release):
    topics={r["位置"]:r for r in topic_rows}
    mapping={key:topics[place] for place,key in manifest["专题"].items()}
    table={}
    for row,modules in manifest["矩阵"].items():
        cells={}
        for module,spec in modules.items():
            method,keys=spec["执行方式"],spec["位置"]
            if not keys:
                value=dict(状态="不适用",原因=method)
            elif keys[0].split("__")[1]=="业务对话":
                value=business_cell(module,keys,business)
            elif len(keys)==2 and keys[0] in mapping:
                value=dict(状态="复用固定专题",条件=[])
                for key in keys:
                    r=mapping[key]
                    value["条件"].append({k:r[k] for k in ("位置","条件","诊断符合预期","业务通过") if k in r})
            elif method=="服务轨迹回放":
                value=service_cell(row,root,release)
            elif method=="评分轨迹复核":
                record=shared.get(row) if row.startswith(("D","S")) else replay(root,row,"评分","复核",release)
                if record is None:
                    value=dict(状态="待取得结果")
                else:
                    value=dict(状态="实跑评分复核",复核一致=record["一致"])
                    for k in ("原分","原整题分","复核分","原分项","复核分项"):
                        if k in record:
                            value[k]=record[k]
            else:
                raise ValueError(f"未识别的预登记入口 {row} {module}")
            cells[module]=dict(入口=method,位置=keys,**value)
        table[row]=cells
    return table


def short(cell):
    status=cell["状态"]
    if status=="已配对":
        provenance="复/实" if cell["来源"]==["复用","实跑"] else "实/实"
        seen="触" if any(cell["冻结组观测"].values()) else "未触"
        return provenance+" "+" / ".join(str(int(v)) for v in cell["整题分"])+" "+seen
    if status=="实跑服务回放":
        def state(v):
            if v["状态"]=="已回放":
                return "✓" if v["通过"] else "×"
            return v["状态"]
        return "实 "+" · ".join("/".join(state(cell["条件"][a][v]) for v in ("冻结服务","早期按钮服务")) for a in ("正常","故障"))
    if status=="实跑评分复核":
        return "实 "+("一致" if cell["复核一致"] else "有差异")
    if status=="复用固定专题":
        def label(item):
            if "业务通过" in item:
                return "通过" if item["业务通过"] else "失败"
            return "诊断符合" if item["诊断符合预期"] else "诊断不符"
        return "复 "+" / ".join(label(v) for v in cell["条件"])
    return status


def main(args):
    manifest=build(args.source)
    business=read(args.business)
    if any(r["服务参照"]!=args.release for r in business.values()):
        raise ValueError("业务分数包含其他冻结版本")
    shared=read(args.reports/"共享27题评分复核.json")
    if shared["服务提交"]!=args.release:
        raise ValueError("共享评分不是指定版本")
    table=assemble(manifest,business,args.root,read(args.reports/"专题与复合63位置.json"),shared["逐题"],args.release)
    counts=Counter(v["状态"] for row in table.values() for v in row.values())
    assert len(table)==113 and sum(counts.values())==904
    if args.完整 and (counts["待取得结果"] or len(business)!=632):
        raise ValueError(f"尚有 {counts['待取得结果']} 个格子缺结果，业务分数已齐 {len(business)}/632")
    args.out.mkdir(parents=True,exist_ok=True)
    save(args.out/"逐格证据.json",dict(冻结提交=args.release,输入摘要=manifest["输入摘要"],状态统计=dict(counts),矩阵=table))
    modules=list(next(iter(table.values())))
    lines=["# 八模块逐题矩阵","",f"冻结提交 `{args.release}`。每道题的每种条件一次，同一轨迹可在多个模块中引用。","",
           "业务格按完整版 / 关闭组列整题分。实与复分别表示服务器实跑、复用同一冻结提交的归档。触与未触指完整版轨迹里是否观测到本列专用事件或工具。提示也会影响纯文字回答，其作用无法单独识别。上下文列同时改变状态卡和检索能力。","",
           "服务格先列正常，再列故障，每组顺序为冻结服务 / 早期按钮服务。✓ 与 × 是固定输入上的服务检验。固定专题保留业务是否通过或能否检出预设异常。其余复核列说明原轨迹评分与再次核验是否一致。",""]
    for name,prefix in (("τ-bench 中文零售","tau-"),("ECom-Bench 中文文本","ecom-"),("共享中文场景",("D","S"))):
        lines.extend(["## "+name,"","| 题号 | "+" | ".join(modules)+" |","|---|"+"---|"*len(modules)])
        for row,cells in table.items():
            if row.startswith(prefix):
                lines.append("| "+row+" | "+" | ".join(short(cells[m]) for m in modules)+" |")
        lines.append("")
    (args.out/"113题八模块矩阵.md").write_text("\n".join(lines)+"\n",encoding="utf8")
    print("矩阵题数",len(table),"格数",sum(counts.values()),"状态统计",dict(counts))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("source","root","business","reports","out"):
        parser.add_argument(name,type=Path)
    parser.add_argument("release")
    parser.add_argument("--完整",action="store_true")
    main(parser.parse_args())
