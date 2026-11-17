"""从上游步骤日志恢复首次预算耗尽的评分输入，原文仅保留于服务器。"""
import json
import re
from pathlib import Path
from tau2.data_model.message import AssistantMessage, UserMessage, ToolMessage, ToolCall

def parse_message(text, pending):
    header, _, body = text.partition("\n")
    if header in {"AssistantMessage", "UserMessage"}:
        body, sep, tail = body.rpartition("\nis_final_chunk: ")
        assert sep and tail in {"True", "False"}
        content, sep, calls = body.partition("\nToolCalls:\n")
        if body.startswith("ToolCalls:\n"):
            content, sep, calls = "", "yes", body[len("ToolCalls:\n"):]
        assert not content or content.startswith("content: ")
        cls = AssistantMessage if header == "AssistantMessage" else UserMessage
        tool_calls = [ToolCall.from_string(x) for x in re.split(r"\n(?=ToolCall \(from )", calls)] if sep else None
        message = cls(role=header.removesuffix("Message").lower(), content=content[len("content: "):] if content else None,
                      tool_calls=tool_calls, is_final_chunk=tail=="True", timestamp=None)
        if tool_calls:
            assert not pending
            pending.extend(tool_calls)
    elif header.startswith("ToolMessage (responding to "):
        assert len(pending)==1
        call=pending.pop(0)
        turn, timestamp = None, None
        if body.startswith("turn_idx: "):
            line, _, body=body.partition("\n")
            turn=int(line.split(": ",1)[1])
        if body.startswith("timestamp: "):
            line, _, body=body.partition("\n")
            timestamp=line.split(": ",1)[1]
        error=body.endswith("\nError")
        if error:
            body=body[:-len("\nError")]
        assert body.startswith("content: ")
        message=ToolMessage(id=call.id,role="tool",requestor=call.requestor,
                            content=body[len("content: "):],timestamp=timestamp,turn_idx=turn,error=error)
    else:
        raise ValueError("遇到未支持的日志消息格式")
    assert str(message)==text, "恢复消息与原有逐字展示不一致"
    return message

def recover(log):
    starts=list(re.finditer(r" - Step 0\.\nFrom role:",log))
    first=log[:starts[1].start()] if len(starts)>1 else log
    if "达到单轮工具调用上限" not in first:
        raise ValueError("首个对话没有明确的预算耗尽证据")
    entries=re.split(r"(?m)^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| ",log)
    messages,pending,steps=[],[],[]
    pattern=r" - Step (\d+)\.\nFrom role: [^\n]+\nTo role: [^\n]+\nMessage: ([\s\S]*)"
    for entry in entries:
        match=re.search(pattern,entry)
        if match:
            if int(match[1])==0 and steps:
                break
            steps.append(int(match[1]))
            messages.append(parse_message(match[2].rstrip("\n"),pending))
    assert steps==list(range(len(steps))) and messages and not pending
    return messages

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder",type=Path)
    args=parser.parse_args()
    log=args.folder/"native/zh-retail/workers/worker_0.log"
    messages=recover(log.read_text())
    print(json.dumps(dict(完整连续步骤=len(messages),逐字校验=True,角色计数={r:sum(m.role==r for m in messages) for r in ("assistant","user","tool")}),ensure_ascii=False))
