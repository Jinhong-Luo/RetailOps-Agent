"""完整记录客服可见输入输出，保留耗尽预算前的未提交轨迹；不记录顾客模拟器私有提示。"""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

def append(path,row):
    with Path(path).open("a",encoding="utf8") as stream:
        stream.write(json.dumps(row,ensure_ascii=False)+"\n")

def start(folder,payload):
    messages=payload.get("messages") or []
    if not messages or messages[0].get("role")!="system" or not str(messages[0].get("content","")).startswith("你是中文零售助手。"):
        return None
    key=uuid4().hex
    path=Path(folder)/"客服实际调用.jsonl"
    append(path,dict(事件="请求",编号=key,内容=payload))
    return path,key

def finish(handle,response):
    if handle is None:
        return
    path,key=handle
    try:
        body=response.json()
    except ValueError:
        body=dict(非JSON响应=response.text)
    append(path,dict(事件="返回",编号=key,状态码=response.status_code,内容=body))

def lossless(path):
    """相同消息只存一份正文，以引用保留每次出现和先后顺序；不裁剪任何消息。"""
    messages={}
    events=[]
    for line in Path(path).read_text(encoding="utf8").splitlines():
        event=json.loads(line)
        if event["事件"]=="请求":
            payload=event["内容"]
            refs=[]
            for message in payload["messages"]:
                serialized=json.dumps(message,ensure_ascii=False,sort_keys=True)
                key=hashlib.sha256(serialized.encode()).hexdigest()
                messages[key]=message
                refs.append(key)
            event=dict(event,内容={**payload,"messages":dict(按顺序引用=refs)})
        events.append(event)
    return dict(说明="消息字典保存精确正文；请求通过有序摘要列表引用。同一消息的重复出现仍完整保留。仅包含客服实际可见内容；历史记忆返回不是独立事实证据。",消息字典=messages,事件=events)

def restore(value):
    """核对可无损还原逐次请求，供自动测试与运行审计使用。"""
    result=[]
    for row in value["事件"]:
        if row["事件"]=="请求":
            payload=row["内容"]
            row=dict(row,内容={**payload,"messages":[value["消息字典"][k] for k in payload["messages"]["按顺序引用"]]})
        result.append(row)
    return result
