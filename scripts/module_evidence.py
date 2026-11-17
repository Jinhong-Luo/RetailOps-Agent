"""只从成功位置的官方轨迹或完成标记提取脱敏业务分量、模块触发计数和用量。"""
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from shared_grade import grade


def read(path):
    return json.loads(path.read_bytes())


def objects(path):
    with path.open(encoding="utf8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def parse(value):
    if isinstance(value,str):
        try:
            value=json.loads(value)
        except (TypeError,ValueError):
            return {}
    return value if isinstance(value,dict) else {}


def observations(messages, events):
    obs=Counter()
    for msg in messages:
        for entry in msg.get("tool_calls") or []:
            call=entry.get("function",entry)
            name,args=call["name"],parse(call.get("arguments"))
            if name=="service_step":
                name,args=args.get("operation"),parse(args.get("arguments"))
            if name=="propose_plan":
                obs["提交草案调用"]+=1
                obs["带咨询答复的草案调用"]+=int(bool(args.get("consultation_reply")))
                if any(isinstance(a,dict) and a.get("tool")=="manage_ecard_tool" for a in args.get("actions") or []):
                    obs["含卡业务的草案调用"]+=1
            elif name in {"register_needs","record_booking_wait","reply_with_booking_wait","resolve_booking_wait","search_policy","find_variants"}:
                obs[name]+=1
    for event in events:
        stage=event["stage"]
        if stage=="跨会话参考":
            obs["参考内容注入"]+=1
            obs["经验命中条数"]+=len(event.get("hits") or [])
        if stage in {"完成","模型往返","执行前检查","等待确认","用户确认"}:
            obs[stage]+=1
    return dict(obs)


def sqlite_trace(path):
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)) as db:
        messages=[m for body, in db.execute("SELECT history FROM sessions ORDER BY rowid") for m in json.loads(body)]
        events=[json.loads(body) for body, in db.execute("SELECT body FROM events ORDER BY id")]
    return messages,events


def meter(rows):
    # 零条响应或缺用量时保留覆盖数，和真实的零个令牌分开。
    used=[row for row in rows if row]
    return dict(已记录响应=len(rows),有用量响应=len(used),
                输入令牌=sum(r["prompt_tokens"] for r in used),输出令牌=sum(r["completion_tokens"] for r in used))



def model_check(metadata):
    expected=[bool(m) and m.get("actual_model",m.get("model"))=="gpt-6.1-sol" and
              m.get("reasoning_effort",m.get("effort"))=="medium" for m in metadata]
    return dict(有元数据响应=sum(bool(m) for m in metadata),待核对响应=len(metadata),
                模型与强度匹配=bool(expected) and all(expected),
                采样参数生效=all(m.get("sampling_parameters_applied") is True for m in metadata) if metadata else None)


def collect(base, key, release):
    row,kind,arm=key.split("__")
    if kind!="业务对话":
        raise ValueError("此入口只处理业务对话")
    public=row.startswith(("tau-","ecom-"))
    place=base/("公开" if public else "共享")/key
    matches=list(place.glob("*/"+("官方评分.json" if public else "已采集.json")))
    if not matches:
        return None
    if len(matches)!=1:
        raise ValueError("同位置存在多个结果："+key)
    marker=matches[0]
    value=read(marker)
    if value["冻结参照"]!=release:
        raise ValueError("源结果冻结提交不匹配："+key)
    folder=marker.parent
    reused=value.get("来源")=="复用"
    if reused:
        folder=Path(value["原评分文件"]).parent
        source=read(folder/"官方评分.json")
        if source["提交"]!=release or source["题号"]!=int(row.split("-")[1]) or any(source[k]!=value[k] for k in ("score","action","search","output")):
            raise ValueError("归档源版本、题号或分数不匹配："+key)
    result=dict(题号=row,条件=arm,来源="复用" if reused else "实跑",所在位置无分异常数=len(list(place.glob("*/异常.json"))),
                服务参照=release,运行器提交=value["运行器提交"])
    if public:
        result.update(服务提交=value["服务提交"],整题分=value["score"],位置用时秒=value.get("用时秒"))
    if row.startswith("tau-"):
        sim=read(next(folder.glob("native/*/results.json")))["simulations"]
        if len(sim)!=1 or sim[0]["reward_info"]["reward"]!=value["score"]:
            raise ValueError("零售官方原始分不一致："+key)
        messages=sim[0]["messages"]
        events=read(folder/"events.json") if (folder/"events.json").is_file() else []
        used=[m["usage"] for m in messages if m.get("usage")]
        result["模型核对"]=model_check([(m.get("raw_data") or {}).get("bridge_metadata",{}) for m in messages if m.get("usage")])
        result["计量"]=meter(used)
        result["计量"]["范围"]="上游消息中带用量的响应，独立语义评分未计入"
    elif row.startswith("ecom-"):
        messages,events=sqlite_trace(folder/"business.db")
        result["官方分量"]={name:value[k] for name,k in (("终态","action"),("指定查询","search"),("输出","output"))}
        records=objects(folder/"usage.jsonl")
        result["模型核对"]=model_check(records)
        result["计量"]=meter([v["usage"] for v in records])
        result["模型核对"]["采样参数生效"]=None
        result["计量"]["范围"]="桥接成功响应中实际记录的用量"
    else:
        raw=read(folder/"逐轮证据.json")
        checks=grade(row,raw)
        result.update(整题分=int(all(checks.values())),逐项业务判断=checks,成功回执数=len(raw["成功回执"]))
        replies=objects(folder/"模型回执.jsonl")
        result["模型核对"]=model_check([r.get("元数据") or {} for r in replies])
        messages=[r["消息"] for r in replies]
        events=[e for session in raw["事件"] for e in session]
        result["计量"]=meter([r["用量"] for r in replies])
        result["计量"].update(响应耗时合计秒=round(sum(r["秒"] for r in replies),2),范围="客服模型成功响应，顾客按题卡投递")
    result["观测"]=observations(messages,events)
    return result


if __name__=="__main__":
    import argparse
    from module_manifest import build
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("source","root","out"):
        parser.add_argument(name,type=Path)
    parser.add_argument("release")
    parser.add_argument("--完整",action="store_true")
    args=parser.parse_args()
    keys=[k for k,p in build(args.source)["位置"].items() if p["入口"]=="业务对话"]
    rows={k:r for k in keys if (r:=collect(args.root,k,args.release)) is not None}
    if args.完整 and len(rows)!=len(keys):
        raise ValueError(f"业务成绩尚有 {len(keys)-len(rows)} 个位置未取得")
    args.out.write_text(json.dumps(rows,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    print("已核对业务位置",len(rows),"计划",len(keys),"按来源统计",dict(Counter(r["来源"] for r in rows.values())))
