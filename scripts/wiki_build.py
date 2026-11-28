"""每次从一个独立开发会话生成笔记，再增量更新一页 Wiki。"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sqlite3

from wiki_experiment import (BUILD, SHORT_LIMIT, Meter, ModelBudget, bridge, client,
                             position, read_json, setup, sha, write_json)

SOURCES = (8, 21, 27)
TOPICS = ("订单","运单","物流","商品","安装","辅材","比较","预约","费用","取消","地址","电话",
          "个人发票","抬头","再次","确认","改成","还是","已提供","请提供","请问","核实","缺少",
          "无法","没有","暂缓","不用","可以","同意","换货","转接","已完成","未执行","等待")


def events(source):
    with sqlite3.connect((source/"business.db").as_uri()+"?mode=ro",uri=True) as db:
        history = json.loads(db.execute("select history from sessions").fetchone()[0])
    # 用题内别名保留“同参重复”的证据；不让订单标识、姓名或联系方式进入笔记生成器。
    aliases = {}
    out = []
    for n, m in enumerate(history):
        value = m.get("content") or ""
        row = dict(序=n, 角色=m["role"], 可见主题=[t for t in TOPICS if t in value])
        for c in m.get("tool_calls") or []:
            fn = c["function"]
            signature = (fn["name"], json.dumps(json.loads(fn["arguments"]),sort_keys=True))
            row.update(工具=fn["name"], 参数组合=aliases.setdefault(signature,len(aliases)+1))
        out.append(row)
    return out


async def generate(run, key, system, data, ledger, effort="low"):
    folder = run/key
    folder.mkdir(exist_ok=True)
    saved = folder/"结构.json"
    payload = dict(model=BUILD+"@"+effort,messages=[dict(role="system",content=system),
        dict(role="user",content=json.dumps(data,ensure_ascii=False))])
    identity = sha(json.dumps(payload,sort_keys=True).encode())
    stamp = folder/"输入摘要.json"
    if stamp.exists() and read_json(stamp) != identity:
        raise ValueError("续跑输入变化")
    write_json(stamp, identity)
    if not saved.exists():
        private = folder/"私有回复.jsonl"
        if not private.exists():
            async with client(Meter(ledger,folder,BUILD,effort,1)) as http:
                await http.post("chat/completions",json=payload)
        msg = json.loads(private.read_text().splitlines()[-1])
        text = msg["content"].strip()
        if text.startswith("```"):
            text = "\n".join(text.splitlines()[1:-1])
        write_json(saved,json.loads(text))
    return read_json(saved)


def check_text(text):
    if not isinstance(text,str) or len(text)>900 or re.search(r"https?://|@|\d{5,}|\[\[",text):
        raise ValueError("页面正文长度、隐私或链接核验失败")
    return text


async def build(run, batch):
    archive = run/"原始流水"
    archive.mkdir(exist_ok=True)
    wiki = run/"wiki"
    wiki.mkdir(exist_ok=True)
    ledger = ModelBudget(run/"短试预算.db",SHORT_LIMIT)
    notes = []
    page = {}
    for i, case in enumerate(SOURCES,1):
        source = position(batch,"ECom-Bench",case)
        recorded = events(source)
        note_id = f"note{i}"
        write_json(archive/(note_id+".json"),dict(原始库=str(source/"business.db"),
            轨迹摘要=sha((source/"trajectory.json").read_bytes()),逐事件视图=recorded))
        note = await generate(run,"单题"+str(i),
            '仅根据一个已结束会话的脱敏事件摘要提出可迁移的客服办理方法。角色、工具和同参数组合是直接证据；主题词缺少语义方向，不推断用户已授权或操作已成功。不得存客户资料、具体价格或题号。输出JSON，字段为“经验”（字符串，可空）、“例外”（字符串）、“依据”（本单事件序号整数数组）。不要猜未记录事实。',
            recorded,ledger)
        evidence = note["依据"]
        if not isinstance(evidence,list) or any(type(n)!=int or not 0<=n<len(recorded) for n in evidence):
            raise ValueError("引用事件不存在")
        advice, exception = check_text(note["经验"]),check_text(note["例外"])
        if not advice or not evidence:
            continue
        notes.append(note_id)
        (wiki/(note_id+".md")).write_text(f"# 单次办理经验\n{advice}\n\n例外：{exception}\n\n来源：{note_id} 事件 {evidence}\n\n主题：[[guide]]\n")
        page = await generate(run,"归并"+str(i),
            '你负责增量合并客服经验。只读当前主题页和本次新笔记，保留适用边界，冲突缩小范围。仅归纳办理方法；不得从主题词推断业务规则或授权，不能保存任何历史客户事实。正文要给出可执行的下一步。返回JSON，只有“适用”“例外”“正文”三个短字符串。目录字段各限80个汉字，正文最多350汉字。不要生成链接。',
            dict(旧页=page,新增笔记=note),ledger)
        for k in ("适用","例外","正文"):
            check_text(page[k])
        if len(page["适用"])+len(page["例外"])>200:
            raise ValueError("目录过长")
    if len(notes)<2 or not page.get("正文"):
        raise ValueError("独立来源不足以发布")
    content = "# 办理经验\n"+page["正文"]+"\n\n适用："+page["适用"]+"\n例外："+page["例外"]+"\n来源："+" ".join("[["+n+"]]" for n in notes)
    (wiki/"guide.md").write_text(content)
    write_json(wiki/"manifest.json",dict(pages={"guide":dict(适用=page["适用"],例外=page["例外"],sha256=sha(content.encode()))}))
    write_json(run/"建库完成.json",dict(开发题=SOURCES,笔记数=len(notes),预算=ledger.summary(),
                                      版本=sha((wiki/"manifest.json").read_bytes())))


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("run",type=Path)
    parser.add_argument("batch",type=Path)
    args=parser.parse_args()
    setup()
    with bridge(args.run,BUILD,18871):
        asyncio.run(build(args.run,args.batch))
