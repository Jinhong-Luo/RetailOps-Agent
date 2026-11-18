"""只在会话结束后更新经验；会话内读取不可变快照，保留来源与修订记录。"""
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path

LIMIT=800
PREFIX="以下是历史经验，可能有误。政策、当前账本和本单授权优先；不得照搬旧字段。\n"

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def empty_bank():
    return dict(schema=1,revision=0,parent=None,entries=[],processed=[])

def validate_bank(bank):
    if not isinstance(bank,dict) or bank.get("schema")!=1 or not isinstance(bank.get("revision"),int):
        raise ValueError("记忆快照格式不符")
    if not isinstance(bank.get("entries"),list) or not isinstance(bank.get("processed"),list):
        raise ValueError("记忆目录格式不符")
    ids=[]
    for row in bank["entries"]:
        if set(row)!={"id","topic","when","method","status","sources","updated"}:
            raise ValueError("记忆条目字段不符")
        if not all(isinstance(row[k],str) and row[k].strip() for k in ("id","topic","when","method")):
            raise ValueError("记忆条目缺少正文")
        if row["status"] not in {"有轨迹支持","待验证"} or not isinstance(row["sources"],list):
            raise ValueError("记忆依据格式不符")
        ids.append(row["id"])
    if len(ids)!=len(set(ids)):
        raise ValueError("记忆标识重复")
    return bank

def terms(text):
    chinese=re.findall(r"[\u4e00-\u9fff]+",text)
    return set(re.findall(r"[a-z_]+",text.lower()))|{s[i:i+2] for s in chinese for i in range(len(s)-1)}

class ContinualMemory:
    """仅检索当前问题及当前未结事项；不接触评分、未来输入或更新中的文件。"""
    def __init__(self,path):
        self.bank=validate_bank(json.loads(Path(path).read_text(encoding="utf8")))
        self.version=digest(self.bank)
        self.arm="持续记忆"

    def retrieve(self,message,state):
        query=message+" "+" ".join(n["evidence"] for n in state.get("needs",[])
                                 if n["status"] in {"待办理","待补充","待确认"})
        q=terms(query)
        ranked=[]
        for row in self.bank["entries"]:
            overlap=len(q&terms(row["topic"]+" "+row["when"]+" "+row["method"]))
            if overlap:
                ranked.append((-overlap,-row["updated"],row["id"],row))
        output,hits="",[]
        for _,_,_,row in sorted(ranked):
            note=f'[{row["status"]}] {row["when"]}：{row["method"]}\n'
            candidate=(output or PREFIX)+note
            if len(hits)<3 and len(candidate.encode())<=LIMIT:
                output=candidate
                hits.append(row["id"])
        return output,hits

def apply_update(bank,patch,source,transcript,sensitive=()):
    """校验新增、修订、删除及证据位置；失败时整批不写入。"""
    validate_bank(bank)
    if source in bank["processed"]:
        raise ValueError("本轨迹已整理")
    if not isinstance(patch,dict) or set(patch)!={"changes"} or not isinstance(patch["changes"],list):
        raise ValueError("记忆更新格式不符")
    if len(patch["changes"])>3:
        raise ValueError("单次变更过多")
    result=deepcopy(bank)
    result.update(revision=bank["revision"]+1,parent=digest(bank))
    result["processed"].append(source)
    records={r["id"]:r for r in result["entries"]}
    touched=set()
    audit=[]
    for change in patch["changes"]:
        if not isinstance(change,dict):
            raise ValueError("记忆变更不是对象")
        action=change.get("action")
        if action not in {"add","update","delete"}:
            raise ValueError("未知记忆变更")
        evidence=change.get("evidence")
        if not isinstance(evidence,list) or not evidence or any(type(i)!=int or i<0 or i>=len(transcript) for i in evidence):
            raise ValueError("记忆缺少本次轨迹证据")
        target=change.get("id")
        if action!="add" and target not in records:
            raise ValueError("待修订记忆不存在")
        if action=="add":
            target="经验-"+digest([source,change])[:16]
        if target in touched:
            raise ValueError("单次重复修改同一记忆")
        touched.add(target)
        if action=="delete":
            del records[target]
        else:
            if not all(isinstance(change.get(k),str) and change[k].strip() for k in ("topic","when","method")):
                raise ValueError("记忆正文为空")
            content=" ".join(change[k] for k in ("topic","when","method"))
            if len(content)>180 or len(content.encode())>650:
                raise ValueError("记忆正文超限")
            if re.search(r"[0-9@]|https?://|标准答案|参考答案|题号|跳过确认|忽略政策|无需授权",content):
                raise ValueError("记忆包含具体字段或不当指令")
            if any(isinstance(v,str) and len(v)>=2 and v in content for v in sensitive):
                raise ValueError("记忆含本单身份或字段")
            if change.get("status") not in {"有轨迹支持","待验证"}:
                raise ValueError("缺少依据强度")
            origin=records[target]["sources"] if action=="update" else []
            records[target]=dict(id=target,topic=change["topic"],when=change["when"],method=change["method"],
                status=change["status"],sources=origin+[dict(source=source,evidence=evidence)],updated=result["revision"])
        audit.append(dict(action=action,id=target,evidence=evidence))
    result["entries"]=list(records.values())
    validate_bank(result)
    return result,audit
