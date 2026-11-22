"""只在当前业务条件可辨认时取用已验证经验。"""
from dataclasses import dataclass
import re

from .verified_memory import RULES, digest, read


def applicable(text, state, history):
    # 原话只用于给出候选范围，实际事实仍由账本和当前服务状态核对。
    previous = "\n".join(str(msg.get("content") or "") for msg in history
                         if msg.get("role") in ("tool", "system"))
    names = []
    waits = [row for row in state.get("booking_waits", []) if not row["resolved"]]
    appointment = any(word in text for word in ("预约", "先别约", "安装", "维修", "上门"))
    if appointment:
        if any(word in text for word in ("暂缓", "先别约", "先不约", "再约", "先等报价")) and not any(
                word in text for word in ("继续", "请安排", "给我草案", "请用")):
            names.append("预约暂缓")
        elif any(row["order_id"] in text and row["service"] in text and
                 any(word in text for word in ("继续", "补充", "请用", "请安排", "可以"))
                 for row in waits):
            names.append("预约补齐")
    invoice = [name for name, word in (("个人开票", "个人发票"), ("企业开票", "企业发票")) if word in text]
    if len(invoice) == 1:
        names.extend(invoice)
    if "价保" in text and "退货" not in text and "取消" not in text and any(
            word in text for word in ("退差价", "办理", "申请")):
        names.append("独立价保")
    if "价保" not in text and any(word in text for word in ("退货", "取消")) and "退款" in text:
        if any(word in text for word in ("不同意", "拒绝", "暂缓", "别办", "不要拆")):
            names.append("拒绝退款")
        elif "京东E卡" in text and re.search(r"支付方式[^\n]{0,24}京东E卡", previous):
            names.append("关联退款")
    if "换货成功" in previous and any(word in text for word in ("原款", "新款", "原来", "换来")) and any(
            word in text for word in ("赠品", "活动", "维修", "安装", "辅材")):
        names.append("换货后型号")
    return names


@dataclass(frozen=True)
class Selection:
    ids: tuple
    text: str


class VerifiedReader:
    def __init__(self, path, version):
        self.bank = read(path)
        self.version = digest(self.bank)
        if self.bank["契约"] != version:
            raise ValueError("经验快照的业务契约与当前配置不符")

    def select(self, names):
        chosen = tuple(name for name in names if name in self.bank["经验"] and name in RULES)[:2]
        if not chosen:
            return Selection((), "")
        content = "以下提醒来自已核验的历史经验。当前政策、账本和本轮授权优先。\n"
        content += "\n".join(f"〔{name}〕{RULES[name].action}" for name in chosen)
        return Selection(chosen, content)
