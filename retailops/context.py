"""按完整用户轮选择历史，针对商品查询提供可回查的库存与价格证据。"""
import json
import re

from .store import dump


def variants(product, options, offset=0, limit=12):
    rows = [v for v in product["variants"].values()
            if all(v["options"].get(k) == val for k, val in options.items())]
    rows.sort(key=lambda v: v["item_id"])
    return dict(product_id=product["product_id"], name=product["name"], total=len(rows),
                offset=offset, next_offset=offset+limit if offset+limit < len(rows) else None,
                variants=rows[offset:offset+limit])


def context(mode, state, history, recent=3):
    if mode not in {"A", "B", "C"} or recent < 1:
        raise ValueError("上下文方案或窗口无效")
    preamble = [] if mode == "A" else [dict(role="system", content="服务端记录的业务进度：" + dump(state))]
    if mode != "C":
        return preamble + history
    rounds = []
    for msg in history:
        if not rounds or msg["role"] == "user":
            rounds.append([])
        rounds[-1].append(msg)
    tail = rounds[-recent:]
    needle = dump([state["actions"], tail[-1][0] if tail else {}])
    ids = set(re.findall(r"#?[\w-]*\d[\w-]*", needle, flags=re.ASCII))
    earlier = [group for group in rounds[:-recent] if any(i in dump(group) for i in ids)]
    groups = earlier[-2:] + tail
    compact = []
    for group in groups:
        for msg in group:
            if msg.get("role") == "tool":
                try:
                    product = json.loads(msg["content"])
                except (TypeError, ValueError):
                    product = None
                if isinstance(product, dict) and isinstance(product.get("variants"), dict):
                    page = variants(product, {})
                    chosen = {v["item_id"]: v for v in page["variants"]}
                    chosen.update({i: product["variants"][i] for i in ids & product["variants"].keys()})
                    page["variants"] = list(chosen.values())
                    msg = dict(msg, content=dump(page))
            compact.append(msg)
    return preamble + compact

