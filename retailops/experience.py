"""只读的跨会话方法检索；不接收评分、顾客脚本或未来消息。"""
import hashlib
import json
import os
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "configs"
TOPICS = {"发票": ("发票","开票","抬头","票据"), "授权": ("只","先","另外","暂","同时","仅","一起","以及","加急"),
          "预约": ("安装","预约","排班","维修")}
HINT = "逐项核对本轮请求和授权。只将资料齐备且顾客要办理的事项放进草案，咨询单独答复，缺字段用本单证据补齐。"
PREFIX = "以下为历史处理参考。身份、字段和授权均须取自本单。\n"
LIMIT = 800


class Experience:
    def __init__(self, arm):
        if arm not in {"无记忆","人工提示","历史检索","提炼记忆"}:
            raise ValueError("未知的参考材料组")
        self.arm = arm
        self.rows = []
        if arm in {"历史检索","提炼记忆"}:
            file = "experience_sources.json" if arm == "历史检索" else "experience_notes.json"
            bank = os.getenv("RETAILOPS_MEMORY_BANK") if arm == "提炼记忆" else None
            self.rows = json.loads((Path(bank) if bank else CONFIG / file).read_text(encoding="utf8"))
            if not isinstance(self.rows, list) or any(not isinstance(row, list) or len(row)!=3
                    or row[0] not in TOPICS or not all(isinstance(v,str) and v.strip() for v in row)
                    for row in self.rows):
                raise ValueError("经验快照格式或主题不符")
        self.version = hashlib.sha256(json.dumps([arm,HINT,self.rows,TOPICS,LIMIT],ensure_ascii=False).encode()).hexdigest()[:16]

    def retrieve(self, message, state):
        if self.arm == "无记忆":
            return "", []
        if self.arm == "人工提示":
            return PREFIX + HINT, ["人工提示"]
        query = message + " " + " ".join(n["evidence"] for n in state["needs"] if n["status"] in {"待办理","待补充","待确认"})
        scores = {topic: sum(word in query for word in words) for topic,words in TOPICS.items()}
        matches = sorted(self.rows,key=lambda r:-scores[r[0]])
        output, hits = "", []
        for topic, source, body in matches:
            if scores[topic] and len(hits)<3:
                candidate = (output or PREFIX) + body + "\n"
                if len(candidate.encode()) <= LIMIT:
                    output = candidate
                    hits.append(source)
        return output,hits
